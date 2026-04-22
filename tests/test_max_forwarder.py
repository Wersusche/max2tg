from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.max_client import DownloadResult, MaxMessage
from app.max_forwarder import forward_max_message
from app.message_store import MessageStore
from app.resolver import ContactResolver


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


def _make_media_sender(*, send_message_id: int = 7001, voice_message_id: int = 7101):
    return SimpleNamespace(
        chat_id="-100",
        send=AsyncMock(return_value=SimpleNamespace(message_id=send_message_id)),
        send_photo=AsyncMock(),
        send_document=AsyncMock(),
        send_video=AsyncMock(),
        send_voice=AsyncMock(return_value=SimpleNamespace(message_id=voice_message_id)),
        send_sticker=AsyncMock(),
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
async def test_forward_max_audio_sends_voice_when_download_retry_succeeds(tmp_path):
    sender = _make_media_sender()
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file_result=AsyncMock(return_value=DownloadResult(data=b"voice-bytes", status=200, used_authorization=False)),
        fetch_message=AsyncMock(),
        download_file=AsyncMock(return_value=None),
    )
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        msg = MaxMessage(
            chat_id=42,
            sender_id=7,
            text="",
            message_id="max-audio-1",
            attaches=[
                {
                    "_type": "AUDIO",
                    "url": "https://media.okcdn.ru/stale.ogg?expires=1&sig=abc",
                    "token": "audio-token",
                    "audioId": 77,
                }
            ],
        )

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        client.download_file_result.assert_awaited_once_with("https://media.okcdn.ru/stale.ogg?expires=1&sig=abc")
        client.fetch_message.assert_not_awaited()
        assert sender.send_voice.await_count == 1
        assert sender.send_voice.await_args.args[0] == b"voice-bytes"
        assert sender.send.await_count == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_forward_max_audio_falls_back_to_text_when_download_retry_fails(tmp_path):
    sender = _make_media_sender(send_message_id=7201)
    resolver = _make_resolver()
    client = SimpleNamespace(
        download_file_result=AsyncMock(return_value=DownloadResult(status=403, used_authorization=False)),
        fetch_message=AsyncMock(),
        download_file=AsyncMock(return_value=None),
    )
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        msg = MaxMessage(
            chat_id=42,
            sender_id=7,
            text="",
            message_id="max-audio-3",
            attaches=[
                {
                    "_type": "AUDIO",
                    "url": "https://media.okcdn.ru/stale-fail.ogg?expires=1&sig=abc",
                    "token": "audio-token",
                    "audioId": 99,
                }
            ],
        )

        await forward_max_message(
            msg,
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        client.download_file_result.assert_awaited_once_with("https://media.okcdn.ru/stale-fail.ogg?expires=1&sig=abc")
        client.fetch_message.assert_not_awaited()
        assert sender.send_voice.await_count == 0
        assert sender.send.await_count == 1
        assert "[аудио]" in sender.send.await_args.args[0]
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


@pytest.mark.asyncio
async def test_forward_max_reply_accepts_message_id_reply_link(tmp_path):
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
            link={"type": "REPLY", "messageId": "max-1"},
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


@pytest.mark.asyncio
async def test_forward_max_message_uses_display_name_after_string_contact_resolution(tmp_path):
    sender = _make_sender(message_ids=[7001])
    resolver = ContactResolver()
    resolver._parse_contacts_response({"contacts": [{"id": "7", "firstName": "Alice"}]})
    resolver.chat_types[42] = "DIALOG"
    client = SimpleNamespace(download_file=AsyncMock(return_value=None))
    store = MessageStore(str(tmp_path / "messages.sqlite3"))

    try:
        await forward_max_message(
            MaxMessage(chat_id=42, sender_id=7, text="Hello from Max", message_id="max-1"),
            client=client,
            sender=sender,
            resolver=resolver,
            message_store=store,
        )

        payload = sender.send.await_args.args[0]
        assert "<b>Alice</b>" in payload
        assert "<b>7</b>" not in payload
    finally:
        store.close()
