"""Replay a recorded TxLINE match into the group for a demo/video.

Feeds a recording (real TxLINE data) through the SAME settlement + announcer
pipeline the live bot uses, posting goals, incidents, phases, voice notes and
the final leaderboard — paced for the camera. Fully isolated: a temp DB and a
demo fixture id, so the real pool is never touched.

Dry run (prints, no Telegram):
  .venv/bin/python scripts/replay_match.py data/recordings/semi1_fra_esp.jsonl
Live (posts to the group's announcements topic):
  .venv/bin/python scripts/replay_match.py data/recordings/semi1_fra_esp.jsonl \
      --send --chat -1004425833960 --topic 77 --pace 3
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.bot.main import _incident_message
from src.engine.models import Pick, Pool
from src.engine.settlement import SettlementService
from src.engine.store import Store
from src.narrator.narrator import narrate

DEMO_FIXTURE = 99_000_001
DEMO_LABEL = "França x Espanha"
DEMO_PLAYERS = [(1, "Crash", "1", 3.41), (2, "Bia", "2", 3.35),
                (3, "Léo", "X", 3.10)]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("--send", action="store_true", help="post to Telegram")
    ap.add_argument("--chat", type=int)
    ap.add_argument("--topic", type=int)
    ap.add_argument("--pace", type=float, default=3.0, help="seconds between beats")
    args = ap.parse_args()
    load_dotenv()

    store = Store(path=tempfile.mktemp(suffix=".sqlite3"))
    pool = store.create_pool(Pool(id=uuid.uuid4().hex, name="🎬 Ensaio — FRA x ESP",
                                  creator_id=1))
    store.set_fixture_label(DEMO_FIXTURE, DEMO_LABEL)
    for uid, name, sel, odd in DEMO_PLAYERS:
        store.join(pool.id, uid, name)
        store.place_pick(Pick(id=uuid.uuid4().hex, pool_id=pool.id, user_id=uid,
                              fixture_id=DEMO_FIXTURE, market="1x2",
                              selection=sel, odds_decimal=odd))

    bot = None
    if args.send:
        from aiogram import Bot
        from aiogram.types import FSInputFile
        import os
        bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])

    async def post(text: str, voice=None) -> None:
        clean = text
        print("  »", text.replace("<b>", "").replace("</b>", ""))
        if bot:
            await bot.send_message(args.chat, clean, parse_mode="HTML",
                                   message_thread_id=args.topic)
            if voice:
                await bot.send_voice(args.chat, FSInputFile(voice),
                                     message_thread_id=args.topic)
            await asyncio.sleep(args.pace)  # pace only matters for the camera

    async def on_goal(s):
        result = await narrate("goal", DEMO_LABEL, s.home_goals or 0,
                               s.away_goals or 0) if args.send else None
        await post(f"⚽ <b>GOOOL!</b>\n{html.escape(DEMO_LABEL)}: "
                   f"<b>{s.home_goals} x {s.away_goals}</b>",
                   result[0] if result else None)

    async def on_phase(s, label):
        await post(f"{label}\n⚽ <b>{html.escape(DEMO_LABEL)}</b>")

    async def on_final(s, n):
        rows = store.standings(pool.id)
        medals = ["🥇", "🥈", "🥉"]
        board = "\n".join(f"{medals[i] if i < 3 else f'{i+1}.'} "
                          f"<b>{html.escape(nm)}</b> — {p} pts"
                          for i, (_, nm, p) in enumerate(rows))
        result = await narrate("final", DEMO_LABEL, s.home_goals or 0,
                               s.away_goals or 0,
                               leader=rows[0][1] if rows else "") \
            if args.send else None
        await post(f"🏁 <b>Fim de jogo!</b>\n{html.escape(DEMO_LABEL)}: "
                   f"<b>{s.home_goals} x {s.away_goals}</b>\n\n"
                   f"Palpites liquidados. 📊 <b>{html.escape(pool.name)}</b>:\n{board}",
                   result[0] if result else None)

    svc = SettlementService(store=store, on_goal=on_goal, on_final=on_final,
                            on_phase=on_phase)

    if args.send:
        await post("🎬 <b>ENSAIO</b> — replay da semifinal real (dados TxLINE) 👇")

    seen: set[str] = set()
    for line in open(args.recording):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("kind") != "scores":
            continue
        d = ev.get("data", {})
        if d.get("FixtureId") != 18_237_038:
            continue
        d = {**d, "FixtureId": DEMO_FIXTURE}  # isolate from the real fixture
        await svc.handle_event({**ev, "data": d})
        inc = _incident_message(d, DEMO_LABEL)
        if inc and inc[0] not in seen:
            seen.add(inc[0])
            await post(inc[1])

    if bot:
        await bot.session.close()
    print("replay done.")


if __name__ == "__main__":
    asyncio.run(main())
