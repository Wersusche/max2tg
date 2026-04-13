from __future__ import annotations

import json
import sqlite3
import threading
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
                    text TEXT NOT NULL,
                    elements_json TEXT NOT NULL,
                    leased_at TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._conn.commit()

    def enqueue(self, max_chat_id: Any, text: str, elements: list[dict[str, Any]] | None = None) -> MaxCommand:
        payload = json.dumps(elements or [], ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO max_commands (max_chat_id, text, elements_json, leased_at)
                VALUES (?, ?, ?, NULL)
                """,
                (str(max_chat_id), text, payload),
            )
            self._conn.commit()
            command_id = int(cur.lastrowid)
        return MaxCommand(id=command_id, max_chat_id=str(max_chat_id), text=text, elements=list(elements or []))

    def lease_next(self, lease_timeout_seconds: int = 60) -> MaxCommand | None:
        expiry_expr = f"-{int(lease_timeout_seconds)} seconds"
        with self._lock:
            self._conn.execute(
                """
                UPDATE max_commands
                SET leased_at = NULL
                WHERE leased_at IS NOT NULL
                  AND leased_at <= datetime('now', ?)
                """,
                (expiry_expr,),
            )
            row = self._conn.execute(
                """
                SELECT id, max_chat_id, text, elements_json
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

    @staticmethod
    def _row_to_command(row: sqlite3.Row) -> MaxCommand:
        return MaxCommand(
            id=int(row["id"]),
            max_chat_id=str(row["max_chat_id"]),
            text=str(row["text"]),
            elements=list(json.loads(row["elements_json"])),
        )
