import logging
from datetime import datetime

from app.max_client import MaxClient
from app.max_forwarder import _guess_media_kind, _human_size, forward_max_message
from app.message_store import MessageStore
from app.relay_client import RelayClient
from app.resolver import ContactResolver
from app.tg_sender import TelegramSender
from app.topic_router import TopicRouter

log = logging.getLogger(__name__)

__all__ = ["create_max_client", "_guess_media_kind", "_human_size"]


def create_max_client(
    max_token: str,
    max_device_id: str,
    sender: TelegramSender,
    max_chat_ids: str | None = None,
    debug: bool = False,
    reply_enabled: bool = False,
    topic_router: TopicRouter | None = None,
    relay_client: RelayClient | None = None,
    message_store: MessageStore | None = None,
) -> MaxClient:
    del reply_enabled

    client = MaxClient(token=max_token, device_id=max_device_id, debug=debug, chat_ids=max_chat_ids)
    resolver = ContactResolver(client=client)

    first_connect = True
    notification_count = 0
    last_notification_at: datetime | None = None

    def can_notify() -> bool:
        if last_notification_at is None:
            return True
        elapsed = (datetime.now() - last_notification_at).total_seconds()
        if notification_count == 1:
            return elapsed >= 3600
        if notification_count == 2:
            return elapsed >= 10800
        return elapsed >= 86400

    @client.on_ready
    async def handle_ready(snapshot: dict) -> None:
        nonlocal first_connect

        participant_ids = resolver.load_snapshot(snapshot)
        if participant_ids:
            log.info("Batch-resolving %d participants...", len(participant_ids))
            await resolver.resolve_users_batch(participant_ids)
            log.info("Resolved users: %s", resolver.users)
            log.info("Known chats: %s", resolver.chats)
            log.info("Known users: %s", resolver.users)

        if not first_connect:
            await sender.send("✅ <b>Max:</b> соединение восстановлено")
        else:
            await sender.send(f"✅ <b>Max:</b> подключён | чатов: {len(resolver.chats)}")
        first_connect = False

    @client.on_disconnect
    async def handle_disconnect() -> None:
        nonlocal notification_count, last_notification_at

        if not can_notify():
            log.info("Disconnect notification suppressed (throttle)")
            return

        notification_count += 1
        last_notification_at = datetime.now()
        await sender.send("⚠️ <b>Max:</b> соединение потеряно, переподключение...")

    @client.on_message
    async def handle_message(msg) -> None:
        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            topic_router=topic_router,
            relay_client=relay_client,
            message_store=message_store,
        )

    return client
