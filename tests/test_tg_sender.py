"""Tests for app/tg_sender.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.tg_sender import TelegramSender


def _make_sender():
    sender = TelegramSender("123456:ABCDEF", "-100")
    sender._bot = MagicMock()
    return sender


@pytest.mark.asyncio
async def test_send_message_passes_message_thread_id():
    sender = _make_sender()
    sender._bot.send_message = AsyncMock(return_value="ok")

    await sender.send("hello", message_thread_id=99)

    assert sender._bot.send_message.await_args.kwargs["message_thread_id"] == 99


@pytest.mark.asyncio
async def test_send_message_passes_reply_to_message_id():
    sender = _make_sender()
    sender._bot.send_message = AsyncMock(return_value="ok")

    await sender.send("hello", reply_to_message_id=77)

    assert sender._bot.send_message.await_args.kwargs["reply_to_message_id"] == 77
    assert sender._bot.send_message.await_args.kwargs["allow_sending_without_reply"] is True


@pytest.mark.asyncio
async def test_send_photo_passes_message_thread_id():
    sender = _make_sender()
    sender._bot.send_photo = AsyncMock(return_value="ok")

    await sender.send_photo(b"data", message_thread_id=99)

    assert sender._bot.send_photo.await_args.kwargs["message_thread_id"] == 99


@pytest.mark.asyncio
async def test_send_document_passes_message_thread_id():
    sender = _make_sender()
    sender._bot.send_document = AsyncMock(return_value="ok")

    await sender.send_document(b"data", message_thread_id=99)

    assert sender._bot.send_document.await_args.kwargs["message_thread_id"] == 99


@pytest.mark.asyncio
async def test_send_document_passes_reply_to_message_id():
    sender = _make_sender()
    sender._bot.send_document = AsyncMock(return_value="ok")

    await sender.send_document(b"data", reply_to_message_id=77)

    assert sender._bot.send_document.await_args.kwargs["reply_to_message_id"] == 77
    assert sender._bot.send_document.await_args.kwargs["allow_sending_without_reply"] is True


@pytest.mark.asyncio
async def test_send_video_passes_message_thread_id():
    sender = _make_sender()
    sender._bot.send_video = AsyncMock(return_value="ok")

    await sender.send_video(b"data", message_thread_id=99)

    assert sender._bot.send_video.await_args.kwargs["message_thread_id"] == 99


@pytest.mark.asyncio
async def test_send_voice_passes_message_thread_id():
    sender = _make_sender()
    sender._bot.send_voice = AsyncMock(return_value="ok")

    await sender.send_voice(b"data", message_thread_id=99)

    assert sender._bot.send_voice.await_args.kwargs["message_thread_id"] == 99


@pytest.mark.asyncio
async def test_send_sticker_passes_message_thread_id():
    sender = _make_sender()
    sender._bot.send_sticker = AsyncMock(return_value="ok")

    await sender.send_sticker(b"data", message_thread_id=99)

    assert sender._bot.send_sticker.await_args.kwargs["message_thread_id"] == 99


@pytest.mark.asyncio
async def test_create_forum_topic_uses_configured_chat_id():
    sender = _make_sender()
    sender._bot.create_forum_topic = AsyncMock(return_value="topic")

    await sender.create_forum_topic("Alice")

    sender._bot.create_forum_topic.assert_awaited_once_with(chat_id="-100", name="Alice")
