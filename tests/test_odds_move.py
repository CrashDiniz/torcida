"""_odds_move_note: the in-play odds-movement line fed to the narrator."""
import asyncio
import uuid

from src.engine.models import Pick, Pool
from src.engine.store import Store


def _snapshot(home_milli: int, draw_milli: int, away_milli: int) -> list[dict]:
    return [{"SuperOddsType": "1X2_PARTICIPANT_RESULT", "MarketPeriod": "",
             "Ts": 2, "PriceNames": ["part1", "draw", "part2"],
             "Prices": [home_milli, draw_milli, away_milli]}]


class FakeTx:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    async def odds_snapshot(self, fixture_id):
        return self._snapshot


def _botmain(tmp_path, monkeypatch):
    # import inside the test so the module-level Store() never touches data/
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    from src.bot import main as botmain
    botmain.store = Store(path=str(tmp_path / "test.sqlite3"))
    return botmain


def test_odds_move_note_names_the_lone_holder(tmp_path, monkeypatch):
    botmain = _botmain(tmp_path, monkeypatch)
    botmain.txline = FakeTx(_snapshot(1500, 4000, 6000))  # home now 1.5
    botmain.store.record_opening_odds(900, 4.0, 3.2, 1.9, ts=1.0)
    pool = botmain.store.create_pool(
        Pool(id=uuid.uuid4().hex, name="Bolão", creator_id=1))
    botmain.store.join(pool.id, 42, "Pedro")
    botmain.store.place_pick(Pick(id="", pool_id=pool.id, user_id=42,
                                  fixture_id=900, market="1x2",
                                  selection="1", odds_decimal=4.0))
    note = asyncio.run(botmain._odds_move_note(900, "Argentina x Inglaterra", 1, 0))
    assert note is not None
    assert "4.0 pra 1.5" in note
    assert "Argentina" in note
    assert "só Pedro segurou" in note


def test_odds_move_note_skips_small_moves_and_draws(tmp_path, monkeypatch):
    botmain = _botmain(tmp_path, monkeypatch)
    botmain.txline = FakeTx(_snapshot(3800, 3200, 1900))  # 4.0 -> 3.8: no story
    botmain.store.record_opening_odds(900, 4.0, 3.2, 1.9, ts=1.0)
    assert asyncio.run(
        botmain._odds_move_note(900, "Argentina x Inglaterra", 1, 0)) is None
    assert asyncio.run(  # draw: no leading side to talk about
        botmain._odds_move_note(900, "Argentina x Inglaterra", 1, 1)) is None
    assert asyncio.run(  # no opening baseline recorded
        botmain._odds_move_note(901, "Espanha x França", 1, 0)) is None
