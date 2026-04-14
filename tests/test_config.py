"""Tests for app/config.py."""

import base64
import os
from unittest.mock import patch

import pytest

from app.config import (
    APP_ROLE_MAX_BRIDGE,
    APP_ROLE_TG_RELAY,
    Settings,
    load_settings,
)

OPENSSH_PRIVATE_KEY = "\n".join(
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW",
        "QyNTUxOQAAACD58iHNas/wVNn0hgWHdOv19P5F8m8M7bZ179RoBTMDpQAAAKAIqVTdCKlU",
        "3QAAAAtzc2gtZWQyNTUxOQAAACD58iHNas/wVNn0hgWHdOv19P5F8m8M7bZ179RoBTMDpQ",
        "AAAEDzts/jCJHZ2VR/DDkD9xtu8X14UPsPR8jdkpAKlH35HvnyIc1qz/BU2fSGBYd06/X0",
        "/kXybwzttnXv1GgFMwOlAAAAFm1heC1icmlkZ2UtdG8tdGctcmVsYXkBAgMEBQYH",
        "-----END OPENSSH PRIVATE KEY-----",
    ]
) + "\n"
OPENSSH_PRIVATE_KEY_LITERAL_N = OPENSSH_PRIVATE_KEY.rstrip("\n").replace("\n", "\\n")
OPENSSH_PRIVATE_KEY_LITERAL_CRLF = OPENSSH_PRIVATE_KEY.rstrip("\n").replace("\n", "\\r\\n")
OPENSSH_PRIVATE_KEY_CRLF = OPENSSH_PRIVATE_KEY.replace("\n", "\r\n")


def _load_settings_with_env(env: dict) -> Settings:
    with patch("app.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
        return load_settings()


def _b64_env(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _bridge_env(**overrides) -> dict[str, str]:
    env = {
        "APP_ROLE": APP_ROLE_MAX_BRIDGE,
        "RELAY_SHARED_SECRET": "secret-123",
        "MAX_TOKEN": "token123",
        "MAX_DEVICE_ID": "device-abc",
        "FOREIGN_SSH_HOST": "relay.example.com",
        "FOREIGN_SSH_USER": "deploy",
        "FOREIGN_SSH_PRIVATE_KEY": OPENSSH_PRIVATE_KEY,
        "FOREIGN_RELAY_ENV_B64": _b64_env(
            "\n".join(
                [
                    "APP_ROLE=tg-relay",
                    "RELAY_SHARED_SECRET=secret-123",
                    "TG_BOT_TOKEN=123456:AAABBBCCC",
                    "TG_CHAT_ID=-100123456",
                ]
            )
        ),
    }
    env.update(overrides)
    return env


def _relay_env(**overrides) -> dict[str, str]:
    env = {
        "APP_ROLE": APP_ROLE_TG_RELAY,
        "RELAY_SHARED_SECRET": "secret-123",
        "TG_BOT_TOKEN": "123456:AAABBBCCC",
        "TG_CHAT_ID": "-100123456",
    }
    env.update(overrides)
    return env


class TestSettingsDataclass:
    def test_frozen(self):
        settings = Settings(app_role=APP_ROLE_MAX_BRIDGE, relay_shared_secret="secret")
        with pytest.raises((AttributeError, TypeError)):
            settings.app_role = APP_ROLE_TG_RELAY  # type: ignore[misc]

    def test_defaults(self):
        settings = Settings(app_role=APP_ROLE_MAX_BRIDGE, relay_shared_secret="secret")
        assert settings.debug is False
        assert settings.reply_enabled is False
        assert settings.max_chat_ids is None
        assert settings.topic_db_path == "data/topics.sqlite3"
        assert settings.command_db_path == "data/commands.sqlite3"
        assert settings.message_db_path == "data/messages.sqlite3"
        assert settings.relay_bind_host == "127.0.0.1"
        assert settings.relay_bind_port == 8080
        assert settings.relay_host_port == 8080
        assert settings.relay_tunnel_local_port == 18080
        assert settings.foreign_app_dir == "/home/relay/max2tg"
        assert settings.foreign_relay_host_port == 8080


class TestLoadSettingsMaxBridge:
    def test_valid_env(self):
        settings = _load_settings_with_env(_bridge_env(DEBUG="true", REPLY_ENABLED="yes", MAX_CHAT_IDS="1,2"))
        assert settings.app_role == APP_ROLE_MAX_BRIDGE
        assert settings.max_token == "token123"
        assert settings.max_device_id == "device-abc"
        assert settings.max_chat_ids == "1,2"
        assert settings.debug is True
        assert settings.reply_enabled is True
        assert settings.foreign_ssh_host == "relay.example.com"
        assert settings.foreign_ssh_private_key == OPENSSH_PRIVATE_KEY
        assert settings.foreign_relay_env_text.startswith("APP_ROLE=tg-relay")

    def test_remote_deploy_env_blob_optional_when_deploy_disabled(self):
        settings = _load_settings_with_env(
            _bridge_env(REMOTE_DEPLOY_ENABLED="false", FOREIGN_RELAY_ENV_B64="")
        )
        assert settings.remote_deploy_enabled is False
        assert settings.foreign_relay_env_b64 is None

    def test_multiline_private_key_is_preserved(self):
        settings = _load_settings_with_env(_bridge_env(FOREIGN_SSH_PRIVATE_KEY=OPENSSH_PRIVATE_KEY))
        assert settings.foreign_ssh_private_key == OPENSSH_PRIVATE_KEY

    def test_literal_newline_private_key_is_normalized(self):
        settings = _load_settings_with_env(
            _bridge_env(FOREIGN_SSH_PRIVATE_KEY=OPENSSH_PRIVATE_KEY_LITERAL_N)
        )
        assert settings.foreign_ssh_private_key == OPENSSH_PRIVATE_KEY

    def test_literal_windows_newline_private_key_is_normalized(self):
        settings = _load_settings_with_env(
            _bridge_env(FOREIGN_SSH_PRIVATE_KEY=OPENSSH_PRIVATE_KEY_LITERAL_CRLF)
        )
        assert settings.foreign_ssh_private_key == OPENSSH_PRIVATE_KEY

    def test_windows_line_endings_private_key_are_normalized(self):
        settings = _load_settings_with_env(_bridge_env(FOREIGN_SSH_PRIVATE_KEY=OPENSSH_PRIVATE_KEY_CRLF))
        assert settings.foreign_ssh_private_key == OPENSSH_PRIVATE_KEY

    def test_default_app_role_is_max_bridge(self):
        env = _bridge_env()
        env.pop("APP_ROLE")
        settings = _load_settings_with_env(env)
        assert settings.app_role == APP_ROLE_MAX_BRIDGE

    def test_bridge_allows_shared_compose_relay_host(self):
        settings = _load_settings_with_env(_bridge_env(RELAY_BIND_HOST="0.0.0.0"))
        assert settings.relay_bind_host == "0.0.0.0"

    def test_bridge_allows_custom_relay_port_for_tunnel(self):
        settings = _load_settings_with_env(_bridge_env(RELAY_BIND_PORT="19090"))
        assert settings.relay_bind_port == 19090
        assert settings.relay_host_port == 19090
        assert settings.foreign_relay_host_port == 19090

    def test_bridge_allows_custom_relay_host_port_for_tunnel(self):
        settings = _load_settings_with_env(_bridge_env(RELAY_BIND_PORT="19090", RELAY_HOST_PORT="29090"))
        assert settings.relay_bind_port == 19090
        assert settings.relay_host_port == 29090

    def test_bridge_reads_foreign_relay_host_port_from_remote_env_blob(self):
        remote_env_text = "\n".join(
            [
                "APP_ROLE=tg-relay",
                "RELAY_SHARED_SECRET=secret-123",
                "TG_BOT_TOKEN=123456:AAABBBCCC",
                "TG_CHAT_ID=-100123456",
                "RELAY_BIND_PORT=8080",
                "RELAY_HOST_PORT=38080",
            ]
        )
        settings = _load_settings_with_env(
            _bridge_env(
                RELAY_HOST_PORT="29090",
                FOREIGN_RELAY_ENV_B64=_b64_env(remote_env_text),
            )
        )
        assert settings.relay_host_port == 29090
        assert settings.foreign_relay_host_port == 38080

    def test_bridge_uses_new_default_foreign_app_dir(self):
        settings = _load_settings_with_env(_bridge_env(FOREIGN_APP_DIR=""))
        assert settings.foreign_app_dir == "/home/relay/max2tg"

    def test_missing_required_bridge_var_raises(self):
        env = _bridge_env()
        env.pop("MAX_TOKEN")
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(env)
        assert "MAX_TOKEN" in str(exc.value)

    def test_missing_shared_secret_raises(self):
        env = _bridge_env()
        env.pop("RELAY_SHARED_SECRET")
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(env)
        assert "RELAY_SHARED_SECRET" in str(exc.value)

    def test_rejects_tg_specific_vars(self):
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(_bridge_env(TG_BOT_TOKEN="123", TG_CHAT_ID="-1001"))
        msg = str(exc.value)
        assert "TG_BOT_TOKEN" in msg
        assert "TG_CHAT_ID" in msg

    def test_invalid_remote_env_base64_raises(self):
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(_bridge_env(FOREIGN_RELAY_ENV_B64="%%%"))
        assert "FOREIGN_RELAY_ENV_B64" in str(exc.value)

    def test_invalid_remote_relay_host_port_in_env_blob_raises(self):
        remote_env_text = "\n".join(
            [
                "APP_ROLE=tg-relay",
                "RELAY_SHARED_SECRET=secret-123",
                "TG_BOT_TOKEN=123456:AAABBBCCC",
                "TG_CHAT_ID=-100123456",
                "RELAY_HOST_PORT=not-a-port",
            ]
        )
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(_bridge_env(FOREIGN_RELAY_ENV_B64=_b64_env(remote_env_text)))
        assert "RELAY_HOST_PORT" in str(exc.value)

    def test_invalid_private_key_raises(self):
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(_bridge_env(FOREIGN_SSH_PRIVATE_KEY="not-a-private-key"))
        assert "FOREIGN_SSH_PRIVATE_KEY" in str(exc.value)


class TestLoadSettingsRelay:
    def test_valid_env(self):
        settings = _load_settings_with_env(
            _relay_env(
                DEBUG="1",
                REPLY_ENABLED="true",
                COMMAND_DB_PATH="/tmp/commands.sqlite3",
                MESSAGE_DB_PATH="/tmp/messages.sqlite3",
            )
        )
        assert settings.app_role == APP_ROLE_TG_RELAY
        assert settings.tg_bot_token == "123456:AAABBBCCC"
        assert settings.tg_chat_id == "-100123456"
        assert settings.command_db_path == "/tmp/commands.sqlite3"
        assert settings.message_db_path == "/tmp/messages.sqlite3"
        assert settings.debug is True
        assert settings.reply_enabled is True

    def test_invalid_tg_chat_id_raises(self):
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(_relay_env(TG_CHAT_ID="not-an-int"))
        assert "TG_CHAT_ID" in str(exc.value)

    def test_rejects_max_specific_vars(self):
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(_relay_env(MAX_TOKEN="tok", MAX_DEVICE_ID="dev"))
        msg = str(exc.value)
        assert "MAX_TOKEN" in msg
        assert "MAX_DEVICE_ID" in msg

    def test_missing_required_relay_var_raises(self):
        env = _relay_env()
        env.pop("TG_BOT_TOKEN")
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(env)
        assert "TG_BOT_TOKEN" in str(exc.value)


def test_invalid_app_role_raises():
    with pytest.raises(SystemExit) as exc:
        _load_settings_with_env({"APP_ROLE": "unknown", "RELAY_SHARED_SECRET": "secret"})
    assert "APP_ROLE" in str(exc.value)
