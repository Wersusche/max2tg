from __future__ import annotations

import logging
from html import escape
from typing import Any

from telegram.error import BadRequest

from app.max_client import MaxClient, MaxMessage
from app.relay_client import RelayClient
from app.relay_models import RelayOperationBuilder
from app.resolver import ContactResolver
from app.topic_router import TopicRouter

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _header(msg: MaxMessage, sender_label: str, chat_label: str, is_dm: bool) -> str:
    if is_dm:
        return f"✉ <b>{sender_label}</b>"
    return f"💬 <b>{chat_label}</b> | {sender_label}"


def _extract_photo_url(attach: dict) -> str | None:
    return attach.get("baseUrl") or attach.get("url")


def _extract_file_url(attach: dict) -> str | None:
    url = attach.get("url")
    if url and url.startswith("http"):
        return url
    return None


def _guess_media_kind(filename: str) -> str:
    name_lower = filename.lower()
    for ext in PHOTO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "photo"
    for ext in VIDEO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "video"
    return "document"


async def _send_attach(
    attach: dict,
    client: MaxClient,
    sender: Any,
    header_text: str,
    *,
    message_thread_id: int | None = None,
    raise_bad_request: bool = False,
) -> bool:
    atype = attach.get("_type", "")
    log.info("Processing attach _type=%s keys=%s", atype, list(attach.keys()))

    if atype in {"CONTROL", "WIDGET", "INLINE_KEYBOARD"}:
        return False

    if atype == "PHOTO":
        url = _extract_photo_url(attach)
        if not url:
            log.warning("PHOTO attach has no URL: %s", attach)
            return False
        data = await client.download_file(url)
        if data:
            await sender.send_photo(
                data,
                caption=header_text,
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )
            return True
        await sender.send(
            f"{header_text}\n<i>[фото — не удалось загрузить]</i>",
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return True

    if atype == "VIDEO":
        thumb = attach.get("thumbnail")
        if thumb:
            data = await client.download_file(thumb)
            if data:
                await sender.send_photo(
                    data,
                    caption=f"{header_text}\n<i>[видео — превью]</i>",
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_bad_request,
                )
                return True
        await sender.send(
            f"{header_text}\n<i>[видео]</i>",
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return True

    if atype == "FILE":
        name = attach.get("name", "file")
        size = attach.get("size", 0)
        token_url = _extract_file_url(attach)
        if token_url:
            data = await client.download_file(token_url)
            if data:
                kind = _guess_media_kind(name)
                if kind == "photo":
                    await sender.send_photo(
                        data,
                        caption=header_text,
                        filename=name,
                        message_thread_id=message_thread_id,
                        raise_bad_request=raise_bad_request,
                    )
                elif kind == "video":
                    await sender.send_video(
                        data,
                        caption=header_text,
                        filename=name,
                        message_thread_id=message_thread_id,
                        raise_bad_request=raise_bad_request,
                    )
                else:
                    await sender.send_document(
                        data,
                        caption=header_text,
                        filename=name,
                        message_thread_id=message_thread_id,
                        raise_bad_request=raise_bad_request,
                    )
                return True
        size_str = f" ({_human_size(size)})" if size else ""
        await sender.send(
            f"{header_text}\n📎 <b>{escape(name)}</b>{size_str}",
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return True

    if atype == "AUDIO":
        url = attach.get("url")
        if url:
            data = await client.download_file(url)
            if data:
                await sender.send_voice(
                    data,
                    caption=header_text,
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_bad_request,
                )
                return True
        await sender.send(
            f"{header_text}\n<i>[аудио]</i>",
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return True

    if atype == "STICKER":
        url = attach.get("url")
        if url:
            data = await client.download_file(url)
            if data:
                await sender.send_sticker(
                    data,
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_bad_request,
                )
                return True
        await sender.send(
            f"{header_text}\n<i>[стикер]</i>",
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return True

    if atype == "SHARE":
        share_url = attach.get("url", "")
        title = attach.get("title", "")
        desc = attach.get("description", "")
        parts = [header_text]
        if title:
            parts.append(f"🔗 <b>{escape(title)}</b>")
        if share_url:
            parts.append(escape(share_url))
        if desc:
            parts.append(f"<i>{escape(desc[:200])}</i>")
        await sender.send(
            "\n".join(parts),
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return True

    if atype == "LOCATION":
        lat = attach.get("lat") or attach.get("latitude")
        lon = attach.get("lon") or attach.get("lng") or attach.get("longitude")
        if lat and lon:
            await sender.send(
                f"{header_text}\n📍 {lat}, {lon}",
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )
        else:
            await sender.send(
                f"{header_text}\n<i>[геолокация]</i>",
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )
        return True

    if atype == "CONTACT":
        name = attach.get("name", "")
        phone = attach.get("phone", "")
        text = f"{header_text}\n👤 {escape(name)}"
        if phone:
            text += f" — {escape(phone)}"
        await sender.send(
            text,
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return True

    log.info("Unknown attach type %s, sending as info", atype)
    await sender.send(
        f"{header_text}\n<i>[вложение: {escape(atype or 'unknown')}]</i>",
        message_thread_id=message_thread_id,
        raise_bad_request=raise_bad_request,
    )
    return True


async def _handle_linked_message(
    link: dict,
    link_type: str,
    header_text: str,
    client: MaxClient,
    sender: Any,
    resolver: ContactResolver,
    *,
    message_thread_id: int | None = None,
    raise_bad_request: bool = False,
) -> None:
    inner = link.get("message") or link
    fwd_sender_id = inner.get("sender") or link.get("sender")
    fwd_text = inner.get("text", "") or link.get("text", "")
    fwd_attaches = inner.get("attaches") or link.get("attaches") or []

    fwd_sender_label = ""
    if fwd_sender_id:
        fwd_sender_label = escape(await resolver.resolve_user(fwd_sender_id))

    if link_type == "FORWARD":
        prefix = "↩️ <b>Переслано</b>"
        if fwd_sender_label:
            prefix = f"↩️ <b>Переслано от {fwd_sender_label}</b>"
    else:
        prefix = "↩ <b>Ответ</b>"
        if fwd_sender_label:
            prefix = f"↩ <b>Ответ на {fwd_sender_label}</b>"

    full_header = f"{header_text}\n{prefix}"
    fwd_meaningful = [
        a
        for a in fwd_attaches
        if isinstance(a, dict) and a.get("_type") not in {"CONTROL", "WIDGET", "INLINE_KEYBOARD", None}
    ]

    if fwd_meaningful:
        text_sent = False
        for index, attach in enumerate(fwd_meaningful):
            if index == 0 and fwd_text:
                cap = f"{full_header}\n{escape(fwd_text)}"
                text_sent = True
            else:
                cap = full_header
            await _send_attach(
                attach,
                client,
                sender,
                cap,
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )

        if fwd_text and not text_sent:
            await sender.send(
                f"{full_header}\n{escape(fwd_text)}",
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )
        return

    if fwd_text:
        await sender.send(
            f"{full_header}\n{escape(fwd_text)}",
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return

    await sender.send(
        f"{full_header}\n<i>[без содержимого]</i>",
        message_thread_id=message_thread_id,
        raise_bad_request=raise_bad_request,
    )


def _human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def _is_missing_topic_error(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return (
        "message thread" in text
        and ("not found" in text or "invalid" in text or "topic" in text)
    ) or ("topic" in text and ("not found" in text or "deleted" in text))


async def forward_max_message(
    msg: MaxMessage,
    *,
    client: MaxClient,
    sender: Any,
    resolver: ContactResolver,
    topic_router: TopicRouter | None = None,
    relay_client: RelayClient | None = None,
) -> None:
    log.info(
        "New message: chat=%s sender=%s is_self=%s text=%r attaches=%d",
        msg.chat_id,
        msg.sender_id,
        msg.is_self,
        (msg.text[:80] + "…") if len(msg.text) > 80 else msg.text,
        len(msg.attaches),
    )

    if msg.is_self:
        return

    sender_name = await resolver.resolve_user(msg.sender_id)
    is_dm = resolver.is_dm(msg.chat_id)
    chat_name = resolver.chat_name(msg.chat_id)
    sender_label = escape(sender_name)
    chat_label = escape(chat_name)
    header_text = _header(msg, sender_label, chat_label, is_dm)
    topic_name = sender_name if is_dm else chat_name
    raise_topic_errors = topic_router is not None
    operation_builder = RelayOperationBuilder() if relay_client is not None else None
    dispatch_sender = operation_builder if operation_builder is not None else sender

    async def forward_to_telegram(message_thread_id: int | None) -> None:
        link = msg.link
        link_type = link.get("type") if isinstance(link, dict) else None

        if link_type in ("FORWARD", "REPLY"):
            await _handle_linked_message(
                link,
                link_type,
                header_text,
                client,
                dispatch_sender,
                resolver,
                message_thread_id=message_thread_id,
                raise_bad_request=raise_topic_errors,
            )
            if msg.text:
                await dispatch_sender.send(
                    f"{header_text}\n{escape(msg.text)}",
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_topic_errors,
                )
            log.info("Forwarded link type=%s → TG", link_type)
            return

        meaningful_attaches = [
            a
            for a in msg.attaches
            if isinstance(a, dict) and a.get("_type") not in {"CONTROL", "WIDGET", "INLINE_KEYBOARD", None}
        ]

        if meaningful_attaches:
            text_sent = False
            for index, attach in enumerate(meaningful_attaches):
                if index == 0 and msg.text:
                    cap = f"{header_text}\n{escape(msg.text)}"
                    text_sent = True
                else:
                    cap = header_text
                await _send_attach(
                    attach,
                    client,
                    dispatch_sender,
                    cap,
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_topic_errors,
                )
                log.info("Forwarded attach _type=%s → TG", attach.get("_type"))

            if msg.text and not text_sent:
                await dispatch_sender.send(
                    f"{header_text}\n{escape(msg.text)}",
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_topic_errors,
                )
            return

        body = escape(msg.text) if msg.text else "<i>[нетекстовое сообщение]</i>"
        await dispatch_sender.send(
            f"{header_text}\n{body}",
            message_thread_id=message_thread_id,
            raise_bad_request=raise_topic_errors,
        )
        log.info("Forwarded text → TG")

    if relay_client is not None:
        await forward_to_telegram(None)
        if operation_builder is None or operation_builder.is_empty:
            return
        await relay_client.send_batch(
            operation_builder.build_batch(msg.chat_id, topic_name),
            operation_builder.attachments,
        )
        return

    if topic_router is None:
        await forward_to_telegram(None)
        return

    thread_id = await topic_router.ensure_topic(msg.chat_id, topic_name)
    try:
        await forward_to_telegram(thread_id)
    except BadRequest as exc:
        if not _is_missing_topic_error(exc):
            raise
        log.warning(
            "Telegram topic thread=%s for Max chat %s looks stale, recreating",
            thread_id,
            msg.chat_id,
        )
        topic_router.forget_max_chat(msg.chat_id)
        new_thread_id = await topic_router.ensure_topic(msg.chat_id, topic_name)
        await forward_to_telegram(new_thread_id)
