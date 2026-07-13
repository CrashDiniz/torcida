"""Record TxLINE odds+scores SSE streams to a JSONL file.

Usage: .venv/bin/python scripts/record_streams.py data/recordings/semi1.jsonl
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.ingest.recorder import record
from src.ingest.txline import TxLineClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


async def main() -> None:
    load_dotenv()
    out = sys.argv[1] if len(sys.argv) > 1 else "data/recordings/session.jsonl"
    client = TxLineClient.from_env()
    print(f"recording odds+scores streams -> {out} (Ctrl+C to stop)")
    await record([client.stream("odds"), client.stream("scores")], out)


if __name__ == "__main__":
    asyncio.run(main())
