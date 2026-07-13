"""Settlement service: consume score events, track fixture state, settle picks.

Schema-tolerant by design: TxLINE score payload field names are normalised from
the recorded stream (data/recordings/*). Until we calibrate against a real
match recording, the extractor accepts the common shapes we know from the
docs (soccer feed: goals per team, match phase) plus explicit test events.

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


@dataclass
class FixtureState:
    fixture_id: int
    home_goals: int = 0
    away_goals: int = 0
    finished: bool = False
    settled: bool = False


def extract_score(event: dict) -> FixtureState | None:
    """Best-effort extraction of (fixture, score, finished) from a stream event."""
    data = event.get("data", event)
    if not isinstance(data, dict):
        return None
    fixture_id = (data.get("FixtureId") or data.get("fixtureId")
                  or data.get("fixture_id"))
    if fixture_id is None:
        return None

    def _int(*keys: str) -> int | None:
        for k in keys:
            v = data.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return None

    home = _int("HomeGoals", "homeGoals", "Score1", "Participant1Score", "home")
    away = _int("AwayGoals", "awayGoals", "Score2", "Participant2Score", "away")
    status = str(data.get("Status") or data.get("Phase")
                 or data.get("MatchStatus") or "").lower()
    finished = any(m in status for m in FINISHED_MARKERS)
    if home is None or away is None:
        # score-less event (odds tick, lineup, comment) — only useful if final
        if not finished:
            return None
        home = home or 0
        away = away or 0
    return FixtureState(int(fixture_id), home, away, finished)


@dataclass
class SettlementService:
    store: Store
    on_goal: Callable[[FixtureState], Awaitable[None]] | None = None
    on_final: Callable[[FixtureState, int], Awaitable[None]] | None = None
    _states: dict[int, FixtureState] = field(default_factory=dict)

    async def handle_event(self, event: dict) -> None:
        parsed = extract_score(event)
        if parsed is None:
            return
        prev = self._states.get(parsed.fixture_id)
        current = self._states.setdefault(parsed.fixture_id, parsed)
        if prev is not None:
            goal_scored = (parsed.home_goals, parsed.away_goals) != (
                prev.home_goals, prev.away_goals)
            current.home_goals = parsed.home_goals
            current.away_goals = parsed.away_goals
            current.finished = current.finished or parsed.finished
            if goal_scored and self.on_goal:
                await self.on_goal(current)
        if current.finished and not current.settled:
            settled = self.settle_fixture(current)
            current.settled = True
            if self.on_final:
                await self.on_final(current, settled)

    def settle_fixture(self, state: FixtureState) -> int:
        picks = self.store.open_picks_for_fixture(state.fixture_id)
        for pick in picks:
            self.store.update_pick(
                settle_1x2(pick, state.home_goals, state.away_goals))
        log.info("fixture %s settled: %d-%d, %d picks",
                 state.fixture_id, state.home_goals, state.away_goals, len(picks))
        return len(picks)
