from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Any

from app.config import DEFAULT_PROFILE_ID

SUPPORTED_REACTION_TYPES = {"emoji", "custom_emoji"}
SUPPORTED_REACTION_ACTIONS = {"add", "remove", "replace"}


@dataclass(frozen=True)
class ReactionSyncEvent:
    origin_platform: str
    target_chat_id: str
    target_message_id: str
    reaction_type: str
    reaction_value: str
    action: str
    actor_key: str
    profile_id: str = DEFAULT_PROFILE_ID

    def __post_init__(self) -> None:
        normalized_reaction_type = str(self.reaction_type).lower()
        normalized_action = str(self.action).lower()
        if normalized_reaction_type not in SUPPORTED_REACTION_TYPES:
            raise ValueError(f"Unsupported reaction_type: {self.reaction_type!r}")
        if normalized_action not in SUPPORTED_REACTION_ACTIONS:
            raise ValueError(f"Unsupported action: {self.action!r}")

        object.__setattr__(self, "origin_platform", str(self.origin_platform))
        object.__setattr__(self, "target_chat_id", str(self.target_chat_id))
        object.__setattr__(self, "target_message_id", str(self.target_message_id))
        object.__setattr__(self, "reaction_type", normalized_reaction_type)
        object.__setattr__(self, "reaction_value", str(self.reaction_value))
        object.__setattr__(self, "action", normalized_action)
        object.__setattr__(self, "actor_key", str(self.actor_key))
        object.__setattr__(self, "profile_id", str(self.profile_id or DEFAULT_PROFILE_ID))

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin_platform": self.origin_platform,
            "target_chat_id": self.target_chat_id,
            "target_message_id": self.target_message_id,
            "reaction_type": self.reaction_type,
            "reaction_value": self.reaction_value,
            "action": self.action,
            "actor_key": self.actor_key,
            "profile_id": self.profile_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReactionSyncEvent":
        return cls(
            origin_platform=str(payload["origin_platform"]),
            target_chat_id=str(payload["target_chat_id"]),
            target_message_id=str(payload["target_message_id"]),
            reaction_type=str(payload["reaction_type"]),
            reaction_value=str(payload.get("reaction_value", "")),
            action=str(payload["action"]),
            actor_key=str(payload["actor_key"]),
            profile_id=str(payload.get("profile_id") or DEFAULT_PROFILE_ID),
        )

    def dedupe_key(self) -> tuple[str, str, str, str, str, str, str, str]:
        return (
            self.profile_id,
            self.origin_platform,
            self.target_chat_id,
            self.target_message_id,
            self.actor_key,
            self.reaction_type,
            self.reaction_value,
            self.action,
        )


class ReactionSyncDeduper:
    def __init__(self, ttl_seconds: float = 120.0):
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._entries: dict[tuple[str, str, str, str, str, str, str, str], float] = {}
        self._lock = RLock()

    def check_and_remember(self, event: ReactionSyncEvent) -> bool:
        now = time.monotonic()
        key = event.dedupe_key()
        with self._lock:
            self._purge_expired(now)
            expires_at = self._entries.get(key)
            if expires_at is not None and expires_at > now:
                return False
            self._entries[key] = now + self.ttl_seconds
        return True

    def remember(self, event: ReactionSyncEvent) -> None:
        now = time.monotonic()
        with self._lock:
            self._purge_expired(now)
            self._entries[event.dedupe_key()] = now + self.ttl_seconds

    def _purge_expired(self, now: float) -> None:
        expired_keys = [key for key, expires_at in self._entries.items() if expires_at <= now]
        for key in expired_keys:
            self._entries.pop(key, None)
