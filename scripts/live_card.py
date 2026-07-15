"""Rich live-match card for a Telegram group — 100% TxODDS feed.

Pulls the full /scores snapshot directly (score, minute, who's on the ball,
danger level, last notable event) and auto-edits a pinned message every ~15s.
Runs OUTSIDE the bot process, so it's safe to start/stop during a live game.

Run detached:
  LIVE_CARD_THREAD=77 setsid .venv/bin/python scripts/live_card.py \
      >> data/live_card.log 2>&1 &
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx
from dotenv import load_dotenv

# make `src` importable when run as a detached script (sys.path[0] = scripts/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(".env")

from src.engine.settlement import snapshot_score  # noqa: E402
from src.engine.teams import pt  # noqa: E402
from src.ingest.txline import TxLineClient  # noqa: E402

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = int(os.environ.get("LIVE_CARD_CHAT", "-1004425833960"))
THREAD = os.environ.get("LIVE_CARD_THREAD")
TG = f"https://api.telegram.org/bot{TOKEN}"

PHASE = {"h1": "1º tempo", "ht": "intervalo", "h2": "2º tempo",
         "et1": "prorrogação", "et2": "prorrogação", "pe": "pênaltis"}
DANGER = {"HighDangerPossession": "🔥 ataque perigoso!",
          "AttackPossession": "⚡ no ataque", "DangerPossession": "⚠️ pressionando",
          "SafePossession": "🟢 troca de passes"}
EVENT = {"corner": "🚩 Escanteio", "shot": "🎯 Finalização", "free_kick": "⚽ Falta",
         "goal_kick": "🥅 Tiro de meta", "throw_in": "↩️ Lateral",
         "penalty": "🥅 PÊNALTI!", "red_card": "🟥 Cartão vermelho",
         "injury": "🩹 Contusão", "kickoff": "🟢 Bola rolando"}


def _rich(snap: list, home: str, away: str) -> dict:
    """Minute, current possession + danger, and last notable event."""
    minute = 0
    poss_team = poss_danger = None
    poss_seq = last_seq = -1
    last_event = None
    for it in snap:
        if not isinstance(it, dict):
            continue
        seq = it.get("Seq") or 0
        cs = (it.get("Clock") or {}).get("Seconds") or 0
        minute = max(minute, cs)
        if it.get("PossessionType") and seq >= poss_seq:
            poss_seq = seq
            poss_team = {1: home, 2: away}.get(it.get("Participant"))
            poss_danger = DANGER.get(it.get("PossessionType"))
        label = EVENT.get(str(it.get("Action") or "").lower())
        if label and seq >= last_seq:
            last_seq = seq
            side = {1: home, 2: away}.get(it.get("Participant"))
            last_event = f"{label}" + (f" · {side}" if side else "")
    return {"minute": minute // 60, "poss_team": poss_team,
            "poss_danger": poss_danger, "last_event": last_event}


def card_text(fixture, snap, sc, nxt_label) -> str:
    ts = time.strftime("%H:%M:%S")
    if fixture is None:
        if nxt_label:
            return ("📊 <b>PLACAR AO VIVO</b> · TxODDS\n\n"
                    f"Nenhum jogo rolando.\n⏱ Próximo: <b>{nxt_label}</b>\n\n"
                    f"<i>{ts}</i>")
        return f"📊 <b>PLACAR AO VIVO</b> · TxODDS\n\nSem jogos hoje.\n<i>{ts}</i>"
    home = pt(fixture.get("Participant1", "?"))
    away = pt(fixture.get("Participant2", "?"))
    hs, aw, phase_code = (sc[0], sc[1], sc[2]) if sc else (0, 0, "")
    r = _rich(snap, home, away)
    phase = PHASE.get(phase_code, "em jogo")
    lines = [
        "📊 <b>PLACAR AO VIVO</b> · feed TxODDS",
        "",
        f"⚽ <b>{home} {hs} x {aw} {away}</b>",
        f"🔴 {phase} · {r['minute']}'",
    ]
    if r["poss_team"]:
        d = f" — {r['poss_danger']}" if r["poss_danger"] else ""
        lines.append(f"🏃 Com a bola: <b>{r['poss_team']}</b>{d}")
    if r["last_event"]:
        lines.append(f"📍 Último lance: {r['last_event']}")
    lines += ["", f"<i>atualiza sozinho a cada 15s · {ts}</i>"]
    return "\n".join(lines)


async def _find_live(tx: TxLineClient):
    day = int(time.time() // 86400)
    fx = await tx.fixtures(start_epoch_day=day)
    now = time.time()
    for f in sorted(fx, key=lambda x: x.get("StartTime", 0)):
        if (f.get("StartTime") or 0) / 1000 > now:
            continue
        snap = await tx.scores_snapshot(f["FixtureId"])
        sc = snapshot_score(snap)
        if sc and sc[3]:
            continue  # finished
        return f, snap, sc, None
    upcoming = [f for f in fx if (f.get("StartTime") or 0) / 1000 > now]
    if upcoming:
        nf = min(upcoming, key=lambda x: x.get("StartTime", 0))
        return None, None, None, f"{pt(nf.get('Participant1', '?'))} x {pt(nf.get('Participant2', '?'))}"
    return None, None, None, None


def _tg(method: str, payload: dict) -> dict:
    try:
        return httpx.post(f"{TG}/{method}", json=payload, timeout=10).json()
    except Exception:
        return {"ok": False}


async def main() -> None:
    tx = TxLineClient.from_env()
    fixture, snap, sc, nxt = await _find_live(tx)
    text = card_text(fixture, snap, sc, nxt)
    payload = {"chat_id": CHAT, "text": text, "parse_mode": "HTML"}
    if THREAD:
        payload["message_thread_id"] = int(THREAD)
    sent = _tg("sendMessage", payload)
    if not sent.get("ok"):
        print("failed to post card:", sent)
        return
    mid = sent["result"]["message_id"]
    _tg("pinChatMessage",
        {"chat_id": CHAT, "message_id": mid, "disable_notification": True})
    print(f"rich live card posted + pinned: msg {mid}")
    while True:
        await asyncio.sleep(15)
        try:
            fixture, snap, sc, nxt = await _find_live(tx)
            text = card_text(fixture, snap, sc, nxt)
            _tg("editMessageText", {"chat_id": CHAT, "message_id": mid,
                                    "text": text, "parse_mode": "HTML"})
        except Exception as e:
            print("update failed:", e)


if __name__ == "__main__":
    asyncio.run(main())
