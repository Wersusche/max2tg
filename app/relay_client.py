from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from app.relay_models import MaxCommand, RelayOperation, TelegramBatch

log = logging.getLogger(__name__)

SECRET_HEADER = "X-Relay-Secret"


class RelayClient:
    def __init__(self, base_url: str, shared_secret: str):
        self.base_url = base_url.rstrip("/")
        self.shared_secret = shared_secret
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def wait_until_healthy(self, retries: int = 30, delay_seconds: float = 2.0) -> None:
        for attempt in range(1, retries + 1):
            if await self.healthcheck():
                return
            log.info("Relay healthcheck attempt %d/%d failed, retrying...", attempt, retries)
            await asyncio.sleep(delay_seconds)
        raise RuntimeError("Relay did not become healthy in time")

    async def healthcheck(self) -> bool:
        session = await self._get_session()
        try:
            async with session.get(f"{self.base_url}/healthz", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def send_batch(
        self,
        batch: TelegramBatch,
        attachments: dict[str, tuple[str, bytes]] | None = None,
    ) -> None:
        session = await self._get_session()
        headers = {SECRET_HEADER: self.shared_secret}
        attachments = attachments or {}

        if attachments:
            form = aiohttp.FormData()
            form.add_field("batch", batch.to_json(), content_type="application/json")
            for field_name, (filename, payload) in attachments.items():
                form.add_field(field_name, payload, filename=filename, content_type="application/octet-stream")
            async with session.post(
                f"{self.base_url}/internal/telegram-batch",
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                await _raise_for_status(resp)
            return

        async with session.post(
            f"{self.base_url}/internal/telegram-batch",
            json=batch.to_dict(),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            await _raise_for_status(resp)

    async def send_text(self, text: str, topic_name: str | None = None) -> None:
        await self.send_batch(
            TelegramBatch(
                max_chat_id="__system__",
                topic_name=topic_name,
                operations=[RelayOperation(kind="text", text=text)],
            )
        )

    async def pull_command(self, timeout_seconds: int = 30) -> MaxCommand | None:
        session = await self._get_session()
        async with session.get(
            f"{self.base_url}/internal/max-commands/pull",
            params={"timeout": timeout_seconds},
            headers={SECRET_HEADER: self.shared_secret},
            timeout=aiohttp.ClientTimeout(total=timeout_seconds + 10),
        ) as resp:
            if resp.status == 204:
                return None
            await _raise_for_status(resp)
            return MaxCommand.from_dict(await resp.json())

    async def ack_command(self, command_id: int) -> None:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/internal/max-commands/{int(command_id)}/ack",
            headers={SECRET_HEADER: self.shared_secret},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            await _raise_for_status(resp)

    async def lookup_message_mapping(self, *, max_chat_id: Any, max_message_id: Any) -> int | None:
        session = await self._get_session()
        async with session.get(
            f"{self.base_url}/internal/message-mappings/lookup",
            params={
                "max_chat_id": str(max_chat_id),
                "max_message_id": str(max_message_id),
            },
            headers={SECRET_HEADER: self.shared_secret},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 404:
                return None
            await _raise_for_status(resp)
            payload = await resp.json()
            raw_message_id = payload.get("tg_message_id")
            if raw_message_id is None:
                return None
            return int(raw_message_id)

    async def upsert_message_mapping(
        self,
        *,
        tg_chat_id: int,
        tg_message_id: int,
        max_chat_id: Any,
        max_message_id: Any,
        message_thread_id: int | None = None,
        direction: str = "tg_to_max",
        source: str = "telegram",
    ) -> None:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/internal/message-mappings/upsert",
            json={
                "tg_chat_id": int(tg_chat_id),
                "tg_message_id": int(tg_message_id),
                "max_chat_id": str(max_chat_id),
                "max_message_id": str(max_message_id),
                "message_thread_id": int(message_thread_id) if message_thread_id is not None else None,
                "direction": str(direction),
                "source": str(source),
            },
            headers={SECRET_HEADER: self.shared_secret},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            await _raise_for_status(resp)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session


class RelayStatusSender:
    """Minimal sender used by Max notifications when Telegram lives on relay."""

    def __init__(self, relay_client: RelayClient):
        self.relay_client = relay_client

    async def send(
        self,
        text: str,
        reply_markup=None,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        raise_bad_request: bool = False,
    ):
        del reply_markup, message_thread_id, reply_to_message_id, raise_bad_request
        await self.relay_client.send_batch(
            TelegramBatch(
                max_chat_id="__system__",
                topic_name=None,
                operations=[RelayOperation(kind="text", text=text)],
            )
        )
        return {"ok": True}


async def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
    if resp.status < 400:
        return
    body = await resp.text()
    raise RuntimeError(f"Relay request failed with HTTP {resp.status}: {body[:500]}")
