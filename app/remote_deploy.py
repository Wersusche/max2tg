from __future__ import annotations

import asyncio
import logging
import os
import shlex
import tarfile
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

IGNORED_ARCHIVE_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "logs",
    "data",
    "debug",
}


class RemoteRelayManager:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        private_key: str,
        remote_app_dir: str,
        relay_bind_port: int,
        local_tunnel_port: int,
        remote_env_text: str,
        workspace_dir: str,
        remote_deploy_enabled: bool = True,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.private_key = private_key
        self.remote_app_dir = remote_app_dir
        self.relay_bind_port = relay_bind_port
        self.local_tunnel_port = local_tunnel_port
        self.remote_env_text = remote_env_text
        self.workspace_dir = Path(workspace_dir)
        self.remote_deploy_enabled = remote_deploy_enabled
        self._temp_dir = Path(tempfile.mkdtemp(prefix="max2tg-remote-"))
        self._key_path = self._temp_dir / "foreign.key"
        self._archive_path = self._temp_dir / "bundle.tar.gz"
        self._env_path = self._temp_dir / ".env"
        self._tunnel_proc: asyncio.subprocess.Process | None = None
        self._prepared = False

    async def deploy(self) -> None:
        await self._prepare_local_files()
        await self._run_ssh([f"mkdir -p {shlex.quote(self.remote_app_dir)}"])
        await self._run_scp(self._archive_path, f"{self._remote_target()}:{self.remote_app_dir}/bundle.tar.gz")
        await self._run_scp(self._env_path, f"{self._remote_target()}:{self.remote_app_dir}/.env")
        await self._run_ssh([self._build_remote_deploy_command()])

    async def ensure_tunnel(self) -> None:
        if self._tunnel_proc is not None and self._tunnel_proc.returncode is None:
            return
        await self._prepare_local_files()
        args = [
            "ssh",
            "-N",
            "-L",
            f"{self.local_tunnel_port}:127.0.0.1:{self.relay_bind_port}",
            "-p",
            str(self.port),
            "-i",
            str(self._key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            self._remote_target(),
        ]
        log.info("Opening SSH tunnel to %s:%s", self.host, self.relay_bind_port)
        self._tunnel_proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(1)
        if self._tunnel_proc.returncode is not None:
            stdout, stderr = await self._tunnel_proc.communicate()
            raise RuntimeError(
                "SSH tunnel exited immediately: "
                f"{stdout.decode('utf-8', 'ignore')}\n{stderr.decode('utf-8', 'ignore')}"
            )

    async def close(self) -> None:
        if self._tunnel_proc is not None and self._tunnel_proc.returncode is None:
            self._tunnel_proc.terminate()
            try:
                await asyncio.wait_for(self._tunnel_proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._tunnel_proc.kill()
                await self._tunnel_proc.wait()
        self._cleanup_temp_files()

    async def _prepare_local_files(self) -> None:
        if self._prepared:
            return
        self._key_path.write_text(self.private_key, encoding="utf-8")
        try:
            os.chmod(self._key_path, 0o600)
        except OSError:
            pass

        if self.remote_env_text:
            self._env_path.write_text(self.remote_env_text, encoding="utf-8")
        else:
            self._env_path.write_text("", encoding="utf-8")

        self._build_archive()
        self._prepared = True

    def _build_archive(self) -> None:
        with tarfile.open(self._archive_path, mode="w:gz") as archive:
            for path in self.workspace_dir.rglob("*"):
                relative = path.relative_to(self.workspace_dir)
                if self._should_skip(relative):
                    continue
                archive.add(path, arcname=str(relative))

    def _should_skip(self, relative: Path) -> bool:
        parts = set(relative.parts)
        if parts & IGNORED_ARCHIVE_PARTS:
            return True
        if relative.name.endswith((".pyc", ".pyo")):
            return True
        return False

    async def _run_ssh(self, remote_args: list[str]) -> None:
        await self._prepare_local_files()
        args = [
            "ssh",
            "-p",
            str(self.port),
            "-i",
            str(self._key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            self._remote_target(),
            *remote_args,
        ]
        await _run_command(args)

    async def _run_scp(self, source: Path, target: str) -> None:
        await self._prepare_local_files()
        args = [
            "scp",
            "-P",
            str(self.port),
            "-i",
            str(self._key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            str(source),
            target,
        ]
        await _run_command(args)

    def _remote_target(self) -> str:
        return f"{self.user}@{self.host}"

    def _build_remote_deploy_command(self) -> str:
        remote_dir = shlex.quote(self.remote_app_dir)
        return (
            f"cd {remote_dir} "
            "&& tar -xzf bundle.tar.gz "
            "&& rm -f bundle.tar.gz "
            "&& sh ./scripts/bootstrap_remote.sh"
        )

    def _cleanup_temp_files(self) -> None:
        for path in sorted(self._temp_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
        self._temp_dir.rmdir()


async def _run_command(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        return
    raise RuntimeError(
        f"Command failed ({proc.returncode}): {' '.join(args)}\n"
        f"STDOUT:\n{stdout.decode('utf-8', 'ignore')}\n"
        f"STDERR:\n{stderr.decode('utf-8', 'ignore')}"
    )
