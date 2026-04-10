from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telegram.error import BadRequest

from app.tg_sender import TelegramSender
from app.topic_store import TopicStore

log = logging.getLogger(__name__)

MAX_TOPIC_NAME_LENGTH = 128


class TopicRouter:
    """Create and reuse Telegram forum topics for Max chats."""

    def __init__(self, store: TopicStore, sender: TelegramSender):
        self.store = store
        self.sender = sender
        self.tg_chat_id = int(sender.chat_id)
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure_topic(self, max_chat_id: Any, display_name: str) -> int:
        max_key = str(max_chat_id)
        lock = self._locks.setdefault(max_key, asyncio.Lock())
        async with lock:
            mapping = self.store.get_by_max_chat(self.tg_chat_id, max_key)
            topic_name = self._unique_topic_name(display_name, max_key)

            if mapping:
                if mapping.topic_name != topic_name:
                    try:
                        await self.sender.edit_forum_topic(
                            mapping.message_thread_id,
                            topic_name,
                        )
                    except BadRequest:
                        log.warning(
                            "Could not rename Telegram topic thread=%s for Max chat %s",
                            mapping.message_thread_id,
                            max_key,
                            exc_info=True,
                        )
                    else:
                        self.store.upsert_mapping(
                            self.tg_chat_id,
                            max_key,
                            mapping.message_thread_id,
                            topic_name,
                        )
                return mapping.message_thread_id

            try:
                topic = await self.sender.create_forum_topic(topic_name)
            except BadRequest:
                log.error(
                    "Telegram refused to create a forum topic. Check that TG_CHAT_ID=%s "
                    "is a forum-enabled supergroup and the bot has can_manage_topics.",
                    self.tg_chat_id,
                    exc_info=True,
                )
                raise

            if topic is None:
                raise RuntimeError("Telegram did not return a forum topic")

            self.store.upsert_mapping(
                self.tg_chat_id,
                max_key,
                topic.message_thread_id,
                topic_name,
            )
            log.info(
                "Created Telegram topic %s for Max chat %s (%s)",
                topic.message_thread_id,
                max_key,
                topic_name,
            )
            return int(topic.message_thread_id)

    def forget_max_chat(self, max_chat_id: Any) -> None:
        self.store.delete_by_max_chat(self.tg_chat_id, max_chat_id)

    def _unique_topic_name(self, display_name: str, max_key: str) -> str:
        base = _clean_topic_name(display_name, max_key)
        if not self.store.topic_name_exists(self.tg_chat_id, base, exclude_max_chat_id=max_key):
            return base

        suffix = f" [{max_key}]"
        return _truncate_with_suffix(base, suffix)


def _clean_topic_name(display_name: str, max_key: str) -> str:
    name = re.sub(r"\s+", " ", display_name or "").strip()
    if not name:
        name = f"Max {max_key}"
    return name[:MAX_TOPIC_NAME_LENGTH]


def _truncate_with_suffix(base: str, suffix: str) -> str:
    if len(suffix) >= MAX_TOPIC_NAME_LENGTH:
        return suffix[-MAX_TOPIC_NAME_LENGTH:]
    return f"{base[:MAX_TOPIC_NAME_LENGTH - len(suffix)]}{suffix}"
