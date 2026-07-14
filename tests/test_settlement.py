import uuid

import pytest

from src.engine.models import Pick, Pool
from src.engine.settlement import SettlementService, extract_score
from src.engine.store import Store


def ev(fixture_id=700, home=0, away=0, status="live"):
    return {"kind": "scores", "recv_ts": 0,
            "data": {"FixtureId": fixture_id, "HomeGoals": home,
                     "AwayGoals": away, "Status": status}}


def txline_ev(fixture_id=700, home=None, away=None, status=None,
              game_state="in_running", action="score_update"):
    """Event in the official TxLINE Scores schema (see /docs/docs.yaml)."""
    def _side(goals):
        return {"Total": {"Goals": goals, "YellowCards": 0,
                          "RedCards": 0, "Corners": 0}}
    data = {"FixtureId": fixture_id, "GameState": game_state,
            "Action": action, "SportId": 1, "Seq": 1, "Ts": 0}
    if status is not None:
        data["StatusSoccerId"] = status
    if home is not None:
        data["ScoreSoccer"] = {"Participant1": _side(home),
                               "Participant2": _side(away)}
    return {"kind": "scores", "recv_ts": 0, "data": data}


def test_extract_score_variants():
    assert extract_score(ev(1, 2, 1)).home_goals == 2
    alt = {"data": {"fixtureId": 5, "Score1": 3, "Score2": 0, "Phase": "FullTime"}}
    parsed = extract_score(alt)
    assert parsed.fixture_id == 5 and parsed.finished
    assert extract_score({"data": {"NoFixture": 1}}) is None
    assert extract_score({"data": {"FixtureId": 9}}) is None  # scoreless, not final


def test_extract_score_txline_schema():
    # pre-game comment (like the real snapshot): must NOT create state
    pre = txline_ev(700, game_state="scheduled", action="comment")
    assert extract_score(pre) is None

    live = extract_score(txline_ev(700, 1, 0, status="H1"))
    assert (live.home_goals, live.away_goals, live.finished) == (1, 0, False)

    # score-less but in-play (kickoff comment): state exists, score unknown
    kick = extract_score(txline_ev(700, status="H1", action="comment"))
    assert kick is not None and kick.home_goals is None and not kick.finished

    # terminal codes, incl. enum-as-object serialisation
    assert extract_score(txline_ev(700, 2, 1, status="F")).finished
    assert extract_score(txline_ev(700, 1, 1, status="FPE")).finished
    assert extract_score(txline_ev(700, 2, 2, status={"FET": {}})).finished


@pytest.mark.asyncio
async def test_txline_full_match_flow(tmp_path):
    """Golden test: today's match shape, scheduled -> H1 -> goal -> HT -> F."""
    store = Store(path=str(tmp_path / "s.sqlite3"))
    pool = store.create_pool(Pool(id=uuid.uuid4().hex, name="T", creator_id=1))
    store.join(pool.id, 1, "Ana")
    store.place_pick(Pick(id="", pool_id=pool.id, user_id=1, fixture_id=700,
                          market="1x2", selection="1", odds_decimal=2.4))
    goals, finals = [], []

    async def on_goal(state):
        goals.append((state.home_goals, state.away_goals))

    async def on_final(state, n):
        finals.append((state.home_goals, state.away_goals, n))

    svc = SettlementService(store=store, on_goal=on_goal, on_final=on_final)
    await svc.handle_event(txline_ev(700, game_state="scheduled",
                                     action="comment"))       # ignored
    await svc.handle_event(txline_ev(700, status="H1",
                                     action="comment"))       # kickoff, no score
    assert 700 in svc._states                                 # pick lock engages
    await svc.handle_event(txline_ev(700, 0, 0, status="H1"))  # first score: no goal
    await svc.handle_event(txline_ev(700, 1, 0, status="H1", action="goal"))
    await svc.handle_event(txline_ev(700, status="HT", action="comment"))
    await svc.handle_event(txline_ev(700, 1, 0, status="H2"))  # unchanged
    await svc.handle_event(txline_ev(700, 1, 0, status="F", game_state="finished"))

    assert goals == [(1, 0)]
    assert finals == [(1, 0, 1)]
    assert store.standings(pool.id)[0] == (1, "Ana", 240)


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
async def test_sparse_feed_first_event_is_the_goal(tmp_path):
    """Incident-only feeds: first score event seen may already be 1-0."""
    store = Store(path=str(tmp_path / "s.sqlite3"))
    goals = []

    async def on_goal(state):
        goals.append((state.home_goals, state.away_goals))

    svc = SettlementService(store=store, on_goal=on_goal)
    await svc.handle_event(txline_ev(700, 1, 0, status="H1", action="goal"))
    assert goals == [(1, 0)]
    # but a plain 0-0 opener stays silent
    svc2 = SettlementService(store=store, on_goal=on_goal)
    await svc2.handle_event(txline_ev(701, 0, 0, status="H1"))
    assert goals == [(1, 0)]


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
