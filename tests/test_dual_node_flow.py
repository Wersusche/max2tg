"""Integration-style test for the dual-node Max ↔ Telegram flow."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.command_store import CommandStore
from app.main import _relay_command_loop
from app.max_client import MaxMessage
from app.max_listener import create_max_client
from app.message_store import MessageStore
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
    message_id: int = 9001,
    reply_to_message=None,
):
    import telegram.constants

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.message_id = message_id
    update.message.text = text
    update.message.caption = None
    update.message.message_thread_id = thread_id
    update.message.reply_to_message = reply_to_message
    update.message.effective_attachment = object() if photo else None
    update.message.photo = list(photo or [])
    update.message.document = None
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
    message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
    processor = RelayBatchProcessor(sender, TopicRouter(topic_store, sender), message_store)
    app = create_relay_app(processor, command_store, message_store, "secret")
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
        message_store.close()


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
    message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
    processor = RelayBatchProcessor(sender, TopicRouter(topic_store, sender), message_store)
    app = create_relay_app(processor, command_store, message_store, "secret")
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
        message_store.close()


@pytest.mark.asyncio
async def test_dual_node_max_reply_uses_native_telegram_reply(tmp_path):
    sender = SimpleNamespace(
        chat_id="-100",
        send=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=7001),
                SimpleNamespace(message_id=7002),
            ]
        ),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=8001)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=8101)),
        send_video=AsyncMock(return_value=SimpleNamespace(message_id=8201)),
        send_voice=AsyncMock(return_value=SimpleNamespace(message_id=8301)),
        send_sticker=AsyncMock(return_value=SimpleNamespace(message_id=8401)),
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=55)),
        edit_forum_topic=AsyncMock(return_value=True),
    )
    topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
    command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
    message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
    processor = RelayBatchProcessor(sender, TopicRouter(topic_store, sender), message_store)
    app = create_relay_app(processor, command_store, message_store, "secret")
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

        await max_client._on_message_cb(
            MaxMessage(chat_id=42, sender_id=7, text="Hello from Max", message_id="max-1")
        )
        await max_client._on_message_cb(
            MaxMessage(
                chat_id=42,
                sender_id=7,
                text="Reply from Max",
                message_id="max-2",
                link={"type": "REPLY", "mid": "max-1"},
            )
        )

        assert sender.send.await_count == 2
        second_call = sender.send.await_args_list[1]
        assert "Reply from Max" in second_call.args[0]
        assert second_call.kwargs["reply_to_message_id"] == 7001
        assert second_call.kwargs["message_thread_id"] == 55

        stored = message_store.get_by_max_message(max_chat_id=42, max_message_id="max-2")
        assert stored is not None
        assert stored.tg_message_id == 7002
    finally:
        await relay_client.stop()
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()


@pytest.mark.asyncio
async def test_dual_node_max_reply_falls_back_to_quote_when_mapping_missing(tmp_path):
    sender = SimpleNamespace(
        chat_id="-100",
        send=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=7101),
                SimpleNamespace(message_id=7102),
            ]
        ),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=7201)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=7301)),
        send_video=AsyncMock(return_value=SimpleNamespace(message_id=7401)),
        send_voice=AsyncMock(return_value=SimpleNamespace(message_id=7501)),
        send_sticker=AsyncMock(return_value=SimpleNamespace(message_id=7601)),
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=55)),
        edit_forum_topic=AsyncMock(return_value=True),
    )
    topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
    command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
    message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
    processor = RelayBatchProcessor(sender, TopicRouter(topic_store, sender), message_store)
    app = create_relay_app(processor, command_store, message_store, "secret")
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    relay_client = RelayClient(str(client.make_url("")).rstrip("/"), "secret")
    await relay_client.start()

    try:
        resolver = MagicMock()
        resolver.resolve_user = AsyncMock(side_effect=["Alice", "Alice"])
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

        await max_client._on_message_cb(
            MaxMessage(
                chat_id=42,
                sender_id=7,
                text="Reply from Max",
                message_id="max-2",
                link={
                    "type": "REPLY",
                    "mid": "missing-max-1",
                    "message": {"text": "Original Telegram message", "sender": 7},
                },
            )
        )

        assert sender.send.await_count == 2
        assert sender.send.await_args_list[0].kwargs.get("reply_to_message_id") is None
        assert sender.send.await_args_list[1].kwargs.get("reply_to_message_id") is None
        assert "Original Telegram message" in sender.send.await_args_list[0].args[0]
        assert "Reply from Max" in sender.send.await_args_list[1].args[0]
    finally:
        await relay_client.stop()
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()


@pytest.mark.asyncio
async def test_dual_node_telegram_reply_roundtrip_becomes_native_reply_both_ways(tmp_path):
    sender = SimpleNamespace(
        chat_id="-100",
        send=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=7001),
                SimpleNamespace(message_id=9002),
            ]
        ),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=8001)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=8101)),
        send_video=AsyncMock(return_value=SimpleNamespace(message_id=8201)),
        send_voice=AsyncMock(return_value=SimpleNamespace(message_id=8301)),
        send_sticker=AsyncMock(return_value=SimpleNamespace(message_id=8401)),
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=55)),
        edit_forum_topic=AsyncMock(return_value=True),
    )
    topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
    command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
    message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
    processor = RelayBatchProcessor(sender, TopicRouter(topic_store, sender), message_store)
    app = create_relay_app(processor, command_store, message_store, "secret")
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
            max_to_tg_client = create_max_client(
                max_token="tok",
                max_device_id="dev",
                sender=sender_stub,
                relay_client=relay_client,
            )

        await max_to_tg_client._on_message_cb(
            MaxMessage(chat_id=42, sender_id=7, text="Hello from Max", message_id="max-seed")
        )

        reply_to_message = MagicMock()
        reply_to_message.message_id = 7001
        reply_to_message.text = "Hello from Max"
        reply_to_message.caption = None
        reply_to_message.effective_attachment = None
        reply_to_message.photo = []
        reply_to_message.document = None
        reply_to_message.video = None
        reply_to_message.voice = None
        reply_to_message.sticker = None

        update = _make_topic_update(
            "Reply from Telegram",
            thread_id=55,
            message_id=9001,
            reply_to_message=reply_to_message,
        )
        context = MagicMock()
        context.bot_data = {
            "allowed_chat_id": -100,
            "topic_store": topic_store,
            "message_store": message_store,
            "command_store": command_store,
        }

        await _on_topic_message(update, context)

        bridge_max_client = MagicMock()
        bridge_max_client.send_message = AsyncMock(return_value={"message": {"id": "max-from-tg"}})
        bridge_max_client.extract_sent_message_id = MagicMock(return_value="max-from-tg")

        loop_task = asyncio.create_task(_relay_command_loop(relay_client, bridge_max_client))
        for _ in range(40):
            if bridge_max_client.send_message.await_count == 1:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("relay command loop did not send queued Telegram reply to Max")
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task

        bridge_max_client.send_message.assert_awaited_once_with(
            42,
            "💬 Bob:\nReply from Telegram",
            [{"type": "STRONG", "length": 4, "from": 2}],
            reply_to_max_message_id="max-seed",
        )

        await max_to_tg_client._on_message_cb(
            MaxMessage(
                chat_id=42,
                sender_id=7,
                text="Reply back from Max",
                message_id="max-reply",
                link={"type": "REPLY", "mid": "max-from-tg"},
            )
        )

        assert sender.send.await_count == 2
        last_call = sender.send.await_args_list[-1]
        assert "Reply back from Max" in last_call.args[0]
        assert last_call.kwargs["reply_to_message_id"] == 9001
        assert last_call.kwargs["message_thread_id"] == 55
    finally:
        await relay_client.stop()
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()
