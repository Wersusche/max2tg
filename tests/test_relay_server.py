"""Tests for app/relay_server.py."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from app.command_store import CommandStore
from app.message_store import MessageStore
from app.relay_models import RelayOperation, TelegramBatch
from app.relay_server import RelayBatchProcessor, create_relay_app
from app.topic_router import TopicRouter
from app.topic_store import TopicStore


def _make_sender():
    return SimpleNamespace(
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


async def _make_client(tmp_path):
    sender = _make_sender()
    topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
    command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
    message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
    processor = RelayBatchProcessor(sender, TopicRouter(topic_store, sender), message_store)
    app = create_relay_app(processor, command_store, message_store, "secret")
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client, sender, topic_store, command_store, message_store


@pytest.mark.asyncio
async def test_relay_rejects_invalid_secret(tmp_path):
    client, _sender, topic_store, command_store, message_store = await _make_client(tmp_path)
    try:
        resp = await client.get("/internal/max-commands/pull", headers={"X-Relay-Secret": "wrong"})
        assert resp.status == 401
    finally:
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()


@pytest.mark.asyncio
async def test_relay_processes_multipart_photo_batch(tmp_path):
    client, sender, topic_store, command_store, message_store = await _make_client(tmp_path)
    try:
        batch = TelegramBatch(
            max_chat_id="42",
            topic_name="Alice",
            max_message_id="max-1",
            operations=[
                RelayOperation(
                    kind="photo",
                    text="caption",
                    filename="pic.jpg",
                    attachment_field="file0",
                )
            ],
        )
        form = FormData()
        form.add_field("batch", batch.to_json(), content_type="application/json")
        form.add_field("file0", b"image-bytes", filename="pic.jpg", content_type="application/octet-stream")

        resp = await client.post(
            "/internal/telegram-batch",
            data=form,
            headers={"X-Relay-Secret": "secret"},
        )
        assert resp.status == 200
        sender.send_photo.assert_awaited_once()
        assert sender.send_photo.await_args.kwargs["message_thread_id"] == 55
        assert topic_store.get_by_max_chat(-100, 42).message_thread_id == 55
        assert message_store.get_by_max_message(max_chat_id=42, max_message_id="max-1") is None
    finally:
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()


@pytest.mark.asyncio
async def test_relay_pulls_and_acks_command(tmp_path):
    client, _sender, topic_store, command_store, message_store = await _make_client(tmp_path)
    try:
        queued = command_store.enqueue(
            42,
            "Hello",
            [{"type": "STRONG"}],
            reply_to_max_message_id="max-1",
            tg_chat_id=-100,
            tg_message_id=7001,
            message_thread_id=55,
        )

        pull = await client.get("/internal/max-commands/pull", headers={"X-Relay-Secret": "secret"})
        assert pull.status == 200
        payload = await pull.json()
        assert payload["id"] == queued.id
        assert payload["max_chat_id"] == "42"
        assert payload["text"] == "Hello"
        assert payload["reply_to_max_message_id"] == "max-1"
        assert payload["tg_chat_id"] == -100
        assert payload["tg_message_id"] == 7001
        assert payload["message_thread_id"] == 55

        ack = await client.post(
            f"/internal/max-commands/{queued.id}/ack",
            headers={"X-Relay-Secret": "secret"},
        )
        assert ack.status == 200
        assert command_store.count() == 0
    finally:
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()


@pytest.mark.asyncio
async def test_relay_pulls_photo_command_with_attachment(tmp_path):
    client, _sender, topic_store, command_store, message_store = await _make_client(tmp_path)
    try:
        queued = command_store.enqueue_photo(42, b"image-bytes", caption="Photo", filename="pic.jpg")

        pull = await client.get("/internal/max-commands/pull", headers={"X-Relay-Secret": "secret"})
        assert pull.status == 200
        payload = await pull.json()
        assert payload["id"] == queued.id
        assert payload["kind"] == "photo"
        assert payload["filename"] == "pic.jpg"
        assert payload["attachment_b64"]
    finally:
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()


@pytest.mark.asyncio
async def test_relay_stores_message_mapping_and_exposes_lookup(tmp_path):
    client, sender, topic_store, command_store, message_store = await _make_client(tmp_path)
    sender.send = AsyncMock(return_value=SimpleNamespace(message_id=7001))
    try:
        batch = TelegramBatch(
            max_chat_id="42",
            topic_name="Alice",
            max_message_id="max-42",
            operations=[RelayOperation(kind="text", text="Hello from Max")],
        )

        resp = await client.post(
            "/internal/telegram-batch",
            json=batch.to_dict(),
            headers={"X-Relay-Secret": "secret"},
        )
        assert resp.status == 200

        lookup = await client.get(
            "/internal/message-mappings/lookup",
            params={"max_chat_id": "42", "max_message_id": "max-42"},
            headers={"X-Relay-Secret": "secret"},
        )
        assert lookup.status == 200
        payload = await lookup.json()
        assert payload["tg_message_id"] == 7001
        assert payload["message_thread_id"] == 55
    finally:
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()


@pytest.mark.asyncio
async def test_relay_upserts_tg_to_max_mapping_and_lookup_finds_it(tmp_path):
    client, _sender, topic_store, command_store, message_store = await _make_client(tmp_path)
    try:
        upsert = await client.post(
            "/internal/message-mappings/upsert",
            json={
                "tg_chat_id": -100,
                "tg_message_id": 7002,
                "max_chat_id": "42",
                "max_message_id": "max-2",
                "message_thread_id": 56,
                "direction": "tg_to_max",
                "source": "telegram",
            },
            headers={"X-Relay-Secret": "secret"},
        )
        assert upsert.status == 200

        lookup = await client.get(
            "/internal/message-mappings/lookup",
            params={"max_chat_id": "42", "max_message_id": "max-2"},
            headers={"X-Relay-Secret": "secret"},
        )
        assert lookup.status == 200
        payload = await lookup.json()
        assert payload["tg_chat_id"] == -100
        assert payload["tg_message_id"] == 7002
        assert payload["message_thread_id"] == 56
    finally:
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()


@pytest.mark.asyncio
async def test_relay_skips_duplicate_max_message_id(tmp_path):
    client, sender, topic_store, command_store, message_store = await _make_client(tmp_path)
    sender.send = AsyncMock(return_value=SimpleNamespace(message_id=7001))
    try:
        batch = TelegramBatch(
            max_chat_id="42",
            topic_name="Alice",
            max_message_id="max-42",
            operations=[RelayOperation(kind="text", text="Hello from Max")],
        )

        first = await client.post(
            "/internal/telegram-batch",
            json=batch.to_dict(),
            headers={"X-Relay-Secret": "secret"},
        )
        second = await client.post(
            "/internal/telegram-batch",
            json=batch.to_dict(),
            headers={"X-Relay-Secret": "secret"},
        )

        assert first.status == 200
        assert second.status == 200
        assert sender.send.await_count == 1

        stored = message_store.get_by_max_message(max_chat_id=42, max_message_id="max-42")
        assert stored is not None
        assert stored.tg_message_id == 7001
    finally:
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()


@pytest.mark.asyncio
async def test_relay_does_not_deduplicate_empty_max_message_id(tmp_path):
    client, sender, topic_store, command_store, message_store = await _make_client(tmp_path)
    sender.send = AsyncMock(
        side_effect=[
            SimpleNamespace(message_id=7001),
            SimpleNamespace(message_id=7002),
        ]
    )
    try:
        batch = TelegramBatch(
            max_chat_id="42",
            topic_name="Alice",
            max_message_id="",
            operations=[RelayOperation(kind="text", text="Hello from Max")],
        )

        first = await client.post(
            "/internal/telegram-batch",
            json=batch.to_dict(),
            headers={"X-Relay-Secret": "secret"},
        )
        second = await client.post(
            "/internal/telegram-batch",
            json=batch.to_dict(),
            headers={"X-Relay-Secret": "secret"},
        )

        assert first.status == 200
        assert second.status == 200
        assert sender.send.await_count == 2
        assert message_store.get_by_max_message(max_chat_id=42, max_message_id="") is None
    finally:
        await client.close()
        topic_store.close()
        command_store.close()
        message_store.close()
