"""RPC bridge used by execute_code child processes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.common.logger import get_logger
from src.tools.mcp.tool import Tool
from src.tools.pipeline import ToolCallContext, ToolPipeline
from src.tools.runtime_scope import ExecutionScope
from src.worker.tool_scope import LLM_HIDDEN_TAG

from .code_sandbox_config import build_code_sandbox

logger = get_logger()

_STUB_TEMPLATE = """
import json
import os
import socket
import time
import uuid

def _connect():
    rpc_dir = os.environ.get("LITTLEWANG_RPC_DIR", "")
    if rpc_dir:
        return None
    sock_path = os.environ.get("LITTLEWANG_RPC_SOCKET", "")
    if sock_path:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sock_path)
        return sock
    host = os.environ.get("LITTLEWANG_RPC_HOST", "127.0.0.1")
    port = int(os.environ["LITTLEWANG_RPC_PORT"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    return sock

def _rpc(name, **kwargs):
    rpc_dir = os.environ.get("LITTLEWANG_RPC_DIR", "")
    if rpc_dir:
        request_id = f"{os.getpid()}-{uuid.uuid4().hex}"
        req_path = os.path.join(rpc_dir, f"{request_id}.req.json")
        resp_path = os.path.join(rpc_dir, f"{request_id}.resp.json")
        with open(req_path, "w", encoding="utf-8") as handle:
            json.dump({"tool": name, "input": kwargs}, handle, ensure_ascii=False)
        deadline = time.time() + float(os.environ.get("LITTLEWANG_RPC_TIMEOUT_SECONDS", "30"))
        while time.time() < deadline:
            if os.path.exists(resp_path):
                with open(resp_path, "r", encoding="utf-8") as handle:
                    response = json.load(handle)
                try:
                    os.remove(resp_path)
                except OSError:
                    pass
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("rpc_timeout")
        if "error" in response:
            raise RuntimeError(response["error"])
        if response.get("is_error"):
            message = response.get("content", "") or response.get("metadata", {}).get("stderr_tail", "") or "tool call failed"
            raise RuntimeError(message)
        return response.get("content", "")
    payload = json.dumps({"tool": name, "input": kwargs}, ensure_ascii=False).encode("utf-8") + b"\\n"
    with _connect() as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    response = json.loads(b"".join(chunks).decode("utf-8") or "{}")
    if "error" in response:
        raise RuntimeError(response["error"])
    if response.get("is_error"):
        message = response.get("content", "") or response.get("metadata", {}).get("stderr_tail", "") or "tool call failed"
        raise RuntimeError(message)
    return response.get("content", "")

{functions}
""".strip()


@dataclass(frozen=True)
class CodeExecutionOutcome:
    """Raw result returned by the code execution sandbox."""

    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    tool_calls_made: int
    truncated: bool = False

    @property
    def status(self) -> str:
        if self.exit_code == 0:
            return "success"
        if "timed out" in self.stderr.lower():
            return "timeout"
        return "error"


class _RpcBridgeServer:
    def __init__(
        self,
        *,
        parent_scope: ExecutionScope,
        pipeline: ToolPipeline,
        enabled_tools: Mapping[str, Tool],
        max_tool_calls: int,
    ) -> None:
        self._parent_scope = parent_scope
        self._pipeline = pipeline
        self._enabled_tools = dict(enabled_tools)
        self._max_tool_calls = max(max_tool_calls, 1)
        self.calls_made = 0

    async def handle_request(self, request: dict[str, object]) -> dict[str, object]:
        self.calls_made += 1
        if self.calls_made > self._max_tool_calls:
            return {"error": "tool_call_limit_exceeded"}

        tool_name = str(request.get("tool", "") or "")
        tool_input = request.get("input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool = self._enabled_tools.get(tool_name)
        if tool is None:
            return {"error": f"tool '{tool_name}' not enabled"}

        result = await self._pipeline.execute(
            ToolCallContext.from_scope(
                self._parent_scope,
                tool_name=tool_name,
                tool_input=tool_input,
                risk_level=str(tool.risk_level),
                tool=tool,
                step_name=f"execute_code.rpc#{self.calls_made}",
            )
        )
        return {
            "content": result.content,
            "is_error": result.is_error,
            "metadata": result.metadata,
        }

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            try:
                request = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                await self._write(writer, {"error": "invalid_json"})
                return

            await self._write(writer, await self.handle_request(request))
        except Exception as exc:
            logger.warning("[execute_code] RPC bridge error: %s", exc, exc_info=True)
            await self._write(writer, {"error": str(exc)})
        finally:
            writer.close()
            await writer.wait_closed()

    async def _write(self, writer: asyncio.StreamWriter, payload: dict[str, object]) -> None:
        writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        await writer.drain()


def resolve_script_callable_tools(
    *,
    parent_scope: ExecutionScope,
    pipeline: ToolPipeline,
    enabled_tools: list[str] | None,
) -> dict[str, Tool]:
    """Resolve the tool subset exposed to code executed in the sandbox."""
    executor_tools = dict(getattr(pipeline.executor, "allowed_tools", {}) or {})
    requested = set(enabled_tools or parent_scope.allowed_tool_names)
    requested &= set(parent_scope.allowed_tool_names)

    resolved: dict[str, Tool] = {}
    for tool_name in sorted(requested):
        if tool_name == "execute_code":
            continue
        tool = parent_scope.scoped_tools.get(tool_name) or executor_tools.get(tool_name)
        if tool is None:
            continue
        if LLM_HIDDEN_TAG in getattr(tool, "tags", frozenset()):
            continue
        resolved[tool_name] = tool
    return resolved


async def run_code_in_sandbox(
    *,
    code: str,
    parent_scope: ExecutionScope,
    pipeline: ToolPipeline,
    enabled_tools: Mapping[str, Tool],
    timeout_seconds: int,
    max_tool_calls: int,
    extra_env: Mapping[str, str] | None = None,
) -> CodeExecutionOutcome:
    """Execute Python code in a subprocess and bridge tool calls over UDS."""
    started_at = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="lw-code-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        script_path = tmpdir_path / "script.py"
        stub_path = tmpdir_path / "genworker_tools.py"
        script_path.write_text(code, encoding="utf-8")
        stub_path.write_text(_render_stub_module(enabled_tools), encoding="utf-8")

        bridge = _RpcBridgeServer(
            parent_scope=parent_scope,
            pipeline=pipeline,
            enabled_tools=enabled_tools,
            max_tool_calls=max_tool_calls,
        )
        transport_env: dict[str, str] = {}
        cleanup_socket_path = ""
        server = None
        poller_task = None
        stop_event = None
        if enabled_tools:
            socket_name = hashlib.sha1(tmpdir.encode("utf-8")).hexdigest()[:12]
            socket_path = os.path.abspath(f".lw-rpc-{socket_name}.sock")
            if os.path.exists(socket_path):
                os.unlink(socket_path)
            try:
                server = await asyncio.start_unix_server(bridge.handle, path=socket_path)
                cleanup_socket_path = socket_path
                transport_env = {"LITTLEWANG_RPC_SOCKET": socket_path}
            except PermissionError:
                rpc_dir = tmpdir_path / "rpc"
                rpc_dir.mkdir(parents=True, exist_ok=True)
                stop_event = asyncio.Event()
                poller_task = asyncio.create_task(
                    _poll_rpc_directory(
                        bridge=bridge,
                        rpc_dir=rpc_dir,
                        stop_event=stop_event,
                    )
                )
                transport_env = {
                    "LITTLEWANG_RPC_DIR": str(rpc_dir),
                    "LITTLEWANG_RPC_TIMEOUT_SECONDS": str(timeout_seconds),
                }
        try:
            safe_env = _build_safe_env(
                tmpdir=tmpdir,
                transport_env=transport_env,
                extra_env=extra_env,
            )
            sandbox = build_code_sandbox(timeout_seconds)
            result = await sandbox.execute(
                command=f"{shlex.quote(sys.executable)} {shlex.quote(script_path.name)}",
                working_dir=tmpdir,
                env=safe_env,
            )
        finally:
            if stop_event is not None:
                stop_event.set()
            if poller_task is not None:
                await poller_task
            if server is not None:
                server.close()
                await server.wait_closed()
            if cleanup_socket_path and os.path.exists(cleanup_socket_path):
                os.unlink(cleanup_socket_path)

    return CodeExecutionOutcome(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        duration_seconds=round(time.monotonic() - started_at, 3),
        tool_calls_made=bridge.calls_made,
        truncated=result.truncated,
    )


def _build_safe_env(
    *,
    tmpdir: str,
    transport_env: Mapping[str, str],
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": tmpdir,
        "PYTHONPATH": tmpdir,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TZ": os.environ.get("TZ", "UTC"),
    }
    base_env.update(dict(transport_env))
    if extra_env:
        base_env.update({str(key): str(value) for key, value in extra_env.items()})
    return {key: value for key, value in base_env.items() if value is not None}


def _render_stub_module(enabled_tools: Mapping[str, Tool]) -> str:
    functions: list[str] = []
    for tool_name, tool in enabled_tools.items():
        functions.append(f"def {tool_name}(**kwargs):")
        functions.append(f'    """{tool.description}"""')
        functions.append(f"    return _rpc({tool_name!r}, **kwargs)")
        functions.append("")
    return _STUB_TEMPLATE.replace("{functions}", "\n".join(functions).rstrip())


async def _poll_rpc_directory(
    *,
    bridge: _RpcBridgeServer,
    rpc_dir: Path,
    stop_event: asyncio.Event,
) -> None:
    while True:
        await _drain_rpc_requests(bridge=bridge, rpc_dir=rpc_dir)
        if stop_event.is_set():
            await _drain_rpc_requests(bridge=bridge, rpc_dir=rpc_dir)
            return
        await asyncio.sleep(0.02)


async def _drain_rpc_requests(
    *,
    bridge: _RpcBridgeServer,
    rpc_dir: Path,
) -> None:
    for req_path in sorted(rpc_dir.glob("*.req.json")):
        try:
            raw = req_path.read_text(encoding="utf-8")
            request = json.loads(raw)
        except Exception as exc:
            response = {"error": f"invalid_request: {exc}"}
        else:
            response = await bridge.handle_request(request)
        resp_path = req_path.with_name(req_path.name.replace(".req.json", ".resp.json"))
        resp_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        try:
            req_path.unlink()
        except OSError:
            pass
