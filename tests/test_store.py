import uuid

from src.engine.models import Pick, Pool
from src.engine.scoring import settle_1x2
from src.engine.store import Store


def make_store(tmp_path):
    return Store(path=str(tmp_path / "test.sqlite3"))


def make_pool() -> Pool:
    return Pool(id=uuid.uuid4().hex, name="Bolão da Firma", creator_id=111)


def test_pool_roundtrip_by_invite(tmp_path):
    store = make_store(tmp_path)
    pool = store.create_pool(make_pool())
    loaded = store.pool_by_invite(pool.invite_code)
    assert loaded is not None
    assert loaded.id == pool.id
    assert loaded.payout_preset == pool.payout_preset


def test_join_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    pool = store.create_pool(make_pool())
    store.join(pool.id, 42, "Crash")
    store.join(pool.id, 42, "Crash again")
    standings = store.standings(pool.id)
    assert len(standings) == 1
    assert standings[0][1] == "Crash"


def test_full_pick_cycle_updates_standings(tmp_path):
    store = make_store(tmp_path)
    pool = store.create_pool(make_pool())
    store.join(pool.id, 1, "Ana")
    store.join(pool.id, 2, "Bia")

    p1 = store.place_pick(Pick(id="", pool_id=pool.id, user_id=1, fixture_id=900,
                               market="1x2", selection="1", odds_decimal=2.0))
    p2 = store.place_pick(Pick(id="", pool_id=pool.id, user_id=2, fixture_id=900,
                               market="1x2", selection="2", odds_decimal=3.5))

    for pick in store.open_picks_for_fixture(900):
        store.update_pick(settle_1x2(pick, home_goals=2, away_goals=0))

    standings = store.standings(pool.id)
    assert standings[0] == (1, "Ana", 200)
    assert standings[1] == (2, "Bia", 0)
    assert store.open_picks_for_fixture(900) == []
    assert p1.id != p2.id
