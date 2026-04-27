"""Process-level sandbox used by the bash tool."""
from __future__ import annotations

import asyncio
import json
import os
import resource
from dataclasses import asdict, dataclass
from typing import Sequence

from src.common.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class SandboxConfig:
    """Configuration for process sandbox execution."""

    mode: str = "subprocess"
    timeout_seconds: int = 30
    max_output_bytes: int = 10240
    max_concurrent: int = 5
    memory_limit_mb: int = 256
    cpu_limit: float = 0.5
    network_enabled: bool = False
    writable_paths: tuple[str, ...] = ("/tmp/genworker-bash-sandbox",)
    readonly_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SandboxResult:
    """Serializable command execution result."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class ProcessSandbox:
    """Executes commands in a limited subprocess or external sandbox."""

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._semaphore = asyncio.Semaphore(max(config.max_concurrent, 1))

    async def execute(
        self,
        command: str,
        working_dir: str = "",
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        async with self._semaphore:
            if self._config.mode == "docker":
                return await self._execute_docker(command, working_dir, env=env)
            if self._config.mode == "bubblewrap":
                return await self._execute_bubblewrap(command, working_dir, env=env)
            return await self._execute_subprocess(command, working_dir, env=env)

    async def _execute_subprocess(
        self,
        command: str,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir or None,
            env=env,
            preexec_fn=self._build_preexec_fn(),
        )
        return await self._wait_for_process(process)

    async def _execute_docker(
        self,
        command: str,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        docker_args = [
            "docker",
            "run",
            "--rm",
            "--network=none" if not self._config.network_enabled else "--network=bridge",
            f"--memory={self._config.memory_limit_mb}m",
            f"--cpus={self._config.cpu_limit}",
        ]
        if working_dir:
            docker_args.extend(["-v", f"{working_dir}:/sandbox:rw", "-w", "/sandbox"])
        if env:
            for key, value in env.items():
                docker_args.extend(["-e", f"{key}={value}"])
        docker_args.extend(["alpine:3.20", "/bin/sh", "-lc", command])
        return await self._execute_exec(docker_args, env=env)

    async def _execute_bubblewrap(
        self,
        command: str,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        bwrap_args = [
            "bwrap",
            "--die-with-parent",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind",
            "/",
            "/",
        ]
        if not self._config.network_enabled:
            bwrap_args.append("--unshare-net")
        sandbox_dir = working_dir or "/tmp"
        bwrap_args.extend(["--bind", sandbox_dir, "/sandbox", "--chdir", "/sandbox"])
        bwrap_args.extend(["/bin/sh", "-lc", command])
        return await self._execute_exec(bwrap_args, env=env)

    async def _execute_exec(
        self,
        args: Sequence[str],
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            return SandboxResult(exit_code=-1, stderr=f"Sandbox runtime unavailable: {exc}")
        return await self._wait_for_process(process)

    async def _wait_for_process(self, process) -> SandboxResult:
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=max(self._config.timeout_seconds, 1),
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return SandboxResult(
                exit_code=-1,
                stderr=f"Command timed out ({self._config.timeout_seconds}s)",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        truncated = False
        if len(stdout) > self._config.max_output_bytes:
            stdout = stdout[:self._config.max_output_bytes] + "\n... [output truncated]"
            truncated = True
        if len(stderr) > self._config.max_output_bytes:
            stderr = stderr[:self._config.max_output_bytes] + "\n... [output truncated]"
            truncated = True
        return SandboxResult(
            exit_code=int(process.returncode or 0),
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
        )

    def _build_preexec_fn(self):
        memory_limit = max(self._config.memory_limit_mb, 1) * 1024 * 1024
        cpu_seconds = max(int(self._config.timeout_seconds), 1)

        def _apply_limits() -> None:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            except Exception as exc:
                logger.debug("[ProcessSandbox] Failed to apply rlimits: %s", exc)

        return _apply_limits
