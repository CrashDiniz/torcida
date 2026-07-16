import time
import uuid

from src.engine import hilo
from src.engine.models import Pool
from src.engine.store import Store


def snap_event(seq=1, goals=(1, 0), corners=(3, 1), yellows=(1, 0), reds=(0, 0)):
    def side(i):
        return {"Total": {"Goals": goals[i], "Corners": corners[i],
                          "YellowCards": yellows[i], "RedCards": reds[i]}}
    return {"Seq": seq, "Score": {"Participant1": side(0), "Participant2": side(1)}}


def make_pool(store):
    return store.create_pool(Pool(id=uuid.uuid4().hex, name="p", creator_id=1))


# --- pure engine ---------------------------------------------------------------

def test_snapshot_totals_sums_both_sides_highest_seq():
    items = [snap_event(seq=1, goals=(0, 0)),
             snap_event(seq=9, goals=(1, 2), corners=(4, 3), yellows=(2, 1), reds=(1, 0)),
             {"Seq": 5, "no_score": True}]
    totals = hilo.snapshot_totals(items)
    assert totals == {"goals": 3, "corners": 7, "cards": 4}


def test_snapshot_totals_empty():
    assert hilo.snapshot_totals([]) is None
    assert hilo.snapshot_totals([{"Seq": 1}]) is None


def test_make_question_lines():
    q = hilo.make_question("pool", 1, "goals", 2)
    assert q.line == 2.5
    q = hilo.make_question("pool", 1, "corners", 4)
    assert q.line == 5.5


def test_settle_no_push():
    assert hilo.settle(2.5, 3) == "hi"
    assert hilo.settle(2.5, 2) == "lo"


def test_payout_scales_with_streak():
    assert hilo.payout(1) == 100
    assert hilo.payout(3) == 300


# --- store lifecycle -----------------------------------------------------------

def test_hilo_answer_and_settle(tmp_path):
    store = Store(path=str(tmp_path / "t.sqlite3"))
    pool = make_pool(store)
    q = hilo.make_question(pool.id, 1, "goals", 1)
    store.create_hilo(q)

    assert store.open_hilo_for_pool(pool.id)["id"] == q.id
    assert store.answer_hilo(q.id, 10, "Bia", "hi") == ""      # first answer
    assert store.answer_hilo(q.id, 10, "Bia", "lo") == "hi"    # switched
    assert store.answer_hilo(q.id, 11, "Léo", "hi") == ""
    assert store.hilo_answer_count(q.id) == 2

    winners, losers = store.settle_hilo(q.id, "hi", 2)
    assert winners == [("Léo", 1)]
    assert losers == [("Bia", 0)]
    assert store.open_hilo_for_pool(pool.id) is None
    settled = store.hilo_question(q.id)
    assert settled["status"] == "settled"
    assert settled["result"] == "hi"
    assert settled["final_value"] == 2

    # settling twice is a no-op
    assert store.settle_hilo(q.id, "hi", 2) == ([], [])


def test_hilo_streak_multiplies_and_resets(tmp_path):
    store = Store(path=str(tmp_path / "t.sqlite3"))
    pool = make_pool(store)
    for i, (choice, result) in enumerate([("hi", "hi"), ("hi", "hi"), ("lo", "hi")]):
        q = hilo.make_question(pool.id, i, "goals", 0)
        store.create_hilo(q)
        store.answer_hilo(q.id, 10, "Bia", choice)
        store.settle_hilo(q.id, result, 1)
    board = store.hilo_board(pool.id)
    name, streak, best, points = board[0]
    assert (name, streak, best) == ("Bia", 0, 2)   # reset after the miss
    assert points == 100 + 200                     # 100×1 + 100×2, miss pays 0


def test_hilo_answer_rejected_after_deadline(tmp_path):
    store = Store(path=str(tmp_path / "t.sqlite3"))
    pool = make_pool(store)
    q = hilo.make_question(pool.id, 1, "goals", 0)
    q.resolve_at = time.time() - 1                 # already past
    store.create_hilo(q)
    assert store.answer_hilo(q.id, 10, "Bia", "hi") is None


def test_hilo_open_questions_for_restart(tmp_path):
    store = Store(path=str(tmp_path / "t.sqlite3"))
    pool = make_pool(store)
    a = hilo.make_question(pool.id, 1, "goals", 0)
    store.create_hilo(a)
    store.settle_hilo(a.id, "lo", 0)
    b = hilo.make_question(pool.id, 2, "corners", 3)
    store.create_hilo(b)
    open_qs = store.open_hilo_questions()
    assert [q["id"] for q in open_qs] == [b.id]
