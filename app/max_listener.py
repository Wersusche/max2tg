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
    ready_count = 0
    outage_started_at: datetime | None = None
    outage_handled = False
    outage_notification_sent = False

    def can_notify(now: datetime) -> bool:
        if last_notification_at is None:
            return True
        elapsed = (now - last_notification_at).total_seconds()
        if notification_count == 1:
            return elapsed >= 3600
        if notification_count == 2:
            return elapsed >= 10800
        return elapsed >= 86400

    @client.on_ready
    async def handle_ready(snapshot: dict) -> None:
        nonlocal first_connect, ready_count, outage_started_at, outage_handled, outage_notification_sent

        now = datetime.now()
        ready_count += 1
        if outage_started_at is None:
            outage_duration = None
            log.info("Max ready event #%d received without an active outage", ready_count)
        else:
            outage_duration = (now - outage_started_at).total_seconds()
            log.info(
                "Max ready event #%d received after outage %.1fs (notification_count=%d)",
                ready_count,
                outage_duration,
                notification_count,
            )

        participant_ids = resolver.load_snapshot(snapshot)

        if first_connect:
            await sender.send("✅ <b>Max:</b> подключён | чатов: %d" % len(resolver.chats))
        elif outage_started_at is not None and outage_notification_sent:
            await sender.send(
                "✅ <b>Max:</b> соединение восстановлено | простой: %.1fс | чатов: %d"
                % (outage_duration or 0, len(resolver.chats))
            )
        elif outage_started_at is not None:
            log.info("Reconnect completed without Telegram notification (disconnect notification was suppressed)")
        else:
            log.info("Ready callback completed without Telegram notification")

        first_connect = False
        outage_started_at = None
        outage_handled = False
        outage_notification_sent = False

        if participant_ids:
            log.info("Batch-resolving %d participants...", len(participant_ids))
            await resolver.resolve_users_batch(participant_ids)
            log.info("Resolved users: %s", resolver.users)
            log.info("Known chats: %s", resolver.chats)
            log.info("Known users: %s", resolver.users)

    @client.on_disconnect
    async def handle_disconnect() -> None:
        nonlocal notification_count, last_notification_at, outage_started_at, outage_handled, outage_notification_sent

        now = datetime.now()
        if outage_started_at is None:
            outage_started_at = now
            log.warning("Max connection transitioned to disconnected state")
        else:
            log.info(
                "Additional disconnect callback while outage is active (elapsed=%.1fs)",
                (now - outage_started_at).total_seconds(),
            )

        if outage_handled:
            log.info("Disconnect notification suppressed (active outage already handled)")
            return

        outage_handled = True
        if not can_notify(now):
            log.info("Disconnect notification suppressed (throttle)")
            return

        notification_count += 1
        last_notification_at = now
        outage_notification_sent = True
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
