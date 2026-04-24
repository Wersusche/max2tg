from __future__ import annotations

import logging

from app.message_store import MessageStore
from app.reaction_sync import ReactionSyncEvent
from app.relay_client import RelayClient

log = logging.getLogger(__name__)


async def forward_max_reaction(
    event: ReactionSyncEvent,
    *,
    sender,
    relay_client: RelayClient | None = None,
    message_store: MessageStore | None = None,
) -> None:
    if relay_client is not None:
        await relay_client.apply_telegram_reaction(event)
        return

    if message_store is None:
        log.info(
            "Skipping Max reaction target=%s/%s because no message_store is configured",
            event.target_chat_id,
            event.target_message_id,
        )
        return

    mapping = message_store.get_by_max_message(
        max_chat_id=event.target_chat_id,
        max_message_id=event.target_message_id,
        direction=None,
    )
    if mapping is None:
        log.info(
            "Skipping Max reaction for unmapped message chat=%s message_id=%s",
            event.target_chat_id,
            event.target_message_id,
        )
        return

    await sender.set_message_reaction(
        message_id=mapping.tg_message_id,
        reaction_type=event.reaction_type,
        reaction_value=event.reaction_value,
        action=event.action,
        raise_bad_request=True,
    )
