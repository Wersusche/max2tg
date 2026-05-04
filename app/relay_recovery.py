from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.relay_client import RelayClient
    from app.remote_deploy import RemoteRelayManager

log = logging.getLogger(__name__)


class RelayRecoveryController:
    def __init__(
        self,
        *,
        relay_client: RelayClient,
        remote_manager: RemoteRelayManager,
        enabled: bool = True,
        health_interval_seconds: float = 30.0,
        redeploy_after_failures: int = 3,
        redeploy_cooldown_seconds: float = 600.0,
        max_wait_seconds: float = 120.0,
        remote_deploy_enabled: bool = True,
    ) -> None:
        self.relay_client = relay_client
        self.remote_manager = remote_manager
        self.enabled = enabled
        self.health_interval_seconds = max(1.0, float(health_interval_seconds))
        self.redeploy_after_failures = max(1, int(redeploy_after_failures))
        self.redeploy_cooldown_seconds = max(0.0, float(redeploy_cooldown_seconds))
        self.max_wait_seconds = max(1.0, float(max_wait_seconds))
        self.remote_deploy_enabled = remote_deploy_enabled
        self._failed_recoveries = 0
        self._last_redeploy_started_at: float | None = None
        self._task_lock = asyncio.Lock()
        self._active_recovery_task: asyncio.Task[None] | None = None

    async def recover(self, reason: str = "relay request failed") -> None:
        if not self.enabled:
            log.warning("Relay recovery is disabled; cannot recover from: %s", reason)
            return

        task = await self._get_or_start_recovery_task(reason)
        if task is None:
            return

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.max_wait_seconds)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Relay recovery did not finish within {self.max_wait_seconds:.1f}s"
            ) from exc

    async def run_watchdog(self) -> None:
        if not self.enabled:
            log.info("Relay recovery watchdog disabled")
            return

        while True:
            await asyncio.sleep(self.health_interval_seconds)
            try:
                if await self.relay_client.healthcheck():
                    self._failed_recoveries = 0
                    continue

                log.warning("Relay healthcheck failed; starting recovery")
                await self.recover("watchdog healthcheck failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Relay recovery watchdog failed")

    async def _get_or_start_recovery_task(self, reason: str) -> asyncio.Task[None] | None:
        async with self._task_lock:
            task = self._active_recovery_task
            if task is not None and not task.done():
                return task
            if task is not None:
                self._consume_recovery_task_result(task)
                self._active_recovery_task = None

        if await self.relay_client.healthcheck():
            self._failed_recoveries = 0
            return None

        async with self._task_lock:
            task = self._active_recovery_task
            if task is not None and not task.done():
                return task
            if task is not None:
                self._consume_recovery_task_result(task)

            task = asyncio.create_task(self._recover_sequence(reason))
            task.add_done_callback(self._consume_recovery_task_result)
            self._active_recovery_task = task
            return task

    def _consume_recovery_task_result(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            pass

    async def _recover_sequence(self, reason: str) -> None:
        if await self.relay_client.healthcheck():
            self._failed_recoveries = 0
            return

        log.warning("Relay unavailable (%s); restarting SSH tunnel", reason)
        try:
            await self._restart_tunnel_and_wait()
            self._failed_recoveries = 0
            log.info("Relay recovered after SSH tunnel restart")
            return
        except Exception as exc:
            self._failed_recoveries += 1
            log.warning(
                "Relay tunnel recovery failed (%d/%d): %s",
                self._failed_recoveries,
                self.redeploy_after_failures,
                exc,
            )

        if self._failed_recoveries < self.redeploy_after_failures:
            raise RuntimeError("Relay tunnel recovery failed")

        if not self.remote_deploy_enabled:
            log.warning(
                "Relay recovery reached %d failed tunnel restarts, but remote deploy is disabled",
                self._failed_recoveries,
            )
            raise RuntimeError("Relay tunnel recovery failed and remote deploy is disabled")

        if not self._redeploy_cooldown_elapsed():
            log.warning("Relay redeploy suppressed by cooldown after %d failed recoveries", self._failed_recoveries)
            raise RuntimeError("Relay redeploy suppressed by cooldown")

        await self._redeploy_and_wait()

    async def _restart_tunnel_and_wait(self) -> None:
        await self.remote_manager.restart_tunnel()
        await self.relay_client.wait_until_healthy()

    def _redeploy_cooldown_elapsed(self) -> bool:
        if self._last_redeploy_started_at is None:
            return True
        return (time.monotonic() - self._last_redeploy_started_at) >= self.redeploy_cooldown_seconds

    async def _redeploy_and_wait(self) -> None:
        self._last_redeploy_started_at = time.monotonic()
        log.warning(
            "Relay recovery failed %d times; redeploying tg-relay",
            self._failed_recoveries,
        )
        await self.remote_manager.deploy()
        await self.remote_manager.restart_tunnel()
        await self.relay_client.wait_until_healthy()
        self._failed_recoveries = 0
        log.info("Relay recovered after tg-relay redeploy")
