import asyncio
import json
import logging
import mimetypes
import os
import random
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

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

_PLATFORM_API_BASE_URL = "https://platform-api.max.ru"
_CMD_ERROR_MARKER = "__cmd_error__"
_ATTACHMENT_NOT_READY_CODE = "attachment.not.ready"
_DOCUMENT_READY_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)


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

    async def download_file(self, url: str) -> bytes | None:
        """Download a file by URL, returning raw bytes or None on failure."""
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True
        try:
            async with session.get(
                url,
                headers=_HTTP_HEADERS,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    log.info("Downloaded %s (%d bytes)", url[:120], len(data))
                    return data
                log.warning("Download failed %s - HTTP %d", url[:120], resp.status)
        except Exception:
            log.exception("Download error: %s", url[:120])
        finally:
            if close_after:
                await session.close()
        return None

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
