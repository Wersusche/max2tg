"""Tests for app/max_client.py — OpCode enum and _parse_message."""

import asyncio

import pytest
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from app.max_client import DownloadResult, MaxClient, MaxMessage, OpCode, _HTTP_HEADERS


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        payload: dict | None = None,
        text: str = "",
        body: bytes = b"",
        headers: dict | None = None,
    ):
        self.status = status
        self._payload = payload or {}
        self._text = text
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self, errors=None):
        return self._text

    async def read(self):
        return self._body


class _FakeSession:
    def __init__(self, response: _FakeResponse | list[_FakeResponse]):
        if isinstance(response, list):
            self.responses = list(response)
        else:
            self.responses = [response]
        self.closed = False
        self.calls: list[tuple[str, dict]] = []

    def _next_response(self) -> _FakeResponse:
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._next_response()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._next_response()

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# OpCode enum
# ---------------------------------------------------------------------------

class TestOpCode:
    """Validate that all expected opcodes exist with their correct integer values."""

    def test_heartbeat_ping(self):
        assert OpCode.HEARTBEAT_PING == 1

    def test_handshake(self):
        assert OpCode.HANDSHAKE == 6

    def test_auth_snapshot(self):
        assert OpCode.AUTH_SNAPSHOT == 19

    def test_logout(self):
        assert OpCode.LOGOUT == 20

    def test_sticker_store(self):
        assert OpCode.STICKER_STORE == 27

    def test_asset_get(self):
        assert OpCode.ASSET_GET == 28

    def test_favorite_sticker(self):
        assert OpCode.FAVORITE_STICKER == 29

    def test_contact_get(self):
        assert OpCode.CONTACT_GET == 32

    def test_contact_presence(self):
        assert OpCode.CONTACT_PRESENCE == 35

    def test_chat_get(self):
        assert OpCode.CHAT_GET == 48

    def test_send_message(self):
        assert OpCode.SEND_MESSAGE == 64

    def test_edit_message(self):
        assert OpCode.EDIT_MESSAGE == 67

    def test_video_download_url(self):
        assert OpCode.VIDEO_DOWNLOAD_URL == 83

    def test_file_download_url(self):
        assert OpCode.FILE_DOWNLOAD_URL == 88

    def test_dispatch(self):
        assert OpCode.DISPATCH == 128

    def test_all_values_are_ints(self):
        for member in OpCode:
            assert isinstance(member.value, int), f"{member.name} is not an int"

    def test_no_duplicate_values(self):
        values = [m.value for m in OpCode]
        assert len(values) == len(set(values)), "Duplicate opcode values found"

    def test_can_be_used_as_int(self):
        # IntEnum should compare equal to a plain int
        assert OpCode.HANDSHAKE == 6
        assert 6 == OpCode.HANDSHAKE


# ---------------------------------------------------------------------------
# MaxMessage dataclass defaults
# ---------------------------------------------------------------------------

class TestMaxMessageDefaults:
    def test_default_text_is_empty_string(self):
        msg = MaxMessage()
        assert msg.text == ""

    def test_default_is_self_is_false(self):
        msg = MaxMessage()
        assert msg.is_self is False

    def test_default_attaches_is_empty_list(self):
        msg = MaxMessage()
        assert msg.attaches == []

    def test_default_link_is_empty_dict(self):
        msg = MaxMessage()
        assert msg.link == {}

    def test_default_raw_is_empty_dict(self):
        msg = MaxMessage()
        assert msg.raw == {}

    def test_attaches_are_independent_instances(self):
        # mutable default via field(default_factory=...) must not be shared
        m1 = MaxMessage()
        m2 = MaxMessage()
        m1.attaches.append("x")
        assert m2.attaches == []


# ---------------------------------------------------------------------------
# MaxClient._parse_message
# ---------------------------------------------------------------------------

def _make_client() -> MaxClient:
    return MaxClient(token="tok", device_id="dev")


class TestParseMessage:
    """Tests for _parse_message — the only complex pure-ish method."""

    def test_returns_none_when_no_message_key(self):
        client = _make_client()
        assert client._parse_message({}) is None

    def test_returns_none_when_message_is_not_dict(self):
        client = _make_client()
        assert client._parse_message({"message": "oops"}) is None
        assert client._parse_message({"message": 42}) is None
        assert client._parse_message({"message": None}) is None

    def test_basic_text_message(self):
        client = _make_client()
        payload = {
            "chatId": 100,
            "message": {
                "sender": 7,
                "text": "Hello",
                "time": 1700000000000,
                "id": "abc123",
            },
        }
        msg = client._parse_message(payload)
        assert msg is not None
        assert msg.chat_id == 100
        assert msg.sender_id == 7
        assert msg.text == "Hello"
        assert msg.timestamp == 1700000000000
        assert msg.message_id == "abc123"

    def test_message_id_is_always_string(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"id": 99999}}
        msg = client._parse_message(payload)
        assert isinstance(msg.message_id, str)
        assert msg.message_id == "99999"

    def test_missing_text_defaults_to_empty_string(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"sender": 1}}
        msg = client._parse_message(payload)
        assert msg.text == ""

    def test_attaches_populated(self):
        client = _make_client()
        attaches = [{"_type": "PHOTO", "url": "http://example.com/img.jpg"}]
        payload = {"chatId": 1, "message": {"attaches": attaches}}
        msg = client._parse_message(payload)
        assert msg.attaches == attaches

    def test_attaches_none_becomes_empty_list(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"attaches": None}}
        msg = client._parse_message(payload)
        assert msg.attaches == []

    def test_link_populated(self):
        client = _make_client()
        link = {"type": "FORWARD", "message": {"text": "original"}}
        payload = {"chatId": 1, "message": {"link": link}}
        msg = client._parse_message(payload)
        assert msg.link == link

    def test_link_none_becomes_empty_dict(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"link": None}}
        msg = client._parse_message(payload)
        assert msg.link == {}

    def test_raw_is_full_payload(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"text": "hi"}, "extra": "data"}
        msg = client._parse_message(payload)
        assert msg.raw is payload

    def test_is_self_false_when_my_id_not_set(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"sender": 42}}
        msg = client._parse_message(payload)
        assert msg.is_self is False

    def test_is_self_false_when_sender_differs(self):
        client = _make_client()
        client._my_id = 1
        payload = {"chatId": 1, "message": {"sender": 99}}
        msg = client._parse_message(payload)
        assert msg.is_self is False

    def test_is_self_true_when_sender_matches_my_id(self):
        client = _make_client()
        client._my_id = 42
        payload = {"chatId": 1, "message": {"sender": 42}}
        msg = client._parse_message(payload)
        assert msg.is_self is True

    def test_chat_id_none_when_absent(self):
        client = _make_client()
        payload = {"message": {"text": "no chat id"}}
        msg = client._parse_message(payload)
        assert msg.chat_id is None

    def test_empty_message_dict_returns_none(self):
        # Empty dict is falsy in Python, so _parse_message treats it as absent
        client = _make_client()
        payload = {"chatId": 5, "message": {}}
        msg = client._parse_message(payload)
        assert msg is None


# ---------------------------------------------------------------------------
# MaxClient constructor / basic state
# ---------------------------------------------------------------------------

class TestMaxClientInit:
    def test_token_stored(self):
        c = MaxClient(token="my_token", device_id="dev1")
        assert c.token == "my_token"

    def test_device_id_stored(self):
        c = MaxClient(token="tok", device_id="mydev")
        assert c.device_id == "mydev"

    def test_debug_default_false(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c.debug is False

    def test_debug_explicit_true(self):
        c = MaxClient(token="tok", device_id="dev", debug=True)
        assert c.debug is True

    def test_initial_seq_is_zero(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._seq == 0

    def test_initial_my_id_is_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._my_id is None

    def test_ws_url_constant(self):
        assert MaxClient.WS_URL == "wss://ws-api.oneme.ru/websocket"

    def test_heartbeat_sec_constant(self):
        assert MaxClient.HEARTBEAT_SEC == 30

    def test_reconnect_sec_constant(self):
        assert MaxClient.RECONNECT_SEC == 5

    def test_on_disconnect_cb_initial_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._on_disconnect_cb is None

    def test_on_disconnect_decorator_registers_callback(self):
        c = MaxClient(token="tok", device_id="dev")

        @c.on_disconnect
        async def my_handler():
            pass

        assert c._on_disconnect_cb is my_handler

    def test_on_disconnect_returns_function(self):
        c = MaxClient(token="tok", device_id="dev")

        async def my_handler():
            pass

        result = c.on_disconnect(my_handler)
        assert result is my_handler

    def test_http_headers_do_not_advertise_unsupported_encodings(self):
        assert _HTTP_HEADERS["Accept-Encoding"] == "gzip, deflate"


class TestSendHelpers:
    @pytest.mark.asyncio
    async def test_send_message_passes_attaches_when_present(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(return_value={"ok": True})

        await client.send_message(42, "hello", [{"type": "STRONG"}], attaches=[{"_type": "PHOTO"}])

        payload = client.cmd.await_args.args[1]
        assert payload["chatId"] == 42
        assert payload["message"]["text"] == "hello"
        assert payload["message"]["attaches"] == [{"_type": "PHOTO"}]

    @pytest.mark.asyncio
    async def test_send_message_passes_native_reply_link_when_present(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(return_value={"ok": True})

        await client.send_message(42, "hello", reply_to_max_message_id="max-1")

        payload = client.cmd.await_args.args[1]
        assert payload["message"]["link"] == {"type": "REPLY", "messageId": "max-1"}

    @pytest.mark.asyncio
    async def test_cmd_returns_empty_payload_without_waiting_when_websocket_unavailable(self):
        client = MaxClient(token="tok", device_id="dev")

        payload = await asyncio.wait_for(client.cmd(OpCode.SEND_MESSAGE, {"chatId": 42}), timeout=0.1)

        assert payload == {}
        assert client._pending == {}

    @pytest.mark.asyncio
    async def test_send_photo_uploads_and_sends_attach(self):
        client = MaxClient(token="tok", device_id="dev")
        client.upload_photo = AsyncMock(return_value={"_type": "PHOTO", "photoToken": "abc"})
        client.send_message = AsyncMock(return_value={"ok": True})

        await client.send_photo(42, b"image-bytes", caption="hello", elements=[{"type": "STRONG"}], filename="pic.jpg")

        client.upload_photo.assert_awaited_once_with(42, b"image-bytes", filename="pic.jpg")
        client.send_message.assert_awaited_once_with(
            42,
            "hello",
            [{"type": "STRONG"}],
            attaches=[{"_type": "PHOTO", "photoToken": "abc"}],
            reply_to_max_message_id=None,
        )

    @pytest.mark.asyncio
    async def test_send_document_uploads_and_sends_attach(self):
        client = MaxClient(token="tok", device_id="dev")
        client.upload_document = AsyncMock(return_value={"_type": "FILE", "fileToken": "abc", "name": "report.pdf"})
        client._send_document_attach = AsyncMock(return_value={"ok": True})

        await client.send_document(
            42,
            b"file-bytes",
            caption="hello",
            elements=[{"type": "STRONG"}],
            filename="report.pdf",
        )

        client.upload_document.assert_awaited_once_with(42, b"file-bytes", filename="report.pdf")
        client._send_document_attach.assert_awaited_once_with(
            42,
            "hello",
            [{"type": "STRONG"}],
            {"_type": "FILE", "fileToken": "abc", "name": "report.pdf"},
            reply_to_max_message_id=None,
        )

    @pytest.mark.asyncio
    async def test_send_message_returns_empty_on_cmd_error(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(return_value=MaxClient._wrap_cmd_error({"code": "forbidden"}))

        payload = await client.send_message(42, "hello")

        assert payload == {}

    @pytest.mark.asyncio
    async def test_send_document_retries_when_attachment_not_ready(self):
        client = MaxClient(token="tok", device_id="dev")
        client.upload_document = AsyncMock(return_value={"_type": "FILE", "fileToken": "abc", "name": "report.pdf"})
        client._send_message_request = AsyncMock(
            side_effect=[
                MaxClient._wrap_cmd_error({"code": "attachment.not.ready"}),
                {"message": {"id": "max-1"}},
            ]
        )

        with patch("app.max_client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            payload = await client.send_document(42, b"file-bytes", filename="report.pdf")

        assert payload == {"message": {"id": "max-1"}}
        assert client._send_message_request.await_count == 2
        sleep_mock.assert_awaited_once_with(0.5)

    @pytest.mark.asyncio
    async def test_send_document_stops_after_attachment_not_ready_retries(self):
        client = MaxClient(token="tok", device_id="dev")
        client._send_message_request = AsyncMock(
            side_effect=[
                MaxClient._wrap_cmd_error({"code": "attachment.not.ready"}),
                MaxClient._wrap_cmd_error({"code": "attachment.not.ready"}),
                MaxClient._wrap_cmd_error({"code": "attachment.not.ready"}),
                MaxClient._wrap_cmd_error({"code": "attachment.not.ready"}),
                MaxClient._wrap_cmd_error({"code": "attachment.not.ready"}),
            ]
        )

        with patch("app.max_client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            payload = await client._send_document_attach(
                42,
                "hello",
                [{"type": "STRONG"}],
                {"_type": "FILE", "fileToken": "abc", "name": "report.pdf"},
            )

        assert payload == {}
        assert client._send_message_request.await_count == 5
        assert [call.args[0] for call in sleep_mock.await_args_list] == [0.5, 1.0, 2.0, 4.0]

    @pytest.mark.asyncio
    async def test_request_upload_url_uses_platform_api_authorization(self):
        client = MaxClient(token="tok", device_id="dev")
        session = _FakeSession(_FakeResponse(status=200, payload={"url": "https://upload.example/file"}))
        client._session = session

        payload = await client._request_upload_url("file")

        assert payload == {"url": "https://upload.example/file"}
        url, kwargs = session.calls[0]
        assert url.endswith("/uploads")
        assert kwargs["params"] == {"type": "file"}
        assert kwargs["headers"]["Authorization"] == "tok"
        assert kwargs["headers"]["Accept"] == "application/json"

    @pytest.mark.asyncio
    async def test_fetch_message_uses_platform_api_authorization(self):
        client = MaxClient(token="tok", device_id="dev")
        session = _FakeSession(_FakeResponse(status=200, payload={"message": {"id": "max-1"}}))
        client._session = session

        payload = await client.fetch_message("max-1")

        assert payload == {"message": {"id": "max-1"}}
        url, kwargs = session.calls[0]
        assert url.endswith("/messages/max-1")
        assert kwargs["headers"]["Authorization"] == "tok"
        assert kwargs["headers"]["Accept"] == "application/json"

    @pytest.mark.asyncio
    async def test_fetch_file_download_url_uses_websocket_opcode_and_payload(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(return_value={"url": "https://example.com/report.pdf"})

        payload = await client.fetch_file_download_url(file_id="91", chat_id="42", message_id="max-1")

        assert payload == "https://example.com/report.pdf"
        client.cmd.assert_awaited_once_with(
            OpCode.FILE_DOWNLOAD_URL,
            {"fileId": 91, "chatId": 42, "messageId": "max-1"},
        )

    @pytest.mark.asyncio
    async def test_fetch_file_download_url_returns_none_for_cmd_error(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(return_value=client._wrap_cmd_error({"code": "forbidden"}))

        payload = await client.fetch_file_download_url(file_id=91, chat_id=42, message_id="max-1")

        assert payload is None

    @pytest.mark.asyncio
    async def test_fetch_video_download_url_uses_websocket_opcode_and_prefers_highest_mp4(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(
            return_value={
                "preview": "https://example.com/preview.jpg",
                "MP4_720": "https://example.com/video-720.mp4",
                "MP4_1080": "https://example.com/video-1080.mp4",
            }
        )

        payload = await client.fetch_video_download_url(
            video_id="77",
            chat_id="42",
            message_id="max-video-1",
            token="video-token",
        )

        assert payload == "https://example.com/video-1080.mp4"
        client.cmd.assert_awaited_once_with(
            OpCode.VIDEO_DOWNLOAD_URL,
            {"videoId": 77, "chatId": 42, "messageId": "max-video-1"},
        )

    @pytest.mark.asyncio
    async def test_fetch_video_download_url_returns_none_for_cmd_error(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(return_value=client._wrap_cmd_error({"code": "forbidden"}))

        payload = await client.fetch_video_download_url(video_id=77, chat_id=42, message_id="max-video-1")

        assert payload is None

    @pytest.mark.asyncio
    async def test_fetch_video_download_url_retries_when_first_attempt_has_non_mp4_url(self):
        client = MaxClient(token="auth-token", device_id="dev")
        client.cmd = AsyncMock(
            side_effect=[
                {"url": "https://maxvd692.okcdn.ru/?expires=1&type=0&sig=bad"},
                {"MP4_720": "https://example.com/video-720.mp4"},
            ]
        )

        payload = await client.fetch_video_download_url(
            video_id=77,
            chat_id=42,
            message_id="max-video-2",
            token="attach-token",
        )

        assert payload == "https://example.com/video-720.mp4"
        assert client.cmd.await_args_list[0].args == (
            OpCode.VIDEO_DOWNLOAD_URL,
            {"videoId": 77, "chatId": 42, "messageId": "max-video-2"},
        )
        assert client.cmd.await_args_list[1].args == (
            OpCode.VIDEO_DOWNLOAD_URL,
            {"videoId": 77, "chatId": 42, "messageId": "max-video-2", "token": "attach-token"},
        )

    @pytest.mark.asyncio
    async def test_download_video_attachment_tries_multiple_candidates_until_video_payload(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(
            return_value={
                "MP4_240": "https://example.com/bad.mp4",
                "EXTERNAL": "https://example.com/good",
                "cache": "https://example.com/cache",
            }
        )
        client.download_file_result = AsyncMock(
            side_effect=[
                DownloadResult(status=400, used_authorization=False),
                DownloadResult(data=b"\x00\x00\x00\x18ftypisomvideo", status=200, used_authorization=False, content_type="video/mp4"),
            ]
        )

        payload = await client.download_video_attachment(
            video_id=77,
            chat_id=42,
            message_id="max-video-3",
            token="attach-token",
        )

        assert payload == b"\x00\x00\x00\x18ftypisomvideo"
        assert client.download_file_result.await_args_list[0].args == ("https://example.com/bad.mp4",)
        assert client.download_file_result.await_args_list[1].args == ("https://example.com/good",)

    @pytest.mark.asyncio
    async def test_download_video_attachment_resolves_okru_external_html_to_video(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(
            return_value={
                "MP4_240": "https://example.com/bad.mp4",
                "EXTERNAL": "https://m.ok.ru/video/123",
            }
        )
        client.download_file_result = AsyncMock(
            side_effect=[
                DownloadResult(status=400, used_authorization=False),
                DownloadResult(data=b"<html>page</html>", status=200, used_authorization=False, content_type="text/html;charset=UTF-8"),
                DownloadResult(data=b"\x00\x00\x00\x18ftypisomvideo", status=200, used_authorization=False, content_type="video/mp4"),
            ]
        )
        client._resolve_okru_external_video_url = AsyncMock(return_value="https://example.com/redirected.mp4")

        payload = await client.download_video_attachment(
            video_id=77,
            chat_id=42,
            message_id="max-video-4",
            token="attach-token",
        )

        assert payload == b"\x00\x00\x00\x18ftypisomvideo"
        client._resolve_okru_external_video_url.assert_awaited_once_with("https://m.ok.ru/video/123", b"<html>page</html>")
        assert client.download_file_result.await_args_list[2].args == ("https://example.com/redirected.mp4",)

    def test_extract_okru_video_src_parses_data_video_json(self):
        html_bytes = (
            b'<div data-video=\"{&quot;videoSrc&quot;:&quot;https://vid.example/stream&quot;,&quot;videoName&quot;:&quot;test&quot;}\"></div>'
        )

        assert MaxClient._extract_okru_video_src(html_bytes) == "https://vid.example/stream"

    def test_extract_okru_video_src_falls_back_to_direct_video_src_pattern(self):
        html_bytes = (
            b'<script>window.__STATE__ = {"movie":{"id":1},"videoSrc":"https:\\/\\/vid.example\\/stream.mp4?sig=1"}</script>'
        )

        assert MaxClient._extract_okru_video_src(html_bytes) == "https://vid.example/stream.mp4?sig=1"

    def test_extract_okru_video_id_from_mobile_page(self):
        assert MaxClient._extract_okru_video_id("https://m.ok.ru/video/14261721980814") == "14261721980814"

    def test_extract_okru_player_data_parses_data_options_json(self):
        webpage = (
            '<div data-options="{&quot;flashvars&quot;:{&quot;metadata&quot;:&quot;{'
            r'\\&quot;videos\\&quot;:[{\\&quot;name\\&quot;:\\&quot;hd\\&quot;,'
            r'\\&quot;url\\&quot;:\\&quot;https://cdn.example/video-hd.mp4\\&quot;}]}&quot;}}"></div>'
        )

        payload = MaxClient._extract_okru_player_data(webpage)

        assert payload == {
            "flashvars": {
                "metadata": '{"videos":[{"name":"hd","url":"https://cdn.example/video-hd.mp4"}]}'
            }
        }

    def test_extract_okru_player_data_parses_data_attributes_json(self):
        webpage = (
            '<div data-attributes="{&quot;flashvars&quot;:{&quot;metadataUrl&quot;:&quot;https%3A%2F%2Fok.ru%2Fmetadata&quot;,'
            '&quot;location&quot;:&quot;SEARCH&quot;}}"></div>'
        )

        payload = MaxClient._extract_okru_player_data(webpage)

        assert payload == {
            "flashvars": {
                "metadataUrl": "https%3A%2F%2Fok.ru%2Fmetadata",
                "location": "SEARCH",
            }
        }

    def test_extract_okru_player_data_falls_back_to_flashvars_fields_in_scripts(self):
        webpage = (
            '<script>window.__state = {"some":"value","metadataUrl":"https:\\/\\/ok.ru\\/metadata",'
            '"location":"SEARCH"};</script>'
        )

        payload = MaxClient._extract_okru_player_data(webpage)

        assert payload == {
            "flashvars": {
                "metadataUrl": "https://ok.ru/metadata",
                "location": "SEARCH",
            }
        }

    def test_extract_okru_movie_player_urls_supports_absolute_and_protocol_relative_links(self):
        webpage = (
            '<a href="https://wg31.ok.ru/web-api/video/moviePlayer/14261721980814#abc">video</a>'
            '<script>var url = "\\/\\/wg32.ok.ru\\/web-api\\/video\\/moviePlayer\\/14261721980814#def";</script>'
        )

        payload = MaxClient._extract_okru_movie_player_urls(webpage, "14261721980814")

        assert payload == [
            "https://wg31.ok.ru/web-api/video/moviePlayer/14261721980814",
            "https://wg32.ok.ru/web-api/video/moviePlayer/14261721980814",
        ]

    @pytest.mark.asyncio
    async def test_resolve_okru_desktop_video_url_uses_videoembed_before_video_page(self):
        client = MaxClient(token="tok", device_id="dev")
        client._fetch_text_page = AsyncMock(
            side_effect=[
                '<div data-attributes="{&quot;flashvars&quot;:{&quot;metadataUrl&quot;:&quot;https%3A%2F%2Fok.ru%2Fmetadata&quot;,'
                '&quot;location&quot;:&quot;SEARCH&quot;}}"></div>',
            ]
        )
        client._fetch_json_page = AsyncMock(
            return_value={
                "videos": [
                    {"name": "mobile", "url": "https://cdn.example/video-240.mp4"},
                    {"name": "hd", "url": "https://cdn.example/video-720.mp4"},
                ]
            }
        )

        payload = await client._resolve_okru_desktop_video_url("https://m.ok.ru/video/14261721980814")

        assert payload == "https://cdn.example/video-720.mp4"
        client._fetch_text_page.assert_awaited_once_with("https://ok.ru/videoembed/14261721980814")
        client._fetch_json_page.assert_awaited_once()
        assert client._fetch_json_page.await_args.args[0] == "https://ok.ru/metadata"
        assert client._fetch_json_page.await_args.kwargs["data"] == {"st.location": "SEARCH"}

    @pytest.mark.asyncio
    async def test_resolve_okru_desktop_video_url_discovers_movie_player_page_from_video_html(self):
        client = MaxClient(token="tok", device_id="dev")
        client._fetch_text_page = AsyncMock(
            side_effect=[
                None,
                '<a href="https://wg31.ok.ru/web-api/video/moviePlayer/14261721980814#abc">video</a>',
                '<div data-attributes="{&quot;flashvars&quot;:{&quot;metadataUrl&quot;:&quot;https%3A%2F%2Fok.ru%2Fmetadata&quot;}}"></div>',
            ]
        )
        client._fetch_json_page = AsyncMock(
            return_value={
                "videos": [
                    {"name": "mobile", "url": "https://cdn.example/video-240.mp4"},
                    {"name": "hd", "url": "https://cdn.example/video-720.mp4"},
                ]
            }
        )

        payload = await client._resolve_okru_desktop_video_url("https://m.ok.ru/video/14261721980814")

        assert payload == "https://cdn.example/video-720.mp4"
        assert [call.args[0] for call in client._fetch_text_page.await_args_list] == [
            "https://ok.ru/videoembed/14261721980814",
            "https://ok.ru/video/14261721980814",
            "https://wg31.ok.ru/web-api/video/moviePlayer/14261721980814",
        ]

    @pytest.mark.asyncio
    async def test_download_file_uses_authorization_for_max_media_urls(self):
        client = MaxClient(token="tok", device_id="dev")
        session = _FakeSession(_FakeResponse(status=200, body=b"voice-bytes"))

        with patch("app.max_client.aiohttp.ClientSession", return_value=session):
            payload = await client.download_file("https://maxvd526.okcdn.ru/voice.ogg")

        assert payload == b"voice-bytes"
        _, kwargs = session.calls[0]
        assert kwargs["headers"]["Authorization"] == "tok"
        assert kwargs["headers"]["Origin"] == _HTTP_HEADERS["Origin"]

    @pytest.mark.asyncio
    async def test_download_file_result_retries_signed_max_media_without_authorization(self):
        client = MaxClient(token="tok", device_id="dev")
        session = _FakeSession(
            [
                _FakeResponse(status=400, text="bad signed request"),
                _FakeResponse(status=200, body=b"voice-bytes"),
            ]
        )

        with patch("app.max_client.aiohttp.ClientSession", return_value=session):
            result = await client.download_file_result("https://maxvd526.okcdn.ru/voice.ogg?expires=1&sig=abc")

        assert result.data == b"voice-bytes"
        assert result.status == 200
        assert result.used_authorization is False
        assert len(session.calls) == 2
        _, first_kwargs = session.calls[0]
        _, second_kwargs = session.calls[1]
        assert first_kwargs["headers"]["Authorization"] == "tok"
        assert "Authorization" not in second_kwargs["headers"]
        assert "Origin" not in first_kwargs["headers"]
        assert "Referer" not in first_kwargs["headers"]
        assert "Origin" not in second_kwargs["headers"]
        assert "Referer" not in second_kwargs["headers"]

    @pytest.mark.asyncio
    async def test_download_file_skips_authorization_for_external_urls(self):
        client = MaxClient(token="tok", device_id="dev")
        session = _FakeSession(_FakeResponse(status=200, body=b"file-bytes"))

        with patch("app.max_client.aiohttp.ClientSession", return_value=session):
            payload = await client.download_file("https://example.com/file.bin")

        assert payload == b"file-bytes"
        _, kwargs = session.calls[0]
        assert "Authorization" not in kwargs["headers"]

    @pytest.mark.asyncio
    async def test_upload_document_uses_file_upload_endpoint_and_data_field(self):
        client = MaxClient(token="tok", device_id="dev")
        client._request_upload_url = AsyncMock(return_value={"url": "https://upload.example/file"})
        client._upload_bytes = AsyncMock(return_value={"token": "file-token"})

        payload = await client.upload_document(42, b"file-bytes", filename="report.pdf")

        assert payload == {"_type": "FILE", "fileToken": "file-token", "name": "report.pdf"}
        client._request_upload_url.assert_awaited_once_with("file")
        client._upload_bytes.assert_awaited_once_with(
            "https://upload.example/file",
            b"file-bytes",
            filename="report.pdf",
            content_type="application/pdf",
            field_name="data",
            headers=ANY,
        )
        headers = client._upload_bytes.await_args.kwargs["headers"]
        assert headers["Authorization"] == "tok"

    @pytest.mark.asyncio
    async def test_upload_photo_keeps_legacy_upload_flow(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(side_effect=[{"url": "https://upload.example/photo"}, {"ok": True}])
        client._upload_bytes = AsyncMock(return_value={"photos": {"0": {"token": "photo-token"}}})

        payload = await client.upload_photo(42, b"image-bytes", filename="pic.jpg")

        assert payload == {"_type": "PHOTO", "photoToken": "photo-token"}
        assert client.cmd.await_args_list[0].args == (OpCode.PHOTO_UPLOAD_URL, {"count": 1})
        assert client.cmd.await_args_list[1].args == (OpCode.UPLOAD_ATTACH, {"chatId": 42, "type": "PHOTO"})
        client._upload_bytes.assert_awaited_once_with(
            "https://upload.example/photo",
            b"image-bytes",
            filename="pic.jpg",
            content_type="image/jpeg",
        )

    def test_extract_sent_message_id_prefers_nested_message_payload(self):
        assert MaxClient.extract_sent_message_id({"message": {"id": "max-7"}, "id": "outer"}) == "max-7"

    def test_extract_sent_message_id_accepts_message_id_fallback(self):
        assert MaxClient.extract_sent_message_id({"messageId": "max-9"}) == "max-9"

    def test_extract_sent_message_id_accepts_mid_fallback(self):
        assert MaxClient.extract_sent_message_id({"mid": "max-8"}) == "max-8"


class TestDispatchQueue:
    @pytest.mark.asyncio
    async def test_dispatch_preserves_order_within_same_chat(self):
        client = MaxClient(token="tok", device_id="dev")
        started = asyncio.Event()
        release = asyncio.Event()
        handled: list[tuple[str, str]] = []

        @client.on_message
        async def handle_message(msg):
            handled.append(("start", msg.message_id))
            if msg.message_id == "1":
                started.set()
                await release.wait()
            handled.append(("done", msg.message_id))

        first_dispatch = {
            "opcode": OpCode.DISPATCH,
            "payload": {
                "chatId": 42,
                "message": {"id": 1, "sender": 7, "text": "first"},
            },
        }
        second_dispatch = {
            "opcode": OpCode.DISPATCH,
            "payload": {
                "chatId": 42,
                "message": {"id": 2, "sender": 7, "text": "second"},
            },
        }

        first_task = asyncio.create_task(client._handle(first_dispatch))
        await started.wait()
        second_task = asyncio.create_task(client._handle(second_dispatch))
        await asyncio.sleep(0)

        assert handled == [("start", "1")]

        release.set()
        await first_task
        await second_task
        await client.wait_for_pending_dispatches()

        assert handled == [
            ("start", "1"),
            ("done", "1"),
            ("start", "2"),
            ("done", "2"),
        ]
        await client._shutdown_message_dispatcher()
