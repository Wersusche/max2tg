"""Tests for app/tg_handler.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.command_store import CommandStore
from app.tg_handler import _on_topic_message, build_tg_app
from app.topic_store import TopicStore


def _make_context(bot_data=None):
    ctx = MagicMock()
    ctx.bot_data = bot_data if bot_data is not None else {}
    return ctx


def _make_topic_update(
    text: str | None = "Hello",
    caption: str | None = None,
    thread_id: int = 10,
    chat_id: int = -100,
    user_name: str = "Alice",
    attachment=None,
    photo=None,
    document=None,
):
    import telegram.constants

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.text = text
    update.message.caption = caption
    update.message.message_thread_id = thread_id
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
        try:
            app = build_tg_app(
                "123456:ABCDEF",
                "-100",
                topic_store,
                command_store=command_store,
            )
        finally:
            topic_store.close()
            command_store.close()

        assert app.bot_data["allowed_chat_id"] == -100
        assert app.bot_data["topic_store"] is topic_store
        assert app.bot_data["command_store"] is command_store
        assert len(app.handlers[0]) == 1
        assert app.handlers[0][0].callback is _on_topic_message


class TestOnTopicMessage:
    def _make_context(self, tmp_path, mapping=True, max_client=None):
        store = TopicStore(str(tmp_path / "topics.sqlite3"))
        if mapping:
            store.upsert_mapping(-100, 42, 10, "Alice")
        if max_client is None:
            max_client = MagicMock()
            max_client.send_message = AsyncMock(return_value={"ok": True})
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": store,
                "max_client": max_client,
            }
        )
        return ctx, store, max_client

    @pytest.mark.asyncio
    async def test_sends_topic_text_to_mapped_max_chat(self, tmp_path):
        ctx, store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(text="Hello", user_name="Bob")

        await _on_topic_message(update, ctx)

        call_args = max_client.send_message.call_args
        assert call_args[0][0] == 42
        assert "Bob" in call_args[0][1]
        assert "Hello" in call_args[0][1]
        assert call_args[0][2] != []
        update.message.reply_text.assert_not_called()
        store.close()

    @pytest.mark.asyncio
    async def test_sends_caption_to_mapped_max_chat(self, tmp_path):
        ctx, store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(text=None, caption="Photo caption", user_name="Carol")

        await _on_topic_message(update, ctx)

        sent_text = max_client.send_message.call_args[0][1]
        assert "Carol" in sent_text
        assert "Photo caption" in sent_text
        store.close()

    @pytest.mark.asyncio
    async def test_photo_without_caption_is_sent_to_max(self, tmp_path):
        ctx, store, max_client = self._make_context(tmp_path)
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
        update.message.reply_text.assert_not_called()
        store.close()

    @pytest.mark.asyncio
    async def test_document_without_caption_is_sent_to_max(self, tmp_path):
        ctx, store, max_client = self._make_context(tmp_path)
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
        update.message.reply_text.assert_not_called()
        store.close()

    @pytest.mark.asyncio
    async def test_unknown_topic_warns_and_does_not_send(self, tmp_path):
        ctx, store, max_client = self._make_context(tmp_path, mapping=False)
        update = _make_topic_update(text="Hello")

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()
        update.message.reply_text.assert_called_once()
        store.close()

    @pytest.mark.asyncio
    async def test_unsupported_media_without_text_warns_and_does_not_send(self, tmp_path):
        ctx, store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(text=None, caption=None, attachment=object())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()
        if hasattr(max_client, "send_photo"):
            max_client.send_photo.assert_not_called()
        update.message.reply_text.assert_called_once()
        store.close()

    @pytest.mark.asyncio
    async def test_message_from_another_chat_is_ignored(self, tmp_path):
        ctx, store, max_client = self._make_context(tmp_path)
        update = _make_topic_update(text="Hello", chat_id=-200)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()
        update.message.reply_text.assert_not_called()
        store.close()


class TestRelayQueueMode:
    @pytest.mark.asyncio
    async def test_topic_message_enqueues_command_when_command_store_present(self, tmp_path):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        topic_store.upsert_mapping(-100, 42, 10, "Alice")
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": topic_store,
                "command_store": command_store,
            }
        )
        update = _make_topic_update(text="Hello", user_name="Bob")

        await _on_topic_message(update, ctx)

        queued = command_store.lease_next()
        assert queued is not None
        assert queued.max_chat_id == "42"
        assert "Bob" in queued.text
        assert "Hello" in queued.text
        topic_store.close()
        command_store.close()

    @pytest.mark.asyncio
    async def test_topic_photo_enqueues_photo_command_when_command_store_present(self, tmp_path):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        topic_store.upsert_mapping(-100, 42, 10, "Alice")
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": topic_store,
                "command_store": command_store,
            }
        )
        tg_file = MagicMock()
        tg_file.file_path = "photos/image.jpg"
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"image-bytes"))
        photo = MagicMock()
        photo.get_file = AsyncMock(return_value=tg_file)
        update = _make_topic_update(text=None, caption=None, attachment=object(), photo=[photo], user_name="Bob")

        await _on_topic_message(update, ctx)

        queued = command_store.lease_next()
        assert queued is not None
        assert queued.max_chat_id == "42"
        assert queued.kind == "photo"
        assert queued.attachment == b"image-bytes"
        assert queued.filename == "photo.jpg"
        assert "Bob" in queued.text
        topic_store.close()
        command_store.close()

    @pytest.mark.asyncio
    async def test_topic_document_enqueues_document_command_when_command_store_present(self, tmp_path):
        topic_store = TopicStore(str(tmp_path / "topics.sqlite3"))
        command_store = CommandStore(str(tmp_path / "commands.sqlite3"))
        topic_store.upsert_mapping(-100, 42, 10, "Alice")
        ctx = _make_context(
            bot_data={
                "allowed_chat_id": -100,
                "topic_store": topic_store,
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
        )

        await _on_topic_message(update, ctx)

        queued = command_store.lease_next()
        assert queued is not None
        assert queued.max_chat_id == "42"
        assert queued.kind == "document"
        assert queued.attachment == b"file-bytes"
        assert queued.filename == "report.pdf"
        assert "Bob" in queued.text
        topic_store.close()
        command_store.close()
