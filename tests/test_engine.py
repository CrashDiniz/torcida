from src.engine.models import Pick, PickStatus
from src.engine.scoring import points_for, settle_1x2, settle_pick


def make_pick(selection="1", odds=2.5) -> Pick:
    return Pick(id="p1", pool_id="pool", user_id=1, fixture_id=99,
                market="1x2", selection=selection, odds_decimal=odds)


def test_underdog_pays_more():
    assert points_for(4.0) > points_for(1.5)
    assert points_for(2.5) == 250


def test_odds_are_clamped():
    assert points_for(0.5) == round(100 * 1.01)
    assert points_for(999) == 100 * 50


def test_settle_1x2_home_win():
    pick = settle_1x2(make_pick("1", 2.0), home_goals=3, away_goals=1)
    assert pick.status == PickStatus.WON
    assert pick.points_awarded == 200


def test_settle_1x2_draw_loses_home_pick():
    pick = settle_1x2(make_pick("1", 2.0), home_goals=1, away_goals=1)
    assert pick.status == PickStatus.LOST
    assert pick.points_awarded == 0


def test_settle_1x2_draw_pick_wins_on_draw():
    pick = settle_1x2(make_pick("X", 3.2), 0, 0)
    assert pick.status == PickStatus.WON
    assert pick.points_awarded == 320


def test_void_pick():
    pick = settle_pick(make_pick(), won=None)
    assert pick.status == PickStatus.VOID
    assert pick.points_awarded == 0


def test_settled_pick_is_immutable():
    pick = settle_pick(make_pick("1", 2.0), won=True)
    again = settle_pick(pick, won=False)
    assert again.status == PickStatus.WON
    assert again.points_awarded == 200
