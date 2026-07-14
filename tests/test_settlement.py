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


def real_ev(fixture_id=700, p1=None, p2=None, status_id=None, action="",
            confirmed=True):
    """Event in the REAL devnet soccer schema: top-level `Score` with the
    `Goals` key OMITTED for a 0-goal side, numeric `StatusId`, GameState
    frozen at 'scheduled'."""
    def _side(goals):
        d = {"YellowCards": 0, "Corners": 0}
        if goals:  # 0 goals => key omitted, exactly like the live feed
            d["Goals"] = goals
        return {"Total": d, "HT": d, "H1": d}
    data = {"FixtureId": fixture_id, "GameState": "scheduled",
            "Action": action, "Type": "Soccer", "Confirmed": confirmed}
    if status_id is not None:
        data["StatusId"] = status_id
    if p1 is not None:
        data["Score"] = {"Participant1": _side(p1), "Participant2": _side(p2)}
    return {"kind": "scores", "recv_ts": 0, "data": data}


def test_extract_real_devnet_schema():
    # 0 x 1 with Participant1 Goals key absent (must read as 0, not unknown)
    s = extract_score(real_ev(700, 0, 1, status_id=2))
    assert (s.home_goals, s.away_goals) == (0, 1)
    assert s.phase == "h1" and not s.finished
    assert extract_score(real_ev(700, status_id=3)).phase == "ht"
    assert extract_score(real_ev(700, status_id=4)).phase == "h2"
    # official Game Phase Encoding: 5=F (finished), 10=FET, 13=FPE
    assert extract_score(real_ev(700, 1, 0, status_id=5)).finished
    assert extract_score(real_ev(700, 2, 2, status_id=10)).finished
    assert extract_score(real_ev(700, 3, 1, status_id=13)).finished
    assert not extract_score(real_ev(700, 0, 0, status_id=4)).finished
    # GameState 'scheduled' must NOT read as live on its own
    bare = extract_score({"data": {"FixtureId": 9, "GameState": "scheduled"}})
    assert bare is None


@pytest.mark.asyncio
async def test_full_time_status5_settles(tmp_path):
    """StatusId 5 (F) is full time — settlement must fire and pay the winner."""
    store = Store(path=str(tmp_path / "s.sqlite3"))
    pool = store.create_pool(Pool(id=uuid.uuid4().hex, name="T", creator_id=1))
    store.join(pool.id, 2, "Bia")
    store.place_pick(Pick(id="", pool_id=pool.id, user_id=2, fixture_id=700,
                          market="1x2", selection="2", odds_decimal=3.35))
    finals = []

    async def on_final(state, n):
        finals.append((state.home_goals, state.away_goals, n))

    svc = SettlementService(store=store, on_final=on_final)
    await svc.handle_event(real_ev(700, 0, 1, status_id=2))   # live 0-1
    await svc.handle_event(real_ev(700, 0, 2, status_id=4))   # 0-2 H2
    await svc.handle_event(real_ev(700, 0, 2, status_id=5))   # FULL TIME
    assert finals == [(0, 2, 1)]
    assert store.standings(pool.id)[0] == (2, "Bia", 335)


@pytest.mark.asyncio
async def test_real_match_with_var_reversal(tmp_path):
    """Replays the shape of the recorded FRA x ESP: penalty, HT, H2, goal,
    then a VAR-disallowed goal that must NOT stick as a scoreline."""
    store = Store(path=str(tmp_path / "s.sqlite3"))
    pool = store.create_pool(Pool(id=uuid.uuid4().hex, name="T", creator_id=1))
    store.join(pool.id, 2, "Bia")
    store.place_pick(Pick(id="", pool_id=pool.id, user_id=2, fixture_id=700,
                          market="1x2", selection="2", odds_decimal=3.35))
    goals, notes = [], []

    async def on_goal(s): goals.append((s.home_goals, s.away_goals))
    async def on_phase(s, l): notes.append(l)

    svc = SettlementService(store=store, on_goal=on_goal, on_phase=on_phase)
    await svc.handle_event(real_ev(700, status_id=2, action="kickoff"))
    await svc.handle_event(real_ev(700, 0, 1, status_id=2, action="penalty_outcome"))
    await svc.handle_event(real_ev(700, status_id=3, action="halftime_finalised"))
    await svc.handle_event(real_ev(700, status_id=4, action="kickoff"))
    await svc.handle_event(real_ev(700, status_id=4, action="kickoff"))   # dup
    await svc.handle_event(real_ev(700, 0, 2, status_id=4, action="goal"))
    await svc.handle_event(real_ev(700, 0, 3, status_id=4, action="goal"))
    await svc.handle_event(real_ev(700, 0, 2, status_id=4, action="action_discarded"))

    assert goals == [(0, 1), (0, 2), (0, 3)]
    assert notes[0] == "🟡 Intervalo"
    assert notes[1] == "🟢 Bola rolando — 2º tempo!"
    assert notes.count("🟢 Bola rolando — 2º tempo!") == 1  # no duplicate
    assert "VAR" in notes[-1]  # reversal announced
    assert svc._states[700].home_goals == 0 and svc._states[700].away_goals == 2


@pytest.mark.asyncio
async def test_phase_transitions_announced_once(tmp_path):
    store = Store(path=str(tmp_path / "s.sqlite3"))
    phases = []

    async def on_phase(state, label):
        phases.append(label)

    svc = SettlementService(store=store, on_phase=on_phase)
    await svc.handle_event(txline_ev(700, 0, 0, status="H1"))   # no label
    await svc.handle_event(txline_ev(700, status="HT", action="comment"))
    await svc.handle_event(txline_ev(700, status="HT", action="comment"))  # dup
    await svc.handle_event(txline_ev(700, 0, 0, status="H2"))
    await svc.handle_event(txline_ev(700, 1, 0, status="F",
                                     game_state="finished"))  # final, not phase
    assert phases == ["🟡 Intervalo", "🟢 Bola rolando — 2º tempo!"]


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
