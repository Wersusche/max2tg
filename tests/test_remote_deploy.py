import shutil
import tarfile
import uuid
from pathlib import Path
from unittest.mock import patch

from app.remote_deploy import RemoteRelayManager, _format_command_failure


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
            relay_host_port=8080,
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


def test_build_archive_excludes_local_service_directories():
    workspace_dir = _make_workspace_dir()
    _populate_workspace_dir(workspace_dir)
    (workspace_dir / ".codex_deps").mkdir()
    (workspace_dir / ".codex_deps" / "pkg.txt").write_text("skip\n", encoding="utf-8")
    (workspace_dir / ".tmp").mkdir()
    (workspace_dir / ".tmp" / "scratch.txt").write_text("skip\n", encoding="utf-8")
    (workspace_dir / "pytest-cache-files-abc").mkdir()
    (workspace_dir / "pytest-cache-files-abc" / "cache.txt").write_text("skip\n", encoding="utf-8")
    (workspace_dir / "tests").mkdir()
    (workspace_dir / "tests" / "test_dummy.py").write_text("assert True\n", encoding="utf-8")
    (workspace_dir / "README.md").write_text("docs\n", encoding="utf-8")
    (workspace_dir / "secrets").mkdir()
    (workspace_dir / "secrets" / "foreign.key").write_text("private\n", encoding="utf-8")

    manager = _build_manager(workspace_dir)
    try:
        manager._build_archive()
        with tarfile.open(manager._archive_path, mode="r:gz") as archive:
            names = set(archive.getnames())
    finally:
        manager._cleanup_temp_files()
        shutil.rmtree(workspace_dir, ignore_errors=True)

    assert ".codex_deps/pkg.txt" not in names
    assert ".tmp/scratch.txt" not in names
    assert "pytest-cache-files-abc/cache.txt" not in names
    assert "tests/test_dummy.py" not in names
    assert "README.md" not in names
    assert "secrets/foreign.key" not in names


def test_dockerignore_keeps_remote_deploy_files_in_image():
    dockerignore_lines = {
        line.strip()
        for line in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "docker-compose.yml" not in dockerignore_lines
    assert "Dockerfile" not in dockerignore_lines


def test_dockerignore_excludes_local_service_directories():
    dockerignore_lines = {
        line.strip()
        for line in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".codex_deps/" in dockerignore_lines
    assert ".tmp/" in dockerignore_lines
    assert "pytest-cache-files-*/" in dockerignore_lines
    assert "requirements-dev.txt" in dockerignore_lines
    assert "secrets/" in dockerignore_lines


def test_gitignore_excludes_secrets_directory():
    gitignore_lines = {
        line.strip()
        for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "secrets/" in gitignore_lines


def test_setup_bridge_script_exists():
    script_text = Path("scripts/setup_bridge.sh").read_text(encoding="utf-8")

    assert "--foreign-admin" in script_text
    assert "FOREIGN_RELAY_ENV_FILE" in script_text
    assert "FOREIGN_SSH_PRIVATE_KEY_FILE" in script_text
    assert "FOREIGN_SSH_PRIVATE_KEY" in script_text
    assert "try_legacy_private_key_for_relay_ssh" in script_text
    assert "legacy_relay_env_get" in script_text
    assert "relay_ssh_works" in script_text
    assert "FOREIGN_RELAY_ENV_B64" in script_text
    assert "skipping foreign admin bootstrap" in script_text
    assert "Do not paste a password here" in script_text


def test_bootstrap_remote_script_keeps_download_errors_visible():
    script_text = Path("scripts/bootstrap_remote.sh").read_text(encoding="utf-8")

    assert "wget -q" not in script_text
    assert "[bootstrap]" in script_text


def test_format_command_failure_adds_relay_host_port_hint_for_bind_conflicts():
    message = _format_command_failure(
        args=["ssh", "relay@example.com", "docker", "compose", "up"],
        returncode=1,
        stdout_text="compose output",
        stderr_text=(
            "Error response from daemon: failed to set up container networking: "
            "failed to bind host port 127.0.0.1:8080/tcp: address already in use"
        ),
    )

    assert "RELAY_HOST_PORT" in message
    assert "address already in use" in message
