"""AI narrator: goal/final events -> witty PT-BR line -> voice note (OGG/Opus).

Text: template-based (deterministic, zero deps). If DEEPSEEK_API_KEY is set,
the line is rewritten by the LLM for extra flavour (graceful fallback).
Voice: edge-tts neural voice, converted to OGG/Opus via ffmpeg so Telegram
renders a proper voice note (waveform bubble).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import tempfile
from pathlib import Path

import edge_tts

log = logging.getLogger("narrator")

VOICE = os.environ.get("NARRATOR_VOICE", "pt-BR-AntonioNeural")
RATE = "+12%"  # a bit faster: excited commentary

GOAL_TEMPLATES = [
    "GOOOOOOL! {score_side} marca! {home} {h}, {away} {a}. "
    "Alguém nesse bolão tá rindo à toa agora!",
    "É GOL! Balançou a rede! {home} {h} a {a} {away}. "
    "Confere teu palpite, porque o placar não tem dó!",
    "GOOOOOL, meu amigo! {score_side} fez! Tá {h} a {a} no placar — "
    "e tem gente no grupo passando mal!",
]

FINAL_TEMPLATES = [
    "Apita o árbitro, FIM DE JOGO! {home} {h}, {away} {a}. "
    "Palpites liquidados — {leader} tá voando no bolão!",
    "Acabooou! {home} {h} a {a} {away}. Foi tenso, foi lindo, "
    "e o topo do placar agora é de {leader}!",
    "Fim de papo! {home} {h}, {away} {a}. Pontos na conta — "
    "e {leader} carimbando a liderança!",
]


def _fill(template: str, label: str, h: int, a: int, leader: str = "") -> str:
    home, away = (label.split(" x ", 1) if " x " in label else (label, ""))
    score_side = home if h >= a else away
    return template.format(home=home, away=away, h=h, a=a,
                           score_side=score_side, leader=leader)


def goal_line(label: str, h: int, a: int) -> str:
    return _fill(random.choice(GOAL_TEMPLATES), label, h, a)


def final_line(label: str, h: int, a: int, leader: str) -> str:
    return _fill(random.choice(FINAL_TEMPLATES), label, h, a, leader)


async def _llm_spice(line: str) -> str:
    """Optional DeepSeek rewrite; template line survives any failure."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return line
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=key, base_url="https://api.deepseek.com")
        resp = await asyncio.wait_for(client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content":
                       "Você é um narrador de futebol brasileiro carismático e "
                       "zoeiro. Reescreva a frase mantendo placar e nomes EXATOS, "
                       "em 1-2 frases faladas, sem emojis."},
                      {"role": "user", "content": line}],
            max_tokens=120), timeout=8)
        text = (resp.choices[0].message.content or "").strip()
        return text or line
    except Exception:
        log.warning("LLM spice failed; using template line", exc_info=True)
        return line


async def synth_voice(text: str, out_ogg: Path) -> Path:
    """text -> mp3 (edge-tts) -> ogg/opus (ffmpeg) for Telegram voice notes."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        mp3 = Path(tmp.name)
    try:
        await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(mp3))
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(mp3), "-c:a", "libopus",
            "-b:a", "48k", "-ac", "1", str(out_ogg),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await proc.wait() != 0:
            raise RuntimeError("ffmpeg opus conversion failed")
        return out_ogg
    finally:
        mp3.unlink(missing_ok=True)


async def narrate(kind: str, label: str, h: int, a: int,
                  leader: str = "") -> Path | None:
    """Full pipeline; returns path to .ogg voice note or None on failure."""
    if os.environ.get("TORCIDA_NARRATOR", "1") == "0":
        return None
    try:
        line = goal_line(label, h, a) if kind == "goal" else final_line(
            label, h, a, leader or "o líder")
        line = await _llm_spice(line)
        out = Path(tempfile.gettempdir()) / f"torcida_{kind}_{os.getpid()}_{random.randrange(1 << 30)}.ogg"
        return await synth_voice(line, out)
    except Exception:
        log.warning("narration failed (%s %s)", kind, label, exc_info=True)
        return None
