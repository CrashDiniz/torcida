"""Record SSE events to JSONL; replay them later at any speed.

The recorder is the backbone of testing: record a real match once, then
re-run it deterministically (tests, demos, development without live games).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import AsyncIterator


class Recorder:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")


async def record(streams: list[AsyncIterator[dict]], path: str | Path) -> None:
    """Merge N event streams into one JSONL file (with wall-clock timestamps)."""
    rec = Recorder(path)
    queue: asyncio.Queue = asyncio.Queue()

    async def pump(stream: AsyncIterator[dict]) -> None:
        async for ev in stream:
            await queue.put(ev)

    tasks = [asyncio.create_task(pump(s)) for s in streams]
    try:
        while True:
            ev = await queue.get()
            rec.write(ev)
    finally:
        for t in tasks:
            t.cancel()


async def replay(path: str | Path, speed: float = 1.0) -> AsyncIterator[dict]:
    """Re-emit recorded events. speed=1 real time, 10 = 10x faster, 0 = instant."""
    prev_ts: float | None = None
    with Path(path).open() as f:
        for line in f:
            ev = json.loads(line)
            ts = ev.get("recv_ts") or 0.0
            if prev_ts is not None and speed > 0:
                delay = max(0.0, (ts - prev_ts) / speed)
                await asyncio.sleep(min(delay, 60))
            prev_ts = ts
            ev["replayed"] = True
            ev["recv_ts"] = time.time()
            yield ev
