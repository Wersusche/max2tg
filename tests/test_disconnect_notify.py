"""Tests for disconnect notification behaviour in app/max_listener.py."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.max_listener import create_max_client


def _make_client(sender=None):
    if sender is None:
        sender = SimpleNamespace(send=AsyncMock())
    return (
        create_max_client(
            max_token="tok",
            max_device_id="dev",
            sender=sender,
        ),
        sender,
    )


def _snapshot(chat_count: int = 0) -> dict:
    chats = [
        {"id": index + 100, "type": "GROUP", "title": f"Chat {index}", "participants": {}}
        for index in range(chat_count)
    ]
    return {"profile": {"id": 1, "names": []}, "chats": chats}


class TestDisconnectNotifications:
    @pytest.mark.asyncio
    async def test_first_disconnect_sends_immediately(self):
        client, sender = _make_client()

        await client._on_disconnect_cb()

        sender.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_repeated_disconnect_during_same_outage_is_suppressed(self):
        client, sender = _make_client()
        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t1 = datetime(2026, 4, 5, 11, 30, 0)

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t1
            sender.send.reset_mock()
            await client._on_disconnect_cb()

        sender.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_next_outage_after_ready_is_throttled_within_one_hour(self):
        client, sender = _make_client()
        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t_ready = datetime(2026, 4, 5, 10, 5, 0)
        t1 = datetime(2026, 4, 5, 10, 30, 0)

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t_ready
            await client._on_ready_cb(_snapshot())

            mock_dt.now.return_value = t1
            sender.send.reset_mock()
            await client._on_disconnect_cb()

        sender.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_next_outage_after_ready_sends_after_one_hour(self):
        client, sender = _make_client()
        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t_ready = datetime(2026, 4, 5, 10, 5, 0)
        t1 = datetime(2026, 4, 5, 11, 0, 1)

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t_ready
            await client._on_ready_cb(_snapshot())

            mock_dt.now.return_value = t1
            sender.send.reset_mock()
            await client._on_disconnect_cb()

        sender.send.assert_awaited_once()


class TestReadyNotifications:
    @pytest.mark.asyncio
    async def test_startup_notification_sent_on_first_connect(self):
        client, sender = _make_client()

        await client._on_ready_cb(_snapshot())

        sender.send.assert_awaited_once()
        assert "подключ" in sender.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_startup_notification_includes_chat_count(self):
        client, sender = _make_client()

        await client._on_ready_cb(_snapshot(chat_count=2))

        assert "2" in sender.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_reconnect_sends_restored_notification_after_reported_outage(self):
        client, sender = _make_client()
        snapshot = _snapshot()

        await client._on_ready_cb(snapshot)

        sender.send.reset_mock()
        await client._on_disconnect_cb()
        sender.send.reset_mock()
        await client._on_ready_cb(snapshot)

        sender.send.assert_awaited_once()
        assert "соединение восстановлено" in sender.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_reconnect_does_not_send_restored_notification_after_suppressed_outage(self):
        client, sender = _make_client()
        snapshot = _snapshot()
        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t_ready = datetime(2026, 4, 5, 10, 5, 0)
        t1 = datetime(2026, 4, 5, 10, 30, 0)
        t2 = datetime(2026, 4, 5, 10, 35, 0)

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t_ready
            await client._on_ready_cb(snapshot)

            mock_dt.now.return_value = t1
            sender.send.reset_mock()
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t2
            await client._on_ready_cb(snapshot)

        sender.send.assert_not_awaited()
