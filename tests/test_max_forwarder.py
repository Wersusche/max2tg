from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.max_client import MaxMessage
from app.max_forwarder import forward_max_message
from app.message_store import MessageStore


def _make_sender(*, message_ids: list[int]):
    send_results = [SimpleNamespace(message_id=message_id) for message_id in message_ids]
    return SimpleNamespace(
        chat_id="-100",
        send=AsyncMock(side_effect=send_results),
        send_photo=AsyncMock(),
        send_document=AsyncMock(),
        send_video=AsyncMock(),
        send_voice=AsyncMock(),
        send_sticker=AsyncMock(),
    )


def _make_resolver():
    return SimpleNamespace(
        resolve_user=AsyncMock(return_value="Alice"),
        is_dm=lambda chat_id: True,
        chat_name=lambda chat_id: "Alice",
    )


@pytest.mark.asyncio
async def test_forward_max_message_skips_duplicate_max_message_id(tmp_path):
    sender = _make_sender(message_ids=[7001, 7002])
    resolver = _make_resolver()
    client = SimpleNamespace(download_file=AsyncMock(return_value=None))
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        msg = MaxMessage(chat_id=42, sender_id=7, text="Hello from Max", message_id="max-1")

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )
        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        assert sender.send.await_count == 1
        stored = store.get_by_max_message(max_chat_id=42, max_message_id="max-1")
        assert stored is not None
        assert stored.tg_message_id == 7001
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_message_does_not_deduplicate_empty_max_message_id(tmp_path):
    sender = _make_sender(message_ids=[7001, 7002])
    resolver = _make_resolver()
    client = SimpleNamespace(download_file=AsyncMock(return_value=None))
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        msg = MaxMessage(chat_id=42, sender_id=7, text="Hello from Max", message_id="")

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )
        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        assert sender.send.await_count == 2
        assert store.get_by_max_message(max_chat_id=42, max_message_id="") is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_reply_uses_tg_to_max_mapping_for_native_reply(tmp_path):
    sender = _make_sender(message_ids=[7002])
    resolver = _make_resolver()
    client = SimpleNamespace(download_file=AsyncMock(return_value=None))
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        store.upsert_mapping(
            tg_chat_id=-100,
            max_chat_id=42,
            max_message_id="max-1",
            tg_message_id=7001,
            message_thread_id=55,
            direction="tg_to_max",
            source="telegram",
        )

        msg = MaxMessage(
            chat_id=42,
            sender_id=7,
            text="Reply from Max",
            message_id="max-2",
            link={"type": "REPLY", "mid": "max-1"},
        )

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        assert sender.send.await_count == 1
        assert sender.send.await_args.kwargs["reply_to_message_id"] == 7001
    finally:
        store.close()
