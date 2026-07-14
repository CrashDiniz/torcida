"""Telegram Mini App initData validation (HMAC, per official docs).

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_S = 24 * 3600  # reject initData older than a day


def validate_init_data(init_data: str, bot_token: str,
                       now: float | None = None) -> dict | None:
    """Return the parsed user dict if initData is authentic, else None."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    auth_date = int(pairs.get("auth_date", 0))
    if (now or time.time()) - auth_date > MAX_AGE_S:
        return None
    try:
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None
