"""Odds-priced scoring: calling the underdog pays more.

A correct pick awards round(100 * odds_decimal) points — i.e. a virtual
100-point stake paid back at the consensus odds captured at pick time.
Wrong picks award 0 (no negative scores: keeps casual pools friendly).
"""
from __future__ import annotations

from .models import Pick, PickStatus

STAKE = 100
MIN_ODDS = 1.01
MAX_ODDS = 50.0


def points_for(odds_decimal: float) -> int:
    odds = min(max(odds_decimal, MIN_ODDS), MAX_ODDS)
    return round(STAKE * odds)


def settle_pick(pick: Pick, won: bool | None) -> Pick:
    """won=None voids the pick (cancelled market)."""
    if pick.status != PickStatus.OPEN:
        return pick
    if won is None:
        pick.status = PickStatus.VOID
        pick.points_awarded = 0
    elif won:
        pick.status = PickStatus.WON
        pick.points_awarded = points_for(pick.odds_decimal)
    else:
        pick.status = PickStatus.LOST
        pick.points_awarded = 0
    return pick


def settle_1x2(pick: Pick, home_goals: int, away_goals: int) -> Pick:
    result = "1" if home_goals > away_goals else "2" if away_goals > home_goals else "X"
    return settle_pick(pick, won=(pick.selection == result))
