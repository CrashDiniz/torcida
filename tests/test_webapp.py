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
