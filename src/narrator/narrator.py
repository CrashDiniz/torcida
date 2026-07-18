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

# PT-BR "resenha" style: elongated goal cry + a bordão + tie-in to the bolão
# (naming the side that scored and, at full time, the leader) — the hook no
# real broadcaster has. Research: BR fans want emotion + catchphrases.
# PT-BR "resenha" style: elongated goal cry + a bordão + tie-in to the bolão.
# {score} is always winner-first ("Espanha 2 a 0 França"), the natural way a
# result is called. Research: BR fans want emotion + catchphrases.
GOAL_TEMPLATES = [
    "GOOOOOOOL! É GOL, meu amigo, é GOL! {score_side} balançou a rede! "
    "{score}. Haja coração no bolão — tem gente passando MAL agora!",
    "OLHA O GOOOOL! {score_side} marca! {score}. "
    "Anota no álbum, torcedor: esse ponto tá valendo!",
    "É DELE, GOOOOL! {score_side} na frente! {score}. "
    "Faça a sua festa quem acertou — o resto que chore no grupo!",
    "SEGURA ESSA! GOOOL! {score_side} não perdoou! {score}. "
    "O bolão pegou fogo — sai que é suuua!",
]

FINAL_TEMPLATES = [
    "Apitou o juiz, ACABOU! {score}. "
    "E no bolão, quem faz a festa é {leader} — esse tá voando!",
    "É O FIM, senhoras e senhores! {score}. "
    "Palpites liquidados e {leader} carimbou a liderança. O resto: tenta na próxima!",
    "Fim de jogo! {score}. Pode anotar no álbum: "
    "{leader} é quem manda nesse bolão agora. Haja coração!",
]


# goal lines that name a real group member — the hook no broadcaster has
GOAL_NAMED_HAPPY = [
    "GOOOOOOL! {score_side} balançou a rede! {score}. "
    "E {name} tá PULANDO — cravou {score_side} e agora só falta a taça!",
    "É GOL! {score_side} marca! {score}. "
    "Anota aí: {name} palpitou certinho e tá voando no bolão!",
]
GOAL_NAMED_SAD = [
    "GOOOL! {score_side} na frente! {score}. "
    "E {name}, coitado, apostou no outro — tá passando MAL no grupo agora!",
    "É GOL! {score_side} não perdoou! {score}. "
    "{name} tá no chão — esse palpite foi pro brejo, haja coração!",
]


def _fill(template: str, label: str, h: int, a: int, leader: str = "",
          name: str = "") -> str:
    home, away = (label.split(" x ", 1) if " x " in label else (label, ""))
    score_side = home if h >= a else away
    # winner-first score phrase; natural home-order on a draw
    if a > h:
        score = f"{away} {a} a {h} {home}"
    else:
        score = f"{home} {h} a {a} {away}"
    return template.format(home=home, away=away, h=h, a=a, score=score,
                           score_side=score_side, leader=leader, name=name)


def goal_line(label: str, h: int, a: int,
              happy: list[str] | None = None,
              sad: list[str] | None = None) -> str:
    """Name a group member when we know who picked the (currently) leading side;
    happy = picked the side ahead, sad = picked against. Falls back to generic."""
    if happy:
        return _fill(random.choice(GOAL_NAMED_HAPPY), label, h, a,
                     name=random.choice(happy))
    if sad:
        return _fill(random.choice(GOAL_NAMED_SAD), label, h, a,
                     name=random.choice(sad))
    return _fill(random.choice(GOAL_TEMPLATES), label, h, a)


def final_line(label: str, h: int, a: int, leader: str,
               standings: list[tuple[str, int, int]] | None = None,
               pot: int = 0) -> str:
    if not standings:
        return _fill(random.choice(FINAL_TEMPLATES), label, h, a, leader)
    score = _fill("{score}", label, h, a)
    champ, champ_pts, _ = standings[0]
    pot_txt = f", levando o pote de {pot} fichas" if pot else ""
    board = "; ".join(f"{n} com {p} pontos" for n, p, _ in standings[:3])
    return (f"Apitou o juiz, o bolão fechou! {score}. "
            f"O grande campeão é {champ}, com {champ_pts} pontos{pot_txt}! "
            f"A classificação final: {board}.")


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
                       "Você é um narrador de futebol brasileiro carismático, "
                       "estilo resenha de bar. Reescreva a frase pra soar NATURAL "
                       "FALADA (vira áudio), mantendo os NOMES exatos, o resultado "
                       "correto e os números de odds quando houver (ex: 'de 4.0 pra "
                       "1.5' — pode falar 'de quatro pra um e meio'). REGRA DE OURO: "
                       "NUNCA troque os papéis das pessoas — quem a frase diz que "
                       "cravou/acertou/segurou a odd continua sendo ESSA pessoa, e "
                       "quem apostou contra continua contra; na dúvida, repita a "
                       "frase original sem mudar os fatos. Ao dizer o "
                       "placar, use a forma falada do português "
                       "— ex: '2 para a Argentina e 1 para a Inglaterra' ou 'a "
                       "Argentina marca o segundo' — NUNCA '2 a 1' seco nem 'x'. "
                       "Coloque tags de emoção entre colchetes no meio do texto (são "
                       "do sintetizador de voz): comece o grito de gol com [shouting], "
                       "use [excited] na parte animada e [happy] ao citar quem está "
                       "ganhando. 1 a 2 frases curtas, com energia, sem emojis, sem "
                       "asteriscos, sem markdown."},
                      {"role": "user", "content": line}],
            max_tokens=140, temperature=0.8), timeout=10)
        text = (resp.choices[0].message.content or "").strip()
        return text or line
    except Exception:
        log.warning("LLM spice failed; using template line", exc_info=True)
        return line


def _strip_audio_tags(text: str) -> str:
    """Drop [emotion] tags — only ElevenLabs v3 understands them; edge-tts and
    the v2 model would read them out loud."""
    import re
    return re.sub(r"\s*\[[^\]]*\]\s*", " ", text).strip()


async def _elevenlabs_mp3(text: str, kind: str, out_mp3: Path) -> bool:
    """Synthesize via ElevenLabs into out_mp3. Returns False (never raises) so
    the caller falls back to edge-tts on any missing key/quota/network issue."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return False
    try:
        import httpx
        # read at call time: the bot loads .env inside main(), after this
        # module is imported, so module-level constants would be stale
        voice_id = os.environ.get("ELEVENLABS_VOICE_ID", EL_VOICE_ID)
        model = os.environ.get("ELEVENLABS_MODEL", EL_MODEL)
        if "v3" not in model:  # only v3 parses [emotion] tags
            text = _strip_audio_tags(text)
        settings = EL_STYLE.get(kind, EL_STYLE["default"])
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": key, "accept": "audio/mpeg"},
                json={"text": text, "model_id": model,
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
                _strip_audio_tags(text), VOICE, rate=rate, pitch=pitch).save(str(mp3))
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
                  leader: str = "", happy: list[str] | None = None,
                  sad: list[str] | None = None,
                  standings: list[tuple[str, int, int]] | None = None,
                  pot: int = 0,
                  odds_move: str | None = None) -> tuple[Path, str] | None:
    """Full pipeline; returns (path to .ogg voice note, spoken text) or None.
    The text (audio tags stripped) doubles as the accessibility transcript.
    happy/sad = members who picked the leading/trailing side (goal only).
    standings = [(name, points, chips)] top-first + pot = full-time payout call.
    odds_move = ready-made line about the market ("odd despencou de 4.0 pra 1.5")."""
    if os.environ.get("TORCIDA_NARRATOR", "1") == "0":
        return None
    try:
        line = (goal_line(label, h, a, happy=happy, sad=sad) if kind == "goal"
                else final_line(label, h, a, leader or "o líder",
                                standings=standings, pot=pot))
        if odds_move:
            line = f"{line} {odds_move}"
        template_line = line
        line = await _llm_spice(line)
        if line != template_line:  # audit trail: catch LLM role/name swaps
            log.info("narration %s template=%r spiced=%r", kind, template_line, line)
        out = Path(tempfile.gettempdir()) / f"torcida_{kind}_{os.getpid()}_{random.randrange(1 << 30)}.ogg"
        return await synth_voice(line, out, kind=kind), _strip_audio_tags(line)
    except Exception:
        log.warning("narration failed (%s %s)", kind, label, exc_info=True)
        return None
