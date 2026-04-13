import logging
import os

import telegram.constants
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from app.command_store import CommandStore
from app.max_client import MaxClient
from app.topic_store import TopicStore

log = logging.getLogger(__name__)

PENDING_REPLY_KEY = "pending_reply_chat_id"
PENDING_REPLY_LABEL_KEY = "pending_reply_label"

_ALLOWED_CHAT_ID_KEY = "allowed_chat_id"
_TOPIC_STORE_KEY = "topic_store"
_COMMAND_STORE_KEY = "command_store"


async def _on_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline 'Reply' button press."""
    query = update.callback_query

    allowed_chat_id = context.bot_data.get(_ALLOWED_CHAT_ID_KEY)
    if allowed_chat_id is not None and (
        update.effective_chat.id != allowed_chat_id and update.effective_user.id != allowed_chat_id
    ):
        await query.answer()
        return

    await query.answer()

    data = query.data or ""
    if not data.startswith("reply:"):
        return

    chat_id_str = data[len("reply:") :]
    try:
        max_chat_id = int(chat_id_str)
    except ValueError:
        max_chat_id = chat_id_str

    context.user_data[PENDING_REPLY_KEY] = max_chat_id

    source_text = query.message.text or query.message.caption or ""
    label = source_text.split("\n")[0] if source_text else str(max_chat_id)
    context.user_data[PENDING_REPLY_LABEL_KEY] = label

    await query.message.reply_text(
        f"✏️ Напишите ответ для <b>{label}</b> (ответом на оригинальное сообщение):\n"
        "<i>(или /cancel для отмены)</i>",
        parse_mode="HTML",
    )


async def _on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel pending reply."""
    if context.user_data.pop(PENDING_REPLY_KEY, None):
        context.user_data.pop(PENDING_REPLY_LABEL_KEY, None)
        await update.message.reply_text("❌ Ответ отменён.")
    else:
        await update.message.reply_text("Нет активного ответа для отмены.")


async def _on_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward user's text as a reply to Max."""
    max_chat_id = context.user_data.pop(PENDING_REPLY_KEY, None)
    label = context.user_data.pop(PENDING_REPLY_LABEL_KEY, None)
    if max_chat_id is None:
        return

    max_client: MaxClient | None = context.bot_data.get("max_client")
    command_store: CommandStore | None = context.bot_data.get(_COMMAND_STORE_KEY)
    if not max_client and not command_store:
        await update.message.reply_text("⚠️ Max клиент не подключён.")
        return

    text = update.message.text
    text, elements = _decorate_outbound_text(update.message, text)
    try:
        if max_client:
            resp = await max_client.send_message(max_chat_id, text, elements)
        else:
            command_store.enqueue(max_chat_id, text, elements)
            resp = {"queued": True}
        if resp:
            await update.message.reply_text(
                f"✅ Отправлено → <b>{label or max_chat_id}</b>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("⚠️ Не удалось отправить сообщение в Max.")
    except Exception:
        log.exception("Failed to send reply to Max chat %s", max_chat_id)
        await update.message.reply_text("⚠️ Ошибка при отправке в Max.")


def _parse_max_chat_id(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _message_text_or_caption(update: Update) -> str | None:
    message = update.message
    if not message:
        return None
    return message.text or message.caption


def _decorate_outbound_text(message, text: str | None, *, include_sender_when_empty: bool = False) -> tuple[str, list[dict]]:
    text = text or ""
    elements: list[dict] = []
    if message.chat.type in [telegram.constants.ChatType.GROUP, telegram.constants.ChatType.SUPERGROUP]:
        sender_name = message.from_user.full_name if message.from_user else "Telegram"
        prefix = f"💬 {sender_name}:"
        elements = [
            {
                "type": "STRONG",
                "length": len(sender_name) + 1,
                "from": 2,
            }
        ]
        if text:
            return f"{prefix}\n{text}", elements
        if include_sender_when_empty:
            return prefix, elements
    return text, elements


def _has_photo(message) -> bool:
    return bool(getattr(message, "photo", None))


def _guess_photo_filename(telegram_file) -> str:
    file_path = getattr(telegram_file, "file_path", "") or ""
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        ext = ".jpg"
    if ext == ".jpeg":
        ext = ".jpg"
    return f"photo{ext}"


async def _download_largest_photo(message) -> tuple[bytes, str] | None:
    photos = list(getattr(message, "photo", None) or [])
    if not photos:
        return None

    telegram_file = await photos[-1].get_file()
    payload = await telegram_file.download_as_bytearray()
    return bytes(payload), _guess_photo_filename(telegram_file)


async def _on_topic_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward Telegram forum topic messages to the mapped Max chat."""
    message = update.message
    if not message:
        return

    sender_user = getattr(message, "from_user", None)
    if getattr(sender_user, "is_bot", False) is True:
        return

    allowed_chat_id = context.bot_data.get(_ALLOWED_CHAT_ID_KEY)
    if allowed_chat_id is None:
        return
    if update.effective_chat.id != allowed_chat_id:
        return

    message_thread_id = getattr(message, "message_thread_id", None)
    if message_thread_id is None:
        return

    topic_store: TopicStore | None = context.bot_data.get(_TOPIC_STORE_KEY)
    if topic_store is None:
        await message.reply_text("⚠️ Хранилище топиков не подключено.")
        return

    mapping = topic_store.get_by_thread(int(allowed_chat_id), int(message_thread_id))
    if mapping is None:
        if _message_text_or_caption(update) or _has_photo(message):
            await message.reply_text(
                "⚠️ Не знаю, какому чату Max соответствует этот топик. "
                "Дождитесь входящего сообщения из Max, чтобы бот создал тему."
            )
        return

    max_client: MaxClient | None = context.bot_data.get("max_client")
    command_store: CommandStore | None = context.bot_data.get(_COMMAND_STORE_KEY)
    if not max_client and not command_store:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return

    text = _message_text_or_caption(update)
    parsed_max_chat_id = _parse_max_chat_id(mapping.max_chat_id)

    try:
        if _has_photo(message):
            photo = await _download_largest_photo(message)
            if photo is None:
                await message.reply_text("⚠️ Не удалось скачать фото из Telegram.")
                return
            photo_bytes, filename = photo
            caption, elements = _decorate_outbound_text(
                message,
                text,
                include_sender_when_empty=True,
            )
            if max_client:
                resp = await max_client.send_photo(
                    parsed_max_chat_id,
                    photo_bytes,
                    caption=caption,
                    elements=elements,
                    filename=filename,
                )
            else:
                command_store.enqueue_photo(
                    parsed_max_chat_id,
                    photo_bytes,
                    caption=caption,
                    elements=elements,
                    filename=filename,
                )
                resp = {"queued": True}
            if not resp:
                await message.reply_text("⚠️ Не удалось отправить фото в Max.")
            return

        if not text:
            if getattr(message, "effective_attachment", None) is not None:
                await message.reply_text("⚠️ Пока умею отправлять в Max только текст и фото.")
            return

        text, elements = _decorate_outbound_text(message, text)
        if max_client:
            resp = await max_client.send_message(parsed_max_chat_id, text, elements)
        else:
            command_store.enqueue(parsed_max_chat_id, text, elements)
            resp = {"queued": True}
        if not resp:
            await message.reply_text("⚠️ Не удалось отправить сообщение в Max.")
    except Exception:
        log.exception("Failed to send Telegram topic message to Max chat %s", mapping.max_chat_id)
        await message.reply_text("⚠️ Ошибка при отправке в Max.")


def build_tg_app(
    token: str,
    allowed_chat_id: str,
    topic_store: TopicStore,
    *,
    command_store: CommandStore | None = None,
    max_client: MaxClient | None = None,
) -> Application:
    """Build and configure the Telegram Application with handlers."""
    app = Application.builder().token(token).build()
    if max_client is not None:
        app.bot_data["max_client"] = max_client
    if command_store is not None:
        app.bot_data[_COMMAND_STORE_KEY] = command_store
    app.bot_data[_ALLOWED_CHAT_ID_KEY] = int(allowed_chat_id)
    app.bot_data[_TOPIC_STORE_KEY] = topic_store

    chat_filter = filters.Chat(chat_id=int(allowed_chat_id))
    app.add_handler(MessageHandler(chat_filter & ~filters.COMMAND, _on_topic_message))
    return app
