from src.engine.odds import FALLBACK, parse_snapshot

# real devnet payload shape (captured 2026-07-13)
REAL_ENTRY = {
    "FixtureId": 18241006, "Ts": 1783925101470,
    "Bookmaker": "TXLineStablePriceDemargined",
    "SuperOddsType": "1X2_PARTICIPANT_RESULT",
    "MarketPeriod": "half=1",
    "PriceNames": ["part1", "draw", "part2"],
    "Prices": [3634, 2085, 4078],
}


def test_empty_snapshot_falls_back():
    odds = parse_snapshot([])
    assert not odds.live
    assert odds.home == FALLBACK["1"]


def test_parses_real_devnet_payload():
    odds = parse_snapshot([REAL_ENTRY])
    assert odds.live
    assert odds.home == 3.63
    assert odds.draw == 2.08  # 2085/1000 -> 2.085 rounds down in float
    assert odds.away == 4.08
    assert odds.period == "half=1"


def test_prefers_full_time_over_half():
    ft = dict(REAL_ENTRY, MarketPeriod="regulartime",
              Prices=[1850, 3400, 4200], Ts=1)
    odds = parse_snapshot([REAL_ENTRY, ft])
    assert odds.home == 1.85 and odds.period == ""


def test_prefers_latest_timestamp_within_period():
    newer = dict(REAL_ENTRY, Ts=REAL_ENTRY["Ts"] + 1000, Prices=[3700, 2100, 4000])
    odds = parse_snapshot([REAL_ENTRY, newer])
    assert odds.home == 3.7


def test_survives_garbage_entries():
    snapshot = [42, "junk", None, {"SuperOddsType": "OTHER"},
                {"SuperOddsType": "1X2_PARTICIPANT_RESULT", "PriceNames": ["part1"],
                 "Prices": [2000]},
                REAL_ENTRY]
    odds = parse_snapshot(snapshot)
    assert odds.live and odds.home == 3.63


def test_plain_float_prices_also_work():
    entry = dict(REAL_ENTRY, Prices=[2.5, 3.1, 2.9])
    odds = parse_snapshot([entry])
    assert odds.live and odds.draw == 3.1
