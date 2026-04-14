from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from telegram.error import BadRequest

from app.command_store import CommandStore
from app.message_store import MessageStore
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
    def __init__(self, sender: TelegramSender, topic_router: TopicRouter, message_store: MessageStore):
        self.sender = sender
        self.topic_router = topic_router
        self.message_store = message_store

    async def process_batch(
        self,
        batch: TelegramBatch,
        attachments: dict[str, bytes] | None = None,
    ) -> None:
        attachments = attachments or {}
        if not batch.topic_name:
            mapped_message_id = await self._send_operations(
                batch.operations,
                None,
                attachments,
                reply_to_message_id=batch.reply_to_message_id,
                mapping_operation_index=batch.mapping_operation_index,
            )
            self._store_mapping(batch, None, mapped_message_id)
            return

        thread_id = await self.topic_router.ensure_topic(batch.max_chat_id, batch.topic_name)
        try:
            mapped_message_id = await self._send_operations(
                batch.operations,
                thread_id,
                attachments,
                reply_to_message_id=batch.reply_to_message_id,
                mapping_operation_index=batch.mapping_operation_index,
            )
            self._store_mapping(batch, thread_id, mapped_message_id)
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
            mapped_message_id = await self._send_operations(
                batch.operations,
                new_thread_id,
                attachments,
                reply_to_message_id=batch.reply_to_message_id,
                mapping_operation_index=batch.mapping_operation_index,
            )
            self._store_mapping(batch, new_thread_id, mapped_message_id)

    async def _send_operations(
        self,
        operations: list[RelayOperation],
        message_thread_id: int | None,
        attachments: dict[str, bytes],
        *,
        reply_to_message_id: int | None = None,
        mapping_operation_index: int | None = None,
    ) -> int | None:
        mapped_message_id: int | None = None
        for index, operation in enumerate(operations):
            result = await self._send_operation(
                operation,
                message_thread_id,
                attachments,
                reply_to_message_id=reply_to_message_id,
            )
            if mapping_operation_index is None:
                if mapped_message_id is None:
                    mapped_message_id = _extract_message_id(result)
                continue
            if index == mapping_operation_index:
                mapped_message_id = _extract_message_id(result)
        return mapped_message_id

    async def _send_operation(
        self,
        operation: RelayOperation,
        message_thread_id: int | None,
        attachments: dict[str, bytes],
        *,
        reply_to_message_id: int | None = None,
    ):
        if operation.kind == "text":
            return await self.sender.send(
                operation.text,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                raise_bad_request=True,
            )

        attachment = attachments.get(operation.attachment_field or "")
        if attachment is None:
            raise RuntimeError(f"Attachment part {operation.attachment_field!r} is missing")

        kwargs = {
            "message_thread_id": message_thread_id,
            "reply_to_message_id": reply_to_message_id,
            "raise_bad_request": True,
        }
        if operation.kind == "photo":
            return await self.sender.send_photo(
                attachment,
                caption=operation.text,
                filename=operation.filename or "photo.jpg",
                **kwargs,
            )
        if operation.kind == "document":
            return await self.sender.send_document(
                attachment,
                caption=operation.text,
                filename=operation.filename or "file",
                **kwargs,
            )
        if operation.kind == "video":
            return await self.sender.send_video(
                attachment,
                caption=operation.text,
                filename=operation.filename or "video.mp4",
                **kwargs,
            )
        if operation.kind == "voice":
            return await self.sender.send_voice(attachment, caption=operation.text, **kwargs)
        if operation.kind == "sticker":
            return await self.sender.send_sticker(attachment, **kwargs)
        raise RuntimeError(f"Unsupported relay operation kind: {operation.kind}")

    def _store_mapping(
        self,
        batch: TelegramBatch,
        message_thread_id: int | None,
        tg_message_id: int | None,
    ) -> None:
        if batch.max_message_id is None or tg_message_id is None or batch.max_chat_id == "__system__":
            return
        self.message_store.upsert_mapping(
            tg_chat_id=int(self.sender.chat_id),
            max_chat_id=batch.max_chat_id,
            max_message_id=batch.max_message_id,
            tg_message_id=tg_message_id,
            message_thread_id=message_thread_id,
            direction="max_to_tg",
            source="max",
        )


PROCESSOR_KEY = web.AppKey("processor", RelayBatchProcessor)
COMMAND_STORE_KEY = web.AppKey("command_store", CommandStore)
MESSAGE_STORE_KEY = web.AppKey("message_store", MessageStore)
SHARED_SECRET_KEY = web.AppKey("shared_secret", str)


def create_relay_app(
    processor: RelayBatchProcessor,
    command_store: CommandStore,
    message_store: MessageStore,
    shared_secret: str,
) -> web.Application:
    app = web.Application()
    app[PROCESSOR_KEY] = processor
    app[COMMAND_STORE_KEY] = command_store
    app[MESSAGE_STORE_KEY] = message_store
    app[SHARED_SECRET_KEY] = shared_secret

    app.router.add_get("/healthz", _healthz)
    app.router.add_post("/internal/telegram-batch", _telegram_batch)
    app.router.add_get("/internal/max-commands/pull", _pull_max_command)
    app.router.add_post("/internal/max-commands/{command_id}/ack", _ack_max_command)
    app.router.add_get("/internal/message-mappings/lookup", _lookup_message_mapping)
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


async def _lookup_message_mapping(request: web.Request) -> web.Response:
    _authorize(request)
    message_store = request.app[MESSAGE_STORE_KEY]
    max_chat_id = request.query.get("max_chat_id")
    max_message_id = request.query.get("max_message_id")
    if not max_chat_id or not max_message_id:
        raise web.HTTPBadRequest(text="max_chat_id and max_message_id are required")

    mapping = message_store.get_by_max_message(
        max_chat_id=max_chat_id,
        max_message_id=max_message_id,
    )
    if mapping is None:
        raise web.HTTPNotFound(text="message mapping not found")
    return web.json_response(
        {
            "tg_chat_id": mapping.tg_chat_id,
            "tg_message_id": mapping.tg_message_id,
            "message_thread_id": mapping.message_thread_id,
        }
    )


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


def _extract_message_id(result) -> int | None:
    if result is None:
        return None
    raw_message_id = getattr(result, "message_id", None)
    if raw_message_id is None and isinstance(result, dict):
        raw_message_id = result.get("message_id")
    if raw_message_id is None:
        return None
    try:
        return int(raw_message_id)
    except (TypeError, ValueError):
        return None
