import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    )
    relay_client.ack_command.assert_awaited_once_with(1)
