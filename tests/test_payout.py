import pytest

from src.engine.models import PayoutPreset
from src.engine.payout import distribute, settle_pool


def test_distribute_sums_exactly():
    for pot in (100, 101, 999, 200000, 7):
        for weights in ([1.0], [0.5, 0.3, 0.2], [0.65, 0.35]):
            assert sum(distribute(pot, weights)) == pot


def test_winner_takes_all():
    payouts = settle_pool(1000, PayoutPreset.WINNER_TAKES_ALL,
                          [(1, 500), (2, 300), (3, 100)])
    assert payouts == {1: 1000, 2: 0, 3: 0}


def test_top3_split():
    payouts = settle_pool(1000, PayoutPreset.TOP3,
                          [(1, 500), (2, 300), (3, 100), (4, 50)])
    assert payouts[1] == 500 and payouts[2] == 300 and payouts[3] == 200
    assert payouts[4] == 0
    assert sum(payouts.values()) == 1000


def test_top3_with_two_players():
    payouts = settle_pool(1000, PayoutPreset.TOP3, [(1, 10), (2, 5)])
    assert payouts == {1: 650, 2: 350}


def test_poker_pays_top_20_percent():
    standings = [(i, 1000 - i) for i in range(100)]
    payouts = settle_pool(100_000, PayoutPreset.POKER, standings)
    paid = [u for u, v in payouts.items() if v > 0]
    assert len(paid) == 20
    assert sum(payouts.values()) == 100_000
    assert payouts[0] > payouts[1] > payouts[19]


def test_tie_splits_combined_slots():
    # two players tied for 1st under TOP3 with 3 entries: (500+300)/2 = 400 each
    payouts = settle_pool(1000, PayoutPreset.TOP3,
                          [(1, 500), (2, 500), (3, 100)])
    assert payouts[1] == payouts[2] == 400
    assert payouts[3] == 200
    assert sum(payouts.values()) == 1000


def test_single_entry_gets_pot():
    assert settle_pool(777, PayoutPreset.POKER, [(9, 42)]) == {9: 777}


def test_empty_pool():
    assert settle_pool(1000, PayoutPreset.TOP3, []) == {}


@pytest.mark.parametrize("preset", list(PayoutPreset))
def test_all_presets_conserve_pot(preset):
    standings = [(i, (i * 37) % 11) for i in range(23)]
    payouts = settle_pool(54_321, preset, standings)
    assert sum(payouts.values()) == 54_321
