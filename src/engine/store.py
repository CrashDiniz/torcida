"""SQLite persistence for pools, entries and picks. Thread-safe enough for
a single asyncio process (WAL + one connection per call)."""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import Entry, PayoutPreset, Pick, PickStatus, Pool

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  creator_id INTEGER NOT NULL,
  payout_preset TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'pt-BR',
  narrator_delay_s INTEGER NOT NULL DEFAULT 0,
  entry_points INTEGER NOT NULL DEFAULT 1000,
  created_at REAL NOT NULL,
  invite_code TEXT NOT NULL UNIQUE,
  telegram_chat_id INTEGER
);
CREATE TABLE IF NOT EXISTS entries (
  pool_id TEXT NOT NULL REFERENCES pools(id),
  user_id INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  points INTEGER NOT NULL DEFAULT 0,
  joined_at REAL NOT NULL,
  PRIMARY KEY (pool_id, user_id)
);
CREATE TABLE IF NOT EXISTS picks (
  id TEXT PRIMARY KEY,
  pool_id TEXT NOT NULL REFERENCES pools(id),
  user_id INTEGER NOT NULL,
  fixture_id INTEGER NOT NULL,
  market TEXT NOT NULL,
  selection TEXT NOT NULL,
  odds_decimal REAL NOT NULL,
  placed_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  points_awarded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_picks_fixture ON picks(fixture_id, status);
CREATE INDEX IF NOT EXISTS idx_picks_pool ON picks(pool_id);
"""


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("DATABASE_PATH", "data/app.sqlite3")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- pools ---------------------------------------------------------------

    def create_pool(self, pool: Pool, telegram_chat_id: int | None = None) -> Pool:
        with self._conn() as c:
            c.execute(
                "INSERT INTO pools VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pool.id, pool.name, pool.creator_id, pool.payout_preset.value,
                 pool.language, pool.narrator_delay_s, pool.entry_points,
                 pool.created_at, pool.invite_code, telegram_chat_id),
            )
        return pool

    def pool_by_invite(self, invite_code: str) -> Pool | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM pools WHERE invite_code=?", (invite_code,)
            ).fetchone()
        return self._pool(row) if row else None

    def pool_by_id(self, pool_id: str) -> Pool | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM pools WHERE id=?", (pool_id,)).fetchone()
        return self._pool(row) if row else None

    @staticmethod
    def _pool(row: sqlite3.Row) -> Pool:
        return Pool(
            id=row["id"], name=row["name"], creator_id=row["creator_id"],
            payout_preset=PayoutPreset(row["payout_preset"]),
            language=row["language"], narrator_delay_s=row["narrator_delay_s"],
            entry_points=row["entry_points"], created_at=row["created_at"],
            invite_code=row["invite_code"],
        )

    # --- entries -------------------------------------------------------------

    def join(self, pool_id: str, user_id: int, display_name: str) -> Entry:
        entry = Entry(pool_id=pool_id, user_id=user_id, display_name=display_name)
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?)",
                (entry.pool_id, entry.user_id, entry.display_name,
                 entry.points, entry.joined_at),
            )
        return entry

    def standings(self, pool_id: str) -> list[tuple[int, str, int]]:
        """[(user_id, display_name, points)] ordered by points desc."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT e.user_id, e.display_name,
                          e.points + COALESCE(SUM(p.points_awarded), 0) AS total
                   FROM entries e
                   LEFT JOIN picks p ON p.pool_id = e.pool_id AND p.user_id = e.user_id
                   WHERE e.pool_id = ?
                   GROUP BY e.user_id ORDER BY total DESC""",
                (pool_id,),
            ).fetchall()
        return [(r["user_id"], r["display_name"], r["total"]) for r in rows]

    # --- picks ---------------------------------------------------------------

    def place_pick(self, pick: Pick) -> Pick:
        if not pick.id:
            pick.id = uuid.uuid4().hex
        with self._conn() as c:
            c.execute(
                "INSERT INTO picks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pick.id, pick.pool_id, pick.user_id, pick.fixture_id, pick.market,
                 pick.selection, pick.odds_decimal, pick.placed_at,
                 pick.status.value, pick.points_awarded),
            )
        return pick

    def open_picks_for_fixture(self, fixture_id: int) -> list[Pick]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM picks WHERE fixture_id=? AND status='open'",
                (fixture_id,),
            ).fetchall()
        return [self._pick(r) for r in rows]

    def update_pick(self, pick: Pick) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE picks SET status=?, points_awarded=? WHERE id=?",
                (pick.status.value, pick.points_awarded, pick.id),
            )

    @staticmethod
    def _pick(row: sqlite3.Row) -> Pick:
        return Pick(
            id=row["id"], pool_id=row["pool_id"], user_id=row["user_id"],
            fixture_id=row["fixture_id"], market=row["market"],
            selection=row["selection"], odds_decimal=row["odds_decimal"],
            placed_at=row["placed_at"], status=PickStatus(row["status"]),
            points_awarded=row["points_awarded"],
        )
