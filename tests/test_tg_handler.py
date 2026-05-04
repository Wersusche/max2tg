"""Tests for app/tg_handler.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.command_store import CommandStore
from app.message_store import MessageStore
from app.tg_handler import _on_topic_message, build_tg_app
from app.topic_store import TopicStore


def _make_context(bot_data=None):
    ctx = MagicMock()
    ctx.bot_data = bot_data if bot_data is not None else {}
    return ctx


def _make_replied_message(
    *,
    message_id: int = 7001,
    text: str | None = "Original",
    caption: str | None = None,
    attachment=None,
    photo=None,
    document=None,
    thread_id: int | None = None,
    forum_topic_created=None,
):
    replied = MagicMock()
    replied.message_id = message_id
    replied.message_thread_id = thread_id
    replied.text = text
    replied.caption = caption
    replied.effective_attachment = attachment
    replied.photo = list(photo or [])
    replied.document = document
    replied.video = None
    replied.voice = None
    replied.sticker = None
    replied.forum_topic_created = forum_topic_created
    return replied


def _make_topic_update(
    text: str | None = "Hello",
    caption: str | None = None,
    thread_id: int = 10,
    chat_id: int = -100,
    user_name: str = "Alice",
    attachment=None,
    photo=None,
    document=None,
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
    update.message.caption = caption
    update.message.message_thread_id = thread_id
    update.message.reply_to_message = reply_to_message
    update.message.effective_attachment = attachment
    update.message.photo = list(photo or [])
    update.message.document = document
    update.message.chat = MagicMock()
    update.message.chat.type = telegram.constants.ChatType.SUPERGROUP
    update.message.from_user = MagicMock()
    update.message.from_user.full_name = user_name
    update.message.from_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


class TestBuildTgApp:
    def test_registers_real_topic_handler(self, tmp_path):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
        try:
            app = build_tg_app(
                "123456:ABCDEF",
                "-100",
                topic_store,
                command_store=command_store,
                message_store=message_store,
            )
        finally:
            topic_store.close()
            command_store.close()
            message_store.close()

        assert app.bot_data["allowed_chat_id"] == -100
        assert app.bot_data["topic_store"] is topic_store
        assert app.bot_data["command_store"] is command_store
        assert app.bot_data["message_store"] is message_store
        assert len(app.handlers[0]) == 1
        assert app.handlers[0][0].callback is _on_topic_message


class TestOnTopicMessage:
    def _make_context(self, tmp_path, mapping=True, max_client=None):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
        if mapping:
            topic_store.upsert_mapping(-100, 42, 10, "Alice")
        if max_client is None:
            max_client = MagicMock()
            max_client.send_message = AsyncMock(return_value={"ok": True})
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": topic_store,
                "message_store": message_store,
                "max_client": max_client,
            }
        )
        return ctx, topic_store, message_store, max_client

    @pytest.mark.asyncio
    async def test_sends_topic_text_to_mapped_max_chat(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(text="Hello", user_name="Bob")

        await _on_topic_message(update, ctx)

        call_args = max_client.send_message.await_args
        assert call_args.args[0] == 42
        assert "Bob" in call_args.args[1]
        assert "Hello" in call_args.args[1]
        assert call_args.args[2] != []
        assert call_args.kwargs["reply_to_max_message_id"] is None
        update.message.reply_text.assert_not_called()
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_uses_native_reply_when_tg_reply_mapping_exists(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        message_store.upsert_mapping(
            tg_chat_id=-100,
            max_chat_id=42,
            max_message_id="max-1",
            tg_message_id=7001,
            message_thread_id=10,
            direction="max_to_tg",
            source="max",
        )
        update = _make_topic_update(
            text="Reply text",
            user_name="Bob",
            reply_to_message=_make_replied_message(message_id=7001, text="Original"),
        )

        await _on_topic_message(update, ctx)

        call_args = max_client.send_message.await_args
        assert call_args.kwargs["reply_to_max_message_id"] == "max-1"
        assert "↪ Original" not in call_args.args[1]
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_reply_without_mapping_adds_fallback_quote(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(
            text="Reply text",
            user_name="Bob",
            reply_to_message=_make_replied_message(message_id=7001, text="Original Telegram message"),
        )

        await _on_topic_message(update, ctx)

        call_args = max_client.send_message.await_args
        assert call_args.kwargs["reply_to_max_message_id"] is None
        assert "↪ Original Telegram message" in call_args.args[1]
        assert "Reply text" in call_args.args[1]
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_topic_root_service_reply_is_ignored_for_regular_message(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(
            text="Hello",
            user_name="Bob",
            reply_to_message=_make_replied_message(
                message_id=10,
                text=None,
                thread_id=10,
                forum_topic_created=object(),
            ),
        )

        await _on_topic_message(update, ctx)

        call_args = max_client.send_message.await_args
        assert call_args.kwargs["reply_to_max_message_id"] is None
        assert "\u21aa" not in call_args.args[1]
        assert call_args.args[1] == "\U0001F4AC Bob:\nHello"
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_reply_to_message_from_other_max_chat_falls_back_to_quote(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        message_store.upsert_mapping(
            tg_chat_id=-100,
            max_chat_id=99,
            max_message_id="max-foreign",
            tg_message_id=7001,
            message_thread_id=10,
            direction="max_to_tg",
            source="max",
        )
        update = _make_topic_update(
            text="Reply text",
            user_name="Bob",
            reply_to_message=_make_replied_message(message_id=7001, text="Wrong chat"),
        )

        await _on_topic_message(update, ctx)

        call_args = max_client.send_message.await_args
        assert call_args.kwargs["reply_to_max_message_id"] is None
        assert "↪ Wrong chat" in call_args.args[1]
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_sends_caption_to_mapped_max_chat(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(text=None, caption="Photo caption", user_name="Carol")

        await _on_topic_message(update, ctx)

        sent_text = max_client.send_message.await_args.args[1]
        assert "Carol" in sent_text
        assert "Photo caption" in sent_text
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_photo_without_caption_is_sent_to_max(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        max_client.send_photo = AsyncMock(return_value={"ok": True})
        tg_file = MagicMock()
        tg_file.file_path = "photos/image.jpeg"
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"image-bytes"))
        photo = MagicMock()
        photo.get_file = AsyncMock(return_value=tg_file)
        update = _make_topic_update(text=None, caption=None, attachment=object(), photo=[photo], user_name="Carol")

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()
        max_client.send_photo.assert_awaited_once()
        call_args = max_client.send_photo.await_args
        assert call_args.args[0] == 42
        assert call_args.args[1] == b"image-bytes"
        assert "Carol" in call_args.kwargs["caption"]
        assert call_args.kwargs["filename"] == "photo.jpg"
        assert call_args.kwargs["reply_to_max_message_id"] is None
        update.message.reply_text.assert_not_called()
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_photo_reply_uses_native_max_reply_when_mapping_exists(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        max_client.send_photo = AsyncMock(return_value={"ok": True})
        message_store.upsert_mapping(
            tg_chat_id=-100,
            max_chat_id=42,
            max_message_id="max-1",
            tg_message_id=7001,
            message_thread_id=10,
            direction="max_to_tg",
            source="max",
        )
        tg_file = MagicMock()
        tg_file.file_path = "photos/image.jpeg"
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"image-bytes"))
        photo = MagicMock()
        photo.get_file = AsyncMock(return_value=tg_file)
        update = _make_topic_update(
            text=None,
            caption=None,
            attachment=object(),
            photo=[photo],
            user_name="Carol",
            reply_to_message=_make_replied_message(message_id=7001, text="Original"),
        )

        await _on_topic_message(update, ctx)

        assert max_client.send_photo.await_args.kwargs["reply_to_max_message_id"] == "max-1"
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_document_without_caption_is_sent_to_max(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        max_client.send_document = AsyncMock(return_value={"ok": True})
        tg_file = MagicMock()
        tg_file.file_path = "docs/report.pdf"
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"file-bytes"))
        document = MagicMock()
        document.file_name = "report.pdf"
        document.get_file = AsyncMock(return_value=tg_file)
        update = _make_topic_update(
            text=None,
            caption=None,
            attachment=object(),
            document=document,
            user_name="Carol",
        )

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()
        max_client.send_document.assert_awaited_once()
        call_args = max_client.send_document.await_args
        assert call_args.args[0] == 42
        assert call_args.args[1] == b"file-bytes"
        assert "Carol" in call_args.kwargs["caption"]
        assert call_args.kwargs["filename"] == "report.pdf"
        assert call_args.kwargs["reply_to_max_message_id"] is None
        update.message.reply_text.assert_not_called()
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_image_document_stays_document_when_sent_to_max(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        max_client.send_document = AsyncMock(return_value={"ok": True})
        tg_file = MagicMock()
        tg_file.file_path = "photos/image.jpg"
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"image-bytes"))
        document = MagicMock()
        document.file_name = "image.jpg"
        document.get_file = AsyncMock(return_value=tg_file)
        update = _make_topic_update(
            text=None,
            caption=None,
            attachment=object(),
            document=document,
            user_name="Carol",
        )

        await _on_topic_message(update, ctx)

        max_client.send_photo.assert_not_called()
        max_client.send_document.assert_awaited_once()
        call_args = max_client.send_document.await_args
        assert call_args.args[0] == 42
        assert call_args.args[1] == b"image-bytes"
        assert call_args.kwargs["filename"] == "image.jpg"
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_unknown_topic_warns_and_does_not_send(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path, mapping=False)
        update = _make_topic_update(text="Hello")

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()
        update.message.reply_text.assert_called_once()
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_unsupported_media_without_text_warns_and_does_not_send(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(text=None, caption=None, attachment=object())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()
        if hasattr(max_client, "send_photo"):
            max_client.send_photo.assert_not_called()
        update.message.reply_text.assert_called_once()
        topic_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_message_from_another_chat_is_ignored(self, tmp_path):
        ctx, topic_store, message_store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(text="Hello", chat_id=-200)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()
        update.message.reply_text.assert_not_called()
        topic_store.close()
        message_store.close()


class TestRelayQueueMode:
    @pytest.mark.asyncio
    async def test_topic_message_enqueues_command_when_command_store_present(self, tmp_path, caplog):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
        topic_store.upsert_mapping(-100, 42, 10, "Alice")
        message_store.upsert_mapping(
            tg_chat_id=-100,
            max_chat_id=42,
            max_message_id="max-1",
            tg_message_id=7001,
            message_thread_id=10,
            direction="max_to_tg",
            source="max",
        )
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": topic_store,
                "message_store": message_store,
                "command_store": command_store,
            }
        )
        update = _make_topic_update(
            text="Hello",
            user_name="Bob",
            message_id=9001,
            reply_to_message=_make_replied_message(message_id=7001, text="Original"),
        )

        caplog.set_level("INFO")
        await _on_topic_message(update, ctx)

        queued = command_store.lease_next()
        assert queued is not None
        assert queued.max_chat_id == "42"
        assert "Bob" in queued.text
        assert "Hello" in queued.text
        assert queued.reply_to_max_message_id == "max-1"
        assert queued.tg_chat_id == -100
        assert queued.tg_message_id == 9001
        assert queued.message_thread_id == 10
        assert "Queued Telegram->Max command id=" in caplog.text
        topic_store.close()
        command_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_topic_message_enqueues_command_for_configured_profile(self, tmp_path):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
        topic_store.upsert_mapping(-100, 42, 10, "Default Alice")
        topic_store.upsert_mapping(-100, 42, 20, "Beta Alice", profile_id="beta")
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "profile_id": "beta",
                "topic_store": topic_store,
                "message_store": message_store,
                "command_store": command_store,
            }
        )
        update = _make_topic_update(text="Hello beta", user_name="Bob", thread_id=20)

        await _on_topic_message(update, ctx)

        assert command_store.lease_next(profile_id="default") is None
        queued = command_store.lease_next(profile_id="beta")
        assert queued is not None
        assert queued.profile_id == "beta"
        assert queued.max_chat_id == "42"
        assert "Hello beta" in queued.text
        topic_store.close()
        command_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_topic_message_without_reply_mapping_enqueues_fallback_quote(self, tmp_path):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
        topic_store.upsert_mapping(-100, 42, 10, "Alice")
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": topic_store,
                "message_store": message_store,
                "command_store": command_store,
            }
        )
        update = _make_topic_update(
            text="Hello",
            user_name="Bob",
            message_id=9001,
            reply_to_message=_make_replied_message(message_id=7001, text="Original"),
        )

        await _on_topic_message(update, ctx)

        queued = command_store.lease_next()
        assert queued is not None
        assert queued.reply_to_max_message_id is None
        assert "↪ Original" in queued.text
        topic_store.close()
        command_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_topic_root_service_reply_is_ignored_when_enqueuing_command(self, tmp_path):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
        topic_store.upsert_mapping(-100, 42, 10, "Alice")
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": topic_store,
                "message_store": message_store,
                "command_store": command_store,
            }
        )
        update = _make_topic_update(
            text="Hello",
            user_name="Bob",
            message_id=9001,
            reply_to_message=_make_replied_message(
                message_id=10,
                text=None,
                thread_id=10,
                forum_topic_created=object(),
            ),
        )

        await _on_topic_message(update, ctx)

        queued = command_store.lease_next()
        assert queued is not None
        assert queued.reply_to_max_message_id is None
        assert "\u21aa" not in queued.text
        assert queued.text == "\U0001F4AC Bob:\nHello"
        topic_store.close()
        command_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_topic_photo_enqueues_photo_command_when_command_store_present(self, tmp_path):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
        topic_store.upsert_mapping(-100, 42, 10, "Alice")
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": topic_store,
                "message_store": message_store,
                "command_store": command_store,
            }
        )
        tg_file = MagicMock()
        tg_file.file_path = "photos/image.jpg"
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"image-bytes"))
        photo = MagicMock()
        photo.get_file = AsyncMock(return_value=tg_file)
        update = _make_topic_update(
            text=None,
            caption=None,
            attachment=object(),
            photo=[photo],
            user_name="Bob",
            message_id=9002,
        )

        await _on_topic_message(update, ctx)

        queued = command_store.lease_next()
        assert queued is not None
        assert queued.max_chat_id == "42"
        assert queued.kind == "photo"
        assert queued.attachment == b"image-bytes"
        assert queued.filename == "photo.jpg"
        assert "Bob" in queued.text
        assert queued.tg_message_id == 9002
        topic_store.close()
        command_store.close()
        message_store.close()

    @pytest.mark.asyncio
    async def test_topic_document_enqueues_document_command_when_command_store_present(self, tmp_path):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        message_store = MessageStore(str(tmp_path / "messages.sqlite3"))
        topic_store.upsert_mapping(-100, 42, 10, "Alice")
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": topic_store,
                "message_store": message_store,
                "command_store": command_store,
            }
        )
        tg_file = MagicMock()
        tg_file.file_path = "docs/report.pdf"
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"file-bytes"))
        document = MagicMock()
        document.file_name = "report.pdf"
        document.get_file = AsyncMock(return_value=tg_file)
        update = _make_topic_update(
            text=None,
            caption=None,
            attachment=object(),
            document=document,
            user_name="Bob",
            message_id=9003,
        )

        await _on_topic_message(update, ctx)

        queued = command_store.lease_next()
        assert queued is not None
        assert queued.max_chat_id == "42"
        assert queued.kind == "document"
        assert queued.attachment == b"file-bytes"
        assert queued.filename == "report.pdf"
        assert "Bob" in queued.text
        assert queued.tg_message_id == 9003
        topic_store.close()
        command_store.close()
        message_store.close()
