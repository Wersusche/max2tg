from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from telegram.error import BadRequest

from app.command_store import CommandStore
from app.relay_client import SECRET_HEADER
from app.relay_models import RelayOperation, TelegramBatch
from app.tg_sender import TelegramSender
from app.topic_router import TopicRouter

log = logging.getLogger(__name__)


def _is_missing_topic_error(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return (
        "message thread" in text
        and ("not found" in text or "invalid" in text or "topic" in text)
    ) or ("topic" in text and ("not found" in text or "deleted" in text))


class RelayBatchProcessor:
    def __init__(self, sender: TelegramSender, topic_router: TopicRouter):
        self.sender = sender
        self.topic_router = topic_router

    async def process_batch(
        self,
        batch: TelegramBatch,
        attachments: dict[str, bytes] | None = None,
    ) -> None:
        attachments = attachments or {}
        if not batch.topic_name:
            await self._send_operations(batch.operations, None, attachments)
            return

        thread_id = await self.topic_router.ensure_topic(batch.max_chat_id, batch.topic_name)
        try:
            await self._send_operations(batch.operations, thread_id, attachments)
        except BadRequest as exc:
            if not _is_missing_topic_error(exc):
                raise
            log.warning(
                "Telegram topic thread=%s for Max chat %s looks stale, recreating",
                thread_id,
                batch.max_chat_id,
            )
            self.topic_router.forget_max_chat(batch.max_chat_id)
            new_thread_id = await self.topic_router.ensure_topic(batch.max_chat_id, batch.topic_name)
            await self._send_operations(batch.operations, new_thread_id, attachments)

    async def _send_operations(
        self,
        operations: list[RelayOperation],
        message_thread_id: int | None,
        attachments: dict[str, bytes],
    ) -> None:
        for operation in operations:
            await self._send_operation(operation, message_thread_id, attachments)

    async def _send_operation(
        self,
        operation: RelayOperation,
        message_thread_id: int | None,
        attachments: dict[str, bytes],
    ) -> None:
        if operation.kind == "text":
            await self.sender.send(operation.text, message_thread_id=message_thread_id, raise_bad_request=True)
            return

        attachment = attachments.get(operation.attachment_field or "")
        if attachment is None:
            raise RuntimeError(f"Attachment part {operation.attachment_field!r} is missing")

        kwargs = {
            "message_thread_id": message_thread_id,
            "raise_bad_request": True,
        }
        if operation.kind == "photo":
            await self.sender.send_photo(attachment, caption=operation.text, filename=operation.filename or "photo.jpg", **kwargs)
            return
        if operation.kind == "document":
            await self.sender.send_document(attachment, caption=operation.text, filename=operation.filename or "file", **kwargs)
            return
        if operation.kind == "video":
            await self.sender.send_video(attachment, caption=operation.text, filename=operation.filename or "video.mp4", **kwargs)
            return
        if operation.kind == "voice":
            await self.sender.send_voice(attachment, caption=operation.text, **kwargs)
            return
        if operation.kind == "sticker":
            await self.sender.send_sticker(attachment, **kwargs)
            return
        raise RuntimeError(f"Unsupported relay operation kind: {operation.kind}")


PROCESSOR_KEY = web.AppKey("processor", RelayBatchProcessor)
COMMAND_STORE_KEY = web.AppKey("command_store", CommandStore)
SHARED_SECRET_KEY = web.AppKey("shared_secret", str)


def create_relay_app(
    processor: RelayBatchProcessor,
    command_store: CommandStore,
    shared_secret: str,
) -> web.Application:
    app = web.Application()
    app[PROCESSOR_KEY] = processor
    app[COMMAND_STORE_KEY] = command_store
    app[SHARED_SECRET_KEY] = shared_secret

    app.router.add_get("/healthz", _healthz)
    app.router.add_post("/internal/telegram-batch", _telegram_batch)
    app.router.add_get("/internal/max-commands/pull", _pull_max_command)
    app.router.add_post("/internal/max-commands/{command_id}/ack", _ack_max_command)
    return app


async def _healthz(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _telegram_batch(request: web.Request) -> web.Response:
    _authorize(request)
    processor = request.app[PROCESSOR_KEY]

    if request.content_type.startswith("multipart/"):
        payload, attachments = await _read_multipart_batch(request)
    else:
        payload = await request.json()
        attachments = {}

    batch = TelegramBatch.from_dict(payload)
    await processor.process_batch(batch, attachments)
    return web.json_response({"ok": True})


async def _pull_max_command(request: web.Request) -> web.Response:
    _authorize(request)
    command_store = request.app[COMMAND_STORE_KEY]
    timeout = _float_query(request, "timeout", 30.0)
    command = await command_store.wait_for_command(timeout)
    if command is None:
        return web.Response(status=204)
    return web.json_response(command.to_dict())


async def _ack_max_command(request: web.Request) -> web.Response:
    _authorize(request)
    command_store = request.app[COMMAND_STORE_KEY]
    command_store.ack(int(request.match_info["command_id"]))
    return web.json_response({"ok": True})


def _authorize(request: web.Request) -> None:
    expected = request.app[SHARED_SECRET_KEY]
    actual = request.headers.get(SECRET_HEADER)
    if actual != expected:
        raise web.HTTPUnauthorized(text="invalid relay secret")


async def _read_multipart_batch(request: web.Request) -> tuple[dict[str, Any], dict[str, bytes]]:
    reader = await request.multipart()
    payload: dict[str, Any] | None = None
    attachments: dict[str, bytes] = {}
    async for part in reader:
        if part.name == "batch":
            payload = json.loads(await part.text())
            continue
        attachments[str(part.name)] = await part.read(decode=False)
    if payload is None:
        raise web.HTTPBadRequest(text="missing batch field")
    return payload, attachments


def _float_query(request: web.Request, name: str, default: float) -> float:
    raw = request.query.get(name)
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"{name} must be a number") from exc
