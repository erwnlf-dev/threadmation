"""Penyimpanan SQLite: leads, log post, outbox DM, dan key-value state."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id          TEXT PRIMARY KEY,           -- media id post/reply di Threads
    source      TEXT NOT NULL,              -- search | mention | reply | webhook
    query       TEXT,
    username    TEXT,
    text        TEXT,
    permalink   TEXT,
    posted_at   TEXT,
    score       REAL DEFAULT 0,
    matched     TEXT,                       -- JSON list keyword yang cocok
    status      TEXT NOT NULL DEFAULT 'new',-- new | replied | queued | skipped | contacted | won | lost
    raw         TEXT,
    handled_at  TEXT,                       -- diisi setelah diproses responder
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);

CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- post | reply
    reply_to    TEXT,
    text        TEXT,
    origin      TEXT,                       -- cli | api | responder | ...
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT,
    username    TEXT NOT NULL,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | sent | skipped
    created_at  TEXT NOT NULL,
    sent_at     TEXT,
    UNIQUE(lead_id, username)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = "threadmation.db"):
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            self.conn.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self.conn:
            return self.conn.execute(sql, args)

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default: str | None = None) -> str | None:
        rows = self._all("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --------------------------------------------------------------- leads
    def add_lead(
        self,
        post: dict,
        source: str,
        query: str | None = None,
        score: float = 0.0,
        matched: list[str] | None = None,
    ) -> bool:
        """Simpan lead. Return True kalau lead baru (belum pernah tersimpan)."""
        ts = now_iso()
        cur = self._exec(
            """INSERT OR IGNORE INTO leads
               (id, source, query, username, text, permalink, posted_at, score, matched, raw,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                post["id"],
                source,
                query,
                post.get("username"),
                post.get("text"),
                post.get("permalink"),
                post.get("timestamp"),
                score,
                json.dumps(matched or []),
                json.dumps(post, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        return cur.rowcount > 0

    def get_lead(self, lead_id: str) -> dict | None:
        rows = self._all("SELECT * FROM leads WHERE id=?", (lead_id,))
        return _decode_lead(rows[0]) if rows else None

    def list_leads(self, status: str | None = None, source: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM leads WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if source:
            sql += " AND source=?"
            args.append(source)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [_decode_lead(r) for r in self._all(sql, tuple(args))]

    def unhandled_leads(self, limit: int = 100) -> list[dict]:
        return [
            _decode_lead(r)
            for r in self._all(
                "SELECT * FROM leads WHERE handled_at IS NULL ORDER BY created_at, id LIMIT ?", (limit,)
            )
        ]

    def mark_handled(self, lead_id: str, status: str | None = None) -> None:
        ts = now_iso()
        if status:
            self._exec(
                "UPDATE leads SET handled_at=?, status=?, updated_at=? WHERE id=?",
                (ts, status, ts, lead_id),
            )
        else:
            self._exec("UPDATE leads SET handled_at=?, updated_at=? WHERE id=?", (ts, ts, lead_id))

    def set_lead_status(self, lead_id: str, status: str) -> bool:
        cur = self._exec(
            "UPDATE leads SET status=?, updated_at=? WHERE id=?", (status, now_iso(), lead_id)
        )
        return cur.rowcount > 0

    # --------------------------------------------------------------- posts
    def log_post(self, media_id: str, kind: str, text: str | None, reply_to: str | None = None, origin: str = "cli") -> None:
        self._exec(
            "INSERT OR REPLACE INTO posts(id,kind,reply_to,text,origin,created_at) VALUES(?,?,?,?,?,?)",
            (media_id, kind, reply_to, text, origin, now_iso()),
        )

    def has_replied_to(self, media_id: str) -> bool:
        return bool(self._all("SELECT 1 FROM posts WHERE reply_to=? LIMIT 1", (media_id,)))

    # -------------------------------------------------------------- outbox
    def queue_dm(self, username: str, message: str, lead_id: str | None = None) -> int | None:
        cur = self._exec(
            "INSERT OR IGNORE INTO outbox(lead_id,username,message,created_at) VALUES(?,?,?,?)",
            (lead_id, username, message, now_iso()),
        )
        return cur.lastrowid if cur.rowcount else None

    def list_outbox(self, status: str | None = "pending", limit: int = 200) -> list[dict]:
        if status:
            return self._all(
                "SELECT * FROM outbox WHERE status=? ORDER BY id LIMIT ?", (status, limit)
            )
        return self._all("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,))

    def set_outbox_status(self, item_id: int, status: str) -> bool:
        sent_at = now_iso() if status == "sent" else None
        cur = self._exec(
            "UPDATE outbox SET status=?, sent_at=? WHERE id=?", (status, sent_at, item_id)
        )
        return cur.rowcount > 0


def _decode_lead(row: dict) -> dict:
    row = dict(row)
    row["matched"] = json.loads(row.get("matched") or "[]")
    row.pop("raw", None)
    return row
