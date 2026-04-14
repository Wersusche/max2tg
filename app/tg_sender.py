import asyncio
import io
import logging

from telegram import Bot, InputFile
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut

log = logging.getLogger(__name__)

TG_MAX_LENGTH = 4096
TG_CAPTION_MAX = 1024
MAX_RETRIES = 3


class TelegramSender:
    def __init__(self, token: str, chat_id: str):
        self._bot = Bot(token=token)
        self._chat_id = chat_id

    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def chat_id(self) -> str:
        return self._chat_id

    async def start(self):
        await self._bot.initialize()
        me = await self._bot.get_me()
        log.info("Telegram bot ready: @%s", me.username)

    async def stop(self):
        await self._bot.shutdown()

    def _truncate_caption(self, text: str) -> str:
        if len(text) > TG_CAPTION_MAX:
            return text[: TG_CAPTION_MAX - 20] + "\n\n[...усечено]"
        return text

    async def _retry(self, coro_factory, *, raise_bad_request: bool = False):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except BadRequest:
                log.exception("Telegram rejected request")
                if raise_bad_request:
                    raise
                return None
            except RetryAfter as e:
                log.warning("Telegram rate limit, retry after %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                log.warning("Telegram timeout (attempt %d/%d)", attempt, MAX_RETRIES)
                await asyncio.sleep(2 * attempt)
            except Exception:
                log.exception("Failed to send to Telegram (attempt %d/%d)", attempt, MAX_RETRIES)
                if attempt == MAX_RETRIES:
                    return None
                await asyncio.sleep(2 * attempt)
        return None

    async def send(
        self,
        text: str,
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        if not text:
            return

        if len(text) > TG_MAX_LENGTH:
            text = text[: TG_MAX_LENGTH - 20] + "\n\n[...усечено]"

        return await self._retry(
            lambda: self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            ),
            raise_bad_request=raise_bad_request,
        )

    async def send_photo(
        self,
        data: bytes,
        caption: str = "",
        filename: str = "photo.jpg",
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_photo(
                chat_id=self._chat_id,
                photo=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            ),
            raise_bad_request=raise_bad_request,
        )

    async def send_document(
        self,
        data: bytes,
        caption: str = "",
        filename: str = "file",
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_document(
                chat_id=self._chat_id,
                document=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            ),
            raise_bad_request=raise_bad_request,
        )

    async def send_video(
        self,
        data: bytes,
        caption: str = "",
        filename: str = "video.mp4",
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_video(
                chat_id=self._chat_id,
                video=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            ),
            raise_bad_request=raise_bad_request,
        )

    async def send_voice(
        self,
        data: bytes,
        caption: str = "",
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        caption = self._truncate_caption(caption)
        result = await self._retry(
            lambda: self._bot.send_voice(
                chat_id=self._chat_id,
                voice=InputFile(io.BytesIO(data), filename="voice.ogg"),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            ),
            raise_bad_request=raise_bad_request,
        )
        if result is None:
            log.info("send_voice failed, falling back to send_audio")
            return await self._retry(
                lambda: self._bot.send_audio(
                    chat_id=self._chat_id,
                    audio=InputFile(io.BytesIO(data), filename="audio.m4a"),
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    message_thread_id=message_thread_id,
                ),
                raise_bad_request=raise_bad_request,
            )
        return result

    async def send_sticker(
        self,
        data: bytes,
        reply_markup=None,
        message_thread_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        return await self._retry(
            lambda: self._bot.send_sticker(
                chat_id=self._chat_id,
                sticker=InputFile(io.BytesIO(data), filename="sticker.webp"),
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            ),
            raise_bad_request=raise_bad_request,
        )

    async def create_forum_topic(self, name: str):
        return await self._retry(
            lambda: self._bot.create_forum_topic(chat_id=self._chat_id, name=name),
            raise_bad_request=True,
        )

    async def edit_forum_topic(self, message_thread_id: int, name: str):
        return await self._retry(
            lambda: self._bot.edit_forum_topic(
                chat_id=self._chat_id,
                message_thread_id=message_thread_id,
                name=name,
            ),
            raise_bad_request=True,
        )
