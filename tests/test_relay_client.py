from unittest.mock import AsyncMock

import pytest

from app.relay_client import RelayClient, RelayStatusSender
from app.relay_models import RelayOperation, TelegramBatch


class _FakeResponse:
    def __init__(self, *, status: int = 200, payload: dict | None = None, text: str = ""):
        self.status = status
        self._payload = payload or {"ok": True}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.closed = False
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _batch() -> TelegramBatch:
    return TelegramBatch(
        max_chat_id="42",
        topic_name="Alice",
        operations=[RelayOperation(kind="text", text="hello")],
    )


@pytest.mark.asyncio
async def test_relay_client_recovers_and_retries_transient_network_error():
    recovery_hook = AsyncMock(return_value=None)
    relay_client = RelayClient("http://relay.local", "secret", recovery_hook=recovery_hook)
    session = _FakeSession(OSError("tunnel down"), _FakeResponse())
    relay_client._session = session

    await relay_client.send_batch(_batch())

    recovery_hook.assert_awaited_once_with("send Telegram batch")
    assert len(session.post_calls) == 2


@pytest.mark.asyncio
async def test_relay_client_does_not_recover_http_auth_error():
    recovery_hook = AsyncMock(return_value=None)
    relay_client = RelayClient("http://relay.local", "secret", recovery_hook=recovery_hook)
    session = _FakeSession(_FakeResponse(status=401, text="invalid relay secret"))
    relay_client._session = session

    with pytest.raises(RuntimeError, match="HTTP 401"):
        await relay_client.send_batch(_batch())

    recovery_hook.assert_not_awaited()
    assert len(session.post_calls) == 1


@pytest.mark.asyncio
async def test_relay_status_sender_suppresses_relay_failure():
    relay_client = AsyncMock()
    relay_client.send_batch = AsyncMock(side_effect=OSError("tunnel still down"))
    sender = RelayStatusSender(relay_client)

    result = await sender.send("bridge disconnected")

    assert result == {"ok": False}
    relay_client.send_batch.assert_awaited_once()
