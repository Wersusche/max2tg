"""Tests for app/relay_server.py."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from app.command_store import CommandStore
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
    processor = RelayBatchProcessor(sender, TopicRouter(topic_store, sender))
    app = create_relay_app(processor, command_store, "secret")
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client, sender, topic_store, command_store


@pytest.mark.asyncio
async def test_relay_rejects_invalid_secret(tmp_path):
    client, _sender, topic_store, command_store = await _make_client(tmp_path)
    try:
        resp = await client.get("/internal/max-commands/pull", headers={"X-Relay-Secret": "wrong"})
        assert resp.status == 401
    finally:
        await client.close()
        topic_store.close()
        command_store.close()


@pytest.mark.asyncio
async def test_relay_processes_multipart_photo_batch(tmp_path):
    client, sender, topic_store, command_store = await _make_client(tmp_path)
    try:
        batch = TelegramBatch(
            max_chat_id="42",
            topic_name="Alice",
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
    finally:
        await client.close()
        topic_store.close()
        command_store.close()


@pytest.mark.asyncio
async def test_relay_pulls_and_acks_command(tmp_path):
    client, _sender, topic_store, command_store = await _make_client(tmp_path)
    try:
        queued = command_store.enqueue(42, "Hello", [{"type": "STRONG"}])

        pull = await client.get("/internal/max-commands/pull", headers={"X-Relay-Secret": "secret"})
        assert pull.status == 200
        payload = await pull.json()
        assert payload["id"] == queued.id
        assert payload["max_chat_id"] == "42"
        assert payload["text"] == "Hello"

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


@pytest.mark.asyncio
async def test_relay_pulls_photo_command_with_attachment(tmp_path):
    client, _sender, topic_store, command_store = await _make_client(tmp_path)
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
