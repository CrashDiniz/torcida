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
CREATE TABLE IF NOT EXISTS fixture_labels (
  fixture_id INTEGER PRIMARY KEY,
  label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pool_leavers (
  pool_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  left_at REAL NOT NULL,
  PRIMARY KEY (pool_id, user_id)
);
CREATE TABLE IF NOT EXISTS chat_topics (
  chat_id INTEGER NOT NULL,
  topic TEXT NOT NULL,
  thread_id INTEGER NOT NULL,
  PRIMARY KEY (chat_id, topic)
);
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

    def pool_by_chat(self, chat_id: int) -> Pool | None:
        """Latest pool bound to a telegram chat (survives bot restarts)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM pools WHERE telegram_chat_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return self._pool(row) if row else None

    def bind_chat(self, pool_id: str, chat_id: int) -> None:
        """Make pool the chat's single active pool: older pools lose the chat
        binding (they keep settling, but stop receiving group announcements)."""
        with self._conn() as c:
            c.execute("UPDATE pools SET telegram_chat_id=NULL "
                      "WHERE telegram_chat_id=? AND id<>?", (chat_id, pool_id))
            c.execute("UPDATE pools SET telegram_chat_id=? WHERE id=?",
                      (chat_id, pool_id))

    def pools_for_user(self, user_id: int) -> list[Pool]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT p.* FROM pools p JOIN entries e ON e.pool_id = p.id
                   WHERE e.user_id=? ORDER BY p.created_at DESC""",
                (user_id,),
            ).fetchall()
        return [self._pool(r) for r in rows]

    def chats_for_fixture(self, fixture_id: int) -> list[tuple[str, int]]:
        """[(pool_id, telegram_chat_id)] of pools holding picks on a fixture."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT DISTINCT p.pool_id, po.telegram_chat_id
                   FROM picks p JOIN pools po ON po.id = p.pool_id
                   WHERE p.fixture_id=? AND po.telegram_chat_id IS NOT NULL""",
                (fixture_id,),
            ).fetchall()
        return [(r["pool_id"], r["telegram_chat_id"]) for r in rows]

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

    def leave(self, pool_id: str, user_id: int) -> bool:
        """Remove the user's entry and picks; True if they were in the pool."""
        import time as _time
        with self._conn() as c:
            gone = c.execute(
                "DELETE FROM entries WHERE pool_id=? AND user_id=?",
                (pool_id, user_id),
            ).rowcount
            c.execute("DELETE FROM picks WHERE pool_id=? AND user_id=?",
                      (pool_id, user_id))
            if gone:  # remembered so a comeback can be celebrated
                c.execute("INSERT OR REPLACE INTO pool_leavers VALUES (?,?,?)",
                          (pool_id, user_id, _time.time()))
        return bool(gone)

    def has_left(self, pool_id: str, user_id: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM pool_leavers WHERE pool_id=? AND user_id=?",
                (pool_id, user_id),
            ).fetchone()
        return row is not None

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

    def pick_for(self, pool_id: str, user_id: int, fixture_id: int,
                 market: str = "1x2") -> Pick | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM picks WHERE pool_id=? AND user_id=? "
                "AND fixture_id=? AND market=? AND status='open'",
                (pool_id, user_id, fixture_id, market),
            ).fetchone()
        return self._pick(row) if row else None

    def replace_pick(self, pick_id: str, selection: str, odds_decimal: float,
                     placed_at: float) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE picks SET selection=?, odds_decimal=?, placed_at=? "
                "WHERE id=? AND status='open'",
                (selection, odds_decimal, placed_at, pick_id),
            )

    def picks_for_user(self, pool_id: str, user_id: int) -> list[Pick]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM picks WHERE pool_id=? AND user_id=? "
                "ORDER BY placed_at",
                (pool_id, user_id),
            ).fetchall()
        return [self._pick(r) for r in rows]

    def open_picks_for_fixture(self, fixture_id: int) -> list[Pick]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM picks WHERE fixture_id=? AND status='open'",
                (fixture_id,),
            ).fetchall()
        return [self._pick(r) for r in rows]

    def open_fixture_ids(self) -> list[int]:
        """Distinct fixtures that still have open picks (need settlement)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT fixture_id FROM picks WHERE status='open'"
            ).fetchall()
        return [r["fixture_id"] for r in rows]

    def update_pick(self, pick: Pick) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE picks SET status=?, points_awarded=? WHERE id=?",
                (pick.status.value, pick.points_awarded, pick.id),
            )

    # --- fixture labels --------------------------------------------------------

    def set_fixture_label(self, fixture_id: int, label: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO fixture_labels VALUES (?,?)",
                      (fixture_id, label))

    def bound_chats(self) -> list[int]:
        """Distinct telegram chats with an active (bound) pool."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT telegram_chat_id FROM pools "
                "WHERE telegram_chat_id IS NOT NULL").fetchall()
        return [r["telegram_chat_id"] for r in rows]

    def chat_for_pool(self, pool_id: str) -> int | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT telegram_chat_id FROM pools WHERE id=?", (pool_id,)
            ).fetchone()
        return row["telegram_chat_id"] if row else None

    def set_chat_topic(self, chat_id: int, topic: str, thread_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO chat_topics VALUES (?,?,?) ON CONFLICT(chat_id, topic) "
                "DO UPDATE SET thread_id=excluded.thread_id",
                (chat_id, topic, thread_id),
            )

    def chat_topic(self, chat_id: int, topic: str) -> int | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT thread_id FROM chat_topics WHERE chat_id=? AND topic=?",
                (chat_id, topic),
            ).fetchone()
        return row["thread_id"] if row else None

    def fixture_label(self, fixture_id: int) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT label FROM fixture_labels WHERE fixture_id=?",
                (fixture_id,),
            ).fetchone()
        return row["label"] if row else None

    @staticmethod
    def _pick(row: sqlite3.Row) -> Pick:
        return Pick(
            id=row["id"], pool_id=row["pool_id"], user_id=row["user_id"],
            fixture_id=row["fixture_id"], market=row["market"],
            selection=row["selection"], odds_decimal=row["odds_decimal"],
            placed_at=row["placed_at"], status=PickStatus(row["status"]),
            points_awarded=row["points_awarded"],
        )
