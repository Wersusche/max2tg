import asyncio
import concurrent.futures
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiohttp import web
from telegram import Update

from app.command_store import CommandStore
from app.config import APP_ROLE_MAX_BRIDGE, APP_ROLE_TG_RELAY, load_settings
from app.max_listener import create_max_client
from app.relay_client import RelayClient, RelayStatusSender
from app.relay_server import RelayBatchProcessor, create_relay_app
from app.remote_deploy import RemoteRelayManager
from app.tg_handler import build_tg_app
from app.tg_sender import TelegramSender
from app.topic_router import TopicRouter
from app.topic_store import TopicStore

threading.stack_size(524288)

log = logging.getLogger("max2tg")
APP_ROOT_DIR = Path(__file__).resolve().parent.parent


class _SyncExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor that runs callables synchronously without spawning threads."""

    def submit(self, fn, /, *args, **kwargs):
        f: concurrent.futures.Future = concurrent.futures.Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as exc:
            f.set_exception(exc)
        return f


def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    log_dir = os.environ.get("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "max2tg.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    logging.basicConfig(level=level, handlers=[console_handler, file_handler], force=True)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING if not debug else logging.DEBUG)


def _parse_max_chat_id(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


async def _relay_command_loop(relay_client: RelayClient, max_client) -> None:
    while True:
        try:
            command = await relay_client.pull_command(timeout_seconds=30)
            if command is None:
                continue
            response = await max_client.send_message(
                _parse_max_chat_id(command.max_chat_id),
                command.text,
                command.elements,
            )
            if response:
                await relay_client.ack_command(command.id)
            else:
                log.warning("Max rejected queued command id=%s chat=%s", command.id, command.max_chat_id)
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Relay command loop failed")
            await asyncio.sleep(5)


async def _start_relay_http_server(settings, processor: RelayBatchProcessor, command_store: CommandStore) -> web.AppRunner:
    app = create_relay_app(processor, command_store, settings.relay_shared_secret)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.relay_bind_host, port=settings.relay_bind_port)
    await site.start()
    log.info(
        "Relay HTTP server started on %s:%s",
        settings.relay_bind_host,
        settings.relay_bind_port,
    )
    return runner


async def _run_max_bridge(settings) -> None:
    relay_client = RelayClient(settings.relay_base_url, settings.relay_shared_secret)
    await relay_client.start()

    remote_manager = RemoteRelayManager(
        host=settings.foreign_ssh_host,
        port=settings.foreign_ssh_port,
        user=settings.foreign_ssh_user,
        private_key=settings.foreign_ssh_private_key,
        remote_app_dir=settings.foreign_app_dir,
        relay_host_port=settings.foreign_relay_host_port,
        local_tunnel_port=settings.relay_tunnel_local_port,
        remote_env_text=settings.foreign_relay_env_text,
        workspace_dir=str(APP_ROOT_DIR),
        remote_deploy_enabled=settings.remote_deploy_enabled,
    )

    sender = RelayStatusSender(relay_client)
    command_task = None
    try:
        if settings.remote_deploy_enabled:
            log.info("Deploying tg-relay to %s ...", settings.foreign_ssh_host)
            await remote_manager.deploy()
        await remote_manager.ensure_tunnel()
        await relay_client.wait_until_healthy()

        client = create_max_client(
            settings.max_token,
            settings.max_device_id,
            sender=sender,
            max_chat_ids=settings.max_chat_ids,
            debug=settings.debug,
            reply_enabled=settings.reply_enabled,
            relay_client=relay_client,
        )

        if settings.reply_enabled:
            command_task = asyncio.create_task(_relay_command_loop(relay_client, client))

        log.info("Starting Max listener in APP_ROLE=max-bridge ...")
        await client.run()
    finally:
        if command_task is not None:
            command_task.cancel()
            try:
                await command_task
            except asyncio.CancelledError:
                pass
        await relay_client.stop()
        await remote_manager.close()


async def _run_tg_relay(settings) -> None:
    sender = TelegramSender(settings.tg_bot_token, settings.tg_chat_id)
    await sender.start()
    topic_store = TopicStore(settings.topic_db_path)
    command_store = CommandStore(settings.command_db_path)
    topic_router = TopicRouter(topic_store, sender)
    processor = RelayBatchProcessor(sender, topic_router)
    http_runner = await _start_relay_http_server(settings, processor, command_store)

    tg_app = None
    if settings.reply_enabled:
        tg_app = build_tg_app(
            settings.tg_bot_token,
            settings.tg_chat_id,
            topic_store,
            command_store=command_store,
        )
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        log.info("Telegram polling started on tg-relay")
    else:
        log.info("Reply to Max disabled (REPLY_ENABLED=false)")

    try:
        await asyncio.Future()
    finally:
        if tg_app:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        await http_runner.cleanup()
        command_store.close()
        topic_store.close()
        await sender.stop()


async def main():
    loop = asyncio.get_running_loop()
    loop.set_default_executor(_SyncExecutor())

    settings = load_settings()
    _configure_logging(settings.debug)

    log.info("APP_ROLE=%s | Debug=%s", settings.app_role, "ON" if settings.debug else "OFF")

    if settings.app_role == APP_ROLE_MAX_BRIDGE:
        await _run_max_bridge(settings)
        return
    if settings.app_role == APP_ROLE_TG_RELAY:
        await _run_tg_relay(settings)
        return
    raise RuntimeError(f"Unsupported APP_ROLE: {settings.app_role}")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
