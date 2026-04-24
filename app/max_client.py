import asyncio
import html
import json
import logging
import mimetypes
import os
import random
import re
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlparse

import aiohttp

from app.max_dispatcher import MessageDispatchQueue

log = logging.getLogger(__name__)

DEBUG_DIR = "debug"


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    # aiohttp in our runtime can always handle gzip/deflate, but not
    # brotli/zstd unless extra native dependencies are installed.
    "Accept-Encoding": "gzip, deflate",
    "sec-ch-ua": '"Chromium";v="131", "Google Chrome";v="131", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

_WS_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_HTTP_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Referer": "https://web.max.ru/",
    "Accept": "*/*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}
_SIGNED_MEDIA_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": _BROWSER_HEADERS["Accept-Language"],
    "Accept-Encoding": _BROWSER_HEADERS["Accept-Encoding"],
}

_PLATFORM_API_BASE_URL = "https://platform-api.max.ru"
_CMD_ERROR_MARKER = "__cmd_error__"
_ATTACHMENT_NOT_READY_CODE = "attachment.not.ready"
_DOCUMENT_READY_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
_MAX_MEDIA_HOST_SUFFIXES = ("okcdn.ru", "mycdn.me", "max.ru", "oneme.ru")
_SIGNED_MEDIA_QUERY_KEYS = frozenset({"expires", "sig"})


class OpCode(IntEnum):
    HEARTBEAT_PING = 1
    HANDSHAKE = 6
    AUTH_SNAPSHOT = 19
    LOGOUT = 20
    STICKER_STORE = 27
    ASSET_GET = 28
    FAVORITE_STICKER = 29
    CONTACT_GET = 32
    CONTACT_PRESENCE = 35
    CHAT_GET = 48
    SEND_MESSAGE = 64
    UPLOAD_ATTACH = 65
    EDIT_MESSAGE = 67
    PHOTO_UPLOAD_URL = 80
    VIDEO_DOWNLOAD_URL = 83
    FILE_DOWNLOAD_URL = 88
    DISPATCH = 128


@dataclass
class MaxMessage:
    chat_id: Any = None
    sender_id: Any = None
    text: str = ""
    timestamp: Any = None
    message_id: str = ""
    is_self: bool = False
    attaches: list = field(default_factory=list)
    link: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class DownloadResult:
    data: bytes | None = None
    status: int | None = None
    used_authorization: bool = False
    content_type: str | None = None


class MaxClient:
    WS_URL = "wss://ws-api.oneme.ru/websocket"
    HEARTBEAT_SEC = 30
    RECONNECT_SEC = 5
    DISPATCH_QUEUE_SIZE = 128
    DISPATCH_WORKERS = 4

    def __init__(self, token: str, device_id: str, chat_ids: str | None = None, debug: bool = False):
        self.token = token
        self.device_id = device_id
        self.debug = debug
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._seq = 0
        self._my_id = None
        self._on_ready_cb = None
        self._on_message_cb = None
        self._heartbeat_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._dispatch_counter = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._on_disconnect_cb = None
        self._message_dispatcher: MessageDispatchQueue | None = None
        self.chat_ids: list[int] = []
        if chat_ids:
            self.chat_ids = list(map(int, map(str.strip, chat_ids.split(","))))

    def on_ready(self, func):
        self._on_ready_cb = func
        return func

    def on_message(self, func):
        self._on_message_cb = func
        return func

    def on_disconnect(self, func):
        self._on_disconnect_cb = func
        return func

    async def _send(self, opcode: int, payload: dict) -> int:
        if not self._ws or self._ws.closed:
            return -1
        seq = self._seq
        pkt = {
            "ver": 11,
            "cmd": 0,
            "seq": seq,
            "opcode": opcode,
            "payload": payload,
        }
        self._seq += 1
        raw = json.dumps(pkt, ensure_ascii=False)
        log.debug(">>> SEND op=%d seq=%d | %s", opcode, seq, raw[:800])
        await self._ws.send_str(raw)
        return seq

    async def cmd(self, opcode: int, payload: dict, timeout: float = 10) -> dict:
        """Send a request and wait for the response payload."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        seq = await self._send(opcode, payload)
        if seq < 0:
            log.warning("cmd skipped: op=%d websocket is not connected", opcode)
            return {}
        self._pending[seq] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("cmd timeout: op=%d seq=%d", opcode, seq)
            return {}
        finally:
            self._pending.pop(seq, None)

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.HEARTBEAT_SEC)
            try:
                if self._ws and not self._ws.closed:
                    await self._send(OpCode.HEARTBEAT_PING, {"interactive": False})
                else:
                    break
            except Exception:
                log.exception("Heartbeat error, stopping heartbeat loop")
                break

    async def run(self):
        if self.debug:
            os.makedirs(DEBUG_DIR, exist_ok=True)

        try:
            async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
                self._session = session
                while True:
                    try:
                        log.info("Connecting to %s ...", self.WS_URL)
                        async with session.ws_connect(self.WS_URL, headers=_WS_HEADERS) as ws:
                            self._ws = ws
                            self._seq = 0
                            self._pending.clear()

                            log.info("Connected. Sending handshake...")
                            await self._send(
                                OpCode.HANDSHAKE,
                                {
                                    "deviceId": self.device_id,
                                    "userAgent": {
                                        "deviceType": "WEB",
                                        "deviceName": "Chrome 131.0.0.0",
                                    },
                                    "appVersion": "25.12.11",
                                },
                            )

                            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    await self._handle(json.loads(msg.data))
                                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    log.warning("WebSocket closed/error: %s", msg.type)
                                    break

                    except Exception:
                        log.exception("Connection error")

                    finally:
                        if self._heartbeat_task:
                            self._heartbeat_task.cancel()
                        for fut in self._pending.values():
                            if not fut.done():
                                fut.cancel()
                        self._pending.clear()

                    if self._on_disconnect_cb:
                        try:
                            await self._on_disconnect_cb()
                        except Exception:
                            log.exception("on_disconnect callback error")

                    log.info("Reconnecting in %ds...", self.RECONNECT_SEC)
                    await asyncio.sleep(self.RECONNECT_SEC)
        finally:
            await self._shutdown_message_dispatcher()

    async def _handle(self, data: dict):
        op = data.get("opcode")
        cmd = data.get("cmd")
        seq = data.get("seq")
        payload = data.get("payload", {})

        if cmd == 1 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result(payload)
            if op not in (OpCode.HANDSHAKE, OpCode.AUTH_SNAPSHOT):
                log.debug("<<< RESP  op=%-4s seq=%s", op, seq)
        elif cmd == 3 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result(self._wrap_cmd_error(payload))
            log.warning("<<< ERROR op=%-4s seq=%s | %s", op, seq, payload)
        else:
            payload_preview = json.dumps(payload, ensure_ascii=False)
            if len(payload_preview) > 3000:
                payload_preview = payload_preview[:3000] + "..."

            if op == OpCode.HANDSHAKE and cmd == 1:
                log.info("Handshake OK -> sending auth token...")
                await self._send(
                    OpCode.AUTH_SNAPSHOT,
                    {
                        "chatsCount": 10,
                        "interactive": True,
                        "token": self.token,
                    },
                )
            elif op == OpCode.AUTH_SNAPSHOT and cmd == 1:
                self._my_id = payload.get("profile", {}).get("id")
                log.info("Authorized! my_id=%s", self._my_id)
                if self.debug:
                    self._dump_json("snapshot.json", payload)

                if self._on_ready_cb:
                    await self._on_ready_cb(payload)
            elif op == OpCode.DISPATCH:
                self._dispatch_counter += 1
                if self.debug and self._dispatch_counter <= 20:
                    self._dump_json(f"dispatch_{self._dispatch_counter:04d}.json", payload)

                if self._on_message_cb:
                    msg = self._parse_message(payload)
                    if msg is not None and ((not self.chat_ids) or (msg.chat_id in self.chat_ids)):
                        await self._dispatch_message(msg)
            elif op in (OpCode.HEARTBEAT_PING,):
                log.debug("Heartbeat op=%s", op)
            elif cmd not in (1, 3):
                log.info("<<< EVENT op=%-4s cmd=%-3s | %s", op, cmd, payload_preview[:500])

    async def fetch_contacts(self, contact_ids: list[int]) -> dict:
        """Fetch contact info via WS opcode 32. Returns raw response payload."""
        if not contact_ids:
            return {}
        resp = await self.cmd(OpCode.CONTACT_GET, {"contactIds": contact_ids})
        if self.debug:
            self._dump_json("contacts_response.json", resp)
        log.info("fetch_contacts(%s) -> keys: %s", contact_ids, list(resp.keys()))
        return resp

    async def _send_message_request(
        self,
        chat_id,
        text: str,
        elements=None,
        attaches=None,
        reply_to_max_message_id: str | None = None,
    ) -> dict:
        if elements is None:
            elements = []
        if attaches is None:
            attaches = []
        cid = int(time.time() * 1000) * 1000 + random.randint(0, 999)
        message = {"text": text, "cid": cid, "elements": elements}
        if attaches:
            message["attaches"] = attaches
        if reply_to_max_message_id is not None:
            message["link"] = {"type": "REPLY", "messageId": str(reply_to_max_message_id)}
        return await self.cmd(
            OpCode.SEND_MESSAGE,
            {
                "chatId": chat_id,
                "message": message,
                "notify": True,
            },
        )

    async def send_message(
        self,
        chat_id,
        text: str,
        elements=None,
        attaches=None,
        reply_to_max_message_id: str | None = None,
    ) -> dict:
        """Send a message to a Max chat."""
        resp = await self._send_message_request(
            chat_id,
            text,
            elements,
            attaches=attaches,
            reply_to_max_message_id=reply_to_max_message_id,
        )
        error_payload = self._extract_cmd_error(resp)
        if error_payload is not None:
            log.warning("send_message(chat=%s) failed: %s", chat_id, error_payload)
            return {}
        log.info("send_message(chat=%s) -> %s", chat_id, "OK" if resp else "FAIL")
        return resp

    async def send_photo(
        self,
        chat_id,
        data: bytes,
        caption: str = "",
        elements=None,
        filename: str = "photo.jpg",
        reply_to_max_message_id: str | None = None,
    ) -> dict:
        """Upload a photo and send it to a Max chat."""
        if not data:
            log.warning("send_photo(chat=%s) skipped: empty payload", chat_id)
            return {}

        attach = await self.upload_photo(chat_id, data, filename=filename)
        if not attach:
            return {}
        return await self.send_message(
            chat_id,
            caption,
            elements,
            attaches=[attach],
            reply_to_max_message_id=reply_to_max_message_id,
        )

    async def send_document(
        self,
        chat_id,
        data: bytes,
        caption: str = "",
        elements=None,
        filename: str = "file",
        reply_to_max_message_id: str | None = None,
    ) -> dict:
        """Upload a document and send it to a Max chat."""
        if not data:
            log.warning("send_document(chat=%s) skipped: empty payload", chat_id)
            return {}

        attach = await self.upload_document(chat_id, data, filename=filename)
        if not attach:
            return {}
        return await self._send_document_attach(
            chat_id,
            caption,
            elements,
            attach,
            reply_to_max_message_id=reply_to_max_message_id,
        )

    async def upload_photo(self, chat_id, data: bytes, filename: str = "photo.jpg") -> dict:
        """Upload photo bytes to Max and return attach payload for send_message."""
        upload_info = await self.cmd(OpCode.PHOTO_UPLOAD_URL, {"count": 1})
        upload_url = upload_info.get("url")
        if not upload_url:
            log.warning("Photo upload URL missing for chat=%s: %s", chat_id, upload_info)
            return {}

        init_payload = await self.cmd(
            OpCode.UPLOAD_ATTACH,
            {
                "chatId": chat_id,
                "type": "PHOTO",
            },
        )
        init_error = self._extract_cmd_error(init_payload)
        if init_error is not None:
            log.warning("Photo upload init failed for chat=%s: %s", chat_id, init_error)
            return {}
        if init_payload == {}:
            log.warning("Photo upload init failed for chat=%s", chat_id)
            return {}

        response_payload = await self._upload_bytes(
            upload_url,
            data,
            filename=filename,
            content_type="image/jpeg",
        )
        if not response_payload:
            return {}

        photos = response_payload.get("photos") or {}
        first_photo = next(iter(photos.values()), None)
        if not isinstance(first_photo, dict) or not first_photo.get("token"):
            log.warning("Photo upload token missing for chat=%s: %s", chat_id, response_payload)
            return {}

        return {
            "_type": "PHOTO",
            "photoToken": first_photo["token"],
        }

    async def upload_document(self, chat_id, data: bytes, filename: str = "file") -> dict:
        """Upload generic file bytes to Max and return attach payload for send_message."""
        upload_info = await self._request_upload_url("file")
        upload_url = upload_info.get("url")
        if not upload_url:
            log.warning("Document file upload URL missing for chat=%s: %s", chat_id, upload_info)
            return {}

        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        response_payload = await self._upload_bytes(
            upload_url,
            data,
            filename=filename,
            content_type=content_type,
            field_name="data",
            headers=self._platform_api_headers(),
        )
        if not response_payload:
            return {}

        attach = self._normalize_document_attach(response_payload, filename=filename)
        if not attach:
            log.warning("Document upload payload missing token for chat=%s: %s", chat_id, response_payload)
        return attach

    async def _send_document_attach(
        self,
        chat_id,
        caption: str,
        elements,
        attach: dict,
        *,
        reply_to_max_message_id: str | None = None,
    ) -> dict:
        for attempt, delay in enumerate((0.0, *_DOCUMENT_READY_RETRY_DELAYS), start=1):
            if delay:
                log.warning(
                    "Document attachment not ready for chat=%s attempt=%d; retrying send in %.1fs",
                    chat_id,
                    attempt - 1,
                    delay,
                )
                await asyncio.sleep(delay)

            response = await self._send_message_request(
                chat_id,
                caption,
                elements,
                attaches=[attach],
                reply_to_max_message_id=reply_to_max_message_id,
            )
            error_payload = self._extract_cmd_error(response)
            if error_payload is None:
                log.info("send_document(chat=%s) -> %s", chat_id, "OK" if response else "FAIL")
                return response
            if not self._is_attachment_not_ready_error(error_payload):
                log.warning("send_document(chat=%s) failed: %s", chat_id, error_payload)
                return {}

        log.warning("Document attachment still not ready for chat=%s after %d attempts", chat_id, len(_DOCUMENT_READY_RETRY_DELAYS) + 1)
        return {}

    async def _request_upload_url(self, upload_type: str) -> dict:
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True

        request_url = f"{_PLATFORM_API_BASE_URL}/uploads"
        try:
            async with session.post(
                request_url,
                params={"type": upload_type},
                headers=self._platform_api_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                body = await resp.text()
                log.warning(
                    "%s upload URL request failed - HTTP %d: %s",
                    upload_type,
                    resp.status,
                    body[:500],
                )
        except Exception:
            log.exception("%s upload URL request error", upload_type)
        finally:
            if close_after:
                await session.close()
        return {}

    def _platform_api_headers(self) -> dict[str, str]:
        return {
            **_BROWSER_HEADERS,
            "Accept": "application/json",
            "Authorization": self.token,
        }

    @staticmethod
    def _is_max_media_url(url: str) -> bool:
        hostname = urlparse(url).hostname
        if not hostname:
            return False

        host = hostname.lower().rstrip(".")
        for suffix in _MAX_MEDIA_HOST_SUFFIXES:
            if host == suffix or host.endswith(f".{suffix}"):
                return True
        return False

    @classmethod
    def _is_signed_max_media_url(cls, url: str) -> bool:
        if not cls._is_max_media_url(url):
            return False

        query_keys = {key.lower() for key, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)}
        return _SIGNED_MEDIA_QUERY_KEYS.issubset(query_keys)

    def _download_headers(self, url: str, *, use_authorization: bool | None = None) -> tuple[dict[str, str], bool]:
        headers = dict(_SIGNED_MEDIA_HEADERS if self._is_signed_max_media_url(url) else _HTTP_HEADERS)
        if use_authorization is None:
            use_authorization = self._is_max_media_url(url)
        if use_authorization:
            headers["Authorization"] = self.token
        return headers, use_authorization

    async def _download_file_once(
        self,
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str],
        *,
        used_authorization: bool,
    ) -> tuple[DownloadResult, str]:
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return (
                        DownloadResult(
                            data=data,
                            status=resp.status,
                            used_authorization=used_authorization,
                            content_type=resp.headers.get("Content-Type"),
                        ),
                        "",
                    )
                body = await resp.text(errors="ignore")
                return (
                    DownloadResult(
                        status=resp.status,
                        used_authorization=used_authorization,
                    ),
                    body[:200],
                )
        except Exception:
            log.exception("Download error: %s", url[:120])
            return DownloadResult(used_authorization=used_authorization), ""

    async def _upload_bytes(
        self,
        url: str,
        data: bytes,
        *,
        filename: str,
        content_type: str | None,
        field_name: str = "file",
        headers: dict[str, str] | None = None,
    ) -> dict:
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True

        form = aiohttp.FormData()
        if content_type:
            form.add_field(field_name, data, filename=filename, content_type=content_type)
        else:
            form.add_field(field_name, data, filename=filename)
        try:
            async with session.post(
                url,
                headers=headers or _HTTP_HEADERS,
                data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                body = await resp.text()
                log.warning("Upload failed %s - HTTP %d: %s", url[:120], resp.status, body[:500])
        except Exception:
            log.exception("Upload error: %s", url[:120])
        finally:
            if close_after:
                await session.close()
        return {}

    async def fetch_message(self, message_id: str) -> dict:
        """Fetch a message payload from the documented platform API."""
        if not message_id:
            return {}

        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True

        request_url = f"{_PLATFORM_API_BASE_URL}/messages/{quote(str(message_id), safe='')}"
        try:
            async with session.get(
                request_url,
                headers=self._platform_api_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                body = await resp.text(errors="ignore")
                log.warning(
                    "Fetch message failed %s - HTTP %d: %s",
                    request_url[:120],
                    resp.status,
                    body[:500],
                )
        except Exception:
            log.exception("Fetch message error: %s", request_url[:120])
        finally:
            if close_after:
                await session.close()
        return {}

    async def fetch_file_download_url(self, *, file_id: Any, chat_id: Any, message_id: str) -> str | None:
        """Resolve a downloadable file URL via the MAX WebSocket protocol."""
        if file_id in (None, "") or chat_id in (None, "") or not message_id:
            return None

        try:
            normalized_file_id = int(file_id)
            normalized_chat_id = int(chat_id)
        except (TypeError, ValueError):
            log.warning(
                "Fetch file download URL skipped due to invalid identifiers: file_id=%r chat_id=%r message_id=%r",
                file_id,
                chat_id,
                message_id,
            )
            return None

        response_payload = await self.cmd(
            OpCode.FILE_DOWNLOAD_URL,
            {
                "fileId": normalized_file_id,
                "chatId": normalized_chat_id,
                "messageId": str(message_id),
            },
        )
        cmd_error = self._extract_cmd_error(response_payload)
        if cmd_error is not None:
            log.warning(
                "Fetch file download URL failed file_id=%s chat_id=%s message_id=%s: %s",
                normalized_file_id,
                normalized_chat_id,
                message_id,
                cmd_error,
            )
            return None

        url = self._extract_http_url(response_payload)
        if url:
            return url

        payload_preview = list(response_payload.keys()) if isinstance(response_payload, dict) else type(response_payload).__name__
        log.warning(
            "Fetch file download URL returned no URL for file_id=%s chat_id=%s message_id=%s payload=%s",
            normalized_file_id,
            normalized_chat_id,
            message_id,
            payload_preview,
        )
        return None

    async def fetch_video_download_urls(
        self,
        *,
        video_id: Any,
        chat_id: Any,
        message_id: str,
        token: str | None = None,
    ) -> list[str]:
        """Resolve downloadable video URLs via the MAX WebSocket protocol."""
        if video_id in (None, "") or chat_id in (None, "") or not message_id:
            return []

        try:
            normalized_video_id = int(video_id)
            normalized_chat_id = int(chat_id)
        except (TypeError, ValueError):
            log.warning(
                "Fetch video download URL skipped due to invalid identifiers: video_id=%r chat_id=%r message_id=%r",
                video_id,
                chat_id,
                message_id,
            )
            return []

        request_payloads = self._build_video_download_request_payloads(
            video_id=normalized_video_id,
            chat_id=normalized_chat_id,
            message_id=str(message_id),
            token=token,
        )
        last_payload_preview: Any = None

        for index, request_payload in enumerate(request_payloads, start=1):
            response_payload = await self.cmd(OpCode.VIDEO_DOWNLOAD_URL, request_payload)
            cmd_error = self._extract_cmd_error(response_payload)
            if cmd_error is not None:
                log.warning(
                    "Fetch video download URL attempt=%d failed video_id=%s chat_id=%s message_id=%s: %s",
                    index,
                    normalized_video_id,
                    normalized_chat_id,
                    message_id,
                    cmd_error,
                )
                continue

            urls = self._extract_video_http_urls(response_payload)
            if urls:
                log.info(
                    "Resolved %d video download URL(s) via websocket attempt=%d video_id=%s keys=%s",
                    len(urls),
                    index,
                    normalized_video_id,
                    list(response_payload.keys()) if isinstance(response_payload, dict) else type(response_payload).__name__,
                )
                return urls

            last_payload_preview = list(response_payload.keys()) if isinstance(response_payload, dict) else type(response_payload).__name__
            log.info(
                "Video download URL attempt=%d returned no MP4 candidate for video_id=%s payload=%s",
                index,
                normalized_video_id,
                last_payload_preview,
            )

        log.warning(
            "Fetch video download URL returned no MP4 URL for video_id=%s chat_id=%s message_id=%s payload=%s",
            normalized_video_id,
            normalized_chat_id,
            message_id,
            last_payload_preview,
        )
        return []

    async def fetch_video_download_url(
        self,
        *,
        video_id: Any,
        chat_id: Any,
        message_id: str,
        token: str | None = None,
    ) -> str | None:
        urls = await self.fetch_video_download_urls(
            video_id=video_id,
            chat_id=chat_id,
            message_id=message_id,
            token=token,
        )
        return urls[0] if urls else None

    async def download_video_attachment(
        self,
        *,
        video_id: Any,
        chat_id: Any,
        message_id: str,
        token: str | None = None,
    ) -> bytes | None:
        if video_id in (None, "") or chat_id in (None, "") or not message_id:
            return None

        try:
            normalized_video_id = int(video_id)
            normalized_chat_id = int(chat_id)
        except (TypeError, ValueError):
            log.warning(
                "Download video attachment skipped due to invalid identifiers: video_id=%r chat_id=%r message_id=%r",
                video_id,
                chat_id,
                message_id,
            )
            return None

        request_payloads = self._build_video_download_request_payloads(
            video_id=normalized_video_id,
            chat_id=normalized_chat_id,
            message_id=str(message_id),
            token=token,
        )
        seen_urls: set[str] = set()
        candidate_index = 0

        for attempt_index, request_payload in enumerate(request_payloads, start=1):
            response_payload = await self.cmd(OpCode.VIDEO_DOWNLOAD_URL, request_payload)
            cmd_error = self._extract_cmd_error(response_payload)
            if cmd_error is not None:
                log.warning(
                    "Download video attachment URL attempt=%d failed video_id=%s chat_id=%s message_id=%s: %s",
                    attempt_index,
                    normalized_video_id,
                    normalized_chat_id,
                    message_id,
                    cmd_error,
                )
                continue

            attempt_urls = []
            for url in self._extract_video_http_urls(response_payload):
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                attempt_urls.append(url)

            if attempt_urls:
                log.info(
                    "Trying %d fresh video URL(s) from websocket attempt=%d video_id=%s keys=%s",
                    len(attempt_urls),
                    attempt_index,
                    normalized_video_id,
                    list(response_payload.keys()) if isinstance(response_payload, dict) else type(response_payload).__name__,
                )
            else:
                log.info(
                    "Video attachment websocket attempt=%d yielded no fresh URL candidates for video_id=%s",
                    attempt_index,
                    normalized_video_id,
                )

            for url in attempt_urls:
                candidate_index += 1
                result = await self.download_file_result(url)
                if self._is_video_download_result(result):
                    log.info(
                        "Downloaded playable video candidate=%d video_id=%s content_type=%s bytes=%d",
                        candidate_index,
                        video_id,
                        result.content_type,
                        len(result.data or b""),
                    )
                    return result.data
                if result.data is not None:
                    if self._is_okru_video_page_download(url, result):
                        resolved_url = await self._resolve_okru_external_video_url(url, result.data)
                        if resolved_url:
                            redirected_result = await self.download_file_result(resolved_url)
                            if self._is_video_download_result(redirected_result):
                                log.info(
                                    "Downloaded playable external video candidate=%d video_id=%s redirected_url=%s content_type=%s bytes=%d",
                                    candidate_index,
                                    video_id,
                                    resolved_url[:120],
                                    redirected_result.content_type,
                                    len(redirected_result.data or b""),
                                )
                                return redirected_result.data
                            if redirected_result.data is not None:
                                log.warning(
                                    "External video redirect for candidate=%d video_id=%s returned non-video payload content_type=%s bytes=%d",
                                    candidate_index,
                                    video_id,
                                    redirected_result.content_type,
                                    len(redirected_result.data),
                                )
                    log.warning(
                        "Video candidate=%d for video_id=%s downloaded non-video payload content_type=%s bytes=%d",
                        candidate_index,
                        video_id,
                        result.content_type,
                        len(result.data),
                    )
        return None

    def _build_video_download_request_payloads(
        self,
        *,
        video_id: int,
        chat_id: int,
        message_id: str,
        token: str | None = None,
    ) -> list[dict[str, Any]]:
        request_payloads: list[dict[str, Any]] = [
            {
                "videoId": video_id,
                "chatId": chat_id,
                "messageId": message_id,
            }
        ]
        if isinstance(token, str) and token:
            request_payloads.append(
                {
                    "videoId": video_id,
                    "chatId": chat_id,
                    "messageId": message_id,
                    "token": token,
                }
            )

        unique_payloads: list[dict[str, Any]] = []
        seen_payloads: set[tuple[tuple[str, Any], ...]] = set()
        for request_payload in request_payloads:
            payload_key = tuple(sorted(request_payload.items()))
            if payload_key in seen_payloads:
                continue
            seen_payloads.add(payload_key)
            unique_payloads.append(request_payload)
        return unique_payloads

    async def download_file_result(self, url: str) -> DownloadResult:
        """Download a file by URL, returning payload and response metadata."""
        if not url:
            return DownloadResult()

        # Use a dedicated session without browser default headers so signed media
        # requests don't inherit Origin/Referer/Sec-Fetch from the websocket session.
        session = aiohttp.ClientSession()

        headers, used_authorization = self._download_headers(url)
        try:
            result, body = await self._download_file_once(
                session,
                url,
                headers,
                used_authorization=used_authorization,
            )
            if result.data is not None:
                log.info(
                    "Downloaded %s (%d bytes, authorized=%s)",
                    url[:120],
                    len(result.data),
                    result.used_authorization,
                )
                return result

            should_retry_anonymously = (
                result.used_authorization
                and result.status in {400, 401, 403}
                and self._is_signed_max_media_url(url)
            )
            if should_retry_anonymously:
                log.info(
                    "Download failed %s - HTTP %s with Authorization; retrying anonymously for signed media URL",
                    url[:120],
                    result.status,
                )
                retry_headers, retry_used_authorization = self._download_headers(url, use_authorization=False)
                retry_result, retry_body = await self._download_file_once(
                    session,
                    url,
                    retry_headers,
                    used_authorization=retry_used_authorization,
                )
                if retry_result.data is not None:
                    log.info(
                        "Downloaded %s (%d bytes, authorized=%s)",
                        url[:120],
                        len(retry_result.data),
                        retry_result.used_authorization,
                    )
                    return retry_result
                log.warning(
                    "Download failed %s - authorized HTTP %s, anonymous HTTP %s: %s",
                    url[:120],
                    result.status,
                    retry_result.status,
                    retry_body or body,
                )
                return retry_result

            log.warning(
                "Download failed %s - HTTP %s (authorized=%s): %s",
                url[:120],
                result.status,
                result.used_authorization,
                body,
            )
            return result
        finally:
            await session.close()

    async def download_file(self, url: str) -> bytes | None:
        """Download a file by URL, returning raw bytes or None on failure."""
        return (await self.download_file_result(url)).data

    def _parse_message(self, payload: dict) -> MaxMessage | None:
        msg_body = payload.get("message")
        if not msg_body or not isinstance(msg_body, dict):
            return None

        msg = MaxMessage(
            chat_id=payload.get("chatId"),
            sender_id=msg_body.get("sender"),
            text=msg_body.get("text", ""),
            timestamp=msg_body.get("time"),
            message_id=str(msg_body.get("id", "")),
            attaches=msg_body.get("attaches") or [],
            link=msg_body.get("link") or {},
            raw=payload,
        )

        if self._my_id and msg.sender_id == self._my_id:
            msg.is_self = True

        return msg

    async def _dispatch_message(self, msg: MaxMessage) -> None:
        if self._message_dispatcher is None:
            self._message_dispatcher = MessageDispatchQueue(
                self._on_message_cb,
                maxsize=self.DISPATCH_QUEUE_SIZE,
                worker_count=self.DISPATCH_WORKERS,
            )
            await self._message_dispatcher.start()
        await self._message_dispatcher.submit(msg)

    async def wait_for_pending_dispatches(self) -> None:
        if self._message_dispatcher is not None:
            await self._message_dispatcher.join()

    async def _shutdown_message_dispatcher(self) -> None:
        if self._message_dispatcher is None:
            return
        await self._message_dispatcher.join()
        await self._message_dispatcher.stop()
        self._message_dispatcher = None

    @staticmethod
    def _dump_json(filename: str, data: dict) -> None:
        path = os.path.join(DEBUG_DIR, filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log.info("Dumped %s (%d bytes)", path, os.path.getsize(path))
        except Exception:
            log.exception("Failed to dump %s", path)

    @staticmethod
    def _extract_upload_token(
        payload: dict,
        *,
        container_keys: tuple[str, ...] = ("files", "file"),
    ) -> str | None:
        direct_token = payload.get("token")
        if isinstance(direct_token, str) and direct_token:
            return direct_token

        for key in container_keys:
            container = payload.get(key)
            if isinstance(container, dict):
                if isinstance(container.get("token"), str) and container.get("token"):
                    return container["token"]
                first_item = next(iter(container.values()), None)
                if isinstance(first_item, dict):
                    nested_token = first_item.get("token")
                    if isinstance(nested_token, str) and nested_token:
                        return nested_token
            if isinstance(container, list):
                first_item = container[0] if container else None
                if isinstance(first_item, dict):
                    nested_token = first_item.get("token")
                    if isinstance(nested_token, str) and nested_token:
                        return nested_token

        nested_file = payload.get("payload")
        if isinstance(nested_file, dict):
            return MaxClient._extract_upload_token(nested_file, container_keys=container_keys)
        return None

    @staticmethod
    def _extract_http_url(payload: Any) -> str | None:
        if isinstance(payload, str) and payload.startswith("http"):
            return payload

        if isinstance(payload, dict):
            for key in ("url", "downloadUrl", "download", "src"):
                value = payload.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
            for value in payload.values():
                nested_url = MaxClient._extract_http_url(value)
                if nested_url:
                    return nested_url
            return None

        if isinstance(payload, list):
            for item in payload:
                nested_url = MaxClient._extract_http_url(item)
                if nested_url:
                    return nested_url

        return None

    @staticmethod
    def _extract_video_http_urls(payload: Any) -> list[str]:
        candidates: list[tuple[int, int, str]] = []
        seen: set[str] = set()

        def _quality_from_key(key: str) -> int:
            best = 0
            for part in key.upper().split("_"):
                if part.isdigit():
                    best = max(best, int(part))
            return best

        def _register_candidate(path: str, url: str) -> None:
            if not isinstance(url, str) or not url.startswith("http") or url in seen:
                return

            path_upper = path.upper()
            if path_upper.startswith("MP4") or ".MP4" in path_upper or "MP4" in path_upper or url.lower().endswith(".mp4"):
                priority = 0
                quality = _quality_from_key(path_upper)
            elif "EXTERNAL" in path_upper:
                priority = 1
                quality = 0
            elif "CACHE" in path_upper:
                priority = 2
                quality = 0
            else:
                return

            seen.add(url)
            candidates.append((priority, -quality, url))

        def _collect(value: Any, *, path: str = "") -> None:
            if isinstance(value, str):
                _register_candidate(path, value)
                return

            if isinstance(value, dict):
                for key, nested in value.items():
                    next_path = f"{path}.{key}" if path else str(key)
                    _collect(nested, path=next_path)
                return

            if isinstance(value, list):
                for index, item in enumerate(value):
                    next_path = f"{path}[{index}]" if path else f"[{index}]"
                    _collect(item, path=next_path)

        _collect(payload)
        candidates.sort()
        return [url for _, _, url in candidates]

    @staticmethod
    def _extract_video_http_url(payload: Any) -> str | None:
        urls = MaxClient._extract_video_http_urls(payload)
        return urls[0] if urls else None

    @staticmethod
    def _is_okru_video_page_download(url: str, result: DownloadResult) -> bool:
        content_type = (result.content_type or "").split(";", 1)[0].strip().lower()
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        return content_type == "text/html" and (host == "ok.ru" or host.endswith(".ok.ru"))

    async def _resolve_okru_external_video_url(self, page_url: str, html_bytes: bytes) -> str | None:
        html_text = html_bytes.decode("utf-8", errors="ignore")
        video_id = self._extract_okru_video_id(page_url)
        video_src = self._extract_okru_video_src(html_bytes)
        if video_src:
            return await self._resolve_redirect_url(video_src)

        player = self._extract_okru_player_data(html_text, video_id=video_id)
        if isinstance(player, dict):
            flashvars = player.get("flashvars")
            if isinstance(flashvars, dict):
                metadata = await self._extract_okru_metadata(video_id or "", flashvars)
                if isinstance(metadata, dict):
                    urls = self._extract_okru_metadata_urls(metadata)
                    if urls:
                        log.info("Resolved %d OK.ru URL(s) directly from mobile page for video_id=%s", len(urls), video_id)
                        return urls[0]

        seed_urls = self._extract_okru_movie_player_urls(html_text, video_id or "") if video_id else []
        fallback_url = await self._resolve_okru_desktop_video_url(page_url, seed_urls=seed_urls)
        if fallback_url:
            return fallback_url

        if not video_src:
            log.warning(
                "Failed to extract OK.ru videoSrc from external page %s (has_data_video=%s has_video_src=%s)",
                page_url[:120],
                "data-video" in html_text,
                "videoSrc" in html_text,
            )
            return None
        return await self._resolve_redirect_url(video_src)

    async def _resolve_okru_desktop_video_url(self, page_url: str, *, seed_urls: list[str] | None = None) -> str | None:
        video_id = self._extract_okru_video_id(page_url)
        if not video_id:
            return None

        page_variants = list(seed_urls or [])
        page_variants.extend([
            f"https://ok.ru/videoembed/{video_id}",
            f"https://ok.ru/video/{video_id}",
            f"https://ok.ru/web-api/video/moviePlayer/{video_id}",
        ])
        seen_pages: set[str] = set()
        last_reason = "webpage missing"
        queue_index = 0
        while queue_index < len(page_variants):
            desktop_url = page_variants[queue_index]
            queue_index += 1
            if desktop_url in seen_pages:
                continue
            seen_pages.add(desktop_url)

            webpage = await self._fetch_text_page(desktop_url)
            if not webpage:
                last_reason = f"fetch failed for {desktop_url}"
                continue

            discovered_urls = self._extract_okru_movie_player_urls(webpage, video_id)
            for nested_url in reversed(discovered_urls):
                if nested_url not in seen_pages and nested_url not in page_variants:
                    page_variants.insert(queue_index, nested_url)

            player = self._extract_okru_player_data(webpage, video_id=video_id)
            if not isinstance(player, dict):
                last_reason = f"player payload missing in {desktop_url}"
                continue

            flashvars = player.get("flashvars")
            if not isinstance(flashvars, dict):
                last_reason = f"flashvars missing in {desktop_url}"
                continue

            metadata = await self._extract_okru_metadata(video_id, flashvars)
            if not isinstance(metadata, dict):
                last_reason = f"metadata missing in {desktop_url}"
                continue

            urls = self._extract_okru_metadata_urls(metadata)
            if urls:
                log.info(
                    "Resolved %d OK.ru desktop metadata URL(s) for video_id=%s via %s",
                    len(urls),
                    video_id,
                    desktop_url,
                )
                return urls[0]
            last_reason = f"metadata URL list empty in {desktop_url}"

        log.warning("Failed to extract OK.ru player payload for video_id=%s (%s)", video_id, last_reason)
        return None

    async def _fetch_text_page(self, url: str, *, headers: dict[str, str] | None = None) -> str | None:
        session = aiohttp.ClientSession()
        try:
            async with session.get(
                url,
                headers=headers or dict(_BROWSER_HEADERS),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    return await resp.text(errors="ignore")
                body = await resp.text(errors="ignore")
                log.warning("Fetch text page failed %s - HTTP %d: %s", url[:120], resp.status, body[:200])
        except Exception:
            log.debug("Fetch text page error for %s", url[:120], exc_info=True)
        finally:
            await session.close()
        return None

    async def _fetch_json_page(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> dict | None:
        session = aiohttp.ClientSession()
        try:
            async with session.post(
                url,
                headers=headers or {"Accept": "application/json", **_BROWSER_HEADERS},
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                body = await resp.text(errors="ignore")
                log.warning("Fetch JSON page failed %s - HTTP %d: %s", url[:120], resp.status, body[:200])
        except Exception:
            log.debug("Fetch JSON page error for %s", url[:120], exc_info=True)
        finally:
            await session.close()
        return None

    @staticmethod
    def _extract_okru_video_id(url: str) -> str | None:
        parsed = urlparse(url)
        path = parsed.path or ""
        for pattern in (r"/video(?:embed)?/(?P<id>[\d-]+)", r"/live/(?P<id>[\d-]+)"):
            match = re.search(pattern, path)
            if match:
                return match.group("id")

        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key in ("st.mvId", "mvId", "videoId", "id"):
            value = query.get(key)
            if value and re.fullmatch(r"[\d-]+", value):
                return value
        return None

    @staticmethod
    def _extract_okru_player_data(webpage: str, video_id: str | None = None) -> dict | None:
        direct_payload = MaxClient._parse_jsonish(webpage)
        discovered_payload = MaxClient._find_okru_player_data(direct_payload, video_id=video_id)
        if discovered_payload:
            return discovered_payload

        candidate_values: list[str] = []
        for attr_name in ("data-options", "data-attributes"):
            attr_value = MaxClient._extract_html_attribute(webpage, attr_name)
            if attr_value:
                candidate_values.append(attr_value)

        regex_match = re.search(
            r"data-options=(?P<quote>[\"'])(?P<payload>.+?)(?P=quote)",
            webpage,
            flags=re.DOTALL,
        )
        if regex_match:
            candidate_values.append(regex_match.group("payload"))
        regex_match = re.search(
            r"data-attributes=(?P<quote>[\"'])(?P<payload>.+?)(?P=quote)",
            webpage,
            flags=re.DOTALL,
        )
        if regex_match:
            candidate_values.append(regex_match.group("payload"))

        for raw_value in candidate_values:
            payload = MaxClient._parse_jsonish(raw_value)
            discovered_payload = MaxClient._find_okru_player_data(payload, video_id=video_id)
            if discovered_payload:
                return discovered_payload

        flashvars = MaxClient._extract_okru_flashvars(webpage)
        if flashvars:
            return {"flashvars": flashvars}

        return None

    @staticmethod
    def _find_okru_player_data(payload: Any, *, video_id: str | None = None, _depth: int = 0) -> dict | None:
        if _depth > 5:
            return None

        if isinstance(payload, dict):
            if MaxClient._looks_like_okru_metadata(payload):
                return {"flashvars": {"metadata": json.dumps(payload, ensure_ascii=False)}}

            flashvars = payload.get("flashvars")
            if isinstance(flashvars, dict):
                return {"flashvars": flashvars}
            if isinstance(flashvars, str):
                parsed_flashvars = MaxClient._parse_jsonish(flashvars)
                if isinstance(parsed_flashvars, dict):
                    return {"flashvars": parsed_flashvars}

            if any(isinstance(payload.get(key), str) and payload.get(key) for key in ("metadata", "metadataUrl")):
                return {"flashvars": {key: payload[key] for key in ("metadata", "metadataUrl", "location", "url") if isinstance(payload.get(key), str) and payload.get(key)}}

            for key in ("player", "moviePlayer", "html", "content", "data", "payload", "result", "widget"):
                if key in payload:
                    discovered = MaxClient._find_okru_player_data(payload[key], video_id=video_id, _depth=_depth + 1)
                    if discovered:
                        return discovered

            for nested in payload.values():
                discovered = MaxClient._find_okru_player_data(nested, video_id=video_id, _depth=_depth + 1)
                if discovered:
                    return discovered
            return None

        if isinstance(payload, list):
            for item in payload:
                discovered = MaxClient._find_okru_player_data(item, video_id=video_id, _depth=_depth + 1)
                if discovered:
                    return discovered
            return None

        if isinstance(payload, str):
            if not payload:
                return None
            if video_id and video_id not in payload and "flashvars" not in payload and "metadataUrl" not in payload and "data-options" not in payload and "data-attributes" not in payload:
                return None
            return MaxClient._extract_okru_player_data(payload, video_id=video_id)

        return None

    @staticmethod
    def _looks_like_okru_metadata(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if isinstance(payload.get("movie"), dict):
            return True
        if isinstance(payload.get("videos"), list):
            return True
        return any(
            isinstance(payload.get(key), str) and payload.get(key)
            for key in (
                "hlsManifestUrl",
                "ondemandHls",
                "ondemandDash",
                "metadataWebmUrl",
                "metadataEmbedded",
                "hlsMasterPlaylistUrl",
                "rtmpUrl",
            )
        )

    @staticmethod
    def _extract_okru_flashvars(text: str) -> dict | None:
        flashvars: dict[str, str] = {}
        for source in (text, html.unescape(text)):
            for field_name in ("metadataUrl", "metadata", "location", "url"):
                field_value = MaxClient._search_jsonish_string_field(source, field_name)
                if isinstance(field_value, str) and field_value:
                    flashvars.setdefault(field_name, field_value)
            if "metadata" in flashvars or "metadataUrl" in flashvars:
                return flashvars
        return None

    @staticmethod
    def _extract_okru_movie_player_urls(text: str, video_id: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        patterns = (
            rf"https?://[^\s\"'<>]+/web-api/video/moviePlayer/{re.escape(video_id)}(?:#[^\s\"'<>]*)?",
            rf"//[^\s\"'<>]+/web-api/video/moviePlayer/{re.escape(video_id)}(?:#[^\s\"'<>]*)?",
            rf"(?<![:\w.])/web-api/video/moviePlayer/{re.escape(video_id)}(?:#[^\s\"'<>]*)?",
        )

        for source in (text, html.unescape(text)):
            normalized_source = source.replace("\\/", "/")
            for pattern in patterns:
                for match in re.finditer(pattern, normalized_source):
                    url = match.group(0).split("#", 1)[0]
                    if url.startswith("//"):
                        url = f"https:{url}"
                    elif url.startswith("/"):
                        url = f"https://ok.ru{url}"
                    if url.startswith("http") and url not in seen:
                        seen.add(url)
                        candidates.append(url)

        return candidates

    async def _extract_okru_metadata(self, video_id: str, flashvars: dict) -> dict | None:
        metadata = flashvars.get("metadata")
        if isinstance(metadata, dict):
            return metadata
        if isinstance(metadata, str) and metadata:
            payload = self._parse_jsonish(metadata)
            if isinstance(payload, dict):
                return payload

        metadata_url = flashvars.get("metadataUrl")
        if not isinstance(metadata_url, str) or not metadata_url:
            return None

        request_url = html.unescape(unquote(metadata_url))
        if request_url.startswith("/"):
            request_url = f"https://ok.ru{request_url}"
        elif not request_url.startswith("http"):
            request_url = f"https://ok.ru/{request_url.lstrip('/')}"

        data: dict[str, str] | None = None
        location = flashvars.get("location")
        if isinstance(location, str) and location:
            data = {"st.location": location}

        headers = {
            **_BROWSER_HEADERS,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "https://ok.ru",
            "Referer": f"https://ok.ru/video/{video_id}",
        }
        return await self._fetch_json_page(request_url, headers=headers, data=data)

    @staticmethod
    def _extract_okru_metadata_urls(metadata: dict) -> list[str]:
        candidates: list[tuple[int, int, str]] = []
        seen: set[str] = set()

        def _quality_from_text(value: Any) -> int:
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                numbers = [int(part) for part in re.findall(r"\d+", value)]
                if numbers:
                    return max(numbers)
                mapping = {
                    "ultrahd": 2160,
                    "quad": 1440,
                    "fullhd": 1080,
                    "hd": 720,
                    "sd": 480,
                    "low": 360,
                    "mobile": 240,
                }
                lowered = value.lower()
                for label, quality in mapping.items():
                    if label in lowered:
                        return quality
            return 0

        videos = metadata.get("videos")
        if isinstance(videos, list):
            for index, item in enumerate(videos):
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                if not isinstance(url, str) or not url.startswith("http") or url in seen:
                    continue
                quality = max(
                    _quality_from_text(item.get("name")),
                    _quality_from_text(item.get("quality")),
                    _quality_from_text(item.get("type")),
                )
                seen.add(url)
                candidates.append((-quality, index, url))

        for key in ("videoSrc", "videoUrl"):
            url = metadata.get(key)
            if isinstance(url, str) and url.startswith("http") and url not in seen:
                seen.add(url)
                candidates.append((0, len(candidates), url))

        candidates.sort()
        return [url for _, _, url in candidates]

    @staticmethod
    def _extract_okru_video_src(html_bytes: bytes) -> str | None:
        html_text = html_bytes.decode("utf-8", errors="ignore")

        for text in (html_text, html.unescape(html_text)):
            attr_value = MaxClient._extract_html_attribute(text, "data-video")
            if attr_value:
                payload = MaxClient._parse_jsonish(attr_value)

                video_src = payload.get("videoSrc") if isinstance(payload, dict) else None
                if isinstance(video_src, str) and video_src.startswith("http"):
                    return video_src

            direct_video_src = MaxClient._search_video_src_in_text(text)
            if direct_video_src:
                return direct_video_src

        return None

    @staticmethod
    def _parse_jsonish(raw_value: str) -> Any:
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(value: str | None) -> None:
            if not isinstance(value, str) or value in seen:
                return
            seen.add(value)
            candidates.append(value)

        _add(raw_value)
        _add(html.unescape(raw_value))

        for value in list(candidates):
            normalized = value
            for _ in range(3):
                collapsed = normalized.replace("\\\\\"", "\\\"").replace("\\\\'", "\\'")
                if collapsed == normalized:
                    break
                normalized = collapsed
                _add(normalized)

            if "\\u" in value or "\\x" in value or "\\\\" in value:
                try:
                    _add(bytes(value, "utf-8").decode("unicode_escape"))
                except Exception:
                    pass

        for candidate in candidates:
            try:
                return json.loads(candidate)
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_html_attribute(text: str, attr_name: str) -> str | None:
        for quote in ('"', "'"):
            marker = f"{attr_name}={quote}"
            start = text.find(marker)
            if start < 0:
                continue

            index = start + len(marker)
            value_chars: list[str] = []
            escaped = False
            while index < len(text):
                char = text[index]
                if char == quote and not escaped:
                    return "".join(value_chars)
                if char == "\\" and not escaped:
                    escaped = True
                    value_chars.append(char)
                    index += 1
                    continue
                escaped = False
                value_chars.append(char)
                index += 1

        return None

    @staticmethod
    def _search_video_src_in_text(text: str) -> str | None:
        patterns = (
            r'"videoSrc"\s*:\s*"(?P<url>(?:\\.|[^"])*)"',
            r"'videoSrc'\s*:\s*'(?P<url>(?:\\.|[^'])*)'",
            r'videoSrc\s*:\s*"(?P<url>(?:\\.|[^"])*)"',
            r'videoSrc\s*:\s*\'(?P<url>(?:\\.|[^\'])*)\'',
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.DOTALL)
            if not match:
                continue

            raw_url = match.group("url")
            try:
                decoded = json.loads(f'"{raw_url}"')
            except Exception:
                decoded = raw_url.replace("\\/", "/")

            if isinstance(decoded, str) and decoded.startswith("http"):
                return decoded

        return None

    @staticmethod
    def _search_jsonish_string_field(text: str, field_name: str) -> str | None:
        escaped_field = re.escape(field_name)
        patterns = (
            rf'"{escaped_field}"\s*:\s*"(?P<value>(?:\\.|[^"])*)"',
            rf"'{escaped_field}'\s*:\s*'(?P<value>(?:\\.|[^'])*)'",
            rf"{escaped_field}\s*:\s*\"(?P<value>(?:\\.|[^\"])*)\"",
            rf"{escaped_field}\s*:\s*'(?P<value>(?:\\.|[^'])*)'",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.DOTALL)
            if not match:
                continue
            raw_value = match.group("value")
            try:
                decoded = json.loads(f'"{raw_value}"')
            except Exception:
                decoded = html.unescape(raw_value).replace("\\/", "/")
            if isinstance(decoded, str) and decoded:
                return decoded
        return None

    async def _resolve_redirect_url(self, url: str) -> str | None:
        headers = dict(_SIGNED_MEDIA_HEADERS)
        session = aiohttp.ClientSession()
        try:
            try:
                async with session.head(
                    url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 400 and str(resp.url).startswith("http"):
                        return str(resp.url)
            except Exception:
                log.debug("HEAD redirect resolution failed for %s", url[:120], exc_info=True)

            try:
                async with session.get(
                    url,
                    headers=headers,
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 400 and str(resp.url).startswith("http"):
                        return str(resp.url)
            except Exception:
                log.debug("GET redirect resolution failed for %s", url[:120], exc_info=True)
        finally:
            await session.close()
        return None

    @staticmethod
    def _is_video_download_result(result: DownloadResult) -> bool:
        data = result.data or b""
        content_type = (result.content_type or "").split(";", 1)[0].strip().lower()
        if content_type.startswith("video/"):
            return True

        # MP4/MOV place the file type marker after the 32-bit size field.
        if len(data) >= 12 and data[4:8] == b"ftyp":
            return True

        # WebM / Matroska EBML header.
        if data.startswith(b"\x1a\x45\xdf\xa3"):
            return True

        return False

    @staticmethod
    def _wrap_cmd_error(payload: Any) -> dict:
        return {
            _CMD_ERROR_MARKER: True,
            "payload": payload if isinstance(payload, dict) else {"raw": payload},
        }

    @staticmethod
    def _extract_cmd_error(payload: Any) -> dict | None:
        if not isinstance(payload, dict) or payload.get(_CMD_ERROR_MARKER) is not True:
            return None
        error_payload = payload.get("payload")
        if isinstance(error_payload, dict):
            return error_payload
        return {"raw": error_payload}

    @staticmethod
    def _is_attachment_not_ready_error(payload: dict) -> bool:
        codes: list[str] = []
        for key in ("code", "error_code"):
            value = payload.get(key)
            if value is not None:
                codes.append(str(value).lower())

        nested_error = payload.get("error")
        if isinstance(nested_error, dict):
            for key in ("code", "error_code"):
                value = nested_error.get(key)
                if value is not None:
                    codes.append(str(value).lower())

        return _ATTACHMENT_NOT_READY_CODE in codes

    @staticmethod
    def _normalize_document_attach(payload: dict, *, filename: str) -> dict:
        if not isinstance(payload, dict):
            return {}

        if payload.get("_type") == "FILE" and isinstance(payload.get("fileToken"), str) and payload.get("fileToken"):
            attach = dict(payload)
            attach.setdefault("name", filename)
            return attach

        if isinstance(payload.get("fileToken"), str) and payload.get("fileToken"):
            return {
                "_type": "FILE",
                "fileToken": payload["fileToken"],
                "name": payload.get("name") or filename,
            }

        nested_payload = payload.get("payload")
        if isinstance(nested_payload, dict):
            attach = MaxClient._normalize_document_attach(nested_payload, filename=filename)
            if attach:
                return attach

        token = MaxClient._extract_upload_token(payload, container_keys=("files", "file", "attachments"))
        if not token:
            return {}

        attach_name = filename
        for key in ("name", "filename", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                attach_name = value
                break

        return {
            "_type": "FILE",
            "fileToken": token,
            "name": attach_name,
        }

    @staticmethod
    def extract_sent_message_id(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None

        message_payload = payload.get("message")
        candidates: list[Any] = []
        if isinstance(message_payload, dict):
            candidates.extend(
                (
                    message_payload.get("id"),
                    message_payload.get("mid"),
                    message_payload.get("messageId"),
                )
            )
        candidates.extend((payload.get("id"), payload.get("mid"), payload.get("messageId")))

        for candidate in candidates:
            if candidate is not None and str(candidate):
                return str(candidate)
        return None
