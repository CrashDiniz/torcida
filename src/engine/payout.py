"""Poker-style payout tables.

Given a pot (in cents or points), number of entries and a preset, produce the
exact per-rank distribution. Ties split the combined slots of the tied ranks.
Sum of payouts ALWAYS equals the pot exactly (largest-remainder rounding).
"""
from __future__ import annotations

from .models import PayoutPreset


def payout_weights(preset: PayoutPreset, n_entries: int) -> list[float]:
    if n_entries < 1:
        return []
    if preset == PayoutPreset.WINNER_TAKES_ALL:
        return [1.0]
    if preset == PayoutPreset.TOP3:
        if n_entries == 1:
            return [1.0]
        if n_entries == 2:
            return [0.65, 0.35]
        return [0.5, 0.3, 0.2]
    # POKER: pay top 20% (min 1), harmonic decay 1/rank
    paid = max(1, round(n_entries * 0.2))
    raw = [1.0 / (i + 1) for i in range(paid)]
    total = sum(raw)
    return [w / total for w in raw]


def distribute(pot: int, weights: list[float]) -> list[int]:
    """Split integer pot by weights; remainders go to best ranks first."""
    if not weights:
        return []
    shares = [pot * w for w in weights]
    floored = [int(s) for s in shares]
    remainder = pot - sum(floored)
    order = sorted(range(len(shares)), key=lambda i: shares[i] - floored[i], reverse=True)
    for i in order[:remainder]:
        floored[i] += 1
    return floored


def settle_pool(pot: int, preset: PayoutPreset,
                standings: list[tuple[int, int]]) -> dict[int, int]:
    """standings: [(user_id, points)] any order. Returns {user_id: payout}.

    Ties: users with equal points split the combined payout of the ranks
    they jointly occupy (poker convention).
    """
    if not standings:
        return {}
    ranked = sorted(standings, key=lambda t: t[1], reverse=True)
    table = distribute(pot, payout_weights(preset, len(ranked)))
    table += [0] * (len(ranked) - len(table))

    payouts: dict[int, int] = {}
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][1] == ranked[i][1]:
            j += 1
        group = ranked[i:j + 1]
        slot_sum = sum(table[i:j + 1])
        share = distribute(slot_sum, [1.0 / len(group)] * len(group))
        for (user_id, _), amount in zip(group, share):
            payouts[user_id] = amount
        i = j + 1
    return payouts
