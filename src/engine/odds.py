"""Extract 1X2 consensus odds from TxLINE odds snapshots.

Real TxLINE snapshot schema (calibrated 2026-07-13 against devnet):
    {"FixtureId": ..., "Ts": ..., "Bookmaker": "TXLineStablePriceDemargined",
     "SuperOddsType": "1X2_PARTICIPANT_RESULT", "MarketPeriod": "half=1",
     "PriceNames": ["part1", "draw", "part2"], "Prices": [3634, 2085, 4078]}
Prices are decimal odds in milli-units (3634 -> 3.634).

Preference order: full-time 1X2 market > any 1X2 market (e.g. half=1) >
neutral fallback (flagged live=False so the UI can label it).
"""
from __future__ import annotations

from dataclasses import dataclass

FALLBACK = {"1": 2.50, "X": 3.20, "2": 2.80}

MARKET_1X2 = "1X2_PARTICIPANT_RESULT"
_SELECTION_BY_NAME = {"part1": "1", "draw": "X", "part2": "2",
                      "home": "1", "away": "2", "1": "1", "x": "X", "2": "2"}


@dataclass
class Odds1X2:
    home: float
    draw: float
    away: float
    live: bool
    period: str = ""  # e.g. "" (full time) or "half=1"

    def for_selection(self, selection: str) -> float:
        return {"1": self.home, "X": self.draw, "2": self.away}[selection]


def _decimal(raw) -> float | None:
    """Prices come as milli-units ints (3634 = 3.634) or plain floats."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 100:          # milli-units
        value = value / 1000.0
    return value if value >= 1.01 else None


def _is_full_time(period: str) -> bool:
    p = (period or "").strip().lower()
    return p in ("", "ft", "full", "fulltime", "regulartime", "match")


def _parse_entry(entry: dict) -> dict[str, float] | None:
    names = entry.get("PriceNames") or []
    prices = entry.get("Prices") or []
    if not names or len(names) != len(prices):
        return None
    out: dict[str, float] = {}
    for name, raw in zip(names, prices):
        sel = _SELECTION_BY_NAME.get(str(name).strip().lower())
        dec = _decimal(raw)
        if sel and dec:
            out[sel] = dec
    return out if set(out) == {"1", "X", "2"} else None


def parse_snapshot(snapshot) -> Odds1X2:
    """Pick the most recent 1X2 market, preferring full-time periods."""
    best: tuple[int, int, dict[str, float], str] | None = None  # (ft?, ts, odds, period)
    for entry in snapshot or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("SuperOddsType") != MARKET_1X2:
            continue
        parsed = _parse_entry(entry)
        if not parsed:
            continue
        period = str(entry.get("MarketPeriod") or "")
        rank = (1 if _is_full_time(period) else 0, int(entry.get("Ts") or 0))
        if best is None or rank > (best[0], best[1]):
            best = (rank[0], rank[1], parsed, period)

    if best is None:
        return Odds1X2(FALLBACK["1"], FALLBACK["X"], FALLBACK["2"], live=False)
    _, _, odds, period = best
    return Odds1X2(
        home=round(odds["1"], 2), draw=round(odds["X"], 2),
        away=round(odds["2"], 2), live=True,
        period="" if _is_full_time(period) else period,
    )
