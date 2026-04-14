"""Resolve numeric Max IDs to human-readable names via WebSocket RPC."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.max_client import MaxClient

log = logging.getLogger(__name__)


class ContactResolver:
    FETCH_FAILED_TTL_SEC = 300

    def __init__(self, client: MaxClient | None = None):
        self.chats: dict[Any, str] = {}
        self.chat_types: dict[Any, str] = {}
        self.users: dict[Any, str] = {}
        self._client = client
        self._fetch_failed_until: dict[Any, float] = {}
        self._my_id: Any = None

    def chat_name(self, chat_id: Any) -> str:
        normalized_chat_id = self._normalize_id(chat_id)
        return self.chats.get(normalized_chat_id, str(normalized_chat_id))

    def is_dm(self, chat_id: Any) -> bool:
        normalized_chat_id = self._normalize_id(chat_id)
        return self.chat_types.get(normalized_chat_id) == "DIALOG"

    def user_name(self, user_id: Any) -> str:
        normalized_user_id = self._normalize_id(user_id)
        return self.users.get(normalized_user_id, str(normalized_user_id))

    async def resolve_user(self, user_id: Any) -> str:
        normalized_user_id = self._normalize_id(user_id)
        if normalized_user_id in self.users:
            return self.users[normalized_user_id]
        if self._is_fetch_failed(normalized_user_id):
            return str(normalized_user_id)

        fetch_succeeded = await self._ws_fetch_contacts([normalized_user_id])

        if normalized_user_id in self.users:
            return self.users[normalized_user_id]
        if fetch_succeeded:
            self._mark_fetch_failed(normalized_user_id)
        return str(normalized_user_id)

    async def resolve_users_batch(self, user_ids: list) -> None:
        """Pre-fetch a batch of unknown user IDs in one WS call."""
        unknown = [
            uid
            for uid in dict.fromkeys(self._normalize_id(raw_uid) for raw_uid in user_ids)
            if uid not in self.users and not self._is_fetch_failed(uid)
        ]
        if unknown:
            fetch_succeeded = await self._ws_fetch_contacts(unknown)
            if fetch_succeeded:
                for uid in unknown:
                    if uid not in self.users:
                        self._mark_fetch_failed(uid)

    # ── populate from AUTH_SNAPSHOT ────────────────────────────────

    def load_snapshot(self, snapshot: dict) -> list:
        profile = snapshot.get("profile", {})
        self._my_id = self._normalize_id(profile.get("id"))
        names = profile.get("names", [])
        if names and self._my_id is not None:
            n = names[0]
            first = n.get("firstName", "")
            last = n.get("lastName", "")
            self.users[self._my_id] = f"{first} {last}".strip() or n.get("name", "")

        all_participant_ids: set[int] = set()

        for chat in snapshot.get("chats", []):
            cid = self._normalize_id(chat.get("id"))
            ctype = chat.get("type")
            title = chat.get("title")

            if cid is None:
                continue

            if ctype:
                self.chat_types[cid] = ctype

            if title:
                self.chats[cid] = title

            participants = chat.get("participants", {})
            for uid_str in participants:
                normalized_uid = self._normalize_id(uid_str)
                if isinstance(normalized_uid, int):
                    all_participant_ids.add(normalized_uid)

            if not title and ctype == "DIALOG" and self._my_id is not None:
                peer_id = None
                for uid in participants:
                    uid_int = self._normalize_id(uid)
                    if not isinstance(uid_int, int):
                        continue
                    if uid_int != self._my_id:
                        peer_id = uid_int
                        break
                if peer_id is not None:
                    self.chats[cid] = f"DM:{peer_id}"

        log.info(
            "Snapshot parsed: %d chats, my_id=%s, %d participant IDs to resolve",
            len(self.chats), self._my_id, len(all_participant_ids),
        )
        return list(all_participant_ids)

    # ── WebSocket contact fetch ────────────────────────────────────

    async def _ws_fetch_contacts(self, user_ids: list) -> bool:
        if not self._client:
            return False
        try:
            resp = await self._client.fetch_contacts(user_ids)
            self._parse_contacts_response(resp)
            return True
        except Exception:
            log.exception("Failed to fetch contacts via WS")
            return False

    def _parse_contacts_response(self, resp: dict) -> None:
        """Parse the response from opcode 32 (CONTACT_GET)."""
        if not resp:
            return

        contacts = resp.get("contacts") or resp.get("users") or []
        if isinstance(contacts, dict):
            contacts = contacts.values()

        for c in contacts:
            if not isinstance(c, dict):
                continue
            uid = self._normalize_id(self._contact_id_from_mapping(c))
            name = self._extract_name_from_contact(c)
            if uid is not None and name:
                self.users[uid] = name
                log.info("Resolved contact %s → %s", uid, name)

        # Maybe the response IS the contact (single user)
        if not contacts and self._contact_id_from_mapping(resp) is not None:
            uid = self._normalize_id(self._contact_id_from_mapping(resp))
            name = self._extract_name_from_contact(resp)
            if uid is not None and name:
                self.users[uid] = name
                log.info("Resolved contact %s → %s", uid, name)

        # Walk the entire response for any name-bearing objects
        self._deep_extract(resp, depth=0)

    def _deep_extract(self, obj: Any, depth: int) -> None:
        if depth > 5:
            return
        if isinstance(obj, dict):
            uid = self._normalize_id(self._contact_id_from_mapping(obj))
            name = self._extract_name_from_contact(obj)
            if uid is not None and name and uid not in self.users:
                self.users[uid] = name
                log.info("Deep-resolved contact %s → %s", uid, name)
            for v in obj.values():
                self._deep_extract(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                self._deep_extract(item, depth + 1)

    @staticmethod
    def _extract_name_from_contact(c: dict) -> str:
        # Max stores names in a "names" array: [{firstName, lastName, name, type}]
        names_list = c.get("names")
        if isinstance(names_list, list) and names_list:
            n = names_list[0]
            first = n.get("firstName", "")
            last = n.get("lastName", "")
            if first or last:
                return f"{first} {last}".strip()
            if n.get("name"):
                return str(n["name"])

        first = c.get("firstName") or c.get("first_name") or ""
        last = c.get("lastName") or c.get("last_name") or ""
        if first or last:
            return f"{first} {last}".strip()

        return str(c.get("friendly") or c.get("displayName") or c.get("name") or "")

    def _is_fetch_failed(self, user_id: Any) -> bool:
        normalized_user_id = self._normalize_id(user_id)
        expires_at = self._fetch_failed_until.get(normalized_user_id)
        if expires_at is None:
            return False
        if expires_at > time.monotonic():
            return True
        self._fetch_failed_until.pop(normalized_user_id, None)
        return False

    def _mark_fetch_failed(self, user_id: Any) -> None:
        normalized_user_id = self._normalize_id(user_id)
        self._fetch_failed_until[normalized_user_id] = time.monotonic() + self.FETCH_FAILED_TTL_SEC

    @staticmethod
    def _normalize_id(value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            normalized_value = value.strip()
            if normalized_value and normalized_value.lstrip("-").isdigit():
                return int(normalized_value)
            return normalized_value
        return value

    @staticmethod
    def _contact_id_from_mapping(payload: dict) -> Any:
        if payload.get("id") is not None:
            return payload.get("id")
        return payload.get("userId")
