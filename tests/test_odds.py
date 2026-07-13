from src.engine.odds import FALLBACK, parse_snapshot


def test_empty_snapshot_falls_back():
    odds = parse_snapshot([])
    assert not odds.live
    assert odds.home == FALLBACK["1"]


def test_parses_and_averages_prices():
    snapshot = [
        {"Market": "1X2", "Prices": [
            {"label": "home", "price": 2.0},
            {"label": "draw", "price": 3.0},
            {"label": "away", "price": 4.0},
        ]},
        {"Market": "Full Time Result", "Prices": [
            {"label": "1", "price": 2.2},
            {"label": "X", "price": 3.4},
            {"label": "2", "price": 3.6},
        ]},
    ]
    odds = parse_snapshot(snapshot)
    assert odds.live
    assert odds.home == 2.1
    assert odds.draw == 3.2
    assert odds.away == 3.8
    assert odds.for_selection("X") == 3.2


def test_ignores_unrelated_markets_and_bad_prices():
    snapshot = [
        {"Market": "Total Goals Over/Under", "Prices": [{"label": "over", "price": 1.9}]},
        {"Market": "1x2", "Prices": [{"label": "home", "price": "not-a-number"}]},
    ]
    odds = parse_snapshot(snapshot)
    assert not odds.live
