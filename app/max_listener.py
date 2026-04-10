import logging
from datetime import datetime
from html import escape

from telegram.error import BadRequest

from app.max_client import MaxClient, MaxMessage
from app.resolver import ContactResolver
from app.tg_sender import TelegramSender, reply_keyboard
from app.topic_router import TopicRouter

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _header(msg: MaxMessage, sender_label: str, chat_label: str, is_dm: bool) -> str:
    if is_dm:
        return f"✉ <b>{sender_label}</b>"
    return f"💬 <b>{chat_label}</b> | {sender_label}"


def _extract_photo_url(attach: dict) -> str | None:
    """Extract the best available URL for a PHOTO attachment."""
    return attach.get("baseUrl") or attach.get("url")


def _extract_file_url(attach: dict) -> str | None:
    """Extract download URL for a FILE attachment (url field takes priority)."""
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
    sender: TelegramSender,
    header_text: str,
    kb=None,
    message_thread_id: int | None = None,
    raise_bad_request: bool = False,
) -> bool:
    """Process and send a single attachment. Returns True if handled."""
    atype = attach.get("_type", "")
    log.info("Processing attach _type=%s keys=%s", atype, list(attach.keys()))

    if atype == "CONTROL" or atype == "WIDGET" or atype == "INLINE_KEYBOARD":
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
                reply_markup=kb,
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )
            return True
        await sender.send(
            f"{header_text}\n<i>[фото — не удалось загрузить]</i>",
            reply_markup=kb,
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
                    reply_markup=kb,
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_bad_request,
                )
                return True
        await sender.send(
            f"{header_text}\n<i>[видео]</i>",
            reply_markup=kb,
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
                        reply_markup=kb,
                        message_thread_id=message_thread_id,
                        raise_bad_request=raise_bad_request,
                    )
                elif kind == "video":
                    await sender.send_video(
                        data,
                        caption=header_text,
                        filename=name,
                        reply_markup=kb,
                        message_thread_id=message_thread_id,
                        raise_bad_request=raise_bad_request,
                    )
                else:
                    await sender.send_document(
                        data,
                        caption=header_text,
                        filename=name,
                        reply_markup=kb,
                        message_thread_id=message_thread_id,
                        raise_bad_request=raise_bad_request,
                    )
                return True
        size_str = f" ({_human_size(size)})" if size else ""
        await sender.send(
            f"{header_text}\n📎 <b>{escape(name)}</b>{size_str}",
            reply_markup=kb,
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
                    reply_markup=kb,
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_bad_request,
                )
                return True
        await sender.send(
            f"{header_text}\n<i>[аудио]</i>",
            reply_markup=kb,
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
                    reply_markup=kb,
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_bad_request,
                )
                return True
        await sender.send(
            f"{header_text}\n<i>[стикер]</i>",
            reply_markup=kb,
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
            reply_markup=kb,
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
                reply_markup=kb,
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )
        else:
            await sender.send(
                f"{header_text}\n<i>[геолокация]</i>",
                reply_markup=kb,
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
            reply_markup=kb,
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
        return True

    log.info("Unknown attach type %s, sending as info", atype)
    await sender.send(
        f"{header_text}\n<i>[вложение: {escape(atype or 'unknown')}]</i>",
        reply_markup=kb,
        message_thread_id=message_thread_id,
        raise_bad_request=raise_bad_request,
    )
    return True


async def _handle_linked_message(
    link: dict,
    link_type: str,
    header_text: str,
    client: MaxClient,
    sender: TelegramSender,
    resolver: ContactResolver,
    kb=None,
    message_thread_id: int | None = None,
    raise_bad_request: bool = False,
) -> None:
    """Handle FORWARD or REPLY link inside a message."""
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
        a for a in fwd_attaches
        if isinstance(a, dict) and a.get("_type") not in ("CONTROL", "WIDGET", "INLINE_KEYBOARD", None)
    ]

    if fwd_meaningful:
        text_sent = False
        for i, attach in enumerate(fwd_meaningful):
            if i == 0 and fwd_text:
                cap = f"{full_header}\n{escape(fwd_text)}"
                text_sent = True
            else:
                cap = full_header
            await _send_attach(
                attach,
                client,
                sender,
                cap,
                kb=kb,
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )

        if fwd_text and not text_sent:
            await sender.send(
                f"{full_header}\n{escape(fwd_text)}",
                reply_markup=kb,
                message_thread_id=message_thread_id,
                raise_bad_request=raise_bad_request,
            )
    elif fwd_text:
        await sender.send(
            f"{full_header}\n{escape(fwd_text)}",
            reply_markup=kb,
            message_thread_id=message_thread_id,
            raise_bad_request=raise_bad_request,
        )
    else:
        await sender.send(
            f"{full_header}\n<i>[без содержимого]</i>",
            reply_markup=kb,
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


def create_max_client(
    max_token: str, max_device_id: str, sender: TelegramSender, max_chat_ids: str | None = None,
    debug: bool = False, reply_enabled: bool = False, topic_router: TopicRouter | None = None,
) -> MaxClient:
    client = MaxClient(token=max_token, device_id=max_device_id, debug=debug, chat_ids=max_chat_ids)
    resolver = ContactResolver(client=client)

    _first_connect = True
    _notif_count = 0
    _last_notif_time: datetime | None = None

    def _can_notify() -> bool:
        if _last_notif_time is None:
            return True
        elapsed = (datetime.now() - _last_notif_time).total_seconds()
        if _notif_count == 1:
            return elapsed >= 3600    # 2-е: через 1 час
        if _notif_count == 2:
            return elapsed >= 10800   # 3-е: через 3 часа
        return elapsed >= 86400       # 4-е и далее: раз в сутки

    @client.on_ready
    async def handle_ready(snapshot: dict):
        nonlocal _first_connect
        participant_ids = resolver.load_snapshot(snapshot)

        if participant_ids:
            log.info("Batch-resolving %d participants...", len(participant_ids))
            await resolver.resolve_users_batch(participant_ids)
            log.info("Resolved users: %s", resolver.users)

            log.info("Known chats: %s", resolver.chats)
            log.info("Known users: %s", resolver.users)

        if not _first_connect:
            await sender.send("✅ <b>Max:</b> соединение восстановлено")
        else:
            chat_count = len(resolver.chats)
            await sender.send(f"✅ <b>Max:</b> подключён | чатов: {chat_count}")
        _first_connect = False

    @client.on_disconnect
    async def handle_disconnect():
        nonlocal _notif_count, _last_notif_time
        if not _can_notify():
            log.info("Disconnect notification suppressed (throttle)")
            return
        _notif_count += 1
        _last_notif_time = datetime.now()
        await sender.send("⚠️ <b>Max:</b> соединение потеряно, переподключение...")

    @client.on_message
    async def handle_message(msg: MaxMessage):
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
        kb = reply_keyboard(msg.chat_id) if reply_enabled and topic_router is None else None
        raise_topic_errors = topic_router is not None

        async def forward_to_telegram(message_thread_id: int | None) -> None:
            link = msg.link
            link_type = link.get("type") if isinstance(link, dict) else None

            if link_type in ("FORWARD", "REPLY"):
                await _handle_linked_message(
                    link,
                    link_type,
                    header_text,
                    client,
                    sender,
                    resolver,
                    kb=kb,
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_topic_errors,
                )
                if msg.text:
                    await sender.send(
                        f"{header_text}\n{escape(msg.text)}",
                        reply_markup=kb,
                        message_thread_id=message_thread_id,
                        raise_bad_request=raise_topic_errors,
                    )
                log.info("Forwarded link type=%s → TG", link_type)
                return

            meaningful_attaches = [
                a for a in msg.attaches
                if isinstance(a, dict) and a.get("_type") not in ("CONTROL", "WIDGET", "INLINE_KEYBOARD", None)
            ]

            if meaningful_attaches:
                text_sent = False
                for i, attach in enumerate(meaningful_attaches):
                    if i == 0 and msg.text:
                        cap = f"{header_text}\n{escape(msg.text)}"
                        text_sent = True
                    else:
                        cap = header_text
                    await _send_attach(
                        attach,
                        client,
                        sender,
                        cap,
                        kb=kb,
                        message_thread_id=message_thread_id,
                        raise_bad_request=raise_topic_errors,
                    )
                    log.info("Forwarded attach _type=%s → TG", attach.get("_type"))

                if msg.text and not text_sent:
                    await sender.send(
                        f"{header_text}\n{escape(msg.text)}",
                        reply_markup=kb,
                        message_thread_id=message_thread_id,
                        raise_bad_request=raise_topic_errors,
                    )
            else:
                body = escape(msg.text) if msg.text else "<i>[нетекстовое сообщение]</i>"
                await sender.send(
                    f"{header_text}\n{body}",
                    reply_markup=kb,
                    message_thread_id=message_thread_id,
                    raise_bad_request=raise_topic_errors,
                )
                log.info("Forwarded text → TG")

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

    return client
