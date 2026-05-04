import time
from unittest.mock import AsyncMock

import pytest

from app.relay_recovery import RelayRecoveryController


def _make_controller(
    *,
    relay_client=None,
    remote_manager=None,
    redeploy_after_failures: int = 3,
    redeploy_cooldown_seconds: float = 600.0,
    remote_deploy_enabled: bool = True,
) -> RelayRecoveryController:
    relay_client = relay_client or AsyncMock()
    remote_manager = remote_manager or AsyncMock()
    return RelayRecoveryController(
        relay_client=relay_client,
        remote_manager=remote_manager,
        health_interval_seconds=1,
        redeploy_after_failures=redeploy_after_failures,
        redeploy_cooldown_seconds=redeploy_cooldown_seconds,
        max_wait_seconds=1,
        remote_deploy_enabled=remote_deploy_enabled,
    )


@pytest.mark.asyncio
async def test_recover_restarts_tunnel_and_waits_for_healthcheck():
    relay_client = AsyncMock()
    relay_client.healthcheck = AsyncMock(side_effect=[False, False])
    relay_client.wait_until_healthy = AsyncMock(return_value=None)
    remote_manager = AsyncMock()
    remote_manager.restart_tunnel = AsyncMock(return_value=None)
    remote_manager.deploy = AsyncMock(return_value=None)
    controller = _make_controller(relay_client=relay_client, remote_manager=remote_manager)

    await controller.recover("test failure")

    remote_manager.restart_tunnel.assert_awaited_once()
    relay_client.wait_until_healthy.assert_awaited_once()
    remote_manager.deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_redeploys_after_failed_tunnel_restarts():
    relay_client = AsyncMock()
    relay_client.healthcheck = AsyncMock(side_effect=[False, False, False, False, False, False])
    relay_client.wait_until_healthy = AsyncMock(return_value=None)
    remote_manager = AsyncMock()
    remote_manager.restart_tunnel = AsyncMock(
        side_effect=[
            RuntimeError("tunnel down"),
            RuntimeError("tunnel down"),
            RuntimeError("tunnel down"),
            None,
        ]
    )
    remote_manager.deploy = AsyncMock(return_value=None)
    controller = _make_controller(
        relay_client=relay_client,
        remote_manager=remote_manager,
        redeploy_after_failures=3,
    )

    with pytest.raises(RuntimeError):
        await controller.recover("first failure")
    with pytest.raises(RuntimeError):
        await controller.recover("second failure")
    await controller.recover("third failure")

    assert remote_manager.restart_tunnel.await_count == 4
    remote_manager.deploy.assert_awaited_once()
    assert controller._failed_recoveries == 0


@pytest.mark.asyncio
async def test_recover_respects_redeploy_cooldown():
    relay_client = AsyncMock()
    relay_client.healthcheck = AsyncMock(side_effect=[False, False])
    remote_manager = AsyncMock()
    remote_manager.restart_tunnel = AsyncMock(side_effect=RuntimeError("tunnel down"))
    remote_manager.deploy = AsyncMock(return_value=None)
    controller = _make_controller(
        relay_client=relay_client,
        remote_manager=remote_manager,
        redeploy_after_failures=1,
        redeploy_cooldown_seconds=600,
    )
    controller._last_redeploy_started_at = time.monotonic()

    with pytest.raises(RuntimeError, match="cooldown"):
        await controller.recover("still failing")

    remote_manager.deploy.assert_not_awaited()
