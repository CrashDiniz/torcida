"""Standalone phase voice announcer — halftime (and full-time backup) audio.

Polls the TxODDS feed; when a live fixture reaches half-time (StatusId 3) it
synthesizes a voice note with the score and sends it to the group. Reuses the
narrator's synth_voice. Runs OUTSIDE the bot (no restart needed).

Run detached:
  setsid .venv/bin/python scripts/phase_voice.py >> data/phase_voice.log 2>&1 &
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(".env")

from src.engine.settlement import snapshot_score  # noqa: E402
from src.engine.teams import pt  # noqa: E402
from src.ingest.txline import TxLineClient  # noqa: E402
from src.narrator.narrator import synth_voice  # noqa: E402

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = int(os.environ.get("LIVE_CARD_CHAT", "-1004425833960"))
THREAD = os.environ.get("PHASE_VOICE_THREAD")  # None -> General (Resenha)


def _status_id(snap: list) -> int | None:
    """Current StatusId = the one on the highest-Seq event that carries it."""
    best_seq, status = -1, None
    for it in snap:
        if isinstance(it, dict) and it.get("StatusId") is not None:
            seq = it.get("Seq") or 0
            if seq >= best_seq:
                best_seq, status = seq, it["StatusId"]
    return status


def halftime_line(home: str, away: str, h: int, a: int) -> str:
    if h == 0 and a == 0:
        return (f"Acabou o primeiro tempo! E até agora tudo igual, zero a zero "
                f"entre {home} e {away}. Muita história pra rolar no segundo tempo!")
    if h == a:
        return (f"Fim do primeiro tempo! Empate de {h} a {a} entre {home} e "
                f"{away}. Tá pegando fogo, hein!")
    if h > a:
        return (f"Fim do primeiro tempo! {home} na frente, {h} a {a} no {away}. "
                f"Vantagem pra quem apostou certo!")
    return (f"Fim do primeiro tempo! {away} na frente, {a} a {h} no {home}. "
            f"Vantagem pra quem apostou certo!")


def send_voice(ogg: Path, caption: str) -> None:
    data = {"chat_id": CHAT, "caption": caption}
    if THREAD:
        data["message_thread_id"] = int(THREAD)
    try:
        with open(ogg, "rb") as fp:
            httpx.post(f"https://api.telegram.org/bot{TOKEN}/sendVoice",
                       data=data, files={"voice": fp}, timeout=45)
    except Exception as e:
        print("sendVoice failed:", e)


async def main() -> None:
    tx = TxLineClient.from_env()
    done: set = set()
    print("phase voice watcher up")
    while True:
        try:
            day = int(time.time() // 86400)
            fx = await tx.fixtures(start_epoch_day=day)
            now = time.time()
            for f in fx:
                if (f.get("StartTime") or 0) / 1000 > now:
                    continue
                fid = f["FixtureId"]
                if (fid, "ht") in done:
                    continue
                snap = await tx.scores_snapshot(fid)
                if _status_id(snap) != 3:  # 3 = half-time
                    continue
                done.add((fid, "ht"))
                sc = snapshot_score(snap)
                h, a = (sc[0], sc[1]) if sc else (0, 0)
                home = pt(f.get("Participant1", "?"))
                away = pt(f.get("Participant2", "?"))
                line = halftime_line(home, away, h, a)
                ogg = Path(tempfile.gettempdir()) / f"ht_{fid}_{int(now)}.ogg"
                await synth_voice(line, ogg, kind="final")
                send_voice(ogg, f"🟡 Intervalo — {home} {h} x {a} {away}")
                print(f"halftime voice sent for {fid}: {line}")
        except Exception as e:
            print("loop error:", e)
        await asyncio.sleep(8)


if __name__ == "__main__":
    asyncio.run(main())
