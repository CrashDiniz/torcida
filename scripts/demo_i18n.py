"""Upgrade the /demo replay assets: odds-move beat + EN texts and audio.

Adds to data/demo/demo.json (idempotent — safe to re-run):
- an "odds" beat after the first goal narrating the market crash (the
  check-mate TxODDS feature the live bot already ships), PT + EN audio;
- text_en on every beat and audio_en clips for the narrated ones, spoken
  by the same ElevenLabs voice (multilingual model handles both).

Display text (shown as transcript) and TTS text differ on purpose: the
narrator SPEAKS numbers written out; the page SHOWS digits.

Usage:  .venv/bin/python scripts/demo_i18n.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.narrator.narrator import _elevenlabs_mp3

OUT = Path("data/demo")
AUDIO = OUT / "audio"

ODDS_BEAT_AT = 2100.0  # ~5 min after goal 1 (1800), well before half-time

ODDS_PT_TTS = ("[excited] E o mercado SENTIU o golpe! A Espanha despencou de "
               "três e trinta e nove pra um e oitenta e oito — quem quiser a "
               "zebra agora, paga caro! [laughs] E a Bia carimbou a três e "
               "trinta e cinco antes da bola rolar... isso é faro, minha gente!")
ODDS_PT = ("📉 O mercado sentiu o golpe: Espanha despencou de 3.39 pra 1.88 — "
           "quem quiser a zebra agora paga caro. A Bia carimbou 3.35 antes da "
           "bola rolar. Palpite congelado não muda: isso é faro.")
ODDS_EN_TTS = ("[excited] And the market FELT that one! Spain crashed from "
               "three point four to one point nine — the underdog premium is "
               "GONE! [laughs] And Bia stamped hers at three thirty-five "
               "before kickoff... that is what you call a nose for it!")
ODDS_EN = ("📉 The market felt that one: Spain crashed from 3.39 to 1.88 — "
           "the underdog premium is gone. Bia stamped 3.35 before kickoff. "
           "A frozen pick never moves: that's a nose for it.")

# beat translations keyed by the PT audio file (narrated) or PT text (phases)
EN_BY_AUDIO: dict[str, tuple[str, str]] = {  # audio -> (tts, display)
    "gol1.mp3": (
        "[excited] GOOOOAL! Spain finds the net! Spain one, France nil — and "
        "Bia is BOUNCING: she called Spain, and she's already polishing the "
        "trophy!",
        "GOOOOAL! Spain finds the net! Spain 1, France 0 — and Bia is "
        "bouncing: she called Spain and she's already polishing the trophy!"),
    "gol2.mp3": (
        "[excited] Bia called it and she is FLYING up the board! GOAL FOR "
        "SPAIN! Two for Spain, zero for France!",
        "Bia called it and she's flying up the board! GOAL FOR SPAIN! "
        "2 for Spain, 0 for France!"),
    "gol3.mp3": (
        "[excited] GOAL! Spain scores AGAIN — three nil Spain! Bia nailed the "
        "pick, she is untouchable tonight!",
        "GOAL! Spain scores again — 3 for Spain, 0 for France! Bia nailed "
        "the pick — untouchable tonight!"),
    "final.mp3": (
        "[excited] There's the whistle, the pool is CLOSED! Spain two, France "
        "nil! And the champion is BIA, with three hundred and thirty-five "
        "points! She smoked it — Léo and Crash stuck on zero!",
        "Full-time — the pool is closed! Spain 2, France 0! And the champion "
        "is Bia, with 335 points — she smoked it, Léo and Crash stuck on zero!"),
}
EN_PHASES: dict[str, str] = {
    "🟡 Intervalo": "🟡 Half-time",
    "🟢 Bola rolando — 2º tempo!": "🟢 Second half under way!",
    "❌ VAR: gol anulado — placar volta pra 0 x 2":
        "❌ VAR: goal disallowed — back to 0 x 2",
}


async def speak(tts: str, kind: str, name: str) -> bool:
    ok = await _elevenlabs_mp3(tts, kind, AUDIO / name)
    print(("ok  " if ok else "FAIL") + f" {name}")
    return ok


async def main() -> None:
    load_dotenv()
    demo = json.loads((OUT / "demo.json").read_text())
    demo["label_en"] = "France x Spain"

    # 1. odds-move beat (skip if a previous run already added it)
    if not any(b.get("kind") == "odds" for b in demo["timeline"]):
        beat = {"at": ODDS_BEAT_AT, "kind": "odds", "score": [0, 1],
                "text": ODDS_PT, "audio": None}
        if await speak(ODDS_PT_TTS, "goal", "odds.mp3"):
            beat["audio"] = "odds.mp3"
        demo["timeline"].append(beat)
        demo["timeline"].sort(key=lambda b: b["at"])

    # 2. EN texts + audio on every beat
    for b in demo["timeline"]:
        if b["kind"] == "odds":
            b["text_en"] = ODDS_EN
            if b.get("audio") and await speak(ODDS_EN_TTS, "goal", "odds_en.mp3"):
                b["audio_en"] = "odds_en.mp3"
        elif b.get("audio") in EN_BY_AUDIO:
            tts, display = EN_BY_AUDIO[b["audio"]]
            b["text_en"] = display
            en_name = b["audio"].replace(".mp3", "_en.mp3")
            kind = "final" if "final" in b["audio"] else "goal"
            if (AUDIO / en_name).exists() or await speak(tts, kind, en_name):
                b["audio_en"] = en_name
        elif b["kind"] == "phase":
            b["text_en"] = EN_PHASES.get(b["text"], b["text"])

    (OUT / "demo.json").write_text(
        json.dumps(demo, ensure_ascii=False, indent=1))
    print(f"wrote {OUT/'demo.json'} — {len(demo['timeline'])} beats")


if __name__ == "__main__":
    asyncio.run(main())
