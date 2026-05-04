import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.main as main_module
from app.config import AccountProfile, MaxProfileSettings
from app.main import _relay_command_loop, _run_max_bridge
from app.relay_models import MaxCommand


class _FakeRecoveryController:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def recover(self, reason: str = "test") -> None:
        del reason

    async def run_watchdog(self) -> None:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _bridge_settings(**overrides):
    settings = {
        "relay_base_url": "http://127.0.0.1:18080",
        "relay_shared_secret": "secret",
        "foreign_ssh_host": "relay.example.com",
        "foreign_ssh_port": 22,
        "foreign_ssh_user": "relay",
        "foreign_ssh_private_key": "key",
        "foreign_app_dir": "/home/relay/max2tg",
        "foreign_relay_host_port": 8080,
        "relay_tunnel_local_port": 18080,
        "foreign_relay_env_text": "APP_ROLE=tg-relay\n",
        "remote_deploy_enabled": True,
        "relay_recovery_enabled": True,
        "relay_recovery_health_interval_seconds": 30,
        "relay_recovery_redeploy_after_failures": 3,
        "relay_recovery_redeploy_cooldown_seconds": 600,
        "relay_recovery_max_wait_seconds": 120,
        "max_token": "max-token",
        "max_device_id": "device",
        "max_chat_ids": None,
        "debug": False,
        "reply_enabled": False,
    }
    settings.update(overrides)
    settings.setdefault(
        "profiles",
        (
            AccountProfile(
                id="default",
                label="Default",
                max=MaxProfileSettings(
                    token=settings["max_token"],
                    device_id=settings["max_device_id"],
                    chat_ids=settings["max_chat_ids"],
                ),
            ),
        ),
    )
    settings.setdefault("enabled_profiles", settings["profiles"])
    return SimpleNamespace(**settings)


@pytest.mark.asyncio
async def test_run_max_bridge_starts_and_cancels_recovery_watchdog(monkeypatch):
    relay_client = MagicMock()
    relay_client.start = AsyncMock(return_value=None)
    relay_client.wait_until_healthy = AsyncMock(return_value=None)
    relay_client.stop = AsyncMock(return_value=None)
    relay_client.set_recovery_hook = MagicMock()
    remote_manager = MagicMock()
    remote_manager.deploy = AsyncMock(return_value=None)
    remote_manager.ensure_tunnel = AsyncMock(return_value=None)
    remote_manager.close = AsyncMock(return_value=None)
    recovery_controller = _FakeRecoveryController()
    max_client = MagicMock()

    async def _run_until_watchdog_starts():
        await recovery_controller.started.wait()

    max_client.run = AsyncMock(side_effect=_run_until_watchdog_starts)

    monkeypatch.setattr(main_module, "RelayClient", MagicMock(return_value=relay_client))
    monkeypatch.setattr(main_module, "RemoteRelayManager", MagicMock(return_value=remote_manager))
    monkeypatch.setattr(main_module, "RelayRecoveryController", MagicMock(return_value=recovery_controller))
    monkeypatch.setattr(main_module, "create_max_client", MagicMock(return_value=max_client))

    await _run_max_bridge(_bridge_settings())

    remote_manager.deploy.assert_awaited_once()
    remote_manager.ensure_tunnel.assert_awaited_once()
    relay_client.set_recovery_hook.assert_called_once_with(recovery_controller.recover)
    max_client.run.assert_awaited_once()
    assert recovery_controller.cancelled is True
    relay_client.stop.assert_awaited_once()
    remote_manager.close.assert_awaited_once()


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
        profile_id="default",
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
async def test_relay_command_loop_pulls_and_sends_for_profile():
    relay_client = MagicMock()
    relay_client.pull_command = AsyncMock(
        side_effect=[
            MaxCommand(id=5, profile_id="beta", max_chat_id="42", kind="text", text="beta reply"),
            asyncio.CancelledError(),
        ]
    )
    relay_client.ack_command = AsyncMock(return_value=None)
    relay_client.fail_command = AsyncMock(return_value={"ok": True, "attempt_count": 1, "dead_lettered": False})
    relay_client.upsert_message_mapping = AsyncMock(return_value=None)

    max_client = MagicMock()
    max_client.send_message = AsyncMock(return_value={"message": {"id": "max-beta"}})

    with pytest.raises(asyncio.CancelledError):
        await _relay_command_loop(relay_client, max_client, profile_id="beta")

    relay_client.pull_command.assert_any_await(timeout_seconds=30, profile_id="beta")
    max_client.send_message.assert_awaited_once_with(
        42,
        "beta reply",
        [],
        reply_to_max_message_id=None,
    )
    relay_client.ack_command.assert_awaited_once_with(5)


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
