"""SQLite persistence for chats, messages and extension key/value state."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .events import MessageEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id     TEXT PRIMARY KEY,
    chat_type   TEXT NOT NULL,
    name        TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    message_id  TEXT,
    user_id     TEXT NOT NULL,
    nickname    TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    segments    TEXT NOT NULL DEFAULT '[]',
    reply_to    TEXT,
    ts          INTEGER NOT NULL,
    is_self     INTEGER NOT NULL DEFAULT 0,
    processed   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_id
    ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_unread
    ON messages(chat_id, processed, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extension_kv (
    extension_id TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY(extension_id, key)
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store.init() has not been called")
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def upsert_chat(
        self, chat_id: str, chat_type: str, name: str | None = None
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO chats(chat_id, chat_type, name, created_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                name = COALESCE(excluded.name, chats.name)
            """,
            (chat_id, chat_type, name, int(time.time() * 1000)),
        )
        await self.db.commit()

    async def add_message(self, event: MessageEvent) -> int | None:
        """Insert a message. Returns the row id, or None for a duplicate."""
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO messages
                (chat_id, message_id, user_id, nickname, text, segments,
                 reply_to, ts, is_self, processed)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.chat_id,
                event.message_id,
                event.user_id,
                event.nickname,
                event.text,
                json.dumps(event.segments, ensure_ascii=False),
                event.reply_to,
                event.ts,
                1 if event.is_self else 0,
                1 if event.is_self else 0,
            ),
        )
        await self.db.commit()
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid or 0)

    async def add_self_message(
        self,
        chat_id: str,
        text: str,
        message_id: str | None,
        ts: int | None = None,
        segments: list[dict[str, Any]] | None = None,
    ) -> int | None:
        now = ts or int(time.time() * 1000)
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO messages
                (chat_id, message_id, user_id, nickname, text, segments,
                 reply_to, ts, is_self, processed)
            VALUES(?, ?, ?, ?, ?, ?, NULL, ?, 1, 1)
            """,
            (
                chat_id,
                message_id,
                "self",
                "我",
                text,
                json.dumps(segments or [{"type": "text", "data": {"text": text}}], ensure_ascii=False),
                now,
            ),
        )
        await self.db.commit()
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid or 0)

    @staticmethod
    def _row(row: aiosqlite.Row) -> dict[str, Any]:
        data = dict(row)
        data["segments"] = json.loads(data.get("segments") or "[]")
        data["is_self"] = bool(data.get("is_self"))
        data["processed"] = bool(data.get("processed"))
        return data

    async def recent_messages(
        self, chat_id: str, limit: int
    ) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row(row) for row in rows]

    async def unread_messages(self, chat_id: str) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT * FROM messages
            WHERE chat_id = ? AND processed = 0
            ORDER BY id ASC
            """,
            (chat_id,),
        )
        rows = await cursor.fetchall()
        return [self._row(row) for row in rows]

    async def unread_count(self, chat_id: str) -> int:
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ? AND processed = 0",
            (chat_id,),
        )
        row = await cursor.fetchone()
        return int(row["n"] if row else 0)

    async def mark_processed_until(self, chat_id: str, row_id: int) -> None:
        await self.db.execute(
            """
            UPDATE messages SET processed = 1
            WHERE chat_id = ? AND id <= ? AND processed = 0
            """,
            (chat_id, row_id),
        )
        await self.db.commit()

    async def mark_all_processed(self, chat_id: str) -> None:
        await self.db.execute(
            "UPDATE messages SET processed = 1 WHERE chat_id = ? AND processed = 0",
            (chat_id,),
        )
        await self.db.commit()

    async def get_message(
        self, chat_id: str, message_id: str
    ) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
            (chat_id, message_id),
        )
        row = await cursor.fetchone()
        return self._row(row) if row else None

    async def is_self_message(self, chat_id: str, message_id: str) -> bool:
        message = await self.get_message(chat_id, message_id)
        return bool(message and message["is_self"])

    async def prune(self, retention_days: int, now_ms: int | None = None) -> int:
        cutoff = (now_ms or int(time.time() * 1000)) - retention_days * 86_400_000
        cursor = await self.db.execute(
            "DELETE FROM messages WHERE ts < ? AND processed = 1",
            (cutoff,),
        )
        await self.db.commit()
        return int(cursor.rowcount or 0)

    async def list_chats(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM chats ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def kv_get(self, extension_id: str, key: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT value FROM extension_kv WHERE extension_id = ? AND key = ?",
            (extension_id, key),
        )
        row = await cursor.fetchone()
        return str(row["value"]) if row else None

    async def kv_set(self, extension_id: str, key: str, value: str) -> None:
        await self.db.execute(
            """
            INSERT INTO extension_kv(extension_id, key, value, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(extension_id, key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (extension_id, key, value, int(time.time() * 1000)),
        )
        await self.db.commit()

    async def kv_delete(self, extension_id: str, key: str) -> None:
        await self.db.execute(
            "DELETE FROM extension_kv WHERE extension_id = ? AND key = ?",
            (extension_id, key),
        )
        await self.db.commit()
