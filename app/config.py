import base64
import binascii
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

APP_ROLE_MAX_BRIDGE = "max-bridge"
APP_ROLE_TG_RELAY = "tg-relay"
APP_ROLES = {APP_ROLE_MAX_BRIDGE, APP_ROLE_TG_RELAY}
_PRIVATE_KEY_MARKER_RE = re.compile(r"^-----(BEGIN|END) ([A-Z0-9 ]+PRIVATE KEY)-----$")


@dataclass(frozen=True)
class Settings:
    app_role: str
    relay_shared_secret: str
    debug: bool = False
    reply_enabled: bool = False
    max_chat_ids: str | None = None
    max_token: str | None = None
    max_device_id: str | None = None
    tg_bot_token: str | None = None
    tg_chat_id: str | None = None
    topic_db_path: str = "data/topics.sqlite3"
    command_db_path: str = "data/commands.sqlite3"
    relay_bind_host: str = "127.0.0.1"
    relay_bind_port: int = 8080
    relay_tunnel_local_port: int = 18080
    foreign_ssh_host: str | None = None
    foreign_ssh_port: int = 22
    foreign_ssh_user: str | None = None
    foreign_ssh_private_key: str | None = None
    foreign_app_dir: str = "/opt/max2tg-relay"
    remote_deploy_enabled: bool = True
    foreign_relay_env_b64: str | None = None

    @property
    def relay_base_url(self) -> str:
        return f"http://127.0.0.1:{self.relay_tunnel_local_port}"

    @property
    def foreign_relay_env_text(self) -> str:
        if not self.foreign_relay_env_b64:
            return ""
        try:
            return base64.b64decode(self.foreign_relay_env_b64, validate=True).decode("utf-8")
        except Exception as exc:  # pragma: no cover - guarded in load_settings
            raise SystemExit("FOREIGN_RELAY_ENV_B64 must contain valid base64-encoded UTF-8 text") from exc


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a valid integer, got: {raw!r}") from exc


def _require(env: dict[str, str], names: list[str]) -> None:
    missing = [name for name in names if not env.get(name)]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )


def _reject_present(env: dict[str, str], names: list[str], *, role: str) -> None:
    present = [name for name in names if env.get(name)]
    if present:
        raise SystemExit(
            f"Environment variables not allowed for APP_ROLE={role}: {', '.join(present)}"
        )


def _normalize_ssh_private_key(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value.strip()
    if not normalized:
        return ""

    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in normalized:
        normalized = normalized.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")

    return normalized.rstrip("\n") + "\n"


def _validate_ssh_private_key(value: str | None) -> None:
    if value is None:
        return

    non_empty_lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(non_empty_lines) < 3:
        raise SystemExit(
            "FOREIGN_SSH_PRIVATE_KEY must contain a complete private key with BEGIN/END PRIVATE KEY markers"
        )

    begin_match = _PRIVATE_KEY_MARKER_RE.fullmatch(non_empty_lines[0])
    end_match = _PRIVATE_KEY_MARKER_RE.fullmatch(non_empty_lines[-1])
    if begin_match is None or begin_match.group(1) != "BEGIN":
        raise SystemExit(
            "FOREIGN_SSH_PRIVATE_KEY must contain a complete private key with BEGIN/END PRIVATE KEY markers"
        )
    if end_match is None or end_match.group(1) != "END":
        raise SystemExit(
            "FOREIGN_SSH_PRIVATE_KEY must contain a complete private key with BEGIN/END PRIVATE KEY markers"
        )
    if begin_match.group(2) != end_match.group(2):
        raise SystemExit(
            "FOREIGN_SSH_PRIVATE_KEY must contain matching BEGIN/END PRIVATE KEY markers"
        )


def load_settings() -> Settings:
    load_dotenv()
    env = dict(os.environ)

    app_role = env.get("APP_ROLE", APP_ROLE_MAX_BRIDGE)
    if app_role not in APP_ROLES:
        raise SystemExit(
            f"APP_ROLE must be one of: {', '.join(sorted(APP_ROLES))}; got {app_role!r}"
        )

    _require(env, ["RELAY_SHARED_SECRET"])

    settings = Settings(
        app_role=app_role,
        relay_shared_secret=env["RELAY_SHARED_SECRET"],
        debug=_env_flag("DEBUG"),
        reply_enabled=_env_flag("REPLY_ENABLED"),
        max_chat_ids=env.get("MAX_CHAT_IDS") or None,
        max_token=env.get("MAX_TOKEN") or None,
        max_device_id=env.get("MAX_DEVICE_ID") or None,
        tg_bot_token=env.get("TG_BOT_TOKEN") or None,
        tg_chat_id=env.get("TG_CHAT_ID") or None,
        topic_db_path=env.get("TOPIC_DB_PATH") or "data/topics.sqlite3",
        command_db_path=env.get("COMMAND_DB_PATH") or "data/commands.sqlite3",
        relay_bind_host=env.get("RELAY_BIND_HOST") or "127.0.0.1",
        relay_bind_port=_env_int("RELAY_BIND_PORT", 8080),
        relay_tunnel_local_port=_env_int("RELAY_TUNNEL_LOCAL_PORT", 18080),
        foreign_ssh_host=env.get("FOREIGN_SSH_HOST") or None,
        foreign_ssh_port=_env_int("FOREIGN_SSH_PORT", 22),
        foreign_ssh_user=env.get("FOREIGN_SSH_USER") or None,
        foreign_ssh_private_key=_normalize_ssh_private_key(env.get("FOREIGN_SSH_PRIVATE_KEY") or None),
        foreign_app_dir=env.get("FOREIGN_APP_DIR") or "/opt/max2tg-relay",
        remote_deploy_enabled=_env_flag("REMOTE_DEPLOY_ENABLED", default=True),
        foreign_relay_env_b64=env.get("FOREIGN_RELAY_ENV_B64") or None,
    )

    if settings.app_role == APP_ROLE_MAX_BRIDGE:
        _require(env, ["MAX_TOKEN", "MAX_DEVICE_ID", "FOREIGN_SSH_HOST", "FOREIGN_SSH_USER", "FOREIGN_SSH_PRIVATE_KEY"])
        if settings.remote_deploy_enabled:
            _require(env, ["FOREIGN_RELAY_ENV_B64"])
        _reject_present(
            env,
            ["TG_BOT_TOKEN", "TG_CHAT_ID", "TOPIC_DB_PATH", "COMMAND_DB_PATH"],
            role=settings.app_role,
        )
        _validate_ssh_private_key(settings.foreign_ssh_private_key)
        _validate_base64_env(settings.foreign_relay_env_b64)
        return settings

    _require(env, ["TG_BOT_TOKEN", "TG_CHAT_ID"])
    _reject_present(
        env,
        ["MAX_TOKEN", "MAX_DEVICE_ID", "MAX_CHAT_IDS", "FOREIGN_SSH_HOST", "FOREIGN_SSH_PORT", "FOREIGN_SSH_USER", "FOREIGN_SSH_PRIVATE_KEY", "FOREIGN_APP_DIR", "FOREIGN_RELAY_ENV_B64"],
        role=settings.app_role,
    )

    try:
        int(settings.tg_chat_id or "")
    except ValueError as exc:
        raise SystemExit(f"TG_CHAT_ID must be a valid integer, got: {settings.tg_chat_id!r}") from exc

    return settings


def _validate_base64_env(value: str | None) -> None:
    if not value:
        return
    try:
        base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise SystemExit("FOREIGN_RELAY_ENV_B64 must contain valid base64-encoded UTF-8 text") from exc
