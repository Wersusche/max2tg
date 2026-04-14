from __future__ import annotations

import json
import sqlite3
import threading
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.relay_models import MaxCommand


@dataclass(frozen=True)
class LeaseOptions:
    timeout_seconds: int = 30
    poll_interval_seconds: float = 0.5
    lease_timeout_seconds: int = 60


class CommandStore:
    """Persistent queue of Telegram → Max commands with simple leasing."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._command_event: asyncio.Event | None = None
        self._command_event_loop: asyncio.AbstractEventLoop | None = None
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS max_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    max_chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'text',
                    text TEXT NOT NULL,
                    elements_json TEXT NOT NULL,
                    filename TEXT NULL,
                    attachment_blob BLOB NULL,
                    leased_at TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            existing_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(max_commands)").fetchall()
            }
            if "kind" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'"
                )
            if "filename" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN filename TEXT NULL"
                )
            if "attachment_blob" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN attachment_blob BLOB NULL"
                )
            self._conn.commit()

    def enqueue(self, max_chat_id: Any, text: str, elements: list[dict[str, Any]] | None = None) -> MaxCommand:
        payload = json.dumps(elements or [], ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO max_commands (
                    max_chat_id, kind, text, elements_json, filename, attachment_blob, leased_at
                )
                VALUES (?, 'text', ?, ?, NULL, NULL, NULL)
                """,
                (str(max_chat_id), text, payload),
            )
            self._conn.commit()
            command_id = int(cur.lastrowid)
        self._notify_waiters()
        return MaxCommand(
            id=command_id,
            max_chat_id=str(max_chat_id),
            text=text,
            kind="text",
            elements=list(elements or []),
        )

    def enqueue_photo(
        self,
        max_chat_id: Any,
        photo: bytes,
        caption: str = "",
        elements: list[dict[str, Any]] | None = None,
        filename: str = "photo.jpg",
    ) -> MaxCommand:
        payload = json.dumps(elements or [], ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO max_commands (
                    max_chat_id, kind, text, elements_json, filename, attachment_blob, leased_at
                )
                VALUES (?, 'photo', ?, ?, ?, ?, NULL)
                """,
                (str(max_chat_id), caption, payload, filename, sqlite3.Binary(photo)),
            )
            self._conn.commit()
            command_id = int(cur.lastrowid)
        self._notify_waiters()
        return MaxCommand(
            id=command_id,
            max_chat_id=str(max_chat_id),
            text=caption,
            kind="photo",
            elements=list(elements or []),
            filename=filename,
            attachment=bytes(photo),
        )

    def enqueue_document(
        self,
        max_chat_id: Any,
        document: bytes,
        caption: str = "",
        elements: list[dict[str, Any]] | None = None,
        filename: str = "file",
    ) -> MaxCommand:
        return self.enqueue_attachment(
            max_chat_id,
            kind="document",
            attachment=document,
            text=caption,
            elements=elements,
            filename=filename,
        )

    def enqueue_attachment(
        self,
        max_chat_id: Any,
        *,
        kind: str,
        attachment: bytes,
        text: str = "",
        elements: list[dict[str, Any]] | None = None,
        filename: str | None = None,
    ) -> MaxCommand:
        payload = json.dumps(elements or [], ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO max_commands (
                    max_chat_id, kind, text, elements_json, filename, attachment_blob, leased_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    str(max_chat_id),
                    str(kind),
                    text,
                    payload,
                    filename,
                    sqlite3.Binary(attachment),
                ),
            )
            self._conn.commit()
            command_id = int(cur.lastrowid)
        self._notify_waiters()
        return MaxCommand(
            id=command_id,
            max_chat_id=str(max_chat_id),
            text=text,
            kind=str(kind),
            elements=list(elements or []),
            filename=filename,
            attachment=bytes(attachment),
        )

    def lease_next(self) -> MaxCommand | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, max_chat_id, kind, text, elements_json, filename, attachment_blob
                FROM max_commands
                WHERE leased_at IS NULL
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None
            self._conn.execute(
                "UPDATE max_commands SET leased_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(row["id"]),),
            )
            self._conn.commit()
        return self._row_to_command(row)

    def ack(self, command_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM max_commands WHERE id = ?", (int(command_id),))
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM max_commands").fetchone()
        return int(row["c"])

    def reap_expired_leases(self, lease_timeout_seconds: int = 60) -> int:
        expiry_expr = f"-{int(lease_timeout_seconds)} seconds"
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE max_commands
                SET leased_at = NULL
                WHERE leased_at IS NOT NULL
                  AND leased_at <= datetime('now', ?)
                """,
                (expiry_expr,),
            )
            self._conn.commit()
            updated = int(cur.rowcount or 0)
        if updated:
            self._notify_waiters()
        return updated

    async def wait_for_command(
        self,
        timeout_seconds: float = 30.0,
    ) -> MaxCommand | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)

        while True:
            command = self.lease_next()
            if command is not None:
                return command

            remaining = deadline - loop.time()
            if remaining <= 0:
                return None

            event = self._get_or_create_event()
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            finally:
                event.clear()

    @staticmethod
    def _row_to_command(row: sqlite3.Row) -> MaxCommand:
        return MaxCommand(
            id=int(row["id"]),
            max_chat_id=str(row["max_chat_id"]),
            kind=str(row["kind"]),
            text=str(row["text"]),
            elements=list(json.loads(row["elements_json"])),
            filename=row["filename"],
            attachment=bytes(row["attachment_blob"]) if row["attachment_blob"] is not None else None,
        )

    def _get_or_create_event(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        if self._command_event is None or self._command_event_loop is None or self._command_event_loop != loop:
            self._command_event = asyncio.Event()
            self._command_event_loop = loop
        return self._command_event

    def _notify_waiters(self) -> None:
        if self._command_event is None or self._command_event_loop is None:
            return
        if self._command_event_loop.is_closed():
            self._command_event = None
            self._command_event_loop = None
            return
        self._command_event_loop.call_soon_threadsafe(self._command_event.set)
