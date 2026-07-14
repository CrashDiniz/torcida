"""Settlement service: consume score events, track fixture state, settle picks.

Calibrated against the official TxLINE OpenAPI spec (Scores schema): soccer
goals live in ScoreSoccer.Participant{1,2}.Total.Goals and match phase in
StatusSoccerId (NS/H1/HT/H2/ET1/ET2/PE/... with F/FET/FPE/END terminal).
Legacy flat keys (HomeGoals/Status/...) are kept as fallbacks for tests and
schema drift. 1X2 settles on Total (regular time), which is the market rule.

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
# StatusSoccerId codes from the TxLINE spec
SOCCER_LIVE_CODES = {"h1", "ht", "h2", "et1", "htet", "et2", "pe", "wet", "wpe"}
SOCCER_FINAL_CODES = {"f", "fet", "fpe", "end"}


PHASE_LABELS = {
    "ht": "🟡 Intervalo",
    "h2": "🟢 Bola rolando — 2º tempo!",
    "et1": "⏱ Prorrogação — 1º tempo",
    "et2": "⏱ Prorrogação — 2º tempo",
    "pe": "🥅 Pênaltis!",
}


@dataclass
class FixtureState:
    fixture_id: int
    # None = fixture seen (started/finished signal) but score not yet known
    home_goals: int | None = None
    away_goals: int | None = None
    finished: bool = False
    settled: bool = False
    phase: str = ""  # last StatusSoccerId code seen (h1/ht/h2/...)


def _first(data: dict, *keys: str):
    for k in keys:
        v = data.get(k)
        if v is not None:
            return v
    return None


def extract_score(event: dict) -> FixtureState | None:
    """Best-effort extraction of (fixture, score, finished) from a stream event."""
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
    score = _first(data, "ScoreSoccer", "scoreSoccer", "Score", "score")
    if isinstance(score, dict):
        def _goals(part: str) -> int | None:
            total = (score.get(part) or {}).get("Total") or {}
            try:
                return int(total.get("Goals"))
            except (TypeError, ValueError):
                return None
        home, away = _goals("Participant1"), _goals("Participant2")
    if home is None or away is None:
        home = _int("HomeGoals", "homeGoals", "Score1", "Participant1Score", "home")
        away = _int("AwayGoals", "awayGoals", "Score2", "Participant2Score", "away")

    code = _first(data, "StatusSoccerId", "statusSoccerId")
    if isinstance(code, dict):  # enum may serialise as {"F": {}}
        code = next(iter(code), "")
    code = str(code or "").lower()
    state = str(_first(data, "GameState", "gameState", "Status", "Phase",
                       "MatchStatus") or "").lower()
    finished = code in SOCCER_FINAL_CODES or any(
        m in state for m in FINISHED_MARKERS)
    live = code in SOCCER_LIVE_CODES or any(m in state for m in LIVE_MARKERS)
    if home is None or away is None:
        # score-less event (comment, lineup) — only a live/final signal matters
        if not (finished or live):
            return None
        return FixtureState(int(fixture_id), None, None, finished, phase=code)
    return FixtureState(int(fixture_id), home, away, finished, phase=code)


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
        prev = self._states.get(parsed.fixture_id)
        current = self._states.setdefault(parsed.fixture_id, parsed)
        prev_phase = prev.phase if prev else ""
        if prev is None:
            # sparse feeds only emit on incidents: the FIRST score event we see
            # may itself be the goal — announce a non-0x0 opening score as news
            if (parsed.home_goals or 0, parsed.away_goals or 0) != (0, 0) \
                    and not parsed.finished and self.on_goal:
                await self.on_goal(current)
        else:
            if parsed.home_goals is not None and parsed.away_goals is not None:
                score_known = prev.home_goals is not None
                goal_scored = score_known and (
                    parsed.home_goals, parsed.away_goals) != (
                    prev.home_goals, prev.away_goals)
                current.home_goals = parsed.home_goals
                current.away_goals = parsed.away_goals
                if goal_scored and self.on_goal:
                    await self.on_goal(current)
            current.finished = current.finished or parsed.finished
        # phase transition (halftime, second half, ...) announced once
        if parsed.phase and parsed.phase != prev_phase:
            current.phase = parsed.phase
            label = PHASE_LABELS.get(parsed.phase)
            if label and not current.finished and self.on_phase:
                await self.on_phase(current, label)
        if current.finished and not current.settled:
            settled = self.settle_fixture(current)
            current.settled = True
            if self.on_final:
                await self.on_final(current, settled)

    def settle_fixture(self, state: FixtureState) -> int:
        home = state.home_goals or 0
        away = state.away_goals or 0
        picks = self.store.open_picks_for_fixture(state.fixture_id)
        for pick in picks:
            self.store.update_pick(settle_1x2(pick, home, away))
        log.info("fixture %s settled: %d-%d, %d picks",
                 state.fixture_id, home, away, len(picks))
        return len(picks)
