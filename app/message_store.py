from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import DEFAULT_PROFILE_ID


@dataclass(frozen=True)
class MessageMapping:
    tg_chat_id: int
    max_chat_id: str
    max_message_id: str
    tg_message_id: int
    message_thread_id: int | None
    direction: str
    source: str
    profile_id: str = DEFAULT_PROFILE_ID


class MessageStore:
    """Persistent mapping between Max message ids and Telegram message ids."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            if self._needs_rebuild():
                self._rebuild_with_profile_id()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_mappings (
                    profile_id TEXT NOT NULL DEFAULT 'default',
                    tg_chat_id INTEGER NOT NULL,
                    max_chat_id TEXT NOT NULL,
                    max_message_id TEXT NOT NULL,
                    tg_message_id INTEGER NOT NULL,
                    message_thread_id INTEGER NULL,
                    direction TEXT NOT NULL DEFAULT 'max_to_tg',
                    source TEXT NOT NULL DEFAULT 'max',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (profile_id, direction, max_chat_id, max_message_id),
                    UNIQUE (profile_id, tg_chat_id, tg_message_id)
                )
                """
            )
            self._conn.commit()

    def _needs_rebuild(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'message_mappings'"
        ).fetchone()
        if row is None:
            return False
        columns = {
            str(item["name"])
            for item in self._conn.execute("PRAGMA table_info(message_mappings)").fetchall()
        }
        return "profile_id" not in columns

    def _rebuild_with_profile_id(self) -> None:
        self._conn.execute("ALTER TABLE message_mappings RENAME TO message_mappings_legacy")
        self._conn.execute(
            """
            CREATE TABLE message_mappings (
                profile_id TEXT NOT NULL DEFAULT 'default',
                tg_chat_id INTEGER NOT NULL,
                max_chat_id TEXT NOT NULL,
                max_message_id TEXT NOT NULL,
                tg_message_id INTEGER NOT NULL,
                message_thread_id INTEGER NULL,
                direction TEXT NOT NULL DEFAULT 'max_to_tg',
                source TEXT NOT NULL DEFAULT 'max',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (profile_id, direction, max_chat_id, max_message_id),
                UNIQUE (profile_id, tg_chat_id, tg_message_id)
            )
            """
        )
        self._conn.execute(
            """
            INSERT OR REPLACE INTO message_mappings (
                profile_id,
                tg_chat_id,
                max_chat_id,
                max_message_id,
                tg_message_id,
                message_thread_id,
                direction,
                source,
                created_at,
                updated_at
            )
            SELECT
                'default',
                tg_chat_id,
                max_chat_id,
                max_message_id,
                tg_message_id,
                message_thread_id,
                direction,
                source,
                created_at,
                updated_at
            FROM message_mappings_legacy
            """
        )
        self._conn.execute("DROP TABLE message_mappings_legacy")
        self._conn.commit()

    def upsert_mapping(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        tg_chat_id: int,
        max_chat_id: Any,
        max_message_id: Any,
        tg_message_id: int,
        message_thread_id: int | None,
        direction: str = "max_to_tg",
        source: str = "max",
    ) -> MessageMapping:
        mapping = MessageMapping(
            profile_id=str(profile_id or DEFAULT_PROFILE_ID),
            tg_chat_id=int(tg_chat_id),
            max_chat_id=str(max_chat_id),
            max_message_id=str(max_message_id),
            tg_message_id=int(tg_message_id),
            message_thread_id=int(message_thread_id) if message_thread_id is not None else None,
            direction=str(direction),
            source=str(source),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO message_mappings (
                    profile_id,
                    tg_chat_id,
                    max_chat_id,
                    max_message_id,
                    tg_message_id,
                    message_thread_id,
                    direction,
                    source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, direction, max_chat_id, max_message_id)
                DO UPDATE SET
                    tg_chat_id = excluded.tg_chat_id,
                    tg_message_id = excluded.tg_message_id,
                    message_thread_id = excluded.message_thread_id,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    mapping.profile_id,
                    mapping.tg_chat_id,
                    mapping.max_chat_id,
                    mapping.max_message_id,
                    mapping.tg_message_id,
                    mapping.message_thread_id,
                    mapping.direction,
                    mapping.source,
                ),
            )
            self._conn.commit()
        return mapping

    def get_by_max_message(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        max_chat_id: Any,
        max_message_id: Any,
        direction: str | None = "max_to_tg",
    ) -> MessageMapping | None:
        with self._lock:
            if direction is None:
                row = self._conn.execute(
                    """
                    SELECT
                        profile_id,
                        tg_chat_id,
                        max_chat_id,
                        max_message_id,
                        tg_message_id,
                        message_thread_id,
                        direction,
                        source
                    FROM message_mappings
                    WHERE profile_id = ? AND max_chat_id = ? AND max_message_id = ?
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT 1
                    """,
                    (str(profile_id or DEFAULT_PROFILE_ID), str(max_chat_id), str(max_message_id)),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT
                        profile_id,
                        tg_chat_id,
                        max_chat_id,
                        max_message_id,
                        tg_message_id,
                        message_thread_id,
                        direction,
                        source
                    FROM message_mappings
                    WHERE profile_id = ? AND direction = ? AND max_chat_id = ? AND max_message_id = ?
                    """,
                    (str(profile_id or DEFAULT_PROFILE_ID), str(direction), str(max_chat_id), str(max_message_id)),
                ).fetchone()
        return self._row_to_mapping(row)

    def get_by_tg_message(
        self,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        tg_chat_id: int,
        tg_message_id: int,
    ) -> MessageMapping | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    profile_id,
                    tg_chat_id,
                    max_chat_id,
                    max_message_id,
                    tg_message_id,
                    message_thread_id,
                    direction,
                    source
                FROM message_mappings
                WHERE profile_id = ? AND tg_chat_id = ? AND tg_message_id = ?
                """,
                (str(profile_id or DEFAULT_PROFILE_ID), int(tg_chat_id), int(tg_message_id)),
            ).fetchone()
        return self._row_to_mapping(row)

    @staticmethod
    def _row_to_mapping(row: sqlite3.Row | None) -> MessageMapping | None:
        if row is None:
            return None
        return MessageMapping(
            profile_id=str(row["profile_id"]),
            tg_chat_id=int(row["tg_chat_id"]),
            max_chat_id=str(row["max_chat_id"]),
            max_message_id=str(row["max_message_id"]),
            tg_message_id=int(row["tg_message_id"]),
            message_thread_id=int(row["message_thread_id"]) if row["message_thread_id"] is not None else None,
            direction=str(row["direction"]),
            source=str(row["source"]),
        )
