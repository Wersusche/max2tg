"""Tests for app/max_listener.py — pure helper functions."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import BadRequest

from app.max_client import MaxMessage
from app.max_listener import _human_size, _guess_media_kind, create_max_client


# ---------------------------------------------------------------------------
# _human_size
# ---------------------------------------------------------------------------

class TestHumanSize:
    """Tests for the _human_size byte-formatter."""

    # Byte range (< 1024)
    def test_zero_bytes(self):
        assert _human_size(0) == "0 Б"

    def test_single_byte(self):
        assert _human_size(1) == "1 Б"

    def test_max_bytes(self):
        assert _human_size(1023) == "1023 Б"

    # Kilobyte range (1024 – 1024²-1)
    def test_exact_one_kb(self):
        assert _human_size(1024) == "1.0 КБ"

    def test_fractional_kb(self):
        assert _human_size(1536) == "1.5 КБ"

    def test_large_kb(self):
        assert _human_size(1023 * 1024) == "1023.0 КБ"

    # Megabyte range
    def test_exact_one_mb(self):
        assert _human_size(1024 ** 2) == "1.0 МБ"

    def test_fractional_mb(self):
        assert _human_size(int(2.5 * 1024 ** 2)) == "2.5 МБ"

    def test_large_mb(self):
        assert _human_size(500 * 1024 ** 2) == "500.0 МБ"

    # Gigabyte range
    def test_exact_one_gb(self):
        assert _human_size(1024 ** 3) == "1.0 ГБ"

    def test_fractional_gb(self):
        assert _human_size(int(1.5 * 1024 ** 3)) == "1.5 ГБ"

    # Terabyte range (overflow past ГБ loop)
    def test_terabyte(self):
        result = _human_size(1024 ** 4)
        assert "ТБ" in result

    def test_large_terabyte(self):
        result = _human_size(5 * 1024 ** 4)
        assert result.startswith("5")
        assert "ТБ" in result

    # Return type
    def test_returns_string(self):
        assert isinstance(_human_size(42), str)


# ---------------------------------------------------------------------------
# _guess_media_kind
# ---------------------------------------------------------------------------

class TestGuessMediaKind:
    """Tests for the filename-to-media-kind classifier."""

    # Photo extensions
    def test_jpg_is_photo(self):
        assert _guess_media_kind("image.jpg") == "photo"

    def test_jpeg_is_photo(self):
        assert _guess_media_kind("photo.jpeg") == "photo"

    def test_png_is_photo(self):
        assert _guess_media_kind("screenshot.png") == "photo"

    def test_gif_is_photo(self):
        assert _guess_media_kind("anim.gif") == "photo"

    def test_webp_is_photo(self):
        assert _guess_media_kind("sticker.webp") == "photo"

    def test_bmp_is_photo(self):
        assert _guess_media_kind("old.bmp") == "photo"

    # Video extensions
    def test_mp4_is_video(self):
        assert _guess_media_kind("clip.mp4") == "video"

    def test_mov_is_video(self):
        assert _guess_media_kind("recording.mov") == "video"

    def test_avi_is_video(self):
        assert _guess_media_kind("video.avi") == "video"

    def test_mkv_is_video(self):
        assert _guess_media_kind("movie.mkv") == "video"

    def test_webm_is_video(self):
        assert _guess_media_kind("stream.webm") == "video"

    # Document / unknown extensions
    def test_pdf_is_document(self):
        assert _guess_media_kind("report.pdf") == "document"

    def test_zip_is_document(self):
        assert _guess_media_kind("archive.zip") == "document"

    def test_docx_is_document(self):
        assert _guess_media_kind("contract.docx") == "document"

    def test_txt_is_document(self):
        assert _guess_media_kind("notes.txt") == "document"

    def test_no_extension_is_document(self):
        assert _guess_media_kind("README") == "document"

    def test_empty_string_is_document(self):
        assert _guess_media_kind("") == "document"

    # Case-insensitivity
    def test_uppercase_jpg_is_photo(self):
        assert _guess_media_kind("PHOTO.JPG") == "photo"

    def test_mixed_case_mp4_is_video(self):
        assert _guess_media_kind("Video.MP4") == "video"

    def test_mixed_case_png_is_photo(self):
        assert _guess_media_kind("Image.PNG") == "photo"

    # Paths with directories
    def test_full_path_jpg(self):
        assert _guess_media_kind("/tmp/uploads/img.jpg") == "photo"

    def test_full_path_mp4(self):
        assert _guess_media_kind("/home/user/videos/clip.mp4") == "video"

    # Extension appearing in the middle of filename should not trigger false match
    def test_mp4_in_name_not_extension_is_document(self):
        assert _guess_media_kind("mp4_notes.txt") == "document"


# ---------------------------------------------------------------------------
# Topic routing in create_max_client
# ---------------------------------------------------------------------------

class TestTopicRouting:
    def _make_client(self, is_dm=True):
        sender = AsyncMock()
        sender.send = AsyncMock(return_value="ok")
        sender.send_photo = AsyncMock(return_value="ok")

        router = MagicMock()
        router.ensure_topic = AsyncMock(return_value=77)

        resolver = MagicMock()
        resolver.resolve_user = AsyncMock(return_value="Alice")
        resolver.is_dm.return_value = is_dm
        resolver.chat_name.return_value = "Max Group"
        resolver.load_snapshot.return_value = []

        with patch("app.max_listener.ContactResolver", return_value=resolver):
            client = create_max_client(
                max_token="tok",
                max_device_id="dev",
                sender=sender,
                topic_router=router,
            )

        return client, sender, router, resolver

    @pytest.mark.asyncio
    async def test_dm_uses_sender_name_as_topic(self):
        client, sender, router, _resolver = self._make_client(is_dm=True)
        msg = MaxMessage(chat_id=100, sender_id=2, text="Hi")

        await client._on_message_cb(msg)

        router.ensure_topic.assert_awaited_once_with(100, "Alice")
        assert sender.send.call_args.kwargs["message_thread_id"] == 77
        assert sender.send.call_args.kwargs["raise_bad_request"] is True

    @pytest.mark.asyncio
    async def test_group_uses_chat_name_as_topic(self):
        client, _sender, router, _resolver = self._make_client(is_dm=False)
        msg = MaxMessage(chat_id=200, sender_id=2, text="Hi")

        await client._on_message_cb(msg)

        router.ensure_topic.assert_awaited_once_with(200, "Max Group")

    @pytest.mark.asyncio
    async def test_attachment_goes_to_topic_thread(self):
        client, sender, _router, _resolver = self._make_client(is_dm=False)
        client.download_file = AsyncMock(return_value=b"image")
        msg = MaxMessage(
            chat_id=200,
            sender_id=2,
            attaches=[{"_type": "PHOTO", "baseUrl": "https://example.com/a.jpg"}],
        )

        await client._on_message_cb(msg)

        sender.send_photo.assert_awaited_once()
        assert sender.send_photo.call_args.kwargs["message_thread_id"] == 77

    @pytest.mark.asyncio
    async def test_stale_topic_mapping_is_recreated_once(self):
        client, sender, router, _resolver = self._make_client(is_dm=True)
        router.ensure_topic = AsyncMock(side_effect=[77, 88])
        sender.send = AsyncMock(side_effect=[BadRequest("Message thread not found"), "ok"])
        msg = MaxMessage(chat_id=100, sender_id=2, text="Hi")

        await client._on_message_cb(msg)

        router.forget_max_chat.assert_called_once_with(100)
        assert router.ensure_topic.await_count == 2
        assert sender.send.await_args_list[1].kwargs["message_thread_id"] == 88


class TestRelayBatching:
    def _make_client(self, is_dm=True):
        sender = AsyncMock()
        sender.send = AsyncMock(return_value="ok")

        relay_client = AsyncMock()
        relay_client.send_batch = AsyncMock(return_value=None)

        resolver = MagicMock()
        resolver.resolve_user = AsyncMock(return_value="Alice")
        resolver.is_dm.return_value = is_dm
        resolver.chat_name.return_value = "Max Group"
        resolver.load_snapshot.return_value = []

        with patch("app.max_listener.ContactResolver", return_value=resolver):
            client = create_max_client(
                max_token="tok",
                max_device_id="dev",
                sender=sender,
                relay_client=relay_client,
            )

        return client, relay_client

    @pytest.mark.asyncio
    async def test_text_message_is_batched_for_relay(self):
        client, relay_client = self._make_client(is_dm=True)
        msg = MaxMessage(chat_id=42, sender_id=7, text="Hello")

        await client._on_message_cb(msg)

        relay_client.send_batch.assert_awaited_once()
        batch = relay_client.send_batch.await_args.args[0]
        attachments = relay_client.send_batch.await_args.args[1]
        assert batch.max_chat_id == "42"
        assert batch.topic_name == "Alice"
        assert [op.kind for op in batch.operations] == ["text"]
        assert "Hello" in batch.operations[0].text
        assert attachments == {}

    @pytest.mark.asyncio
    async def test_photo_attachment_is_batched_with_uploaded_part(self):
        client, relay_client = self._make_client(is_dm=False)
        client.download_file = AsyncMock(return_value=b"image-bytes")
        msg = MaxMessage(
            chat_id=77,
            sender_id=2,
            attaches=[{"_type": "PHOTO", "baseUrl": "https://example.com/pic.jpg"}],
        )

        await client._on_message_cb(msg)

        batch = relay_client.send_batch.await_args.args[0]
        attachments = relay_client.send_batch.await_args.args[1]
        assert batch.topic_name == "Max Group"
        assert [op.kind for op in batch.operations] == ["photo"]
        assert batch.operations[0].attachment_field == "file0"
        assert attachments["file0"][1] == b"image-bytes"
