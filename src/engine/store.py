"""SQLite persistence for pools, entries and picks. Thread-safe enough for
a single asyncio process (WAL + one connection per call)."""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import (Entry, JoinRequest, PayoutPreset, Pick, PickStatus, Pool,
                     RequestStatus, Visibility)
from .payout import settle_pool

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
  telegram_chat_id INTEGER,
  buy_in INTEGER NOT NULL DEFAULT 0,
  visibility TEXT NOT NULL DEFAULT 'hidden'
);
CREATE TABLE IF NOT EXISTS join_requests (
  pool_id TEXT NOT NULL REFERENCES pools(id),
  user_id INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  requested_at REAL NOT NULL,
  PRIMARY KEY (pool_id, user_id)
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
CREATE TABLE IF NOT EXISTS fixture_verifications (
  fixture_id INTEGER PRIMARY KEY,
  valid INTEGER NOT NULL,
  tx_sig TEXT,
  home INTEGER NOT NULL,
  away INTEGER NOT NULL,
  seq INTEGER,
  verified_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fixture_opening_odds (
  fixture_id INTEGER PRIMARY KEY,
  home REAL NOT NULL,
  draw REAL NOT NULL,
  away REAL NOT NULL,
  recorded_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("DATABASE_PATH", "data/app.sqlite3")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Add columns introduced after a DB was first created. ALTER TABLE
        ADD COLUMN is metadata-only in sqlite (no row rewrite) — safe on a live
        DB. Idempotent: only adds what's missing, and tolerates a concurrent
        process (bot + web start together) winning the race — a duplicate-column
        error just means the other process already added it."""
        def add_column(ddl: str, name: str) -> None:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(pools)")}
            if name in cols:
                return
            try:
                c.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        add_column("ALTER TABLE pools ADD COLUMN buy_in INTEGER NOT NULL DEFAULT 0",
                   "buy_in")
        add_column("ALTER TABLE pools ADD COLUMN visibility TEXT NOT NULL "
                   "DEFAULT 'hidden'", "visibility")

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
                "INSERT INTO pools (id, name, creator_id, payout_preset, language, "
                "narrator_delay_s, created_at, invite_code, telegram_chat_id, "
                "buy_in, visibility) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pool.id, pool.name, pool.creator_id, pool.payout_preset.value,
                 pool.language, pool.narrator_delay_s, pool.created_at,
                 pool.invite_code, telegram_chat_id, pool.buy_in,
                 pool.visibility.value),
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

    def public_pools(self) -> list[Pool]:
        """Pools discoverable on the showcase (public or request-to-join)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pools WHERE visibility IN ('public','request') "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [self._pool(r) for r in rows]

    def is_member(self, pool_id: str, user_id: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM entries WHERE pool_id=? AND user_id=?",
                (pool_id, user_id),
            ).fetchone()
        return row is not None

    def entry_count(self, pool_id: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM entries WHERE pool_id=?", (pool_id,)
            ).fetchone()
        return row["n"]

    def creator_name(self, pool: Pool) -> str:
        """Display name the creator joined with (they auto-join on create)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT display_name FROM entries WHERE pool_id=? AND user_id=?",
                (pool.id, pool.creator_id),
            ).fetchone()
        return row["display_name"] if row else "Anfitrião"

    def pot_for(self, pool_id: str) -> int:
        """Fictional prize pot: buy_in charged once per entry."""
        pool = self.pool_by_id(pool_id)
        if pool is None:
            return 0
        return pool.buy_in * self.entry_count(pool_id)

    def pot_split(self, pool_id: str) -> list[tuple[int, str, int, int]]:
        """Live projection of the pot payout: [(user_id, name, points, chips)]
        ordered by points desc. chips=0 for everyone when buy_in is 0."""
        pool = self.pool_by_id(pool_id)
        rows = self.standings(pool_id)
        if pool is None or not rows:
            return [(uid, name, pts, 0) for uid, name, pts in rows]
        pot = pool.buy_in * len(rows)
        payouts = settle_pool(pot, pool.payout_preset,
                              [(uid, pts) for uid, _, pts in rows])
        return [(uid, name, pts, payouts.get(uid, 0)) for uid, name, pts in rows]

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
            buy_in=row["buy_in"], visibility=Visibility(row["visibility"]),
            created_at=row["created_at"], invite_code=row["invite_code"],
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

    # --- join requests -------------------------------------------------------

    def create_join_request(self, pool_id: str, user_id: int,
                            display_name: str) -> None:
        import time as _time
        with self._conn() as c:
            c.execute(
                "INSERT INTO join_requests VALUES (?,?,?,?,?) "
                "ON CONFLICT(pool_id, user_id) DO UPDATE SET "
                "status='pending', display_name=excluded.display_name, "
                "requested_at=excluded.requested_at",
                (pool_id, user_id, display_name, RequestStatus.PENDING.value,
                 _time.time()),
            )

    def request_status(self, pool_id: str, user_id: int) -> RequestStatus | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT status FROM join_requests WHERE pool_id=? AND user_id=?",
                (pool_id, user_id),
            ).fetchone()
        return RequestStatus(row["status"]) if row else None

    def set_request_status(self, pool_id: str, user_id: int,
                           status: RequestStatus) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE join_requests SET status=? WHERE pool_id=? AND user_id=?",
                (status.value, pool_id, user_id),
            )

    def pending_requests_for_creator(self, creator_id: int) -> list[JoinRequest]:
        """Open requests across every pool this user created, newest first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT r.* FROM join_requests r JOIN pools p ON p.id = r.pool_id "
                "WHERE p.creator_id=? AND r.status='pending' "
                "ORDER BY r.requested_at DESC",
                (creator_id,),
            ).fetchall()
        return [self._request(r) for r in rows]

    @staticmethod
    def _request(row: sqlite3.Row) -> JoinRequest:
        return JoinRequest(
            pool_id=row["pool_id"], user_id=row["user_id"],
            display_name=row["display_name"],
            status=RequestStatus(row["status"]), requested_at=row["requested_at"],
        )

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

    def named_picks_for_fixture(self, fixture_id: int) -> list[tuple[str, str, float]]:
        """[(display_name, selection, odds_decimal)] of open picks on a fixture,
        for the narrator to name who's happy/sad and who held a fading price."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT e.display_name AS name, p.selection AS selection,
                          p.odds_decimal AS odds
                   FROM picks p JOIN entries e
                     ON e.pool_id = p.pool_id AND e.user_id = p.user_id
                   WHERE p.fixture_id=? AND p.status='open'""",
                (fixture_id,),
            ).fetchall()
        return [(r["name"], r["selection"], r["odds"]) for r in rows]

    def record_verification(self, fixture_id: int, valid: bool, tx_sig: str | None,
                            home: int, away: int, seq: int | None, ts: float) -> None:
        """On-chain Merkle-proof verification of a settled result (validateStatV2)."""
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO fixture_verifications
                   (fixture_id, valid, tx_sig, home, away, seq, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (fixture_id, int(valid), tx_sig, home, away, seq, ts))

    def verification(self, fixture_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM fixture_verifications WHERE fixture_id=?",
                (fixture_id,)).fetchone()
        return dict(row) if row else None

    def record_opening_odds(self, fixture_id: int, home: float, draw: float,
                            away: float, ts: float) -> None:
        """First live 1X2 snapshot we see for a fixture = its opening line
        (INSERT OR IGNORE: later calls never overwrite)."""
        with self._conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO fixture_opening_odds
                   (fixture_id, home, draw, away, recorded_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (fixture_id, home, draw, away, ts))

    def opening_odds(self, fixture_id: int) -> dict[str, float] | None:
        """{'1': home, 'X': draw, '2': away} opening line, or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT home, draw, away FROM fixture_opening_odds "
                "WHERE fixture_id=?", (fixture_id,)).fetchone()
        if row is None:
            return None
        return {"1": row["home"], "X": row["draw"], "2": row["away"]}

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
