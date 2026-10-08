"""SQLite persistence layer.

Single file, single connection, no server.  Everything the bot needs to survive
a restart lives here: the archive, the planned schedule and small key/value
state.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL,
    group_key        TEXT    NOT NULL,
    message_ids      TEXT    NOT NULL,
    media_type       TEXT    NOT NULL,
    category         TEXT    NOT NULL,
    original_date    TEXT,
    has_caption      INTEGER NOT NULL DEFAULT 0,
    repost_count     INTEGER NOT NULL DEFAULT 0,
    last_reposted_at TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    note             TEXT,
    created_at       TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_group ON posts(chat_id, group_key);
CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category, enabled);

CREATE TABLE IF NOT EXISTS schedule (
    day        TEXT    NOT NULL,
    slot       INTEGER NOT NULL,
    planned_at TEXT    NOT NULL,
    category   TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',
    post_id    INTEGER,
    posted_at  TEXT,
    error      TEXT,
    PRIMARY KEY (day, slot)
);
CREATE INDEX IF NOT EXISTS idx_schedule_planned ON schedule(planned_at, status);

CREATE TABLE IF NOT EXISTS reposts (
    chat_id        INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    source_post_id INTEGER,
    posted_at      TEXT    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Slot statuses.
PENDING = "pending"
SENDING = "sending"
POSTED = "posted"
SKIPPED = "skipped"
FAILED = "failed"
MISSED = "missed"
#: A slot that was mid-flight when the process died.  Never auto-resent, so a
#: crash cannot produce a duplicate post.
UNKNOWN = "unknown"

DONE_STATUSES = (SENDING, POSTED, UNKNOWN, SKIPPED)


@dataclass
class Post:
    id: int
    chat_id: int
    message_ids: list[int]
    media_type: str
    category: str
    original_date: str | None
    repost_count: int
    last_reposted_at: str | None

    @property
    def label(self) -> str:
        return str(self.message_ids[0]) if self.message_ids else "?"

    @property
    def is_album(self) -> bool:
        return len(self.message_ids) > 1


@dataclass
class Slot:
    day: str
    slot: int
    planned_at: datetime
    category: str
    status: str
    post_id: int | None
    posted_at: datetime | None
    error: str | None


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Database:
    """Thin, synchronous wrapper around one SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ setup

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn
        log.info("Database ready at %s", self.path)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was not called")
        return self._conn

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------- key/value

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._query_one("SELECT value FROM state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: object) -> None:
        self._write(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get_state(key)
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "on"}

    def set_bool(self, key: str, value: bool) -> None:
        self.set_state(key, "1" if value else "0")

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.get_state(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def get_prefixed_state(self, prefix: str) -> dict[str, str]:
        """Every ``key -> value`` whose key starts with ``prefix``, prefix stripped.

        Filtered in Python rather than with ``LIKE``: these keys contain
        underscores, which ``LIKE`` treats as single-character wildcards, so a
        prefix pattern would also match unrelated rows.
        """
        rows = self._query("SELECT key, value FROM state")
        return {
            row["key"][len(prefix):]: row["value"]
            for row in rows
            if row["key"].startswith(prefix)
        }

    def delete_state(self, key: str) -> bool:
        cur = self._write("DELETE FROM state WHERE key = ?", (key,))
        return bool(cur.rowcount)

    # ---------------------------------------------------------------- archive

    def upsert_post(
        self,
        *,
        chat_id: int,
        group_key: str,
        message_id: int,
        media_type: str,
        category: str,
        original_date: str | None,
        has_caption: bool,
        now: datetime,
        reactivate: bool = False,
    ) -> int:
        """Record one physical message as part of a logical archive post.

        A single message is its own post; every member of an album shares the
        ``group_key`` of that album, so re-seeing the same content only adds a
        message id.  The album's category is decided by its heaviest member
        (video beats photo beats audio beats document beats text).

        Re-importing never resurrects a post that was disabled because its
        source message had been deleted, unless ``reactivate`` is set -- which
        the manual import path does, so an admin can revive a post that was
        disabled by a one-off error.
        """
        row = self._query_one(
            "SELECT id, message_ids, media_type, category, enabled FROM posts "
            "WHERE chat_id = ? AND group_key = ?",
            (chat_id, group_key),
        )
        if row is None:
            cur = self._write(
                "INSERT INTO posts(chat_id, group_key, message_ids, media_type, category, "
                "original_date, has_caption, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, group_key, json.dumps([message_id]), media_type, category,
                 original_date, int(has_caption), now.isoformat()),
            )
            return int(cur.lastrowid or 0)

        if reactivate and not row["enabled"]:
            self._write(
                "UPDATE posts SET enabled = 1, note = NULL WHERE id = ?", (row["id"],)
            )
            log.info("Re-enabled archive post %s after a manual import", row["id"])

        try:
            ids = json.loads(row["message_ids"])
        except (TypeError, ValueError):
            ids = []
        if message_id in ids:
            return int(row["id"])
        ids.append(message_id)

        from .selector import category_rank  # local import avoids a cycle

        if category_rank(category) > category_rank(row["category"]):
            new_category, new_media = category, media_type
        else:
            new_category, new_media = row["category"], row["media_type"]

        self._write(
            "UPDATE posts SET message_ids = ?, category = ?, media_type = ?, "
            "has_caption = MAX(has_caption, ?), "
            "original_date = COALESCE(original_date, ?) WHERE id = ?",
            (json.dumps(sorted(ids)), new_category, new_media,
             int(has_caption), original_date, row["id"]),
        )
        return int(row["id"])

    def _row_to_post(self, row: sqlite3.Row) -> Post:
        try:
            ids = json.loads(row["message_ids"])
        except (TypeError, ValueError):
            ids = []
        return Post(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            message_ids=[int(i) for i in ids],
            media_type=row["media_type"],
            category=row["category"],
            original_date=row["original_date"],
            repost_count=int(row["repost_count"]),
            last_reposted_at=row["last_reposted_at"],
        )

    def get_post(self, post_id: int) -> Post | None:
        row = self._query_one("SELECT * FROM posts WHERE id = ?", (post_id,))
        return self._row_to_post(row) if row else None

    def candidates(self, category: str, exclude_ids: list[int]) -> list[Post]:
        sql = "SELECT * FROM posts WHERE enabled = 1 AND category = ?"
        params: list[object] = [category]
        if exclude_ids:
            # The NOT IN clause is only added when there is something to
            # exclude.  Writing `id NOT IN (NULL)` for an empty list -- the
            # obvious-looking fallback -- is not neutral: `x NOT IN (NULL)`
            # evaluates to NULL, never TRUE, so the query would match *nothing*
            # and the archive would look empty.
            sql += f" AND id NOT IN ({','.join('?' for _ in exclude_ids)})"
            params.extend(exclude_ids)
        return [self._row_to_post(r) for r in self._query(sql, tuple(params))]

    def any_candidates(self, exclude_ids: list[int]) -> list[Post]:
        sql = "SELECT * FROM posts WHERE enabled = 1"
        params: list[object] = []
        if exclude_ids:  # see the note in candidates()
            sql += f" AND id NOT IN ({','.join('?' for _ in exclude_ids)})"
            params.extend(exclude_ids)
        return [self._row_to_post(r) for r in self._query(sql, tuple(params))]

    def mark_reposted(self, post_id: int, when: datetime) -> None:
        self._write(
            "UPDATE posts SET repost_count = repost_count + 1, last_reposted_at = ? "
            "WHERE id = ?",
            (when.isoformat(), post_id),
        )

    def disable_post(self, post_id: int, reason: str) -> None:
        self._write(
            "UPDATE posts SET enabled = 0, note = ? WHERE id = ?",
            (reason[:500], post_id),
        )
        log.warning("Disabled archive post %s: %s", post_id, reason)

    def count_by_category(self) -> dict[str, int]:
        rows = self._query(
            "SELECT category, COUNT(*) AS n FROM posts WHERE enabled = 1 "
            "GROUP BY category"
        )
        return {row["category"]: int(row["n"]) for row in rows}

    def archive_size(self, *, enabled_only: bool = True) -> int:
        where = "WHERE enabled = 1" if enabled_only else ""
        row = self._query_one(f"SELECT COUNT(*) AS n FROM posts {where}")
        return int(row["n"]) if row else 0

    def archive_breakdown(self) -> list[tuple[str, int, int, int]]:
        """(category, available, disabled, reposted at least once) per category."""
        rows = self._query(
            "SELECT category, "
            "SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN enabled = 0 THEN 1 ELSE 0 END) AS off, "
            "SUM(CASE WHEN repost_count > 0 THEN 1 ELSE 0 END) AS used "
            "FROM posts GROUP BY category ORDER BY category"
        )
        return [
            (r["category"], int(r["ok"] or 0), int(r["off"] or 0), int(r["used"] or 0))
            for r in rows
        ]

    def recent_posts(self, limit: int = 10, category: str | None = None) -> list[Post]:
        if category:
            rows = self._query(
                "SELECT * FROM posts WHERE enabled = 1 AND category = ? "
                "ORDER BY id DESC LIMIT ?",
                (category, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM posts WHERE enabled = 1 ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return [self._row_to_post(r) for r in rows]

    def drop_posts_containing(
        self, chat_id: int, message_ids: list[int], since: datetime
    ) -> int:
        """Delete freshly-created archive rows that reference these channel ids.

        A repost produces new message ids that no genuine archive entry can
        reference, so anything matching must be our own copy that the channel
        listener archived before the send window could cover it.
        """
        if not message_ids:
            return 0
        wanted = set(message_ids)
        removed = 0
        for row in self._query(
            "SELECT id, message_ids, created_at FROM posts WHERE chat_id = ?", (chat_id,)
        ):
            try:
                ids = set(json.loads(row["message_ids"]))
            except (TypeError, ValueError):
                continue
            if not ids & wanted:
                continue
            created = _dt(row["created_at"])
            if created is not None and created < since:
                continue
            self._write("DELETE FROM posts WHERE id = ?", (row["id"],))
            removed += 1
        if removed:
            log.warning("Removed %s archive entr(ies) created from our own repost", removed)
        return removed

    # --------------------------------------------------------------- schedule

    def insert_schedule(self, day: str, rows: list[tuple[int, datetime, str]]) -> int:
        """Insert planned slots, ignoring ones that already exist."""
        with self._lock:
            cur = self.conn.executemany(
                "INSERT OR IGNORE INTO schedule(day, slot, planned_at, category, status) "
                "VALUES(?, ?, ?, ?, 'pending')",
                [(day, slot, planned.isoformat(), category) for slot, planned, category in rows],
            )
            self.conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def has_schedule(self, day: str) -> bool:
        row = self._query_one("SELECT COUNT(*) AS n FROM schedule WHERE day = ?", (day,))
        return bool(row and int(row["n"]) > 0)

    def _row_to_slot(self, row: sqlite3.Row) -> Slot:
        planned = _dt(row["planned_at"])
        assert planned is not None
        return Slot(
            day=row["day"],
            slot=int(row["slot"]),
            planned_at=planned,
            category=row["category"],
            status=row["status"],
            post_id=int(row["post_id"]) if row["post_id"] is not None else None,
            posted_at=_dt(row["posted_at"]),
            error=row["error"],
        )

    def get_schedule(self, day: str) -> list[Slot]:
        rows = self._query(
            "SELECT * FROM schedule WHERE day = ? ORDER BY slot", (day,)
        )
        return [self._row_to_slot(r) for r in rows]

    def update_slot(self, day: str, slot: int, **fields: object) -> None:
        allowed = {"status", "post_id", "posted_at", "error", "category", "planned_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values: list[object] = []
        for value in updates.values():
            values.append(value.isoformat() if isinstance(value, datetime) else value)
        values.extend([day, slot])
        self._write(
            f"UPDATE schedule SET {assignments} WHERE day = ? AND slot = ?", tuple(values)
        )

    def pending_due(self, day: str, now: datetime) -> list[Slot]:
        rows = self._query(
            "SELECT * FROM schedule WHERE day = ? AND status = 'pending' "
            "ORDER BY planned_at",
            (day,),
        )
        return [s for s in (self._row_to_slot(r) for r in rows) if s.planned_at <= now]

    def pending_for_day(self, day: str) -> list[Slot]:
        rows = self._query(
            "SELECT * FROM schedule WHERE day = ? AND status = 'pending' ORDER BY slot",
            (day,),
        )
        return [self._row_to_slot(r) for r in rows]

    def next_pending(self, day: str, now: datetime) -> Slot | None:
        rows = self._query(
            "SELECT * FROM schedule WHERE day = ? AND status = 'pending' "
            "ORDER BY planned_at",
            (day,),
        )
        slots = [self._row_to_slot(r) for r in rows]
        if not slots:
            return None
        for slot in slots:
            if slot.planned_at >= now:
                return slot
        return None

    def stale_sending(self) -> list[Slot]:
        rows = self._query("SELECT * FROM schedule WHERE status = 'sending'")
        return [self._row_to_slot(r) for r in rows]

    def used_post_ids(self, day: str) -> list[int]:
        placeholders = ",".join("?" for _ in DONE_STATUSES)
        rows = self._query(
            f"SELECT DISTINCT post_id FROM schedule WHERE day = ? AND post_id IS NOT NULL "
            f"AND status IN ({placeholders})",
            (day, *DONE_STATUSES),
        )
        return [int(r["post_id"]) for r in rows]

    def day_counters(self, day: str) -> dict[str, int]:
        rows = self._query(
            "SELECT status, COUNT(*) AS n FROM schedule WHERE day = ? GROUP BY status",
            (day,),
        )
        return {row["status"]: int(row["n"]) for row in rows}

    def last_posted_at(self, day: str) -> datetime | None:
        row = self._query_one(
            "SELECT MAX(posted_at) AS ts FROM schedule WHERE day = ? AND status = 'posted'",
            (day,),
        )
        return _dt(row["ts"]) if row else None

    def history(self, limit: int = 10) -> list[tuple[str, int, str, int | None, str | None]]:
        rows = self._query(
            "SELECT day, slot, category, post_id, posted_at FROM schedule "
            "WHERE status = 'posted' ORDER BY posted_at DESC LIMIT ?",
            (limit,),
        )
        return [
            (r["day"], int(r["slot"]), r["category"],
             int(r["post_id"]) if r["post_id"] is not None else None, r["posted_at"])
            for r in rows
        ]

    def days_with_schedule(self) -> list[str]:
        rows = self._query("SELECT DISTINCT day FROM schedule ORDER BY day DESC")
        return [r["day"] for r in rows]

    def purge_schedule_before(self, day: date) -> int:
        cur = self._write("DELETE FROM schedule WHERE day < ?", (day.isoformat(),))
        return cur.rowcount or 0

    def delete_schedule(self, day: str) -> int:
        """Drop one day's plan entirely.

        Only ever used on a *future* day whose slots are all still ``pending``,
        when the plan is about to be rebuilt after a settings change.  Nothing
        has been published for such a day, so there is nothing to lose; if the
        process died between the delete and the re-insert, the day would simply
        be planned again on the next tick.
        """
        cur = self._write("DELETE FROM schedule WHERE day = ?", (day,))
        return cur.rowcount or 0

    # ---------------------------------------------------------------- reposts

    def remember_repost(
        self, chat_id: int, message_ids: list[int], source_post_id: int | None, when: datetime
    ) -> None:
        if not message_ids:
            return
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO reposts(chat_id, message_id, source_post_id, posted_at) "
                "VALUES(?, ?, ?, ?)",
                [(chat_id, mid, source_post_id, when.isoformat()) for mid in message_ids],
            )
            self.conn.commit()

    def is_own_repost(self, chat_id: int, message_id: int) -> bool:
        row = self._query_one(
            "SELECT 1 FROM reposts WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        return row is not None

    def purge_reposts_before(self, when: datetime) -> int:
        cur = self._write("DELETE FROM reposts WHERE posted_at < ?", (when.isoformat(),))
        return cur.rowcount or 0
