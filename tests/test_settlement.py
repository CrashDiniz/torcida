import uuid

import pytest

from src.engine.models import Pick, Pool
from src.engine.settlement import SettlementService, extract_score
from src.engine.store import Store


def ev(fixture_id=700, home=0, away=0, status="live"):
    return {"kind": "scores", "recv_ts": 0,
            "data": {"FixtureId": fixture_id, "HomeGoals": home,
                     "AwayGoals": away, "Status": status}}


def test_extract_score_variants():
    assert extract_score(ev(1, 2, 1)).home_goals == 2
    alt = {"data": {"fixtureId": 5, "Score1": 3, "Score2": 0, "Phase": "FullTime"}}
    parsed = extract_score(alt)
    assert parsed.fixture_id == 5 and parsed.finished
    assert extract_score({"data": {"NoFixture": 1}}) is None
    assert extract_score({"data": {"FixtureId": 9}}) is None  # scoreless, not final


@pytest.mark.asyncio
async def test_goal_callback_and_final_settlement(tmp_path):
    store = Store(path=str(tmp_path / "s.sqlite3"))
    pool = store.create_pool(Pool(id=uuid.uuid4().hex, name="T", creator_id=1))
    store.join(pool.id, 1, "Ana")
    store.place_pick(Pick(id="", pool_id=pool.id, user_id=1, fixture_id=700,
                          market="1x2", selection="1", odds_decimal=2.0))

    goals, finals = [], []

    async def on_goal(state):
        goals.append((state.home_goals, state.away_goals))

    async def on_final(state, n):
        finals.append(n)

    svc = SettlementService(store=store, on_goal=on_goal, on_final=on_final)
    await svc.handle_event(ev(700, 0, 0))
    await svc.handle_event(ev(700, 1, 0))          # goal
    await svc.handle_event(ev(700, 1, 0))          # no change
    await svc.handle_event(ev(700, 1, 0, "Finished"))

    assert goals == [(1, 0)]
    assert finals == [1]
    assert store.standings(pool.id)[0] == (1, "Ana", 200)


@pytest.mark.asyncio
async def test_settlement_is_idempotent(tmp_path):
    store = Store(path=str(tmp_path / "s.sqlite3"))
    pool = store.create_pool(Pool(id=uuid.uuid4().hex, name="T", creator_id=1))
    store.join(pool.id, 1, "Ana")
    store.place_pick(Pick(id="", pool_id=pool.id, user_id=1, fixture_id=700,
                          market="1x2", selection="X", odds_decimal=3.0))
    finals = []

    async def on_final(state, n):
        finals.append(n)

    svc = SettlementService(store=store, on_final=on_final)
    await svc.handle_event(ev(700, 1, 1, "Finished"))
    await svc.handle_event(ev(700, 1, 1, "Finished"))
    assert finals == [1]  # settled exactly once
    assert store.standings(pool.id)[0][2] == 300
