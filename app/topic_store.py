from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import DEFAULT_PROFILE_ID


@dataclass(frozen=True)
class TopicMapping:
    tg_chat_id: int
    max_chat_id: str
    message_thread_id: int
    topic_name: str
    profile_id: str = DEFAULT_PROFILE_ID


class TopicStore:
    """Persistent mapping between Max chats and Telegram forum topics."""

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
                CREATE TABLE IF NOT EXISTS topic_mappings (
                    profile_id TEXT NOT NULL DEFAULT 'default',
                    tg_chat_id INTEGER NOT NULL,
                    max_chat_id TEXT NOT NULL,
                    message_thread_id INTEGER NOT NULL,
                    topic_name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (profile_id, tg_chat_id, max_chat_id),
                    UNIQUE (profile_id, tg_chat_id, message_thread_id)
                )
                """
            )
            self._conn.commit()

    def _needs_rebuild(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'topic_mappings'"
        ).fetchone()
        if row is None:
            return False
        columns = {str(item["name"]) for item in self._conn.execute("PRAGMA table_info(topic_mappings)").fetchall()}
        return "profile_id" not in columns

    def _rebuild_with_profile_id(self) -> None:
        self._conn.execute("ALTER TABLE topic_mappings RENAME TO topic_mappings_legacy")
        self._conn.execute(
            """
            CREATE TABLE topic_mappings (
                profile_id TEXT NOT NULL DEFAULT 'default',
                tg_chat_id INTEGER NOT NULL,
                max_chat_id TEXT NOT NULL,
                message_thread_id INTEGER NOT NULL,
                topic_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (profile_id, tg_chat_id, max_chat_id),
                UNIQUE (profile_id, tg_chat_id, message_thread_id)
            )
            """
        )
        self._conn.execute(
            """
            INSERT OR REPLACE INTO topic_mappings (
                profile_id, tg_chat_id, max_chat_id, message_thread_id, topic_name, created_at, updated_at
            )
            SELECT 'default', tg_chat_id, max_chat_id, message_thread_id, topic_name, created_at, updated_at
            FROM topic_mappings_legacy
            """
        )
        self._conn.execute("DROP TABLE topic_mappings_legacy")
        self._conn.commit()

    def get_by_max_chat(
        self,
        tg_chat_id: int,
        max_chat_id: Any,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> TopicMapping | None:
        return self._fetch_one(
            """
            SELECT profile_id, tg_chat_id, max_chat_id, message_thread_id, topic_name
            FROM topic_mappings
            WHERE profile_id = ? AND tg_chat_id = ? AND max_chat_id = ?
            """,
            (str(profile_id), int(tg_chat_id), str(max_chat_id)),
        )

    def get_by_thread(
        self,
        tg_chat_id: int,
        message_thread_id: int,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> TopicMapping | None:
        return self._fetch_one(
            """
            SELECT profile_id, tg_chat_id, max_chat_id, message_thread_id, topic_name
            FROM topic_mappings
            WHERE profile_id = ? AND tg_chat_id = ? AND message_thread_id = ?
            """,
            (str(profile_id), int(tg_chat_id), int(message_thread_id)),
        )

    def topic_name_exists(
        self,
        tg_chat_id: int,
        topic_name: str,
        exclude_max_chat_id: Any | None = None,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> bool:
        params: list[Any] = [str(profile_id), int(tg_chat_id), topic_name]
        sql = """
            SELECT 1
            FROM topic_mappings
            WHERE profile_id = ? AND tg_chat_id = ? AND topic_name = ?
        """
        if exclude_max_chat_id is not None:
            sql += " AND max_chat_id != ?"
            params.append(str(exclude_max_chat_id))
        sql += " LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row is not None

    def upsert_mapping(
        self,
        tg_chat_id: int,
        max_chat_id: Any,
        message_thread_id: int,
        topic_name: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> TopicMapping:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO topic_mappings (
                    profile_id, tg_chat_id, max_chat_id, message_thread_id, topic_name
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, tg_chat_id, max_chat_id)
                DO UPDATE SET
                    message_thread_id = excluded.message_thread_id,
                    topic_name = excluded.topic_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (str(profile_id), int(tg_chat_id), str(max_chat_id), int(message_thread_id), topic_name),
            )
            self._conn.commit()
        return TopicMapping(int(tg_chat_id), str(max_chat_id), int(message_thread_id), topic_name, str(profile_id))

    def delete_by_max_chat(
        self,
        tg_chat_id: int,
        max_chat_id: Any,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM topic_mappings WHERE profile_id = ? AND tg_chat_id = ? AND max_chat_id = ?",
                (str(profile_id), int(tg_chat_id), str(max_chat_id)),
            )
            self._conn.commit()

    def delete_by_thread(
        self,
        tg_chat_id: int,
        message_thread_id: int,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM topic_mappings WHERE profile_id = ? AND tg_chat_id = ? AND message_thread_id = ?",
                (str(profile_id), int(tg_chat_id), int(message_thread_id)),
            )
            self._conn.commit()

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> TopicMapping | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return TopicMapping(
            tg_chat_id=int(row["tg_chat_id"]),
            max_chat_id=str(row["max_chat_id"]),
            message_thread_id=int(row["message_thread_id"]),
            topic_name=str(row["topic_name"]),
            profile_id=str(row["profile_id"]),
        )
