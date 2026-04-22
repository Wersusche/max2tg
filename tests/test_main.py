import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.main as main_module
from app.main import _relay_command_loop
from app.relay_models import MaxCommand


@pytest.mark.asyncio
async def test_relay_command_loop_sends_document_and_acks():
    relay_client = MagicMock()
    relay_client.pull_command = AsyncMock(
        side_effect=[
            MaxCommand(
                id=1,
                max_chat_id="42",
                kind="document",
                text="caption",
                filename="report.pdf",
                attachment=b"file-bytes",
            ),
            asyncio.CancelledError(),
        ]
    )
    relay_client.ack_command = AsyncMock(return_value=None)
    relay_client.fail_command = AsyncMock(return_value={"ok": True, "attempt_count": 1, "dead_lettered": False})
    relay_client.upsert_message_mapping = AsyncMock(return_value=None)

    max_client = MagicMock()
    max_client.send_document = AsyncMock(return_value={"ok": True})

    with pytest.raises(asyncio.CancelledError):
        await _relay_command_loop(relay_client, max_client)

    max_client.send_document.assert_awaited_once_with(
        42,
        b"file-bytes",
        caption="caption",
        elements=[],
        filename="report.pdf",
        reply_to_max_message_id=None,
    )
    relay_client.upsert_message_mapping.assert_not_called()
    relay_client.ack_command.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_relay_command_loop_does_not_ack_failed_document_send():
    relay_client = MagicMock()
    relay_client.pull_command = AsyncMock(
        side_effect=[
            MaxCommand(
                id=3,
                max_chat_id="42",
                kind="document",
                text="caption",
                filename="report.pdf",
                attachment=b"file-bytes",
            ),
            asyncio.CancelledError(),
        ]
    )
    relay_client.ack_command = AsyncMock(return_value=None)
    relay_client.fail_command = AsyncMock(return_value={"ok": True, "attempt_count": 1, "dead_lettered": False})
    relay_client.upsert_message_mapping = AsyncMock(return_value=None)

    max_client = MagicMock()
    max_client.send_document = AsyncMock(return_value={})

    with pytest.raises(asyncio.CancelledError):
        await _relay_command_loop(relay_client, max_client)

    relay_client.fail_command.assert_awaited_once_with(
        3,
        error="Max rejected queued Telegram->Max command",
    )
    relay_client.ack_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_relay_command_loop_stores_tg_to_max_mapping_after_success():
    relay_client = MagicMock()
    relay_client.pull_command = AsyncMock(
        side_effect=[
            MaxCommand(
                id=2,
                max_chat_id="42",
                kind="text",
                text="reply text",
                reply_to_max_message_id="max-1",
                tg_chat_id=-100,
                tg_message_id=7001,
                message_thread_id=55,
            ),
            asyncio.CancelledError(),
        ]
    )
    relay_client.ack_command = AsyncMock(return_value=None)
    relay_client.fail_command = AsyncMock(return_value={"ok": True, "attempt_count": 1, "dead_lettered": False})
    relay_client.upsert_message_mapping = AsyncMock(return_value=None)

    max_client = MagicMock()
    max_client.send_message = AsyncMock(return_value={"message": {"id": "max-2"}})
    max_client.extract_sent_message_id = MagicMock(return_value="max-2")

    with pytest.raises(asyncio.CancelledError):
        await _relay_command_loop(relay_client, max_client)

    max_client.send_message.assert_awaited_once_with(
        42,
        "reply text",
        [],
        reply_to_max_message_id="max-1",
    )
    relay_client.upsert_message_mapping.assert_awaited_once_with(
        tg_chat_id=-100,
        tg_message_id=7001,
        max_chat_id="42",
        max_message_id="max-2",
        message_thread_id=55,
        direction="tg_to_max",
        source="telegram",
    )
    relay_client.ack_command.assert_awaited_once_with(2)
    relay_client.fail_command.assert_not_called()


@pytest.mark.asyncio
async def test_relay_command_loop_times_out_and_marks_command_failed(monkeypatch):
    relay_client = MagicMock()
    relay_client.pull_command = AsyncMock(
        side_effect=[
            MaxCommand(
                id=4,
                max_chat_id="42",
                kind="document",
                text="caption",
                filename="report.pdf",
                attachment=b"file-bytes",
            ),
            asyncio.CancelledError(),
        ]
    )
    relay_client.ack_command = AsyncMock(return_value=None)
    relay_client.fail_command = AsyncMock(return_value={"ok": True, "attempt_count": 1, "dead_lettered": False})
    relay_client.upsert_message_mapping = AsyncMock(return_value=None)

    max_client = MagicMock()

    async def _slow_send(*args, **kwargs):
        await asyncio.sleep(0.05)
        return {"ok": True}

    max_client.send_document = AsyncMock(side_effect=_slow_send)

    monkeypatch.setattr(main_module, "RELAY_COMMAND_PROCESS_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(asyncio.CancelledError):
        await _relay_command_loop(relay_client, max_client)

    relay_client.fail_command.assert_awaited_once()
    fail_call = relay_client.fail_command.await_args
    assert fail_call.args[0] == 4
    assert "Timed out while processing queued Telegram->Max command" in fail_call.kwargs["error"]
    relay_client.ack_command.assert_not_awaited()
