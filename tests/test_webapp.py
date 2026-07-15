import hashlib
import hmac
import json
from urllib.parse import urlencode

from src.web.auth import validate_init_data

TOKEN = "12345:TEST_TOKEN"


def make_init_data(user_id=42, auth_date=1_784_000_000, token=TOKEN):
    pairs = {"auth_date": str(auth_date), "query_id": "AAE",
             "user": json.dumps({"id": user_id, "first_name": "Ana"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def test_valid_init_data_returns_user():
    user = validate_init_data(make_init_data(), TOKEN, now=1_784_000_100)
    assert user == {"id": 42, "first_name": "Ana"}


def test_tampered_hash_rejected():
    data = make_init_data() + "x"
    assert validate_init_data(data, TOKEN, now=1_784_000_100) is None


def test_wrong_token_rejected():
    data = make_init_data(token="999:OTHER")
    assert validate_init_data(data, TOKEN, now=1_784_000_100) is None


def test_stale_init_data_rejected():
    data = make_init_data(auth_date=1_784_000_000)
    assert validate_init_data(data, TOKEN, now=1_784_000_000 + 100_000) is None


def test_garbage_rejected():
    assert validate_init_data("", TOKEN) is None
    assert validate_init_data("not=even&close", TOKEN) is None


def test_fixture_locked_at_kickoff():
    from src.web.app import fixture_locked
    fixture = {"StartTime": 1_784_055_600_000}  # ms epoch
    assert not fixture_locked(fixture, now=1_784_055_599)   # 1s before: open
    assert fixture_locked(fixture, now=1_784_055_600)       # kickoff: locked
    assert fixture_locked(fixture, now=1_784_060_000)       # in play: locked
    assert fixture_locked({}, now=1_784_055_600)            # no start: locked


# --- discovery/pot/request endpoints end-to-end (Fase 2) ---------------------

def _fresh_init(uid, name="Ana"):
    import time
    pairs = {"auth_date": str(int(time.time())), "query_id": "AAE",
             "user": json.dumps({"id": uid, "first_name": name})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    from fastapi.testclient import TestClient
    from src.engine.store import Store
    from src.web import app as webapp
    monkeypatch.setattr(webapp, "store", Store(path=str(tmp_path / "web.sqlite3")))
    return TestClient(webapp.app)


def test_discover_join_and_pot_flow(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ana, bia = _fresh_init(1, "Ana"), _fresh_init(2, "Bia")

    r = c.post("/api/create", json={"initData": ana, "name": "Bolão do Pote",
                                    "visibility": "public", "buy_in": 100,
                                    "payout_preset": "winner_takes_all"})
    assert r.status_code == 200 and r.json()["ok"]

    d = c.post("/api/discover", json={"initData": bia}).json()
    assert len(d["pools"]) == 1
    card = d["pools"][0]
    assert card["visibility"] == "public" and card["buy_in"] == 100
    assert card["pot"] == 100 and card["my_status"] == "none"  # only Ana in, pot=100

    pid = card["id"]
    assert c.post("/api/join", json={"initData": bia, "pool_id": pid}).json()["ok"]
    d2 = c.post("/api/discover", json={"initData": bia}).json()["pools"][0]
    assert d2["my_status"] == "member" and d2["pot"] == 200  # Bia joined -> 2 x 100


def test_request_and_approval_flow(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    ana, ze, mal = _fresh_init(1, "Ana"), _fresh_init(3, "Zé"), _fresh_init(9, "Mal")

    pid = c.post("/api/create", json={"initData": ana, "name": "Fechado",
                                      "visibility": "request", "buy_in": 0,
                                      "payout_preset": "top3"}).json()["pool_id"]

    # public join is refused on a request-only pool
    assert c.post("/api/join", json={"initData": ze, "pool_id": pid}).status_code == 403
    assert c.post("/api/request", json={"initData": ze,
                                        "pool_id": pid}).json()["status"] == "pending"

    # creator sees the request; a stranger cannot approve it
    assert c.post("/api/discover", json={"initData": ze}).json()["pools"][0]["my_status"] == "pending"
    reqs = c.post("/api/state", json={"initData": ana}).json()["requests"]
    assert len(reqs) == 1 and reqs[0]["user_id"] == 3
    assert c.post("/api/approve", json={"initData": mal, "pool_id": pid,
                                        "user_id": 3, "decision": "approve"}).status_code == 403

    # creator approves -> Zé becomes a member
    assert c.post("/api/approve", json={"initData": ana, "pool_id": pid,
                                        "user_id": 3, "decision": "approve"}).json()["ok"]
    assert c.post("/api/discover", json={"initData": ze}).json()["pools"][0]["my_status"] == "member"
    assert c.post("/api/state", json={"initData": ana}).json()["requests"] == []
