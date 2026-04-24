from __future__ import annotations

import logging
from html import escape
from typing import Any

from telegram.error import BadRequest

from app.max_client import MaxClient, MaxMessage
from app.message_store import MessageStore
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


def _first_http_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("http"):
        return value

    if isinstance(value, dict):
        for key in (
            "baseUrl",
            "url",
            "downloadUrl",
            "download",
            "playback",
            "stream",
            "play",
            "ogg",
            "m4a",
            "mp3",
            "src",
        ):
            nested = value.get(key)
            if isinstance(nested, str) and nested.startswith("http"):
                return nested
        for nested in value.values():
            result = _first_http_url(nested)
            if result:
                return result

    if isinstance(value, list):
        for nested in value:
            result = _first_http_url(nested)
            if result:
                return result

    return None


def _extract_attach_url(attach: dict) -> str | None:
    return _first_http_url(
        {
            "baseUrl": attach.get("baseUrl"),
            "url": attach.get("url"),
            "downloadUrl": attach.get("downloadUrl"),
            "urls": attach.get("urls"),
        }
    )


async def _download_audio_data(attach: dict, client: MaxClient) -> bytes | None:
    url = _extract_attach_url(attach)
    result = await client.download_file_result(url) if url else None
    return result.data if result else None


def _guess_media_kind(filename: str) -> str:
    name_lower = filename.lower()
    for ext in PHOTO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "photo"
    for ext in VIDEO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "video"
    return "document"


def _extract_rest_message_attachments(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []

    body = payload.get("body")
    if isinstance(body, dict):
        attachments = body.get("attachments")
        if isinstance(attachments, list):
            return [attach for attach in attachments if isinstance(attach, dict)]

    message = payload.get("message")
    if isinstance(message, dict):
        nested_body = message.get("body")
        if isinstance(nested_body, dict):
            attachments = nested_body.get("attachments")
            if isinstance(attachments, list):
                return [attach for attach in attachments if isinstance(attach, dict)]

    return []


def _extract_rest_file_token(attach: dict) -> str | None:
    payload = attach.get("payload")
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    if isinstance(token, str) and token:
        return token
    return None


def _extract_rest_file_url(attach: dict) -> str | None:
    payload = attach.get("payload")
    if not isinstance(payload, dict):
        return None
    url = payload.get("url")
    if isinstance(url, str) and url.startswith("http"):
        return url
    return None


def _extract_rest_file_name(attach: dict) -> str | None:
    for key in ("filename", "name", "title"):
        value = attach.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_rest_file_attachments(payload: Any) -> list[dict]:
    return [
        attach
        for attach in _extract_rest_message_attachments(payload)
        if str(attach.get("type", "")).lower() == "file"
    ]


def _match_rest_file_attachment(rest_attachments: list[dict], incoming_attach: dict) -> dict | None:
    incoming_token = incoming_attach.get("token") or incoming_attach.get("fileToken")
    if isinstance(incoming_token, str) and incoming_token:
        for rest_attach in rest_attachments:
            if _extract_rest_file_token(rest_attach) == incoming_token:
                return rest_attach

    incoming_name = incoming_attach.get("name")
    if isinstance(incoming_name, str) and incoming_name:
        incoming_name_folded = incoming_name.casefold()
        for rest_attach in rest_attachments:
            rest_name = _extract_rest_file_name(rest_attach)
            if isinstance(rest_name, str) and rest_name.casefold() == incoming_name_folded:
                return rest_attach

    if len(rest_attachments) == 1:
        return rest_attachments[0]

    return None


async def _hydrate_file_attach(
    attach: dict,
    client: MaxClient,
    *,
    message_id: str | None,
) -> dict | None:
    if not message_id:
        log.warning("FILE attach missing source message_id; cannot hydrate URL")
        return None

    fetch_message = getattr(client, "fetch_message", None)
    if not callable(fetch_message):
        log.warning("FILE attach cannot hydrate URL for message_id=%s: fetch_message unavailable", message_id)
        return None

    fetched_message = await fetch_message(message_id)
    rest_attachments = _extract_rest_file_attachments(fetched_message)
    if not rest_attachments:
        log.warning("FILE attach fetch_message(message_id=%s) returned no usable attachment", message_id)
        return None

    matched_attach = _match_rest_file_attachment(rest_attachments, attach)
    if matched_attach is None:
        log.warning(
            "FILE attach token/filename match failed for message_id=%s token=%r name=%r candidates=%d",
            message_id,
            attach.get("token") or attach.get("fileToken"),
            attach.get("name"),
            len(rest_attachments),
        )
        return None

    if _extract_rest_file_url(matched_attach) is None:
        log.warning("FILE attach for message_id=%s matched fetched attachment without payload.url", message_id)
        return None

    return matched_attach


async def _send_downloaded_file(
    sender: Any,
    data: bytes,
    *,
    filename: str,
    header_text: str,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    raise_bad_request: bool = False,
):
    kind = _guess_media_kind(filename)
    if kind == "photo":
        return await sender.send_photo(
            data,
            caption=header_text,
            filename=filename,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )
    if kind == "video":
        return await sender.send_video(
            data,
            caption=header_text,
            filename=filename,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )
    return await sender.send_document(
        data,
        caption=header_text,
        filename=filename,
        message_thread_id=message_thread_id,
        reply_to_message_id=reply_to_message_id,
        raise_bad_request=raise_bad_request,
    )


async def _send_attach(
    attach: dict,
    client: MaxClient,
    sender: Any,
    header_text: str,
    *,
    message_id: str | None = None,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    raise_bad_request: bool = False,
):
    atype = attach.get("_type", "")
    log.info("Processing attach _type=%s keys=%s", atype, list(attach.keys()))

    if atype in {"CONTROL", "WIDGET", "INLINE_KEYBOARD"}:
        return None

    if atype == "PHOTO":
        url = _extract_attach_url(attach) or _extract_photo_url(attach)
        if not url:
            log.warning("PHOTO attach has no URL: %s", attach)
            return None
        data = await client.download_file(url)
        if data:
            return await sender.send_photo(
                data,
                caption=header_text,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                raise_bad_request=raise_bad_request,
            )
        return await sender.send(
            f"{header_text}\n<i>[фото — не удалось загрузить]</i>",
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )

    if atype == "VIDEO":
        thumb = attach.get("thumbnail")
        thumb_url = _first_http_url(thumb)
        if thumb_url:
            data = await client.download_file(thumb_url)
            if data:
                return await sender.send_photo(
                    data,
                    caption=f"{header_text}\n<i>[видео — превью]</i>",
                    message_thread_id=message_thread_id,
                    reply_to_message_id=reply_to_message_id,
                    raise_bad_request=raise_bad_request,
                )
        return await sender.send(
            f"{header_text}\n<i>[видео]</i>",
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )

    if atype == "FILE":
        name = attach.get("name", "file")
        size = attach.get("size", 0)
        token_url = _extract_attach_url(attach) or _extract_file_url(attach)
        if not token_url:
            hydrated_attach = await _hydrate_file_attach(
                attach,
                client,
                message_id=message_id,
            )
            if hydrated_attach is not None:
                token_url = _extract_rest_file_url(hydrated_attach)
                hydrated_name = _extract_rest_file_name(hydrated_attach)
                if hydrated_name:
                    name = hydrated_name
        if token_url:
            data = await client.download_file(token_url)
            if data:
                return await _send_downloaded_file(
                    sender,
                    data,
                    filename=name,
                    header_text=header_text,
                    message_thread_id=message_thread_id,
                    reply_to_message_id=reply_to_message_id,
                    raise_bad_request=raise_bad_request,
                )
        size_str = f" ({_human_size(size)})" if size else ""
        return await sender.send(
            f"{header_text}\n📎 <b>{escape(name)}</b>{size_str}",
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )

    if atype == "AUDIO":
        data = await _download_audio_data(attach, client)
        if data:
            return await sender.send_voice(
                data,
                caption=header_text,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                raise_bad_request=raise_bad_request,
            )
        log.warning("Audio download unavailable; sent text fallback")
        return await sender.send(
            f"{header_text}\n<i>[аудио]</i>",
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )

    if atype == "STICKER":
        url = _extract_attach_url(attach)
        if url:
            data = await client.download_file(url)
            if data:
                return await sender.send_sticker(
                    data,
                    message_thread_id=message_thread_id,
                    reply_to_message_id=reply_to_message_id,
                    raise_bad_request=raise_bad_request,
                )
        return await sender.send(
            f"{header_text}\n<i>[стикер]</i>",
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )

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
        return await sender.send(
            "\n".join(parts),
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )

    if atype == "LOCATION":
        lat = attach.get("lat") or attach.get("latitude")
        lon = attach.get("lon") or attach.get("lng") or attach.get("longitude")
        if lat and lon:
            return await sender.send(
                f"{header_text}\n📍 {lat}, {lon}",
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                raise_bad_request=raise_bad_request,
            )
        return await sender.send(
            f"{header_text}\n<i>[геолокация]</i>",
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )

    if atype == "CONTACT":
        name = attach.get("name", "")
        phone = attach.get("phone", "")
        text = f"{header_text}\n👤 {escape(name)}"
        if phone:
            text += f" — {escape(phone)}"
        return await sender.send(
            text,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            raise_bad_request=raise_bad_request,
        )

    log.info("Unknown attach type %s, sending as info", atype)
    return await sender.send(
        f"{header_text}\n<i>[вложение: {escape(atype or 'unknown')}]</i>",
        message_thread_id=message_thread_id,
        reply_to_message_id=reply_to_message_id,
        raise_bad_request=raise_bad_request,
    )


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
) -> int | None:
    inner = link.get("message") or link
    linked_message_id = _extract_linked_max_message_id(link)
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

    first_message_id: int | None = None

    if fwd_meaningful:
        text_sent = False
        for index, attach in enumerate(fwd_meaningful):
            if index == 0 and fwd_text:
                cap = f"{full_header}\n{escape(fwd_text)}"
                text_sent = True
            else:
                cap = full_header
            result = await _send_attach(
                attach,
                client,
                sender,
                cap,
                message_id=linked_message_id,
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )
            if first_message_id is None:
                first_message_id = _extract_message_id(result)

        if fwd_text and not text_sent:
            result = await sender.send(
                f"{full_header}\n{escape(fwd_text)}",
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )
            if first_message_id is None:
                first_message_id = _extract_message_id(result)
        return first_message_id

    if fwd_text:
        result = await sender.send(
            f"{full_header}\n{escape(fwd_text)}",
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return _extract_message_id(result)

    result = await sender.send(
        f"{full_header}\n<i>[без содержимого]</i>",
        message_thread_id=message_thread_id,
        raise_bad_request=raise_bad_request,
    )
    return _extract_message_id(result)


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


def _extract_linked_max_message_id(link: dict) -> str | None:
    inner = link.get("message") or {}
    for candidate in (
        inner.get("id"),
        inner.get("mid"),
        link.get("id"),
        link.get("mid"),
        link.get("messageId"),
    ):
        if candidate is not None and str(candidate):
            return str(candidate)
    return None


async def _resolve_reply_to_message_id(
    *,
    msg: MaxMessage,
    message_store: MessageStore | None,
    relay_client: RelayClient | None,
) -> int | None:
    link = msg.link if isinstance(msg.link, dict) else {}
    if link.get("type") != "REPLY":
        return None

    linked_max_message_id = _extract_linked_max_message_id(link)
    if linked_max_message_id is None:
        return None

    if relay_client is not None:
        return await relay_client.lookup_message_mapping(
            max_chat_id=msg.chat_id,
            max_message_id=linked_max_message_id,
        )

    if message_store is None:
        return None

    mapping = message_store.get_by_max_message(
        max_chat_id=msg.chat_id,
        max_message_id=linked_max_message_id,
        direction=None,
    )
    if mapping is None:
        return None
    return mapping.tg_message_id


def _get_existing_max_mapping(
    message_store: MessageStore | None,
    *,
    max_chat_id: Any,
    max_message_id: str,
):
    if message_store is None or not max_message_id:
        return None
    return message_store.get_by_max_message(
        max_chat_id=max_chat_id,
        max_message_id=max_message_id,
    )


def _store_message_mapping(
    message_store: MessageStore | None,
    *,
    tg_chat_id: int | None,
    msg: MaxMessage,
    message_thread_id: int | None,
    tg_message_id: int | None,
) -> None:
    if message_store is None or tg_chat_id is None or tg_message_id is None or not msg.message_id:
        return
    message_store.upsert_mapping(
        tg_chat_id=tg_chat_id,
        max_chat_id=msg.chat_id,
        max_message_id=msg.message_id,
        tg_message_id=tg_message_id,
        message_thread_id=message_thread_id,
        direction="max_to_tg",
        source="max",
    )


def _resolve_tg_chat_id(sender: Any, topic_router: TopicRouter | None) -> int | None:
    router_chat_id = getattr(topic_router, "tg_chat_id", None)
    if router_chat_id is not None:
        try:
            return int(router_chat_id)
        except (TypeError, ValueError):
            pass

    sender_chat_id = getattr(sender, "chat_id", None)
    if sender_chat_id is None:
        return None
    try:
        return int(sender_chat_id)
    except (TypeError, ValueError):
        return None


async def _send_current_message_content(
    msg: MaxMessage,
    *,
    client: MaxClient,
    sender: Any,
    header_text: str,
    message_thread_id: int | None,
    reply_to_message_id: int | None,
    raise_bad_request: bool,
) -> int | None:
    meaningful_attaches = [
        a
        for a in msg.attaches
        if isinstance(a, dict) and a.get("_type") not in {"CONTROL", "WIDGET", "INLINE_KEYBOARD", None}
    ]

    first_message_id: int | None = None

    if meaningful_attaches:
        text_sent = False
        for index, attach in enumerate(meaningful_attaches):
            if index == 0 and msg.text:
                cap = f"{header_text}\n{escape(msg.text)}"
                text_sent = True
            else:
                cap = header_text
            result = await _send_attach(
                attach,
                client,
                sender,
                cap,
                message_id=msg.message_id or None,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                raise_bad_request=raise_bad_request,
            )
            if first_message_id is None:
                first_message_id = _extract_message_id(result)

        if msg.text and not text_sent:
            result = await sender.send(
                f"{header_text}\n{escape(msg.text)}",
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
                raise_bad_request=raise_bad_request,
            )
            if first_message_id is None:
                first_message_id = _extract_message_id(result)
        return first_message_id

    body = escape(msg.text) if msg.text else "<i>[нетекстовое сообщение]</i>"
    result = await sender.send(
        f"{header_text}\n{body}",
        message_thread_id=message_thread_id,
        reply_to_message_id=reply_to_message_id,
        raise_bad_request=raise_bad_request,
    )
    return _extract_message_id(result)


async def forward_max_message(
    msg: MaxMessage,
    *,
    client: MaxClient,
    sender: Any,
    resolver: ContactResolver,
    topic_router: TopicRouter | None = None,
    relay_client: RelayClient | None = None,
    message_store: MessageStore | None = None,
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

    existing_mapping = _get_existing_max_mapping(
        message_store,
        max_chat_id=msg.chat_id,
        max_message_id=msg.message_id,
    )
    if existing_mapping is not None:
        log.info(
            "Skipping duplicate Max message chat=%s message_id=%s existing_tg_message_id=%s",
            msg.chat_id,
            msg.message_id,
            existing_mapping.tg_message_id,
        )
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

    native_reply_to_message_id = await _resolve_reply_to_message_id(
        msg=msg,
        message_store=message_store,
        relay_client=relay_client,
    )

    async def forward_to_telegram(message_thread_id: int | None) -> tuple[int | None, int | None]:
        link = msg.link if isinstance(msg.link, dict) else {}
        link_type = link.get("type") if isinstance(link, dict) else None
        mapping_operation_index: int | None = 0

        if link_type == "REPLY" and native_reply_to_message_id is None:
            quote_count_before = len(getattr(operation_builder, "operations", [])) if operation_builder is not None else 0
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
            quote_count_after = len(getattr(operation_builder, "operations", [])) if operation_builder is not None else 0
            current_first_message_id = await _send_current_message_content(
                msg,
                client=client,
                sender=dispatch_sender,
                header_text=header_text,
                message_thread_id=message_thread_id,
                reply_to_message_id=None,
                raise_bad_request=raise_topic_errors,
            )
            if operation_builder is not None:
                mapping_operation_index = quote_count_after if quote_count_after > quote_count_before else 0
            log.info("Forwarded reply fallback with quoted context → TG")
            return current_first_message_id, mapping_operation_index

        if link_type == "FORWARD":
            forwarded_first_message_id = await _handle_linked_message(
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
            return forwarded_first_message_id, 0

        current_first_message_id = await _send_current_message_content(
            msg,
            client=client,
            sender=dispatch_sender,
            header_text=header_text,
            message_thread_id=message_thread_id,
            reply_to_message_id=native_reply_to_message_id,
            raise_bad_request=raise_topic_errors,
        )
        if link_type == "REPLY" and native_reply_to_message_id is not None:
            log.info("Forwarded reply with native Telegram reply target=%s", native_reply_to_message_id)
        else:
            log.info("Forwarded message → TG")
        return current_first_message_id, 0

    if relay_client is not None:
        _current_first_message_id, mapping_operation_index = await forward_to_telegram(None)
        if operation_builder is None or operation_builder.is_empty:
            return
        await relay_client.send_batch(
            operation_builder.build_batch(
                msg.chat_id,
                topic_name,
                max_message_id=msg.message_id or None,
                reply_to_message_id=native_reply_to_message_id,
                mapping_operation_index=mapping_operation_index,
            ),
            operation_builder.attachments,
        )
        return

    if topic_router is None:
        current_first_message_id, _mapping_operation_index = await forward_to_telegram(None)
        tg_chat_id = _resolve_tg_chat_id(sender, topic_router)
        _store_message_mapping(
            message_store,
            tg_chat_id=tg_chat_id,
            msg=msg,
            message_thread_id=None,
            tg_message_id=current_first_message_id,
        )
        return

    thread_id = await topic_router.ensure_topic(msg.chat_id, topic_name)
    try:
        current_first_message_id, _mapping_operation_index = await forward_to_telegram(thread_id)
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
        current_first_message_id, _mapping_operation_index = await forward_to_telegram(new_thread_id)
        thread_id = new_thread_id

    tg_chat_id = _resolve_tg_chat_id(sender, topic_router)
    _store_message_mapping(
        message_store,
        tg_chat_id=tg_chat_id,
        msg=msg,
        message_thread_id=thread_id,
        tg_message_id=current_first_message_id,
    )
