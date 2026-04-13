"""Integration-style test for the dual-node Max ↔ Telegram flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.command_store import CommandStore
from app.max_client import MaxMessage
from app.max_listener import create_max_client
from app.relay_client import RelayClient
from app.relay_server import RelayBatchProcessor, create_relay_app
from app.tg_handler import _on_topic_message
from app.topic_router import TopicRouter
from app.topic_store import TopicStore


def _make_topic_update(
    text: str,
    thread_id: int,
    chat_id: int = -100,
    user_name: str = "Bob",
    photo=None,
):
    import telegram.constants

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.text = text
    update.message.caption = None
    update.message.message_thread_id = thread_id
    update.message.effective_attachment = object() if photo else None
    update.message.photo = list(photo or [])
    update.message.chat = MagicMock()
    update.message.chat.type = telegram.constants.ChatType.SUPERGROUP
    update.message.from_user = MagicMock()
    update.message.from_user.full_name = user_name
    update.message.from_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_dual_node_text_flow_roundtrip(tmp_path):
    sender = SimpleNamespace(
        chat_id="-100",
        send=AsyncMock(return_value="ok"),
        send_photo=AsyncMock(return_value="ok"),
        send_document=AsyncMock(return_value="ok"),
        send_video=AsyncMock(return_value="ok"),
        send_voice=AsyncMock(return_value="ok"),
        send_sticker=AsyncMock(return_value="ok"),
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=55)),
        edit_forum_topic=AsyncMock(return_value=True),
    )
    topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
    command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
    processor = RelayBatchProcessor(sender, TopicRouter(topic_store, sender))
    app = create_relay_app(processor, command_store, "secret")
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    relay_client = RelayClient(str(client.make_url("")).rstrip("/"), "secret")
    await relay_client.start()

    try:
        resolver = MagicMock()
        resolver.resolve_user = AsyncMock(return_value="Alice")
        resolver.is_dm.return_value = True
        resolver.chat_name.return_value = "Alice"
        resolver.load_snapshot.return_value = []

        sender_stub = AsyncMock()
        sender_stub.send = AsyncMock(return_value="ok")

        with patch("app.max_listener.ContactResolver", return_value=resolver):
            max_client = create_max_client(
                max_token="tok",
                max_device_id="dev",
                sender=sender_stub,
                relay_client=relay_client,
            )

        await max_client._on_message_cb(MaxMessage(chat_id=42, sender_id=7, text="Hello from Max"))

        sender.send.assert_awaited_once()
        sent_text = sender.send.await_args.args[0]
        assert "Hello from Max" in sent_text
        assert sender.send.await_args.kwargs["message_thread_id"] == 55

        update = _make_topic_update("Reply from Telegram", thread_id=55)
        context = MagicMock()
        context.bot_data = {
            "allowed_chat_id": -100,
            "topic_store": topic_store,
            "command_store": command_store,
        }

        await _on_topic_message(update, context)

        command = await relay_client.pull_command(timeout_seconds=0)
        assert command is not None
        assert command.max_chat_id == "42"
        assert "Reply from Telegram" in command.text

        await relay_client.ack_command(command.id)
        assert command_store.count() == 0
    finally:
        await relay_client.stop()
        await client.close()
        topic_store.close()
        command_store.close()


@pytest.mark.asyncio
async def test_dual_node_photo_flow_roundtrip(tmp_path):
    sender = SimpleNamespace(
        chat_id="-100",
        send=AsyncMock(return_value="ok"),
        send_photo=AsyncMock(return_value="ok"),
        send_document=AsyncMock(return_value="ok"),
        send_video=AsyncMock(return_value="ok"),
        send_voice=AsyncMock(return_value="ok"),
        send_sticker=AsyncMock(return_value="ok"),
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=55)),
        edit_forum_topic=AsyncMock(return_value=True),
    )
    topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
    command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
    processor = RelayBatchProcessor(sender, TopicRouter(topic_store, sender))
    app = create_relay_app(processor, command_store, "secret")
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    relay_client = RelayClient(str(client.make_url("")).rstrip("/"), "secret")
    await relay_client.start()

    try:
        resolver = MagicMock()
        resolver.resolve_user = AsyncMock(return_value="Alice")
        resolver.is_dm.return_value = True
        resolver.chat_name.return_value = "Alice"
        resolver.load_snapshot.return_value = []

        sender_stub = AsyncMock()
        sender_stub.send = AsyncMock(return_value="ok")

        with patch("app.max_listener.ContactResolver", return_value=resolver):
            max_client = create_max_client(
                max_token="tok",
                max_device_id="dev",
                sender=sender_stub,
                relay_client=relay_client,
            )

        await max_client._on_message_cb(MaxMessage(chat_id=42, sender_id=7, text="Hello from Max"))

        tg_file = MagicMock()
        tg_file.file_path = "photos/image.jpg"
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"image-bytes"))
        photo = MagicMock()
        photo.get_file = AsyncMock(return_value=tg_file)
        update = _make_topic_update("", thread_id=55, photo=[photo])
        context = MagicMock()
        context.bot_data = {
            "allowed_chat_id": -100,
            "topic_store": topic_store,
            "command_store": command_store,
        }

        await _on_topic_message(update, context)

        command = await relay_client.pull_command(timeout_seconds=0)
        assert command is not None
        assert command.max_chat_id == "42"
        assert command.kind == "photo"
        assert command.attachment == b"image-bytes"
        assert "Bob" in command.text

        await relay_client.ack_command(command.id)
        assert command_store.count() == 0
    finally:
        await relay_client.stop()
        await client.close()
        topic_store.close()
        command_store.close()
