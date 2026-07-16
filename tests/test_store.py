import sqlite3
import uuid

from src.engine.models import (PayoutPreset, Pick, Pool, RequestStatus,
                               Visibility)
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


# --- discovery + pot (Fase 1) ------------------------------------------------

def test_visibility_roundtrips_and_public_pools_filters(tmp_path):
    store = make_store(tmp_path)
    pub = store.create_pool(Pool(id=uuid.uuid4().hex, name="Aberto", creator_id=1,
                                 visibility=Visibility.PUBLIC))
    req = store.create_pool(Pool(id=uuid.uuid4().hex, name="Pedir", creator_id=1,
                                 visibility=Visibility.REQUEST))
    store.create_pool(Pool(id=uuid.uuid4().hex, name="Secreto", creator_id=1,
                           visibility=Visibility.HIDDEN))
    assert store.pool_by_id(pub.id).visibility == Visibility.PUBLIC
    discoverable = {p.id for p in store.public_pools()}
    assert discoverable == {pub.id, req.id}  # hidden pool stays off the showcase


def test_pot_for_and_live_split(tmp_path):
    store = make_store(tmp_path)
    pool = store.create_pool(Pool(id=uuid.uuid4().hex, name="Pote", creator_id=1,
                                  buy_in=100, payout_preset=PayoutPreset.WINNER_TAKES_ALL))
    store.join(pool.id, 1, "Ana")
    store.join(pool.id, 2, "Bia")
    assert store.pot_for(pool.id) == 200  # 100 x 2 entries

    store.place_pick(Pick(id="", pool_id=pool.id, user_id=1, fixture_id=900,
                          market="1x2", selection="1", odds_decimal=2.0))
    for pick in store.open_picks_for_fixture(900):
        store.update_pick(settle_1x2(pick, home_goals=1, away_goals=0))
    split = store.pot_split(pool.id)
    assert split[0][:2] == (1, "Ana") and split[0][3] == 200  # winner takes the pot
    assert split[1][3] == 0
    assert sum(chips for *_, chips in split) == store.pot_for(pool.id)


def test_pot_split_is_zero_for_free_pools(tmp_path):
    store = make_store(tmp_path)
    pool = store.create_pool(make_pool())  # buy_in defaults to 0
    store.join(pool.id, 1, "Ana")
    assert store.pot_for(pool.id) == 0
    assert all(chips == 0 for *_, chips in store.pot_split(pool.id))


def test_join_request_lifecycle(tmp_path):
    store = make_store(tmp_path)
    pool = store.create_pool(Pool(id=uuid.uuid4().hex, name="Pedir", creator_id=1,
                                  visibility=Visibility.REQUEST))
    store.join(pool.id, 1, "Host")  # creator
    assert store.request_status(pool.id, 42) is None

    store.create_join_request(pool.id, 42, "Zé")
    assert store.request_status(pool.id, 42) == RequestStatus.PENDING
    pending = store.pending_requests_for_creator(1)
    assert len(pending) == 1 and pending[0].user_id == 42

    store.set_request_status(pool.id, 42, RequestStatus.APPROVED)
    assert store.request_status(pool.id, 42) == RequestStatus.APPROVED
    assert store.pending_requests_for_creator(1) == []  # no longer pending


def test_migration_adds_columns_to_legacy_db(tmp_path):
    """A DB created before buy_in/visibility must gain them with safe defaults."""
    db = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE pools (
             id TEXT PRIMARY KEY, name TEXT NOT NULL, creator_id INTEGER NOT NULL,
             payout_preset TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'pt-BR',
             narrator_delay_s INTEGER NOT NULL DEFAULT 0,
             entry_points INTEGER NOT NULL DEFAULT 1000, created_at REAL NOT NULL,
             invite_code TEXT NOT NULL UNIQUE, telegram_chat_id INTEGER);""")
    con.execute("INSERT INTO pools (id, name, creator_id, payout_preset, "
                "created_at, invite_code) VALUES ('p1','Antigo',1,'top3',0,'abc')")
    con.commit()
    con.close()

    store = Store(path=str(db))  # opening runs the migration
    pool = store.pool_by_id("p1")
    assert pool is not None
    assert pool.buy_in == 0
    assert pool.visibility == Visibility.HIDDEN  # legacy pools stay link-only
    # and new pools still write fine against the migrated table
    fresh = store.create_pool(Pool(id="p2", name="Novo", creator_id=1,
                                   buy_in=50, visibility=Visibility.PUBLIC))
    assert store.pool_by_id(fresh.id).buy_in == 50


def test_opening_odds_first_write_wins(tmp_path):
    store = make_store(tmp_path)
    assert store.opening_odds(900) is None
    store.record_opening_odds(900, 4.0, 3.2, 1.8, ts=1000.0)
    store.record_opening_odds(900, 1.5, 4.0, 6.0, ts=2000.0)  # in-play update
    assert store.opening_odds(900) == {"1": 4.0, "X": 3.2, "2": 1.8}


def test_named_picks_include_odds(tmp_path):
    store = make_store(tmp_path)
    pool = store.create_pool(make_pool())
    store.join(pool.id, 42, "Pedro")
    store.place_pick(Pick(id="", pool_id=pool.id, user_id=42, fixture_id=900,
                          market="1x2", selection="2", odds_decimal=4.0))
    assert store.named_picks_for_fixture(900) == [("Pedro", "2", 4.0)]


def test_verification_roundtrip(tmp_path):
    store = make_store(tmp_path)
    assert store.verification(900) is None
    store.record_verification(900, True, "sig123", 1, 2, 962, ts=1000.0)
    v = store.verification(900)
    assert v["valid"] == 1 and v["tx_sig"] == "sig123" and v["seq"] == 962
