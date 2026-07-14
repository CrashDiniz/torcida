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


def test_leave_removes_entry_and_picks(tmp_path):
    store = make_store(tmp_path)
    pool = store.create_pool(make_pool())
    store.join(pool.id, 42, "Crash")
    store.join(pool.id, 7, "Ana")
    store.place_pick(Pick(id="", pool_id=pool.id, user_id=42, fixture_id=900,
                          market="1x2", selection="1", odds_decimal=2.0))

    assert store.leave(pool.id, 42) is True
    assert store.leave(pool.id, 42) is False  # already gone
    assert store.picks_for_user(pool.id, 42) == []
    assert [uid for uid, _, _ in store.standings(pool.id)] == [7]
    assert [p.id for p in store.pools_for_user(42)] == []
    assert store.has_left(pool.id, 42) is True  # comeback is recognizable
    assert store.has_left(pool.id, 7) is False


def test_pool_by_chat_restores_binding(tmp_path):
    store = make_store(tmp_path)
    old = store.create_pool(make_pool(), telegram_chat_id=-100)
    new = store.create_pool(
        Pool(id=uuid.uuid4().hex, name="Novo", creator_id=111,
             created_at=old.created_at + 1))
    store.bind_chat(new.id, -100)
    found = store.pool_by_chat(-100)
    assert found is not None and found.id == new.id  # latest wins
    assert store.pool_by_chat(-999) is None

    # rebinding unbinds older pools: no duplicate group announcements
    store.place_pick(Pick(id="", pool_id=old.id, user_id=1, fixture_id=900,
                          market="1x2", selection="1", odds_decimal=2.0))
    store.place_pick(Pick(id="", pool_id=new.id, user_id=1, fixture_id=900,
                          market="1x2", selection="X", odds_decimal=3.0))
    assert store.chats_for_fixture(900) == [(new.id, -100)]


def test_pools_for_user_and_picks_for_user(tmp_path):
    store = make_store(tmp_path)
    a, b = store.create_pool(make_pool()), store.create_pool(make_pool())
    store.join(a.id, 42, "Crash")
    store.join(b.id, 42, "Crash")
    store.join(b.id, 7, "Ana")
    assert {p.id for p in store.pools_for_user(42)} == {a.id, b.id}
    assert [p.id for p in store.pools_for_user(7)] == [b.id]

    store.place_pick(Pick(id="", pool_id=a.id, user_id=42, fixture_id=900,
                          market="1x2", selection="1", odds_decimal=2.0))
    store.place_pick(Pick(id="", pool_id=b.id, user_id=42, fixture_id=900,
                          market="1x2", selection="X", odds_decimal=3.0))
    mine = store.picks_for_user(a.id, 42)
    assert len(mine) == 1 and mine[0].selection == "1"
    assert store.picks_for_user(a.id, 7) == []


def test_chats_for_fixture_only_bound_pools_with_picks(tmp_path):
    store = make_store(tmp_path)
    bound = store.create_pool(make_pool(), telegram_chat_id=-100)
    unbound = store.create_pool(make_pool())  # no chat -> never announced
    for pool in (bound, unbound):
        store.place_pick(Pick(id="", pool_id=pool.id, user_id=1, fixture_id=900,
                              market="1x2", selection="1", odds_decimal=2.0))
    store.place_pick(Pick(id="", pool_id=bound.id, user_id=2, fixture_id=901,
                          market="1x2", selection="2", odds_decimal=3.0))
    assert store.chats_for_fixture(900) == [(bound.id, -100)]
    assert store.chats_for_fixture(555) == []


def test_pick_for_and_replace(tmp_path):
    store = make_store(tmp_path)
    pool = store.create_pool(make_pool())
    pick = store.place_pick(Pick(id="", pool_id=pool.id, user_id=42,
                                 fixture_id=900, market="1x2",
                                 selection="1", odds_decimal=2.0))
    found = store.pick_for(pool.id, 42, 900)
    assert found is not None and found.id == pick.id
    assert store.pick_for(pool.id, 42, 901) is None
    assert store.pick_for(pool.id, 7, 900) is None

    store.replace_pick(pick.id, "X", 3.4, placed_at=pick.placed_at + 60)
    updated = store.pick_for(pool.id, 42, 900)
    assert updated is not None
    assert (updated.selection, updated.odds_decimal) == ("X", 3.4)
    assert len(store.picks_for_user(pool.id, 42)) == 1  # replaced, not added

    from src.engine.scoring import settle_1x2
    store.update_pick(settle_1x2(updated, home_goals=1, away_goals=1))
    assert store.pick_for(pool.id, 42, 900) is None  # settled picks not replaceable


def test_fixture_labels_roundtrip(tmp_path):
    store = make_store(tmp_path)
    assert store.fixture_label(900) is None
    store.set_fixture_label(900, "França x Espanha")
    store.set_fixture_label(900, "France x Spain")  # upsert
    assert store.fixture_label(900) == "France x Spain"


def test_chat_topics_roundtrip(tmp_path):
    store = make_store(tmp_path)
    assert store.chat_topic(-100, "anuncios") is None
    store.set_chat_topic(-100, "anuncios", 7)
    store.set_chat_topic(-100, "anuncios", 8)  # upsert
    store.set_chat_topic(-100, "bolao", 9)
    assert store.chat_topic(-100, "anuncios") == 8
    assert store.chat_topic(-100, "bolao") == 9
    assert store.chat_topic(-999, "anuncios") is None


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
