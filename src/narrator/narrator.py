"""AI narrator: goal/final events -> witty PT-BR line -> voice note (OGG/Opus).

Text: template-based (deterministic, zero deps). If DEEPSEEK_API_KEY is set,
the line is rewritten by the LLM for extra flavour (graceful fallback).
Voice: ElevenLabs (natural, expressive) when ELEVENLABS_API_KEY is set, else
edge-tts. Either way converted to OGG/Opus via ffmpeg so Telegram renders a
proper voice note. Per-event delivery: goals explode, finals are triumphant.
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

# ElevenLabs: default is a public multilingual male voice ("Adam"); override
# with a Brazilian voice from the voice library via ELEVENLABS_VOICE_ID.
EL_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
EL_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
# per-event expressiveness (lower stability = more emotional/varied)
EL_STYLE = {
    "goal": {"stability": 0.30, "similarity_boost": 0.75, "style": 0.85,
             "use_speaker_boost": True},
    "final": {"stability": 0.45, "similarity_boost": 0.75, "style": 0.55,
              "use_speaker_boost": True},
    "default": {"stability": 0.50, "similarity_boost": 0.75, "style": 0.40,
                "use_speaker_boost": True},
}
# edge-tts fallback: (rate, pitch) per event so it isn't monotone either
EDGE_STYLE = {
    "goal": ("+28%", "+30Hz"),
    "final": ("+6%", "+8Hz"),
    "default": ("+12%", "+0Hz"),
}

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


async def _elevenlabs_mp3(text: str, kind: str, out_mp3: Path) -> bool:
    """Synthesize via ElevenLabs into out_mp3. Returns False (never raises) so
    the caller falls back to edge-tts on any missing key/quota/network issue."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return False
    try:
        import httpx
        settings = EL_STYLE.get(kind, EL_STYLE["default"])
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE_ID}",
                headers={"xi-api-key": key, "accept": "audio/mpeg"},
                json={"text": text, "model_id": EL_MODEL,
                      "voice_settings": settings})
        if resp.status_code != 200:
            log.warning("ElevenLabs %s: %s — falling back to edge-tts",
                        resp.status_code, resp.text[:160])
            return False
        out_mp3.write_bytes(resp.content)
        return True
    except Exception:
        log.warning("ElevenLabs call failed; falling back to edge-tts",
                    exc_info=True)
        return False


async def synth_voice(text: str, out_ogg: Path, kind: str = "goal") -> Path:
    """text -> mp3 (ElevenLabs if keyed, else edge-tts) -> ogg/opus (ffmpeg)."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        mp3 = Path(tmp.name)
    try:
        if not await _elevenlabs_mp3(text, kind, mp3):
            rate, pitch = EDGE_STYLE.get(kind, EDGE_STYLE["default"])
            await edge_tts.Communicate(
                text, VOICE, rate=rate, pitch=pitch).save(str(mp3))
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
        return await synth_voice(line, out, kind=kind)
    except Exception:
        log.warning("narration failed (%s %s)", kind, label, exc_info=True)
        return None
