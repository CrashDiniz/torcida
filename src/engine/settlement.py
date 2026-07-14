"""Settlement service: consume score events, track fixture state, settle picks.

Calibrated against the REAL TxLINE devnet soccer feed (not just the OpenAPI
`Scores` schema, which documents different field names). Observed live payload:
  - running score:  Score.Participant{1,2}.Total.Goals  — and the `Goals` key
    is OMITTED when a side has 0 (0 goals => absent, NOT null); treat absent
    as zero, or the whole score reads as "unknown" and never settles.
  - match phase:    StatusId (int): 1=NS, 2=H1, 3=HT, 4=H2 (confirmed from a
    full recording); the numeric full-time id is still unconfirmed with TxLINE,
    so finish is detected from the Action stream + the spec's string codes.
  - GameState stays the literal string "scheduled" the whole match — useless
    as a live/finished signal; do NOT rely on it.
Legacy schemas (ScoreSoccer/StatusSoccerId, flat HomeGoals/Status) stay as
fallbacks for tests and drift. 1X2 settles on Total (regular time).

Emits callbacks so the bot can announce goals and final results in groups.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .scoring import settle_1x2
from .store import Store

log = logging.getLogger("settlement")

FINISHED_MARKERS = {"finished", "ft", "full_time", "fulltime", "ended", "final"}
LIVE_MARKERS = {"in_running", "inrunning", "in_play", "inplay", "live"}
SOCCER_LIVE_CODES = {"h1", "ht", "h2", "et1", "htet", "et2", "pe", "wet", "wpe"}
SOCCER_FINAL_CODES = {"f", "fet", "fpe", "end"}
SOCCER_VOID_CODES = {"a", "c", "txcc", "txcs"}  # abandoned/cancelled/tx-cancelled
# numeric StatusId -> phase code — CONFIRMED from the official soccer-feed doc
# (github.com/txodds/tx-on-chain: Game Phase Encoding table)
SOCCER_STATUS_INT = {
    1: "ns", 2: "h1", 3: "ht", 4: "h2", 5: "f", 6: "wet", 7: "et1", 8: "htet",
    9: "et2", 10: "fet", 11: "wpe", 12: "pe", 13: "fpe", 14: "i", 15: "a",
    16: "c", 17: "txcc", 18: "txcs", 19: "p",
}
# secondary full-time signal from the Action stream (belt and suspenders)
FINAL_ACTIONS = {"game_finalised", "fulltime_finalised", "match_finished"}


PHASE_LABELS = {
    "ht": "🟡 Intervalo",
    "h2": "🟢 Bola rolando — 2º tempo!",
    "et1": "⏱ Prorrogação — 1º tempo",
    "et2": "⏱ Prorrogação — 2º tempo",
    "pe": "🥅 Pênaltis!",
}
# forward-only order: sparse/out-of-order events must not re-announce a phase
PHASE_ORDER = {"ns": 0, "h1": 1, "ht": 2, "h2": 3, "et1": 4, "htet": 4,
               "et2": 5, "pe": 6, "wet": 5, "wpe": 6}


@dataclass
class FixtureState:
    fixture_id: int
    # None = fixture seen (started/finished signal) but score not yet known
    home_goals: int | None = None
    away_goals: int | None = None
    finished: bool = False
    settled: bool = False
    phase: str = ""  # last phase code seen (h1/ht/h2/...)


def _first(data: dict, *keys: str):
    for k in keys:
        v = data.get(k)
        if v is not None:
            return v
    return None


def extract_score(event: dict) -> FixtureState | None:
    """Best-effort extraction of (fixture, score, phase, finished) from a stream event."""
    data = event.get("data", event)
    if not isinstance(data, dict):
        return None
    fixture_id = _first(data, "FixtureId", "fixtureId", "fixture_id")
    if fixture_id is None:
        return None

    def _int(*keys: str) -> int | None:
        v = _first(data, *keys)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    home = away = None
    score = _first(data, "Score", "ScoreSoccer", "score", "scoreSoccer")
    if isinstance(score, dict):
        def _goals(part: str) -> int:
            # `Goals` is omitted for a 0-goal side — absent means zero
            total = (score.get(part) or {}).get("Total") or {}
            try:
                return int(total.get("Goals") or 0)
            except (TypeError, ValueError):
                return 0
        home, away = _goals("Participant1"), _goals("Participant2")
    if home is None or away is None:
        home = _int("HomeGoals", "homeGoals", "Score1", "Participant1Score", "home")
        away = _int("AwayGoals", "awayGoals", "Score2", "Participant2Score", "away")

    action = str(_first(data, "Action", "action") or "").lower()
    status_int = _int("StatusId", "statusId")
    code = _first(data, "StatusSoccerId", "statusSoccerId")
    if isinstance(code, dict):  # enum may serialise as {"F": {}}
        code = next(iter(code), "")
    code = str(code or "").lower()
    phase = SOCCER_STATUS_INT.get(status_int) or code
    state = str(_first(data, "Status", "Phase", "MatchStatus") or "").lower()

    finished = (phase in SOCCER_FINAL_CODES or code in SOCCER_FINAL_CODES
                or action in FINAL_ACTIONS
                or any(m in state for m in FINISHED_MARKERS))
    live = (phase in SOCCER_LIVE_CODES or code in SOCCER_LIVE_CODES
            or any(m in state for m in LIVE_MARKERS))
    if home is None or away is None:
        # score-less event (comment, lineup) — only a live/final signal matters
        if not (finished or live):
            return None
        return FixtureState(int(fixture_id), None, None, finished, phase=phase)
    return FixtureState(int(fixture_id), home, away, finished, phase=phase)


@dataclass
class SettlementService:
    store: Store
    on_goal: Callable[[FixtureState], Awaitable[None]] | None = None
    on_final: Callable[[FixtureState, int], Awaitable[None]] | None = None
    on_phase: Callable[[FixtureState, str], Awaitable[None]] | None = None
    _states: dict[int, FixtureState] = field(default_factory=dict)

    async def handle_event(self, event: dict) -> None:
        parsed = extract_score(event)
        if parsed is None:
            return
        current = self._states.get(parsed.fixture_id)
        if current is None:
            current = FixtureState(parsed.fixture_id)
            self._states[parsed.fixture_id] = current

        # score: announce a goal on an INCREASE; a VAR-disallowed goal drops
        # the count back — announce the reversal so the group is never left
        # believing a phantom scoreline
        if parsed.home_goals is not None and parsed.away_goals is not None:
            base_h, base_a = current.home_goals or 0, current.away_goals or 0
            increased = parsed.home_goals > base_h or parsed.away_goals > base_a
            decreased = parsed.home_goals < base_h or parsed.away_goals < base_a
            current.home_goals = parsed.home_goals
            current.away_goals = parsed.away_goals
            if not parsed.finished:
                if increased and self.on_goal:
                    await self.on_goal(current)
                elif decreased and self.on_phase:
                    await self.on_phase(
                        current, f"❌ <b>VAR</b>: gol anulado — placar volta pra "
                        f"<b>{parsed.home_goals} x {parsed.away_goals}</b>")

        # phase transition — forward only, announced once per phase
        new_rank = PHASE_ORDER.get(parsed.phase)
        cur_rank = PHASE_ORDER.get(current.phase, -1)
        if new_rank is not None and new_rank > cur_rank:
            current.phase = parsed.phase
            label = PHASE_LABELS.get(parsed.phase)
            if label and not parsed.finished and self.on_phase:
                await self.on_phase(current, label)

        if parsed.finished:
            current.finished = True
        if current.finished and not current.settled:
            settled = self.settle_fixture(current)
            current.settled = True
            if self.on_final:
                await self.on_final(current, settled)

    def seed(self, fixture_id: int, home: int, away: int, phase: str = "",
             finished: bool = False) -> None:
        """Silently set known state (from a snapshot) without firing callbacks.
        Used to re-hydrate after a restart/reconnect so /aovivo is accurate and
        a game that finished while we were down still settles (idempotent)."""
        st = self._states.get(fixture_id) or FixtureState(fixture_id)
        st.home_goals, st.away_goals = home, away
        if phase:
            st.phase = phase
        if finished:
            st.finished = True
        self._states[fixture_id] = st
        if st.finished and not st.settled:
            self.settle_fixture(st)
            st.settled = True

    def settle_fixture(self, state: FixtureState) -> int:
        home = state.home_goals or 0
        away = state.away_goals or 0
        picks = self.store.open_picks_for_fixture(state.fixture_id)
        for pick in picks:
            self.store.update_pick(settle_1x2(pick, home, away))
        log.info("fixture %s settled: %d-%d, %d picks",
                 state.fixture_id, home, away, len(picks))
        return len(picks)
