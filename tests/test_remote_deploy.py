import shutil
import tarfile
import uuid
from pathlib import Path
from unittest.mock import patch

from app.remote_deploy import RemoteRelayManager


def _build_manager(workspace_dir: Path, remote_app_dir="/home/relay/max2tg"):
    manager_temp_dir = workspace_dir / "manager-temp"
    manager_temp_dir.mkdir()
    with patch("app.remote_deploy.tempfile.mkdtemp", return_value=str(manager_temp_dir)):
        return RemoteRelayManager(
            host="153.80.244.245",
            port=22,
            user="relay",
            private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----\n",
            remote_app_dir=remote_app_dir,
            relay_bind_port=8080,
            local_tunnel_port=18080,
            remote_env_text="APP_ROLE=tg-relay\n",
            workspace_dir=str(workspace_dir),
        )


def _make_workspace_dir() -> Path:
    workspace_dir = Path(f".codex-test-remote-deploy-{uuid.uuid4().hex}").resolve()
    workspace_dir.mkdir()
    return workspace_dir


def _populate_workspace_dir(workspace_dir: Path) -> None:
    (workspace_dir / "app").mkdir()
    (workspace_dir / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (workspace_dir / "scripts").mkdir()
    (workspace_dir / "scripts" / "bootstrap_remote.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (workspace_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (workspace_dir / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (workspace_dir / "requirements.txt").write_text("aiohttp\n", encoding="utf-8")


def test_build_remote_deploy_command_uses_bootstrap_script():
    workspace_dir = _make_workspace_dir()
    _populate_workspace_dir(workspace_dir)
    manager = _build_manager(workspace_dir)
    try:
        command = manager._build_remote_deploy_command()
    finally:
        manager._cleanup_temp_files()
        shutil.rmtree(workspace_dir, ignore_errors=True)

    assert command == (
        "cd /home/relay/max2tg "
        "&& tar -xzf bundle.tar.gz "
        "&& rm -f bundle.tar.gz "
        "&& if [ ! -f ./scripts/bootstrap_remote.sh ]; then echo 'Missing scripts/bootstrap_remote.sh in remote deploy bundle.' >&2; exit 1; fi "
        "&& sh ./scripts/bootstrap_remote.sh"
    )


def test_build_remote_deploy_command_quotes_remote_dir():
    workspace_dir = _make_workspace_dir()
    _populate_workspace_dir(workspace_dir)
    manager = _build_manager(workspace_dir, remote_app_dir="/home/relay/max2tg app")
    try:
        command = manager._build_remote_deploy_command()
    finally:
        manager._cleanup_temp_files()
        shutil.rmtree(workspace_dir, ignore_errors=True)

    assert "cd '/home/relay/max2tg app'" in command


def test_build_archive_includes_required_deploy_files():
    workspace_dir = _make_workspace_dir()
    _populate_workspace_dir(workspace_dir)
    manager = _build_manager(workspace_dir)
    try:
        manager._build_archive()
        with tarfile.open(manager._archive_path, mode="r:gz") as archive:
            names = set(archive.getnames())
    finally:
        manager._cleanup_temp_files()
        shutil.rmtree(workspace_dir, ignore_errors=True)

    assert "app/main.py" in names
    assert "scripts/bootstrap_remote.sh" in names
    assert "docker-compose.yml" in names
    assert "Dockerfile" in names
    assert "requirements.txt" in names
