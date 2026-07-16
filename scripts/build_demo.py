"""Build the /demo replay assets from a recorded TxLINE match.

Reads a recording (real feed data), extracts the timeline (goals, phases,
final + the live 1X2 odds series) and pre-generates the narrator audio for
each beat, so the web demo replays the whole experience for a judge with no
live match and no Telegram account needed.

Output: data/demo/demo.json + data/demo/audio/*.mp3

Usage:
  .venv/bin/python scripts/build_demo.py data/recordings/semi1_fra_esp.jsonl
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.engine.models import Pick, Pool
from src.engine.settlement import SettlementService
from src.engine.store import Store
from src.narrator.narrator import (_elevenlabs_mp3, _llm_spice,
                                   _strip_audio_tags, final_line, goal_line)

FIXTURE = 18_237_038
LABEL = "França x Espanha"
DEMO_FIXTURE = 99_000_002
PLAYERS = [(1, "Crash", "1", 3.41), (2, "Bia", "2", 3.35), (3, "Léo", "X", 3.10)]

OUT = Path("data/demo")
AUDIO = OUT / "audio"


async def _speak(line: str, name: str) -> str | None:
    """Narration text -> mp3 in data/demo/audio (mp3 plays in every browser)."""
    spiced = await _llm_spice(line)
    mp3 = AUDIO / f"{name}.mp3"
    if await _elevenlabs_mp3(spiced, "goal" if "gol" in name else "final", mp3):
        return spiced
    return None


async def main() -> None:
    load_dotenv()
    recording = sys.argv[1] if len(sys.argv) > 1 else "data/recordings/semi1_fra_esp.jsonl"
    OUT.mkdir(parents=True, exist_ok=True)
    AUDIO.mkdir(parents=True, exist_ok=True)

    store = Store(path=tempfile.mktemp(suffix=".sqlite3"))
    pool = store.create_pool(Pool(id=uuid.uuid4().hex, name="Bolão demo",
                                  creator_id=1))
    store.set_fixture_label(DEMO_FIXTURE, LABEL)
    for uid, name, sel, odd in PLAYERS:
        store.join(pool.id, uid, name)
        store.place_pick(Pick(id=uuid.uuid4().hex, pool_id=pool.id, user_id=uid,
                              fixture_id=DEMO_FIXTURE, market="1x2",
                              selection=sel, odds_decimal=odd))

    timeline: list[dict] = []
    t0: list[float | None] = [None]  # first event ts (mutable closure box)

    def at(ts: float) -> float:
        if t0[0] is None:
            t0[0] = ts
        return round(ts - t0[0], 1)

    current_ts: list[float] = [0.0]
    goal_n: list[int] = [0]

    async def on_goal(s) -> None:
        h, a = s.home_goals or 0, s.away_goals or 0
        goal_n[0] += 1
        leading = "1" if h > a else "2" if a > h else None
        happy = [n for _, n, sel, _ in PLAYERS if sel == leading] if leading else []
        sad = [n for _, n, sel, _ in PLAYERS
               if leading and sel != leading and sel != "X"]
        line = goal_line(LABEL, h, a, happy=happy or None, sad=sad or None)
        name = f"gol{goal_n[0]}"
        text = await _speak(line, name)
        timeline.append({"at": at(current_ts[0]), "kind": "goal",
                         "score": [h, a],
                         "text": _strip_audio_tags(text or line),
                         "audio": f"{name}.mp3" if text else None})
        print("goal", h, a)

    async def on_phase(s, label) -> None:
        timeline.append({"at": at(current_ts[0]), "kind": "phase",
                         "score": [s.home_goals or 0, s.away_goals or 0],
                         "text": label.replace("<b>", "").replace("</b>", "")})
        print("phase", label)

    async def on_final(s, n) -> None:
        h, a = s.home_goals or 0, s.away_goals or 0
        rows = store.standings(pool.id)
        standings = [(nm, p, 0) for _, nm, p in rows]
        line = final_line(LABEL, h, a, rows[0][1] if rows else "",
                          standings=standings)
        text = await _speak(line, "final")
        timeline.append({"at": at(current_ts[0]), "kind": "final",
                         "score": [h, a],
                         "text": _strip_audio_tags(text or line),
                         "audio": "final.mp3" if text else None,
                         "standings": [[nm, p] for _, nm, p in rows]})
        print("final", h, a)

    svc = SettlementService(store=store, on_goal=on_goal, on_final=on_final,
                            on_phase=on_phase)

    odds_series: list[list] = []
    for raw in open(recording):
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        data = ev.get("data", {})
        if data.get("FixtureId") != FIXTURE:
            continue
        current_ts[0] = ev.get("recv_ts", 0.0)
        if ev.get("kind") == "odds":
            if (data.get("SuperOddsType") == "1X2_PARTICIPANT_RESULT"
                    and not data.get("MarketPeriod")
                    and data.get("PriceNames") == ["part1", "draw", "part2"]):
                prices = [round(p / 1000, 2) for p in data.get("Prices", [])]
                if len(prices) == 3:
                    odds_series.append([at(current_ts[0])] + prices)
        elif ev.get("kind") == "scores":
            await svc.handle_event(
                {**ev, "data": {**data, "FixtureId": DEMO_FIXTURE}})

    # rebase the clock: recordings start hours before kickoff. t=0 becomes
    # 30 min before the first score beat (keeps the pre-match odds drift).
    base = (timeline[0]["at"] - 1800) if timeline else 0
    base = max(0.0, base)
    for beat in timeline:
        beat["at"] = round(beat["at"] - base, 1)
    odds_series = [[round(t - base, 1), h, d, a]
                   for t, h, d, a in odds_series if t >= base]

    # downsample odds to ~120 points so the page stays light
    step = max(1, len(odds_series) // 120)
    odds_series = odds_series[::step]

    (OUT / "demo.json").write_text(json.dumps({
        "label": LABEL, "fixture_id": FIXTURE,
        "source": "TxLINE devnet feed, recorded live during the real semifinal",
        "players": [{"name": n, "selection": sel, "odds": odd}
                    for _, n, sel, odd in PLAYERS],
        "duration": timeline[-1]["at"] if timeline else 0,
        "timeline": timeline, "odds": odds_series,
    }, ensure_ascii=False, indent=1))
    print(f"wrote {OUT/'demo.json'} — {len(timeline)} beats, "
          f"{len(odds_series)} odds points")


if __name__ == "__main__":
    asyncio.run(main())
