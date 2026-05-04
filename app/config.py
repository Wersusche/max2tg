import base64
import binascii
import os
import re
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

APP_ROLE_MAX_BRIDGE = "max-bridge"
APP_ROLE_TG_RELAY = "tg-relay"
APP_ROLES = {APP_ROLE_MAX_BRIDGE, APP_ROLE_TG_RELAY}
DEFAULT_PROFILE_ID = "default"
_PRIVATE_KEY_MARKER_RE = re.compile(r"^-----(BEGIN|END) ([A-Z0-9 ]+PRIVATE KEY)-----$")
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ACCOUNTS_CONFIG_ENV_KEYS = {"ACCOUNTS_CONFIG_FILE", "ACCOUNTS_CONFIG_YAML_B64"}


@dataclass(frozen=True)
class MaxProfileSettings:
    token: str
    device_id: str
    chat_ids: str | None = None


@dataclass(frozen=True)
class TelegramProfileSettings:
    bot_token: str
    chat_id: str


@dataclass(frozen=True)
class AccountProfile:
    id: str
    label: str
    enabled: bool = True
    max: MaxProfileSettings | None = None
    telegram: TelegramProfileSettings | None = None


@dataclass(frozen=True)
class Settings:
    app_role: str
    relay_shared_secret: str
    debug: bool = False
    reply_enabled: bool = False
    accounts_config_file: str | None = None
    accounts_config_yaml_b64: str | None = None
    profiles: tuple[AccountProfile, ...] = ()
    max_chat_ids: str | None = None
    max_token: str | None = None
    max_device_id: str | None = None
    tg_bot_token: str | None = None
    tg_chat_id: str | None = None
    topic_db_path: str = "data/topics.sqlite3"
    command_db_path: str = "data/commands.sqlite3"
    message_db_path: str = "data/messages.sqlite3"
    relay_bind_host: str = "127.0.0.1"
    relay_bind_port: int = 8080
    relay_host_port: int = 8080
    relay_tunnel_local_port: int = 18080
    foreign_ssh_host: str | None = None
    foreign_ssh_port: int = 22
    foreign_ssh_user: str | None = None
    foreign_ssh_private_key: str | None = None
    foreign_ssh_private_key_file: str | None = None
    foreign_app_dir: str = "/home/relay/max2tg"
    remote_deploy_enabled: bool = True
    relay_recovery_enabled: bool = True
    relay_recovery_health_interval_seconds: int = 30
    relay_recovery_redeploy_after_failures: int = 3
    relay_recovery_redeploy_cooldown_seconds: int = 600
    relay_recovery_max_wait_seconds: int = 120
    foreign_relay_env_b64: str | None = None
    foreign_relay_env_file: str | None = None
    foreign_relay_env_content: str | None = None
    foreign_relay_host_port: int = 8080

    @property
    def relay_base_url(self) -> str:
        return f"http://127.0.0.1:{self.relay_tunnel_local_port}"

    @property
    def foreign_relay_env_text(self) -> str:
        if self.foreign_relay_env_content is not None:
            return self.foreign_relay_env_content
        return _decode_base64_env_text(self.foreign_relay_env_b64)

    @property
    def enabled_profiles(self) -> tuple[AccountProfile, ...]:
        return tuple(profile for profile in self.profiles if profile.enabled)


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


def _decode_base64_env_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise SystemExit("FOREIGN_RELAY_ENV_B64 must contain valid base64-encoded UTF-8 text") from exc


def _decode_base64_text(var_name: str, value: str | None) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise SystemExit(f"{var_name} must contain valid base64-encoded UTF-8 text") from exc


def _read_env_file(var_name: str, path_value: str | None) -> str | None:
    if not path_value:
        return None

    try:
        return Path(path_value).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SystemExit(f"{var_name} points to a missing file: {path_value}") from exc
    except OSError as exc:
        raise SystemExit(f"{var_name} could not be read: {path_value}: {exc}") from exc


def _read_text_file(var_name: str, path_value: str | None) -> str | None:
    if not path_value:
        return None

    try:
        return Path(path_value).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SystemExit(f"{var_name} points to a missing file: {path_value}") from exc
    except OSError as exc:
        raise SystemExit(f"{var_name} could not be read: {path_value}: {exc}") from exc


def _yaml_module():
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            "PyYAML is required for ACCOUNTS_CONFIG_FILE/ACCOUNTS_CONFIG_YAML_B64. "
            "Install dependencies from requirements.txt."
        ) from exc
    return yaml


def _load_account_profiles_from_yaml(raw_yaml: str, *, source: str) -> tuple[AccountProfile, ...]:
    yaml = _yaml_module()
    try:
        payload = yaml.safe_load(raw_yaml) or {}
    except Exception as exc:
        raise SystemExit(f"{source} must contain valid YAML: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit(f"{source} must be a YAML object")
    try:
        version = int(payload.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{source} version must be 1") from exc
    if version != 1:
        raise SystemExit(f"{source} version must be 1")

    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise SystemExit(f"{source} must define a non-empty profiles list")

    profiles: list[AccountProfile] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_profiles, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"{source} profile #{index} must be an object")

        profile_id = str(item.get("id") or "").strip()
        if not profile_id:
            raise SystemExit(f"{source} profile #{index} is missing id")
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise SystemExit(
                f"{source} profile id {profile_id!r} may contain only letters, digits, '.', '_' and '-'"
            )
        if profile_id in seen_ids:
            raise SystemExit(f"{source} contains duplicate profile id: {profile_id}")
        seen_ids.add(profile_id)

        max_settings = _parse_max_profile(item.get("max"), source=source, profile_id=profile_id)
        telegram_settings = _parse_telegram_profile(
            item.get("telegram"),
            source=source,
            profile_id=profile_id,
        )
        profiles.append(
            AccountProfile(
                id=profile_id,
                label=str(item.get("label") or profile_id),
                enabled=_yaml_bool(item.get("enabled"), default=True),
                max=max_settings,
                telegram=telegram_settings,
            )
        )

    return tuple(profiles)


def _parse_max_profile(
    payload: object,
    *,
    source: str,
    profile_id: str,
) -> MaxProfileSettings | None:
    if payload in (None, ""):
        return None
    if not isinstance(payload, dict):
        raise SystemExit(f"{source} profile {profile_id!r} max must be an object")

    token = str(payload.get("token") or "").strip()
    device_id = str(payload.get("device_id") or "").strip()
    chat_ids = _normalize_yaml_chat_ids(payload.get("chat_ids"))
    if not token and not device_id and not chat_ids:
        return None
    return MaxProfileSettings(token=token, device_id=device_id, chat_ids=chat_ids)


def _parse_telegram_profile(
    payload: object,
    *,
    source: str,
    profile_id: str,
) -> TelegramProfileSettings | None:
    if payload in (None, ""):
        return None
    if not isinstance(payload, dict):
        raise SystemExit(f"{source} profile {profile_id!r} telegram must be an object")

    bot_token = str(payload.get("bot_token") or "").strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    if not bot_token and not chat_id:
        return None
    return TelegramProfileSettings(bot_token=bot_token, chat_id=chat_id)


def _yaml_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _normalize_yaml_chat_ids(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (list, tuple)):
        normalized = [str(item).strip() for item in value if str(item).strip()]
        return ",".join(normalized) if normalized else None
    return str(value).strip() or None


def _load_account_profiles(env: dict[str, str]) -> tuple[AccountProfile, ...]:
    config_file = env.get("ACCOUNTS_CONFIG_FILE") or None
    config_b64 = env.get("ACCOUNTS_CONFIG_YAML_B64") or None
    if config_file:
        text = _read_text_file("ACCOUNTS_CONFIG_FILE", config_file)
        return _load_account_profiles_from_yaml(text or "", source=config_file)
    if config_b64:
        text = _decode_base64_text("ACCOUNTS_CONFIG_YAML_B64", config_b64)
        return _load_account_profiles_from_yaml(text, source="ACCOUNTS_CONFIG_YAML_B64")
    return ()


def _legacy_bridge_profile(settings: "Settings") -> AccountProfile:
    return AccountProfile(
        id=DEFAULT_PROFILE_ID,
        label="Default",
        max=MaxProfileSettings(
            token=settings.max_token or "",
            device_id=settings.max_device_id or "",
            chat_ids=settings.max_chat_ids,
        ),
    )


def _legacy_relay_profile(settings: "Settings") -> AccountProfile:
    return AccountProfile(
        id=DEFAULT_PROFILE_ID,
        label="Default",
        telegram=TelegramProfileSettings(
            bot_token=settings.tg_bot_token or "",
            chat_id=settings.tg_chat_id or "",
        ),
    )


def _validate_bridge_profiles(profiles: tuple[AccountProfile, ...], *, source: str) -> None:
    enabled = [profile for profile in profiles if profile.enabled]
    if not enabled:
        raise SystemExit(f"{source} must contain at least one enabled profile")
    for profile in enabled:
        if profile.max is None or not profile.max.token or not profile.max.device_id:
            raise SystemExit(
                f"{source} profile {profile.id!r} must define max.token and max.device_id"
            )
        if profile.telegram is None or not profile.telegram.bot_token or not profile.telegram.chat_id:
            raise SystemExit(
                f"{source} profile {profile.id!r} must define telegram.bot_token and telegram.chat_id"
            )
        _validate_tg_chat_id(profile.telegram.chat_id, f"{source} profile {profile.id!r}")


def _validate_relay_profiles(profiles: tuple[AccountProfile, ...], *, source: str) -> None:
    enabled = [profile for profile in profiles if profile.enabled]
    if not enabled:
        raise SystemExit(f"{source} must contain at least one enabled profile")
    for profile in enabled:
        if profile.telegram is None or not profile.telegram.bot_token or not profile.telegram.chat_id:
            raise SystemExit(
                f"{source} profile {profile.id!r} must define telegram.bot_token and telegram.chat_id"
            )
        _validate_tg_chat_id(profile.telegram.chat_id, f"{source} profile {profile.id!r}")


def _validate_tg_chat_id(raw_value: str, description: str) -> None:
    try:
        int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{description} telegram.chat_id must be a valid integer, got: {raw_value!r}") from exc


def _dump_telegram_profiles_yaml(profiles: tuple[AccountProfile, ...]) -> str:
    yaml = _yaml_module()
    data = {
        "version": 1,
        "profiles": [
            {
                "id": profile.id,
                "label": profile.label,
                "enabled": profile.enabled,
                "telegram": {
                    "bot_token": profile.telegram.bot_token if profile.telegram else "",
                    "chat_id": profile.telegram.chat_id if profile.telegram else "",
                },
            }
            for profile in profiles
        ],
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _prepare_foreign_relay_env_text(
    *,
    remote_env_text: str,
    profiles: tuple[AccountProfile, ...],
    relay_shared_secret: str,
    reply_enabled: bool,
    debug: bool,
) -> str:
    if not profiles:
        return remote_env_text

    if not remote_env_text:
        remote_env_text = "\n".join(
            [
                "APP_ROLE=tg-relay",
                f"RELAY_SHARED_SECRET={relay_shared_secret}",
                "RELAY_BIND_HOST=0.0.0.0",
                "RELAY_BIND_PORT=8080",
                "TOPIC_DB_PATH=data/topics.sqlite3",
                "COMMAND_DB_PATH=data/commands.sqlite3",
                "MESSAGE_DB_PATH=data/messages.sqlite3",
                f"REPLY_ENABLED={'true' if reply_enabled else 'false'}",
                f"DEBUG={'true' if debug else 'false'}",
                "",
            ]
        )

    telegram_yaml = _dump_telegram_profiles_yaml(profiles)
    telegram_yaml_b64 = base64.b64encode(telegram_yaml.encode("utf-8")).decode("ascii")
    return _replace_env_assignments(
        remote_env_text,
        {
            "ACCOUNTS_CONFIG_YAML_B64": telegram_yaml_b64,
        },
        drop_keys=_ACCOUNTS_CONFIG_ENV_KEYS,
    )


def _replace_env_assignments(
    text: str,
    replacements: dict[str, str],
    *,
    drop_keys: set[str],
) -> str:
    kept_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in drop_keys:
            continue
        kept_lines.append(line)

    if kept_lines and kept_lines[-1].strip():
        kept_lines.append("")
    for key, value in replacements.items():
        kept_lines.append(f"{key}={value}")
    return "\n".join(kept_lines).rstrip() + "\n"


def _resolve_foreign_relay_host_port(remote_env_text: str, default_port: int) -> int:
    if not remote_env_text:
        return default_port

    remote_env = dotenv_values(stream=StringIO(remote_env_text))
    raw_port = remote_env.get("RELAY_HOST_PORT") or remote_env.get("RELAY_BIND_PORT")
    if not raw_port:
        return default_port

    try:
        return int(raw_port)
    except ValueError as exc:
        raise SystemExit(
            "Foreign relay env must define RELAY_HOST_PORT/RELAY_BIND_PORT as a valid integer"
        ) from exc


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


def _require_value(value: str | None, description: str) -> None:
    if not value:
        raise SystemExit(
            f"Missing required environment variable: {description}\n"
            "Copy .env.example to .env and fill in the values."
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

    relay_bind_port = _env_int("RELAY_BIND_PORT", 8080)
    relay_host_port = _env_int("RELAY_HOST_PORT", relay_bind_port)
    foreign_ssh_private_key_file = env.get("FOREIGN_SSH_PRIVATE_KEY_FILE") or None
    foreign_ssh_private_key_text = None
    foreign_relay_env_file = env.get("FOREIGN_RELAY_ENV_FILE") or None
    foreign_relay_env_b64 = env.get("FOREIGN_RELAY_ENV_B64") or None
    foreign_relay_env_text = ""
    account_profiles = _load_account_profiles(env)
    if app_role == APP_ROLE_MAX_BRIDGE:
        foreign_ssh_private_key_text = _read_env_file("FOREIGN_SSH_PRIVATE_KEY_FILE", foreign_ssh_private_key_file)
        if foreign_ssh_private_key_text is None:
            foreign_ssh_private_key_text = env.get("FOREIGN_SSH_PRIVATE_KEY") or None

        foreign_relay_env_text = _read_env_file("FOREIGN_RELAY_ENV_FILE", foreign_relay_env_file)
        if foreign_relay_env_text is None:
            foreign_relay_env_text = _decode_base64_env_text(foreign_relay_env_b64)
        else:
            foreign_relay_env_b64 = None

        if account_profiles:
            _validate_bridge_profiles(account_profiles, source=env.get("ACCOUNTS_CONFIG_FILE") or "ACCOUNTS_CONFIG_YAML_B64")
            foreign_relay_env_text = _prepare_foreign_relay_env_text(
                remote_env_text=foreign_relay_env_text,
                profiles=account_profiles,
                relay_shared_secret=env["RELAY_SHARED_SECRET"],
                reply_enabled=_env_flag("REPLY_ENABLED"),
                debug=_env_flag("DEBUG"),
            )
            foreign_relay_env_b64 = None

    foreign_relay_host_port = _resolve_foreign_relay_host_port(foreign_relay_env_text, relay_host_port)

    settings = Settings(
        app_role=app_role,
        relay_shared_secret=env["RELAY_SHARED_SECRET"],
        debug=_env_flag("DEBUG"),
        reply_enabled=_env_flag("REPLY_ENABLED"),
        accounts_config_file=env.get("ACCOUNTS_CONFIG_FILE") or None,
        accounts_config_yaml_b64=env.get("ACCOUNTS_CONFIG_YAML_B64") or None,
        profiles=account_profiles,
        max_chat_ids=env.get("MAX_CHAT_IDS") or None,
        max_token=env.get("MAX_TOKEN") or None,
        max_device_id=env.get("MAX_DEVICE_ID") or None,
        tg_bot_token=env.get("TG_BOT_TOKEN") or None,
        tg_chat_id=env.get("TG_CHAT_ID") or None,
        topic_db_path=env.get("TOPIC_DB_PATH") or "data/topics.sqlite3",
        command_db_path=env.get("COMMAND_DB_PATH") or "data/commands.sqlite3",
        message_db_path=env.get("MESSAGE_DB_PATH") or "data/messages.sqlite3",
        relay_bind_host=env.get("RELAY_BIND_HOST") or "127.0.0.1",
        relay_bind_port=relay_bind_port,
        relay_host_port=relay_host_port,
        relay_tunnel_local_port=_env_int("RELAY_TUNNEL_LOCAL_PORT", 18080),
        foreign_ssh_host=env.get("FOREIGN_SSH_HOST") or None,
        foreign_ssh_port=_env_int("FOREIGN_SSH_PORT", 22),
        foreign_ssh_user=env.get("FOREIGN_SSH_USER") or None,
        foreign_ssh_private_key=_normalize_ssh_private_key(foreign_ssh_private_key_text),
        foreign_ssh_private_key_file=foreign_ssh_private_key_file,
        foreign_app_dir=env.get("FOREIGN_APP_DIR") or "/home/relay/max2tg",
        remote_deploy_enabled=_env_flag("REMOTE_DEPLOY_ENABLED", default=True),
        relay_recovery_enabled=_env_flag("RELAY_RECOVERY_ENABLED", default=True),
        relay_recovery_health_interval_seconds=_env_int("RELAY_RECOVERY_HEALTH_INTERVAL_SECONDS", 30),
        relay_recovery_redeploy_after_failures=_env_int("RELAY_RECOVERY_REDEPLOY_AFTER_FAILURES", 3),
        relay_recovery_redeploy_cooldown_seconds=_env_int("RELAY_RECOVERY_REDEPLOY_COOLDOWN_SECONDS", 600),
        relay_recovery_max_wait_seconds=_env_int("RELAY_RECOVERY_MAX_WAIT_SECONDS", 120),
        foreign_relay_env_b64=foreign_relay_env_b64,
        foreign_relay_env_file=foreign_relay_env_file,
        foreign_relay_env_content=foreign_relay_env_text,
        foreign_relay_host_port=foreign_relay_host_port,
    )

    if settings.app_role == APP_ROLE_MAX_BRIDGE:
        if not settings.profiles:
            _require(env, ["MAX_TOKEN", "MAX_DEVICE_ID"])
            settings = _replace_settings_profiles(settings, (_legacy_bridge_profile(settings),))
        _require(env, ["FOREIGN_SSH_HOST", "FOREIGN_SSH_USER"])
        _require_value(
            settings.foreign_ssh_private_key,
            "FOREIGN_SSH_PRIVATE_KEY_FILE or FOREIGN_SSH_PRIVATE_KEY",
        )
        if settings.remote_deploy_enabled:
            _require_value(
                settings.foreign_relay_env_text,
                "FOREIGN_RELAY_ENV_FILE or FOREIGN_RELAY_ENV_B64",
            )
        _reject_present(
            env,
            ["TG_BOT_TOKEN", "TG_CHAT_ID", "TOPIC_DB_PATH", "COMMAND_DB_PATH", "MESSAGE_DB_PATH"],
            role=settings.app_role,
        )
        _validate_ssh_private_key(settings.foreign_ssh_private_key)
        _validate_base64_env(settings.foreign_relay_env_b64)
        return settings

    if settings.profiles:
        _validate_relay_profiles(settings.profiles, source=env.get("ACCOUNTS_CONFIG_FILE") or "ACCOUNTS_CONFIG_YAML_B64")
    else:
        _require(env, ["TG_BOT_TOKEN", "TG_CHAT_ID"])
        settings = _replace_settings_profiles(settings, (_legacy_relay_profile(settings),))
    _reject_present(
        env,
        [
            "MAX_TOKEN",
            "MAX_DEVICE_ID",
            "MAX_CHAT_IDS",
            "FOREIGN_SSH_HOST",
            "FOREIGN_SSH_PORT",
            "FOREIGN_SSH_USER",
            "FOREIGN_SSH_PRIVATE_KEY",
            "FOREIGN_SSH_PRIVATE_KEY_FILE",
            "FOREIGN_APP_DIR",
            "FOREIGN_RELAY_ENV_B64",
            "FOREIGN_RELAY_ENV_FILE",
        ],
        role=settings.app_role,
    )

    if not settings.accounts_config_file and not settings.accounts_config_yaml_b64:
        try:
            int(settings.tg_chat_id or "")
        except ValueError as exc:
            raise SystemExit(f"TG_CHAT_ID must be a valid integer, got: {settings.tg_chat_id!r}") from exc

    return settings


def _validate_base64_env(value: str | None) -> None:
    if not value:
        return
    _decode_base64_env_text(value)


def _replace_settings_profiles(
    settings: Settings,
    profiles: tuple[AccountProfile, ...],
) -> Settings:
    return Settings(
        app_role=settings.app_role,
        relay_shared_secret=settings.relay_shared_secret,
        debug=settings.debug,
        reply_enabled=settings.reply_enabled,
        accounts_config_file=settings.accounts_config_file,
        accounts_config_yaml_b64=settings.accounts_config_yaml_b64,
        profiles=profiles,
        max_chat_ids=settings.max_chat_ids,
        max_token=settings.max_token,
        max_device_id=settings.max_device_id,
        tg_bot_token=settings.tg_bot_token,
        tg_chat_id=settings.tg_chat_id,
        topic_db_path=settings.topic_db_path,
        command_db_path=settings.command_db_path,
        message_db_path=settings.message_db_path,
        relay_bind_host=settings.relay_bind_host,
        relay_bind_port=settings.relay_bind_port,
        relay_host_port=settings.relay_host_port,
        relay_tunnel_local_port=settings.relay_tunnel_local_port,
        foreign_ssh_host=settings.foreign_ssh_host,
        foreign_ssh_port=settings.foreign_ssh_port,
        foreign_ssh_user=settings.foreign_ssh_user,
        foreign_ssh_private_key=settings.foreign_ssh_private_key,
        foreign_ssh_private_key_file=settings.foreign_ssh_private_key_file,
        foreign_app_dir=settings.foreign_app_dir,
        remote_deploy_enabled=settings.remote_deploy_enabled,
        relay_recovery_enabled=settings.relay_recovery_enabled,
        relay_recovery_health_interval_seconds=settings.relay_recovery_health_interval_seconds,
        relay_recovery_redeploy_after_failures=settings.relay_recovery_redeploy_after_failures,
        relay_recovery_redeploy_cooldown_seconds=settings.relay_recovery_redeploy_cooldown_seconds,
        relay_recovery_max_wait_seconds=settings.relay_recovery_max_wait_seconds,
        foreign_relay_env_b64=settings.foreign_relay_env_b64,
        foreign_relay_env_file=settings.foreign_relay_env_file,
        foreign_relay_env_content=settings.foreign_relay_env_content,
        foreign_relay_host_port=settings.foreign_relay_host_port,
    )
