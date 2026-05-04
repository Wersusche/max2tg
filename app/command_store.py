from __future__ import annotations

import json
import sqlite3
import threading
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import DEFAULT_PROFILE_ID
from app.relay_models import MaxCommand

DEFAULT_MAX_FAILURE_ATTEMPTS = 3


@dataclass(frozen=True)
class LeaseOptions:
    timeout_seconds: int = 30
    poll_interval_seconds: float = 0.5
    lease_timeout_seconds: int = 60


@dataclass(frozen=True)
class CommandFailureResult:
    command_id: int
    attempt_count: int
    dead_lettered: bool


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
                    profile_id TEXT NOT NULL DEFAULT 'default',
                    max_chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'text',
                    text TEXT NOT NULL,
                    elements_json TEXT NOT NULL,
                    filename TEXT NULL,
                    attachment_blob BLOB NULL,
                    reply_to_max_message_id TEXT NULL,
                    tg_chat_id INTEGER NULL,
                    tg_message_id INTEGER NULL,
                    message_thread_id INTEGER NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    failed_at TEXT NULL,
                    last_error TEXT NULL,
                    leased_at TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            existing_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(max_commands)").fetchall()
            }
            if "profile_id" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default'"
                )
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
            if "reply_to_max_message_id" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN reply_to_max_message_id TEXT NULL"
                )
            if "tg_chat_id" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN tg_chat_id INTEGER NULL"
                )
            if "tg_message_id" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN tg_message_id INTEGER NULL"
                )
            if "message_thread_id" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN message_thread_id INTEGER NULL"
                )
            if "attempt_count" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
                )
            if "failed_at" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN failed_at TEXT NULL"
                )
            if "last_error" not in existing_columns:
                self._conn.execute(
                    "ALTER TABLE max_commands ADD COLUMN last_error TEXT NULL"
                )
            self._conn.commit()

    def enqueue(
        self,
        max_chat_id: Any,
        text: str,
        elements: list[dict[str, Any]] | None = None,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        reply_to_max_message_id: Any | None = None,
        tg_chat_id: int | None = None,
        tg_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> MaxCommand:
        payload = json.dumps(elements or [], ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO max_commands (
                    profile_id,
                    max_chat_id,
                    kind,
                    text,
                    elements_json,
                    filename,
                    attachment_blob,
                    reply_to_max_message_id,
                    tg_chat_id,
                    tg_message_id,
                    message_thread_id,
                    attempt_count,
                    failed_at,
                    last_error,
                    leased_at
                )
                VALUES (?, ?, 'text', ?, ?, NULL, NULL, ?, ?, ?, ?, 0, NULL, NULL, NULL)
                """,
                (
                    str(profile_id or DEFAULT_PROFILE_ID),
                    str(max_chat_id),
                    text,
                    payload,
                    str(reply_to_max_message_id) if reply_to_max_message_id is not None else None,
                    int(tg_chat_id) if tg_chat_id is not None else None,
                    int(tg_message_id) if tg_message_id is not None else None,
                    int(message_thread_id) if message_thread_id is not None else None,
                ),
            )
            self._conn.commit()
            command_id = int(cur.lastrowid)
        self._notify_waiters()
        return MaxCommand(
            id=command_id,
            profile_id=str(profile_id or DEFAULT_PROFILE_ID),
            max_chat_id=str(max_chat_id),
            text=text,
            kind="text",
            elements=list(elements or []),
            reply_to_max_message_id=str(reply_to_max_message_id) if reply_to_max_message_id is not None else None,
            tg_chat_id=int(tg_chat_id) if tg_chat_id is not None else None,
            tg_message_id=int(tg_message_id) if tg_message_id is not None else None,
            message_thread_id=int(message_thread_id) if message_thread_id is not None else None,
        )

    def enqueue_photo(
        self,
        max_chat_id: Any,
        photo: bytes,
        caption: str = "",
        elements: list[dict[str, Any]] | None = None,
        filename: str = "photo.jpg",
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        reply_to_max_message_id: Any | None = None,
        tg_chat_id: int | None = None,
        tg_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> MaxCommand:
        payload = json.dumps(elements or [], ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO max_commands (
                    profile_id,
                    max_chat_id,
                    kind,
                    text,
                    elements_json,
                    filename,
                    attachment_blob,
                    reply_to_max_message_id,
                    tg_chat_id,
                    tg_message_id,
                    message_thread_id,
                    attempt_count,
                    failed_at,
                    last_error,
                    leased_at
                )
                VALUES (?, ?, 'photo', ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL)
                """,
                (
                    str(profile_id or DEFAULT_PROFILE_ID),
                    str(max_chat_id),
                    caption,
                    payload,
                    filename,
                    sqlite3.Binary(photo),
                    str(reply_to_max_message_id) if reply_to_max_message_id is not None else None,
                    int(tg_chat_id) if tg_chat_id is not None else None,
                    int(tg_message_id) if tg_message_id is not None else None,
                    int(message_thread_id) if message_thread_id is not None else None,
                ),
            )
            self._conn.commit()
            command_id = int(cur.lastrowid)
        self._notify_waiters()
        return MaxCommand(
            id=command_id,
            profile_id=str(profile_id or DEFAULT_PROFILE_ID),
            max_chat_id=str(max_chat_id),
            text=caption,
            kind="photo",
            elements=list(elements or []),
            filename=filename,
            attachment=bytes(photo),
            reply_to_max_message_id=str(reply_to_max_message_id) if reply_to_max_message_id is not None else None,
            tg_chat_id=int(tg_chat_id) if tg_chat_id is not None else None,
            tg_message_id=int(tg_message_id) if tg_message_id is not None else None,
            message_thread_id=int(message_thread_id) if message_thread_id is not None else None,
        )

    def enqueue_document(
        self,
        max_chat_id: Any,
        document: bytes,
        caption: str = "",
        elements: list[dict[str, Any]] | None = None,
        filename: str = "file",
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        reply_to_max_message_id: Any | None = None,
        tg_chat_id: int | None = None,
        tg_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> MaxCommand:
        return self.enqueue_attachment(
            max_chat_id,
            kind="document",
            attachment=document,
            text=caption,
            elements=elements,
            filename=filename,
            profile_id=profile_id,
            reply_to_max_message_id=reply_to_max_message_id,
            tg_chat_id=tg_chat_id,
            tg_message_id=tg_message_id,
            message_thread_id=message_thread_id,
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
        profile_id: str = DEFAULT_PROFILE_ID,
        reply_to_max_message_id: Any | None = None,
        tg_chat_id: int | None = None,
        tg_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> MaxCommand:
        payload = json.dumps(elements or [], ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO max_commands (
                    profile_id,
                    max_chat_id,
                    kind,
                    text,
                    elements_json,
                    filename,
                    attachment_blob,
                    reply_to_max_message_id,
                    tg_chat_id,
                    tg_message_id,
                    message_thread_id,
                    attempt_count,
                    failed_at,
                    last_error,
                    leased_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL)
                """,
                (
                    str(profile_id or DEFAULT_PROFILE_ID),
                    str(max_chat_id),
                    str(kind),
                    text,
                    payload,
                    filename,
                    sqlite3.Binary(attachment),
                    str(reply_to_max_message_id) if reply_to_max_message_id is not None else None,
                    int(tg_chat_id) if tg_chat_id is not None else None,
                    int(tg_message_id) if tg_message_id is not None else None,
                    int(message_thread_id) if message_thread_id is not None else None,
                ),
            )
            self._conn.commit()
            command_id = int(cur.lastrowid)
        self._notify_waiters()
        return MaxCommand(
            id=command_id,
            profile_id=str(profile_id or DEFAULT_PROFILE_ID),
            max_chat_id=str(max_chat_id),
            text=text,
            kind=str(kind),
            elements=list(elements or []),
            filename=filename,
            attachment=bytes(attachment),
            reply_to_max_message_id=str(reply_to_max_message_id) if reply_to_max_message_id is not None else None,
            tg_chat_id=int(tg_chat_id) if tg_chat_id is not None else None,
            tg_message_id=int(tg_message_id) if tg_message_id is not None else None,
            message_thread_id=int(message_thread_id) if message_thread_id is not None else None,
        )

    def lease_next(self, profile_id: str = DEFAULT_PROFILE_ID) -> MaxCommand | None:
        normalized_profile_id = str(profile_id or DEFAULT_PROFILE_ID)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    id,
                    profile_id,
                    max_chat_id,
                    kind,
                    text,
                    elements_json,
                    filename,
                    attachment_blob,
                    reply_to_max_message_id,
                    tg_chat_id,
                    tg_message_id,
                    message_thread_id,
                    attempt_count
                FROM max_commands
                WHERE leased_at IS NULL
                  AND failed_at IS NULL
                  AND profile_id = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (normalized_profile_id,),
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None
            self._conn.execute(
                """
                UPDATE max_commands
                SET leased_at = CURRENT_TIMESTAMP,
                    attempt_count = COALESCE(attempt_count, 0) + 1
                WHERE id = ?
                """,
                (int(row["id"]),),
            )
            self._conn.commit()
            leased_row = self._conn.execute(
                """
                SELECT
                    id,
                    profile_id,
                    max_chat_id,
                    kind,
                    text,
                    elements_json,
                    filename,
                    attachment_blob,
                    reply_to_max_message_id,
                    tg_chat_id,
                    tg_message_id,
                    message_thread_id,
                    attempt_count
                FROM max_commands
                WHERE id = ?
                """,
                (int(row["id"]),),
            ).fetchone()
        return self._row_to_command(leased_row)

    def ack(self, command_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM max_commands WHERE id = ?", (int(command_id),))
            self._conn.commit()

    def mark_failed(
        self,
        command_id: int,
        *,
        error: str | None = None,
        max_attempts: int = DEFAULT_MAX_FAILURE_ATTEMPTS,
    ) -> CommandFailureResult | None:
        normalized_error = None if error is None else str(error)[:500]
        with self._lock:
            row = self._conn.execute(
                "SELECT id, attempt_count FROM max_commands WHERE id = ?",
                (int(command_id),),
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None

            attempt_count = int(row["attempt_count"] or 0)
            dead_lettered = attempt_count >= max(1, int(max_attempts))
            if dead_lettered:
                self._conn.execute(
                    """
                    UPDATE max_commands
                    SET leased_at = NULL,
                        failed_at = COALESCE(failed_at, CURRENT_TIMESTAMP),
                        last_error = ?
                    WHERE id = ?
                    """,
                    (normalized_error, int(command_id)),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE max_commands
                    SET leased_at = NULL,
                        last_error = ?
                    WHERE id = ?
                    """,
                    (normalized_error, int(command_id)),
                )
            self._conn.commit()

        self._notify_waiters()
        return CommandFailureResult(
            command_id=int(command_id),
            attempt_count=attempt_count,
            dead_lettered=dead_lettered,
        )

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
        profile_id: str = DEFAULT_PROFILE_ID,
    ) -> MaxCommand | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        normalized_profile_id = str(profile_id or DEFAULT_PROFILE_ID)

        while True:
            command = self.lease_next(normalized_profile_id)
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
            profile_id=str(row["profile_id"]),
            max_chat_id=str(row["max_chat_id"]),
            kind=str(row["kind"]),
            text=str(row["text"]),
            elements=list(json.loads(row["elements_json"])),
            filename=row["filename"],
            attachment=bytes(row["attachment_blob"]) if row["attachment_blob"] is not None else None,
            reply_to_max_message_id=str(row["reply_to_max_message_id"]) if row["reply_to_max_message_id"] is not None else None,
            tg_chat_id=int(row["tg_chat_id"]) if row["tg_chat_id"] is not None else None,
            tg_message_id=int(row["tg_message_id"]) if row["tg_message_id"] is not None else None,
            message_thread_id=int(row["message_thread_id"]) if row["message_thread_id"] is not None else None,
            attempt_count=int(row["attempt_count"]) if row["attempt_count"] is not None else 0,
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
