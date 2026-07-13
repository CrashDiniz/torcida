"""Extract 1X2 consensus odds from TxLINE odds snapshots.

The snapshot schema is normalised but market naming can vary; this parser is
tolerant: it looks for a match-odds-like market and averages bookmaker prices.
When no live odds are available (devnet quiet hours, pre-listing) we fall
back to neutral defaults so pools always work — flagged as `live=False`.
"""
from __future__ import annotations

from dataclasses import dataclass

FALLBACK = {"1": 2.50, "X": 3.20, "2": 2.80}

_MATCH_MARKET_HINTS = ("1x2", "match", "moneyline", "ml", "ftr", "full time result")
_SELECTION_KEYS = {
    "1": ("1", "home", "p1", "participant1"),
    "X": ("x", "draw", "tie"),
    "2": ("2", "away", "p2", "participant2"),
}


@dataclass
class Odds1X2:
    home: float
    draw: float
    away: float
    live: bool

    def for_selection(self, selection: str) -> float:
        return {"1": self.home, "X": self.draw, "2": self.away}[selection]


def _looks_like_match_market(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _MATCH_MARKET_HINTS)


def _classify(label: str) -> str | None:
    lbl = str(label).strip().lower()
    for sel, keys in _SELECTION_KEYS.items():
        if lbl in keys:
            return sel
    return None


def parse_snapshot(snapshot: list[dict]) -> Odds1X2:
    """Average decimal prices per selection across snapshot entries."""
    sums: dict[str, float] = {"1": 0.0, "X": 0.0, "2": 0.0}
    counts: dict[str, int] = {"1": 0, "X": 0, "2": 0}

    for entry in snapshot or []:
        market = str(entry.get("Market") or entry.get("market") or entry.get("MarketName") or "")
        if market and not _looks_like_match_market(market):
            continue
        prices = entry.get("Prices") or entry.get("prices") or entry.get("Outcomes") or []
        if isinstance(prices, dict):
            prices = [{"label": k, "price": v} for k, v in prices.items()]
        for p in prices:
            label = p.get("label") or p.get("Label") or p.get("Outcome") or p.get("Name") or ""
            price = p.get("price") or p.get("Price") or p.get("Decimal") or p.get("Odds")
            sel = _classify(label)
            if sel is None or price is None:
                continue
            try:
                value = float(price)
            except (TypeError, ValueError):
                continue
            if value >= 1.01:
                sums[sel] += value
                counts[sel] += 1

    if all(counts[s] > 0 for s in ("1", "X", "2")):
        return Odds1X2(
            home=round(sums["1"] / counts["1"], 2),
            draw=round(sums["X"] / counts["X"], 2),
            away=round(sums["2"] / counts["2"], 2),
            live=True,
        )
    return Odds1X2(FALLBACK["1"], FALLBACK["X"], FALLBACK["2"], live=False)
