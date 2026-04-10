from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TopicMapping:
    tg_chat_id: int
    max_chat_id: str
    message_thread_id: int
    topic_name: str


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
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS topic_mappings (
                    tg_chat_id INTEGER NOT NULL,
                    max_chat_id TEXT NOT NULL,
                    message_thread_id INTEGER NOT NULL,
                    topic_name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tg_chat_id, max_chat_id),
                    UNIQUE (tg_chat_id, message_thread_id)
                )
                """
            )
            self._conn.commit()

    def get_by_max_chat(self, tg_chat_id: int, max_chat_id: Any) -> TopicMapping | None:
        return self._fetch_one(
            """
            SELECT tg_chat_id, max_chat_id, message_thread_id, topic_name
            FROM topic_mappings
            WHERE tg_chat_id = ? AND max_chat_id = ?
            """,
            (int(tg_chat_id), str(max_chat_id)),
        )

    def get_by_thread(self, tg_chat_id: int, message_thread_id: int) -> TopicMapping | None:
        return self._fetch_one(
            """
            SELECT tg_chat_id, max_chat_id, message_thread_id, topic_name
            FROM topic_mappings
            WHERE tg_chat_id = ? AND message_thread_id = ?
            """,
            (int(tg_chat_id), int(message_thread_id)),
        )

    def topic_name_exists(
        self,
        tg_chat_id: int,
        topic_name: str,
        exclude_max_chat_id: Any | None = None,
    ) -> bool:
        params: list[Any] = [int(tg_chat_id), topic_name]
        sql = """
            SELECT 1
            FROM topic_mappings
            WHERE tg_chat_id = ? AND topic_name = ?
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
    ) -> TopicMapping:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO topic_mappings (
                    tg_chat_id, max_chat_id, message_thread_id, topic_name
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tg_chat_id, max_chat_id)
                DO UPDATE SET
                    message_thread_id = excluded.message_thread_id,
                    topic_name = excluded.topic_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(tg_chat_id), str(max_chat_id), int(message_thread_id), topic_name),
            )
            self._conn.commit()
        return TopicMapping(int(tg_chat_id), str(max_chat_id), int(message_thread_id), topic_name)

    def delete_by_max_chat(self, tg_chat_id: int, max_chat_id: Any) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM topic_mappings WHERE tg_chat_id = ? AND max_chat_id = ?",
                (int(tg_chat_id), str(max_chat_id)),
            )
            self._conn.commit()

    def delete_by_thread(self, tg_chat_id: int, message_thread_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM topic_mappings WHERE tg_chat_id = ? AND message_thread_id = ?",
                (int(tg_chat_id), int(message_thread_id)),
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
        )
