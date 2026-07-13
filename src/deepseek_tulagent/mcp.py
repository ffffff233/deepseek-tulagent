from __future__ import annotations

from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import copy
import ctypes
from ctypes import wintypes
import hashlib
import httpx
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .processes import popen_hidden, run_hidden


PROTOCOL_VERSION = "2025-06-18"
DEFAULT_STARTUP_TIMEOUT = 15.0
DEFAULT_CALL_TIMEOUT = 60.0
DEFAULT_STDERR_LIMIT = 16 * 1024
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_LIST_PAGES = 100
MAX_DIAGNOSTIC_CHARS = 2_000

_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|cookie|credential|"
    r"pass(?:word|wd)?|private[_-]?key|refresh[_-]?token|secret|session[_-]?token)",
    re.IGNORECASE,
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|access[_-]?token|authorization|cookie|password|passwd|"
    r"private[_-]?key|refresh[_-]?token|secret|session[_-]?token)\b[\"']?\s*[=:]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+")
_COMMON_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b"
)
_INVALID_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_RESERVED_HTTP_HEADERS = frozenset({
    "accept",
    "connection",
    "content-length",
    "content-type",
    "host",
    "mcp-protocol-version",
    "mcp-session-id",
    "transfer-encoding",
})


class MCPError(RuntimeError):
    """Base error for an MCP server or protocol failure."""


class MCPConnectionError(MCPError):
    """The MCP subprocess could not start or disconnected unexpectedly."""


class MCPProtocolError(MCPError):
    """The MCP peer returned a malformed JSON-RPC or MCP payload."""


class MCPTimeoutError(MCPError, TimeoutError):
    """An MCP request exceeded its configured deadline."""


class _MCPHTTPSessionExpired(MCPConnectionError):
    def __init__(self, server: str, session_id: str, generation: int):
        super().__init__(f'MCP 服务 "{server}" 的 HTTP 会话已失效')
        self.session_id = session_id
        self.generation = generation


class MCPRemoteError(MCPError):
    """A JSON-RPC error returned by the MCP server."""

    def __init__(self, server: str, method: str, code: int, message: str, data: Any = None):
        super().__init__(f'MCP 服务 "{server}" 调用 {method} 失败（{code}）：{message}')
        self.server = server
        self.method = method
        self.code = code
        self.remote_message = message
        self.data = data


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for one stdio or Streamable HTTP MCP server.

    ``args`` and ``env`` are excluded from ``repr`` so an exception or debug log
    cannot accidentally print credentials supplied to a third-party server.
    """

    name: str
    command: str = ""
    args: tuple[str, ...] = field(default_factory=tuple, repr=False)
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    cwd: str | Path | None = None
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT
    tool_timeouts: Mapping[str, float] = field(default_factory=dict, repr=False)
    stderr_limit: int = DEFAULT_STDERR_LIMIT
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    max_list_pages: int = DEFAULT_MAX_LIST_PAGES
    transport: str = "stdio"
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        name = self.name.strip()
        command = self.command.strip()
        transport = str(self.transport or "stdio").strip().lower().replace("_", "-")
        if transport == "http":
            transport = "streamable-http"
        url = str(self.url or "").strip()
        if not name:
            raise ValueError("MCP 服务名称不能为空")
        if transport not in {"stdio", "streamable-http"}:
            raise ValueError(f'MCP 服务 "{name}" 使用了不支持的传输：{transport}')
        if transport == "stdio" and not command:
            raise ValueError(f'MCP 服务 "{name}" 缺少启动命令')
        if transport == "streamable-http":
            parsed_url = urlsplit(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError(f'MCP 服务 "{name}" 缺少有效的 http/https URL')
            if parsed_url.username is not None or parsed_url.password is not None:
                raise ValueError("MCP HTTP URL 不能包含 username/password")
        if self.startup_timeout <= 0 or self.call_timeout <= 0:
            raise ValueError("MCP 超时时间必须大于 0")
        if self.stderr_limit < 0 or self.max_message_bytes <= 0 or self.max_list_pages <= 0:
            raise ValueError("MCP 运行限制必须为正数")
        args = tuple(str(item) for item in self.args)
        env = {str(key): str(value) for key, value in self.env.items()}
        if not isinstance(self.headers, Mapping):
            raise ValueError("MCP HTTP headers 必须是字符串映射")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in self.headers.items()):
            raise ValueError("MCP HTTP headers 必须是字符串映射")
        headers = dict(self.headers)
        for key, value in headers.items():
            if not _HTTP_HEADER_NAME_RE.fullmatch(key):
                raise ValueError(f"MCP HTTP header 名称无效：{key!r}")
            if key.casefold() in _RESERVED_HTTP_HEADERS:
                raise ValueError(f"MCP HTTP header 由运行时保留：{key}")
            if any((ord(character) < 0x20 and character != "\t") or ord(character) == 0x7F for character in value):
                raise ValueError(f"MCP HTTP header 值包含无效控制字符：{key}")
        tool_timeouts = {str(key): float(value) for key, value in self.tool_timeouts.items()}
        if any(value <= 0 for value in tool_timeouts.values()):
            raise ValueError("MCP 工具超时时间必须大于 0")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "args", args)
        object.__setattr__(self, "env", env)
        object.__setattr__(self, "headers", headers)
        object.__setattr__(self, "tool_timeouts", tool_timeouts)


@dataclass(frozen=True)
class MCPTool:
    server: str
    raw_name: str
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        # MCP annotations are untrusted hints. The permission layer may require
        # an explicit user decision before treating this as automatically safe.
        return self.annotations.get("readOnlyHint") is True

    def definition(self) -> dict[str, Any]:
        """Return the host-neutral dynamic tool contract used by the app."""

        return {
            "name": self.name,
            "description": self.description,
            "schema": copy.deepcopy(self.input_schema),
            "origin": {
                "kind": "mcp",
                "server": self.server,
                "tool": self.raw_name,
            },
            "read_only": self.read_only,
        }

    def openai_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.input_schema),
            },
        }


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: BaseException | None = None


class _TailBuffer:
    def __init__(self, limit: int):
        self.limit = limit
        self._data = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        if not data or self.limit <= 0:
            return
        with self._lock:
            self._data.extend(data)
            if len(self._data) > self.limit:
                del self._data[: len(self._data) - self.limit]

    def text(self) -> str:
        with self._lock:
            data = bytes(self._data)
        return data.decode("utf-8", errors="replace").strip()


def redact_sensitive(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove common credentials and caller-known secret values from diagnostics."""

    redacted = str(text)
    for secret in sorted({str(value) for value in secrets if str(value)}, key=len, reverse=True):
        redacted = redacted.replace(secret, "***")
    redacted = _BEARER_RE.sub(r"\1 ***", redacted)
    redacted = _ASSIGNMENT_SECRET_RE.sub(r"\1***", redacted)
    redacted = _COMMON_TOKEN_RE.sub("***", redacted)
    return redacted


def normalize_tool_component(value: str) -> str:
    """Build a stable model-safe identifier while avoiding normalization collisions."""

    raw = value
    normalized = _INVALID_NAME_RE.sub("_", raw).strip("_") or "unnamed"
    if normalized != raw:
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
        normalized = f"{normalized}_{suffix}"
    return normalized


def model_tool_name(server: str, raw_tool: str) -> str:
    name = f"mcp__{normalize_tool_component(server)}__{normalize_tool_component(raw_tool)}"
    if len(name) <= 64:
        return name
    suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return f"{name[:53]}_{suffix}"


class MCPClient:
    """Thread-safe MCP stdio client with concurrent JSON-RPC request routing."""

    def __init__(self, config: MCPServerConfig | Mapping[str, Any]):
        self.config = _coerce_config(config)
        self._state = "configured"
        self._state_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._next_id = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._stdin: Any = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr = _TailBuffer(self.config.stderr_limit)
        self._fatal_error: BaseException | None = None
        self._last_error = ""
        self._protocol_version = ""
        self._server_info: dict[str, Any] = {}
        self._capabilities: dict[str, Any] = {}
        self._tools: list[MCPTool] | None = None
        self._windows_job: int | None = None
        self._secret_values = _config_secret_values(self.config)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def connected(self) -> bool:
        with self._state_lock:
            process = self._process
            return self._state == "connected" and process is not None and process.poll() is None

    @property
    def tools(self) -> list[MCPTool]:
        with self._state_lock:
            return list(self._tools or ())

    def start(self) -> MCPClient:
        with self._state_lock:
            if self.connected:
                return self
            if self._state == "closed":
                raise MCPConnectionError(f'MCP 服务 "{self.name}" 已关闭，无法重新启动')
            if self._state == "starting":
                raise MCPConnectionError(f'MCP 服务 "{self.name}" 正在启动')
            self._state = "starting"
            self._fatal_error = None
            self._last_error = ""
            self._tools = None

        try:
            self._spawn()
            result = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "DeepSeekFathom", "version": "desktop"},
                },
                self.config.startup_timeout,
                allow_starting=True,
            )
            if not isinstance(result, dict):
                raise MCPProtocolError(f'MCP 服务 "{self.name}" 的 initialize 结果不是对象')
            protocol_version = result.get("protocolVersion")
            if not isinstance(protocol_version, str) or not protocol_version.strip():
                raise MCPProtocolError(f'MCP 服务 "{self.name}" 未返回 protocolVersion')
            capabilities = result.get("capabilities", {})
            server_info = result.get("serverInfo", {})
            if not isinstance(capabilities, dict) or not isinstance(server_info, dict):
                raise MCPProtocolError(f'MCP 服务 "{self.name}" 的初始化信息格式错误')
            self._notify("notifications/initialized", {})
            with self._state_lock:
                if self._state == "closed":
                    raise MCPConnectionError(f'MCP 服务 "{self.name}" 在初始化期间关闭')
                process = self._process
                if self._fatal_error is not None or process is None or process.poll() is not None:
                    detail = self._fatal_error or MCPConnectionError(
                        f'MCP 服务 "{self.name}" 在初始化期间退出'
                    )
                    raise MCPConnectionError(self._diagnostic_error(detail))
                self._protocol_version = protocol_version.strip()
                self._capabilities = copy.deepcopy(capabilities)
                self._server_info = _sanitize_value(server_info, self._secret_values)
                self._state = "connected"
            return self
        except BaseException as exc:
            error = self._diagnostic_error(exc)
            with self._state_lock:
                if self._state != "closed":
                    self._state = "failed"
                    self._last_error = error
            self._shutdown_process()
            if isinstance(exc, MCPError):
                raise
            raise MCPConnectionError(error) from exc

    def list_tools(self, *, refresh: bool = False, timeout: float | None = None) -> list[MCPTool]:
        with self._state_lock:
            if self._tools is not None and not refresh:
                return list(self._tools)
        self._ensure_connected()
        deadline = timeout if timeout is not None else self.config.call_timeout
        cursor: str | None = None
        seen_cursors: set[str] = set()
        raw_tools: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        response_generation: int | None = None

        for _page in range(self.config.max_list_pages):
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result, page_generation = self._request(
                "tools/list",
                params,
                deadline,
                include_generation=True,
            )
            if page_generation is not None:
                if response_generation is None:
                    response_generation = page_generation
                elif response_generation != page_generation:
                    raise MCPConnectionError(
                        f'MCP 服务 "{self.name}" 的工具列表跨越了不同 HTTP 会话，请重试'
                    )
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise self._protocol_failure("tools/list 未返回 tools 数组")
            for item in result["tools"]:
                if not isinstance(item, dict):
                    raise self._protocol_failure("tools/list 包含非对象工具")
                raw_name = item.get("name")
                if not isinstance(raw_name, str) or not raw_name.strip():
                    raise self._protocol_failure("tools/list 包含无名称工具")
                raw_name = raw_name.strip()
                if raw_name in seen_names:
                    raise self._protocol_failure(f'tools/list 重复返回工具 "{raw_name}"')
                seen_names.add(raw_name)
                raw_tools.append(item)

            next_cursor = result.get("nextCursor")
            if next_cursor in (None, ""):
                break
            if not isinstance(next_cursor, str):
                raise self._protocol_failure("tools/list 的 nextCursor 不是字符串")
            if next_cursor in seen_cursors:
                raise self._protocol_failure("tools/list 返回了循环分页游标")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise self._protocol_failure(
                f"tools/list 超过 {self.config.max_list_pages} 页，已停止继续读取"
            )

        tools = [self._decode_tool(item) for item in raw_tools]
        tools.sort(key=lambda item: item.name)
        with self._state_lock:
            self._validate_tool_cache_generation(response_generation)
            self._tools = tools
        return list(tools)

    def _validate_tool_cache_generation(self, generation: int | None) -> None:
        """Validate a transport response epoch while the state lock is held."""

    def tool_definitions(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self.list_tools(refresh=refresh)]

    def openai_tool_definitions(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        return [tool.openai_definition() for tool in self.list_tools(refresh=refresh)]

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self._ensure_connected()
        tool = self._resolve_tool(name)
        args = dict(arguments or {})
        deadline = timeout
        if deadline is None:
            deadline = self.config.tool_timeouts.get(tool.raw_name, self.config.call_timeout)
        result = self._request(
            "tools/call",
            {"name": tool.raw_name, "arguments": args},
            deadline,
        )
        if not isinstance(result, dict):
            raise self._protocol_failure(f'工具 "{tool.raw_name}" 返回结果不是对象')
        return copy.deepcopy(result)

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            state = self._state
            process = self._process
            if state == "connected" and (process is None or process.poll() is not None):
                state = "failed"
            pending_calls = len(self._pending)
            stderr = self._redact(self._stderr.text())
            return {
                "name": self.name,
                "state": state,
                "connected": state == "connected",
                "message": _state_message(state),
                "protocolVersion": self._protocol_version or None,
                "serverInfo": copy.deepcopy(self._server_info),
                "toolCount": len(self._tools or ()),
                "pendingCalls": pending_calls,
                "lastError": self._last_error or None,
                "stderr": _bounded_diagnostic(stderr) or None,
            }

    def close(self) -> None:
        with self._state_lock:
            if self._state == "closed":
                return
            self._state = "closed"
        self._fail_all(MCPConnectionError(f'MCP 服务 "{self.name}" 已关闭'))
        self._shutdown_process()

    def __enter__(self) -> MCPClient:
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _spawn(self) -> None:
        env = os.environ.copy()
        env.update(self.config.env)
        resolved_command = shutil.which(self.config.command, path=env.get("PATH")) or self.config.command
        command = [resolved_command, *self.config.args]
        if os.name == "nt" and Path(resolved_command).suffix.lower() in {".cmd", ".bat"}:
            # CreateProcess cannot execute batch files directly (WinError 193).
            # Bare commands such as `npx` commonly resolve to npx.cmd on Windows,
            # so resolve PATH/PATHEXT before selecting the hidden COMSPEC wrapper.
            comspec = env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
            batch_line = subprocess.list2cmdline(command)
            command = subprocess.list2cmdline([comspec, "/d", "/s", "/c"]) + f' "{batch_line}"'
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(Path(self.config.cwd).expanduser()) if self.config.cwd is not None else None,
            "env": env,
            "shell": False,
            "bufsize": 0,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            process = popen_hidden(command, **kwargs)
        except OSError as exc:
            command_label = Path(self.config.command).name or self.config.command
            raise MCPConnectionError(
                f'MCP 服务 "{self.name}" 启动失败：找不到或无法运行 {command_label}'
            ) from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise MCPConnectionError(f'MCP 服务 "{self.name}" 无法建立 stdio 管道')
        self._process = process
        self._stdin = process.stdin
        self._windows_job = _create_windows_kill_job(process)
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(process.stdout,),
            name=f"mcp-{normalize_tool_component(self.name)}-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            args=(process.stderr,),
            name=f"mcp-{normalize_tool_component(self.name)}-stderr",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()

    def _reader_loop(self, stream: Any) -> None:
        try:
            while True:
                raw = stream.readline(self.config.max_message_bytes + 1)
                if not raw:
                    process = self._process
                    code = process.poll() if process is not None else None
                    suffix = f"（退出码 {code}）" if code is not None else ""
                    raise MCPConnectionError(f'MCP 服务 "{self.name}" 已断开{suffix}')
                if len(raw) > self.config.max_message_bytes:
                    while raw and not raw.endswith(b"\n"):
                        raw = stream.readline(self.config.max_message_bytes + 1)
                    raise MCPProtocolError(
                        f'MCP 服务 "{self.name}" 返回的单条消息超过 '
                        f"{self.config.max_message_bytes} 字节"
                    )
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._record_error(f'MCP 服务 "{self.name}" 输出了无效 JSON：{exc}')
                    continue
                if not isinstance(message, dict):
                    self._record_error(f'MCP 服务 "{self.name}" 输出了非对象 JSON-RPC 消息')
                    continue
                if isinstance(message.get("method"), str):
                    self._handle_server_message(message)
                    continue
                request_id = message.get("id")
                with self._state_lock:
                    pending = self._pending.get(request_id)
                    if pending is not None:
                        pending.response = message
                        pending.event.set()
                        self._pending.pop(request_id, None)
                if pending is None:
                    self._record_error(f'MCP 服务 "{self.name}" 返回了未知请求 ID')
                    continue
        except BaseException as exc:
            with self._state_lock:
                closed = self._state == "closed"
                if not closed:
                    self._state = "failed"
                    self._fatal_error = exc
                    self._last_error = self._diagnostic_error(exc)
            self._fail_all(exc)

    def _stderr_loop(self, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                self._stderr.write(chunk)
        except (OSError, ValueError):
            return

    def _handle_server_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "notifications/tools/list_changed":
            with self._state_lock:
                self._tools = None
        if "id" in message:
            # Sampling, roots, elicitation and other server-to-client requests
            # are intentionally unsupported until the app can apply an explicit
            # user permission boundary to them.
            self._write_json(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": f"DeepSeekFathom does not support server request {method}",
                    },
                }
            )

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        timeout: float,
        *,
        allow_starting: bool = False,
        include_generation: bool = False,
    ) -> Any:
        if timeout <= 0:
            raise ValueError("MCP 请求超时时间必须大于 0")
        with self._state_lock:
            allowed_states = {"connected", "starting"} if allow_starting else {"connected"}
            if self._state not in allowed_states:
                if self._fatal_error is not None:
                    raise MCPConnectionError(self._diagnostic_error(self._fatal_error))
                raise MCPConnectionError(f'MCP 服务 "{self.name}" 当前未连接')
            self._next_id += 1
            request_id = self._next_id
            pending = _PendingRequest()
            self._pending[request_id] = pending
        try:
            self._write_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
        except BaseException:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise

        if not pending.event.wait(timeout):
            with self._state_lock:
                abandoned = self._pending.pop(request_id, None)
            if abandoned is None and pending.event.is_set():
                result = self._decode_response(method, pending)
                return (result, None) if include_generation else result
            try:
                self._notify(
                    "notifications/cancelled",
                    {"requestId": request_id, "reason": "DeepSeekFathom request timeout"},
                )
            except MCPError:
                pass
            error = MCPTimeoutError(
                f'MCP 服务 "{self.name}" 调用 {method} 超时（{timeout:g} 秒）'
            )
            self._record_error(str(error))
            raise error
        result = self._decode_response(method, pending)
        return (result, None) if include_generation else result

    def _decode_response(self, method: str, pending: _PendingRequest) -> Any:
        if pending.error is not None:
            if isinstance(pending.error, MCPError):
                raise pending.error
            raise MCPConnectionError(self._diagnostic_error(pending.error)) from pending.error
        response = pending.response
        if not isinstance(response, dict):
            raise self._protocol_failure(f"{method} 没有收到有效响应")
        if response.get("jsonrpc") not in (None, "2.0"):
            raise self._protocol_failure(f"{method} 返回了不支持的 JSON-RPC 版本")
        remote_error = response.get("error")
        if remote_error is not None:
            if not isinstance(remote_error, dict):
                raise self._protocol_failure(f"{method} 返回了无效 error 对象")
            code = remote_error.get("code", -32000)
            if not isinstance(code, int):
                code = -32000
            message = self._redact(str(remote_error.get("message") or "未知远端错误"))
            data = _sanitize_value(remote_error.get("data"), self._secret_values)
            error = MCPRemoteError(self.name, method, code, message, data)
            self._record_error(str(error))
            raise error
        if "result" not in response:
            raise self._protocol_failure(f"{method} 响应缺少 result")
        return response["result"]

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write_json({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _write_json(self, message: Mapping[str, Any]) -> None:
        try:
            payload = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise MCPProtocolError(f"MCP 请求无法序列化：{exc}") from exc
        with self._write_lock:
            stream = self._stdin
            if stream is None:
                raise MCPConnectionError(f'MCP 服务 "{self.name}" 的输入管道已关闭')
            try:
                stream.write(payload)
                stream.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                error = MCPConnectionError(f'MCP 服务 "{self.name}" 写入失败')
                self._record_error(self._diagnostic_error(error))
                raise error from exc

    def _decode_tool(self, item: dict[str, Any]) -> MCPTool:
        raw_name = str(item["name"]).strip()
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            description = f'MCP 服务 "{self.name}" 的工具 "{raw_name}"'
        schema = item.get("inputSchema", {"type": "object"})
        if not isinstance(schema, dict):
            raise self._protocol_failure(f'工具 "{raw_name}" 的 inputSchema 不是对象')
        if not schema:
            schema = {"type": "object"}
        annotations = item.get("annotations", {})
        if not isinstance(annotations, dict):
            annotations = {}
        return MCPTool(
            server=self.name,
            raw_name=raw_name,
            name=model_tool_name(self.name, raw_name),
            description=description.strip(),
            input_schema=_canonical_object(schema),
            annotations=copy.deepcopy(annotations),
        )

    def _resolve_tool(self, name: str) -> MCPTool:
        query = name.strip()
        tools = self.list_tools()
        for tool in tools:
            if query in {tool.name, tool.raw_name}:
                return tool
        raise MCPError(f'MCP 服务 "{self.name}" 未提供工具 "{query}"')

    def _ensure_connected(self) -> None:
        if not self.connected:
            with self._state_lock:
                fatal = self._fatal_error
            if fatal is not None:
                raise MCPConnectionError(self._diagnostic_error(fatal))
            raise MCPConnectionError(f'MCP 服务 "{self.name}" 当前未连接')

    def _protocol_failure(self, detail: str) -> MCPProtocolError:
        error = MCPProtocolError(f'MCP 服务 "{self.name}" 协议错误：{detail}')
        self._record_error(str(error))
        return error

    def _record_error(self, message: str) -> None:
        with self._state_lock:
            self._last_error = _bounded_diagnostic(self._redact(message))

    def _diagnostic_error(self, exc: BaseException) -> str:
        message = self._redact(str(exc))
        stderr = self._redact(self._stderr.text())
        if stderr and stderr not in message:
            message = f"{message}；stderr: {stderr}"
        return _bounded_diagnostic(message)

    def _redact(self, text: str) -> str:
        return redact_sensitive(text, self._secret_values)

    def _fail_all(self, error: BaseException) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for request in pending:
            request.error = error
            request.event.set()

    def _shutdown_process(self) -> None:
        stream = self._stdin
        self._stdin = None
        process = self._process
        # Kill the tracked tree before closing stdin. Some launchers exit as
        # soon as stdin reaches EOF; if that happens first, taskkill can no
        # longer discover their still-running descendants by the parent PID.
        if process is not None:
            _kill_process_tree(process, self._windows_job)
            self._windows_job = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        if process is None:
            return
        try:
            process.wait(timeout=2.0)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass
        reader = self._reader_thread
        stderr_reader = self._stderr_thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.5)
        if stderr_reader is not None and stderr_reader is not threading.current_thread():
            stderr_reader.join(timeout=0.5)


class MCPHTTPClient(MCPClient):
    """Synchronous Streamable HTTP transport with the same MCP surface as stdio."""

    def __init__(self, config: MCPServerConfig | Mapping[str, Any]):
        super().__init__(config)
        if self.config.transport != "streamable-http":
            raise ValueError("MCPHTTPClient requires streamable-http transport")
        self._http_client: httpx.Client | None = None
        self._session_id = ""
        self._session_generation = 0
        self._reconnect_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._state == "connected" and self._http_client is not None

    def start(self) -> MCPHTTPClient:
        with self._state_lock:
            if self.connected:
                return self
            if self._state == "closed":
                raise MCPConnectionError(f'MCP 服务 "{self.name}" 已关闭，无法重新启动')
            if self._state == "starting":
                raise MCPConnectionError(f'MCP 服务 "{self.name}" 正在启动')
            self._state = "starting"
            self._fatal_error = None
            self._last_error = ""
            self._tools = None

        startup_deadline = time.monotonic() + self.config.startup_timeout
        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "DeepSeekFathom", "version": "desktop"},
                },
                self.config.startup_timeout,
                allow_starting=True,
                absolute_deadline=startup_deadline,
            )
            if not isinstance(result, dict):
                raise MCPProtocolError(f'MCP 服务 "{self.name}" 的 initialize 结果不是对象')
            protocol_version = result.get("protocolVersion")
            if not isinstance(protocol_version, str) or not protocol_version.strip():
                raise MCPProtocolError(f'MCP 服务 "{self.name}" 未返回 protocolVersion')
            capabilities = result.get("capabilities", {})
            server_info = result.get("serverInfo", {})
            if not isinstance(capabilities, dict) or not isinstance(server_info, dict):
                raise MCPProtocolError(f'MCP 服务 "{self.name}" 的初始化信息格式错误')
            with self._state_lock:
                if self._state == "closed" or self._http_client is None:
                    raise MCPConnectionError(f'MCP 服务 "{self.name}" 在初始化期间关闭')
                self._protocol_version = protocol_version.strip()
                self._capabilities = copy.deepcopy(capabilities)
                self._server_info = _sanitize_value(server_info, self._secret_values)
            self._notify("notifications/initialized", {}, deadline=startup_deadline)
            with self._state_lock:
                if self._state == "closed" or self._http_client is None:
                    raise MCPConnectionError(f'MCP 服务 "{self.name}" 在初始化期间关闭')
                self._state = "connected"
            return self
        except BaseException as exc:
            error = self._diagnostic_error(exc)
            with self._state_lock:
                if self._state != "closed":
                    self._state = "failed"
                    self._last_error = error
            self._close_http_transport(send_delete=True)
            if isinstance(exc, MCPError):
                raise
            raise MCPConnectionError(error) from exc

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            state = self._state
            if state == "connected" and self._http_client is None:
                state = "failed"
            return {
                "name": self.name,
                "state": state,
                "connected": state == "connected",
                "message": _state_message(state),
                "protocolVersion": self._protocol_version or None,
                "serverInfo": copy.deepcopy(self._server_info),
                "toolCount": len(self._tools or ()),
                "pendingCalls": len(self._pending),
                "lastError": self._last_error or None,
                "stderr": None,
            }

    def close(self) -> None:
        with self._state_lock:
            if self._state == "closed":
                return
            self._state = "closed"
        self._fail_all(MCPConnectionError(f'MCP 服务 "{self.name}" 已关闭'))
        self._close_http_transport(send_delete=True)

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        timeout: float,
        *,
        allow_starting: bool = False,
        include_generation: bool = False,
        absolute_deadline: float | None = None,
    ) -> Any:
        if timeout <= 0:
            raise ValueError("MCP 请求超时时间必须大于 0")
        deadline = absolute_deadline if absolute_deadline is not None else time.monotonic() + timeout
        with self._state_lock:
            allowed_states = {"connected", "starting"} if allow_starting else {"connected"}
            if self._state not in allowed_states:
                raise MCPConnectionError(f'MCP 服务 "{self.name}" 当前未连接')
            self._next_id += 1
            request_id = self._next_id
            pending = _PendingRequest()
            self._pending[request_id] = pending
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        try:
            recovered = False
            response_generation: int | None = None
            while True:
                try:
                    if include_generation:
                        responses, response_generation = self._post_message(
                            message,
                            deadline=deadline,
                            expected_id=request_id,
                            include_generation=True,
                        )
                    else:
                        responses = self._post_message(
                            message,
                            deadline=deadline,
                            expected_id=request_id,
                        )
                    break
                except _MCPHTTPSessionExpired as exc:
                    if recovered:
                        owns_failed_generation = False
                        with self._state_lock:
                            if (
                                self._session_generation == exc.generation
                                and self._session_id == exc.session_id
                                and self._state == "connected"
                            ):
                                self._session_id = ""
                                self._state = "failed"
                                owns_failed_generation = True
                        error = MCPConnectionError(
                            f'MCP 服务 "{self.name}" 的 HTTP 会话重建后仍然失效'
                        )
                        if owns_failed_generation:
                            self._record_error(str(error))
                        raise error from exc
                    self._recover_http_session(
                        exc.session_id,
                        exc.generation,
                        deadline,
                        operation=method,
                    )
                    recovered = True
            response = next((item for item in responses if item.get("id") == request_id), None)
            if response is None:
                raise self._protocol_failure(f"{method} 没有收到匹配请求 ID 的响应")
            pending.response = response
            result = self._decode_response(method, pending)
            return (result, response_generation) if include_generation else result
        except MCPTimeoutError:
            if time.monotonic() < deadline:
                try:
                    self._post_message(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/cancelled",
                            "params": {"requestId": request_id, "reason": "DeepSeekFathom request timeout"},
                        },
                        deadline=deadline,
                        expected_id=None,
                    )
                except MCPError:
                    pass
            raise
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def _notify(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        deadline: float | None = None,
    ) -> None:
        self._post_message(
            {"jsonrpc": "2.0", "method": method, "params": dict(params)},
            deadline=deadline if deadline is not None else time.monotonic() + self.config.call_timeout,
            expected_id=None,
        )

    def _post_message(
        self,
        message: Mapping[str, Any],
        *,
        deadline: float,
        expected_id: int | str | None,
        include_generation: bool = False,
    ) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
        try:
            body = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MCPProtocolError(f"MCP 请求无法序列化：{exc}") from exc

        operation = str(message.get("method") or "HTTP")
        if time.monotonic() >= deadline:
            raise _http_deadline_error(self.name, operation)
        with self._state_lock:
            client = self._http_client
            if client is None:
                if self._state == "closed":
                    raise MCPConnectionError(f'MCP 服务 "{self.name}" 已关闭')
                client = httpx.Client(follow_redirects=False)
                self._http_client = client
        try:
            with self._state_lock:
                request_headers = self._http_headers()
                sent_session_id = request_headers.get("Mcp-Session-Id", "")
                sent_generation = self._session_generation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _http_deadline_error(self.name, operation)
            with client.stream(
                "POST",
                self.config.url,
                content=body,
                headers=request_headers,
                timeout=httpx.Timeout(remaining),
            ) as response:
                if time.monotonic() >= deadline:
                    raise _http_deadline_error(self.name, operation)
                self._capture_session_id(response, sent_generation)
                if response.status_code in {202, 204}:
                    if expected_id is not None:
                        raise self._protocol_failure(
                            f"HTTP {response.status_code} 未返回 {message.get('method', 'request')} 响应"
                        )
                    return ([], sent_generation) if include_generation else []
                if response.status_code == 404:
                    if sent_session_id:
                        raise _MCPHTTPSessionExpired(self.name, sent_session_id, sent_generation)
                if response.status_code < 200 or response.status_code >= 300:
                    detail = self._redact(_read_bounded_http_body(
                        response,
                        self.config.max_message_bytes,
                        deadline=deadline,
                        server=self.name,
                        operation=operation,
                    ))
                    suffix = f"：{detail}" if detail else ""
                    raise MCPConnectionError(
                        f'MCP 服务 "{self.name}" HTTP {response.status_code}{suffix}'
                    )
                messages = self._read_http_messages(
                    response,
                    expected_id,
                    deadline=deadline,
                    operation=operation,
                )
        except MCPTimeoutError as exc:
            self._record_error(str(exc))
            raise
        except MCPError:
            raise
        except httpx.TimeoutException as exc:
            error = _http_deadline_error(self.name, operation)
            self._record_error(str(error))
            raise error from exc
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            error = MCPConnectionError(
                f'MCP 服务 "{self.name}" HTTP 连接失败：{self._redact(str(exc))}'
            )
            self._record_error(str(error))
            raise error from exc

        output: list[dict[str, Any]] = []
        for item in messages:
            if isinstance(item.get("method"), str):
                self._handle_http_server_message(item)
                continue
            output.append(item)
        return (output, sent_generation) if include_generation else output

    def _validate_tool_cache_generation(self, generation: int | None) -> None:
        if (
            generation is None
            or self._state != "connected"
            or self._session_generation != generation
        ):
            raise MCPConnectionError(
                f'MCP 服务 "{self.name}" 的 HTTP 会话在读取工具列表期间发生变化，请重试'
            )

    def _recover_http_session(
        self,
        expired_session_id: str,
        expired_generation: int,
        deadline: float,
        *,
        operation: str,
    ) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._reconnect_lock.acquire(timeout=remaining):
            raise _http_deadline_error(self.name, operation)
        try:
            if time.monotonic() >= deadline:
                raise _http_deadline_error(self.name, operation)
            with self._state_lock:
                if self._state == "closed":
                    raise MCPConnectionError(f'MCP 服务 "{self.name}" 已关闭')
                if (
                    self._session_generation != expired_generation
                    or self._session_id != expired_session_id
                ):
                    if (
                        self._session_generation > expired_generation
                        and self._state == "connected"
                    ):
                        return
                    raise MCPConnectionError(
                        f'MCP 服务 "{self.name}" 的并发 HTTP 会话重建未成功'
                    )
                recovery_generation = self._session_generation + 1
                self._session_generation = recovery_generation
                self._session_id = ""
                self._protocol_version = ""
                self._tools = None
                self._state = "starting"
                self._next_id += 1
                initialize_id = self._next_id
            initialize = {
                "jsonrpc": "2.0",
                "id": initialize_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "DeepSeekFathom", "version": "desktop"},
                },
            }
            try:
                responses = self._post_message(
                    initialize,
                    deadline=deadline,
                    expected_id=initialize_id,
                )
                response = next((item for item in responses if item.get("id") == initialize_id), None)
                if response is None:
                    raise self._protocol_failure("initialize 没有收到匹配请求 ID 的响应")
                pending = _PendingRequest(response=response)
                result = self._decode_response("initialize", pending)
                if not isinstance(result, dict):
                    raise self._protocol_failure("initialize 结果不是对象")
                protocol_version = result.get("protocolVersion")
                capabilities = result.get("capabilities", {})
                server_info = result.get("serverInfo", {})
                if not isinstance(protocol_version, str) or not protocol_version.strip():
                    raise self._protocol_failure("initialize 未返回 protocolVersion")
                if not isinstance(capabilities, dict) or not isinstance(server_info, dict):
                    raise self._protocol_failure("initialize 返回了无效初始化信息")
                with self._state_lock:
                    if (
                        self._session_generation != recovery_generation
                        or self._state != "starting"
                    ):
                        raise MCPConnectionError(
                            f'MCP 服务 "{self.name}" 的 HTTP 会话重建状态已失效'
                        )
                    self._protocol_version = protocol_version.strip()
                    self._capabilities = copy.deepcopy(capabilities)
                    self._server_info = _sanitize_value(server_info, self._secret_values)
                self._post_message(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                    deadline=deadline,
                    expected_id=None,
                )
                with self._state_lock:
                    if (
                        self._session_generation != recovery_generation
                        or self._state != "starting"
                    ):
                        raise MCPConnectionError(
                            f'MCP 服务 "{self.name}" 的 HTTP 会话重建状态已失效'
                        )
                    self._state = "connected"
            except BaseException as exc:
                with self._state_lock:
                    if (
                        self._session_generation == recovery_generation
                        and self._state != "closed"
                    ):
                        self._state = "failed"
                        self._last_error = self._diagnostic_error(exc)
                if isinstance(exc, MCPError):
                    raise
                raise MCPConnectionError(self._diagnostic_error(exc)) from exc
        finally:
            self._reconnect_lock.release()

    def _http_headers(self, *, session_id: str | None = None) -> dict[str, str]:
        headers = dict(self.config.headers)
        headers.update({
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self._protocol_version or PROTOCOL_VERSION,
        })
        with self._state_lock:
            active_session = self._session_id if session_id is None else session_id
        if active_session:
            headers["Mcp-Session-Id"] = active_session
        return headers

    def _capture_session_id(self, response: httpx.Response, sent_generation: int) -> None:
        session_id = str(response.headers.get("Mcp-Session-Id") or "").strip()
        if not session_id:
            return
        with self._state_lock:
            if (
                self._session_generation != sent_generation
                or self._state not in {"starting", "connected"}
            ):
                return
            if len(session_id) > 4096 or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in session_id
            ):
                raise self._protocol_failure("Mcp-Session-Id 响应头无效")
            if self._session_id and self._session_id != session_id:
                raise self._protocol_failure("Mcp-Session-Id 在会话期间发生变化")
            self._session_id = session_id

    def _read_http_messages(
        self,
        response: httpx.Response,
        expected_id: int | str | None,
        *,
        deadline: float,
        operation: str,
    ) -> list[dict[str, Any]]:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            raw = _read_bounded_http_bytes(
                response,
                self.config.max_message_bytes,
                deadline=deadline,
                server=self.name,
                operation=operation,
            )
            if not raw.strip():
                return []
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise self._protocol_failure(f"HTTP JSON 响应无效：{exc}") from exc
            values = parsed if isinstance(parsed, list) else [parsed]
            if any(not isinstance(item, dict) for item in values):
                raise self._protocol_failure("HTTP JSON-RPC 响应必须是对象")
            return list(values)
        if content_type == "text/event-stream":
            return self._read_sse_messages(
                response,
                expected_id,
                deadline=deadline,
                operation=operation,
            )
        raise self._protocol_failure(f"HTTP 响应 Content-Type 不受支持：{content_type or 'missing'}")

    def _read_sse_messages(
        self,
        response: httpx.Response,
        expected_id: int | str | None,
        *,
        deadline: float,
        operation: str,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        data_lines: list[str] = []
        consumed = 0
        line_buffer = bytearray()

        def flush_event() -> bool:
            if not data_lines:
                return False
            payload = "\n".join(data_lines)
            data_lines.clear()
            if not payload or payload == "[DONE]":
                return False
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise self._protocol_failure(f"SSE data 不是有效 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise self._protocol_failure("SSE JSON-RPC 消息必须是对象")
            messages.append(parsed)
            return expected_id is not None and parsed.get("id") == expected_id

        def process_line(raw_line: bytes) -> bool:
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise self._protocol_failure(f"SSE 行不是有效 UTF-8：{exc}") from exc
            if not line:
                return flush_event()
            if line.startswith(":"):
                return False
            field, separator, value = line.partition(":")
            if field == "data":
                data_lines.append(value[1:] if separator and value.startswith(" ") else value)
            return False

        def drain_lines(*, final: bool = False) -> bool:
            start = 0
            while start < len(line_buffer):
                lf = line_buffer.find(b"\n", start)
                cr = line_buffer.find(b"\r", start)
                boundaries = [position for position in (lf, cr) if position >= 0]
                if not boundaries:
                    break
                boundary = min(boundaries)
                if line_buffer[boundary] == 0x0D and boundary + 1 == len(line_buffer) and not final:
                    break
                next_start = boundary + 1
                if (
                    line_buffer[boundary] == 0x0D
                    and next_start < len(line_buffer)
                    and line_buffer[next_start] == 0x0A
                ):
                    next_start += 1
                if process_line(bytes(line_buffer[start:boundary])):
                    return True
                start = next_start
            if start:
                del line_buffer[:start]
            if final and line_buffer:
                raw_line = bytes(line_buffer)
                line_buffer.clear()
                return process_line(raw_line)
            return False

        with _HTTPResponseDeadline(response, deadline, self.name, operation) as response_deadline:
            for chunk in response.iter_bytes():
                response_deadline.check()
                consumed += len(chunk)
                if consumed > self.config.max_message_bytes:
                    raise self._protocol_failure(
                        f"HTTP SSE 响应超过 {self.config.max_message_bytes} 字节"
                    )
                line_buffer.extend(chunk)
                if drain_lines():
                    return messages
            if drain_lines(final=True) or flush_event():
                return messages
        return messages

    def _handle_http_server_message(self, message: dict[str, Any]) -> None:
        if message.get("method") == "notifications/tools/list_changed":
            with self._state_lock:
                self._tools = None
        if "id" in message:
            self._record_error(
                f'MCP 服务 "{self.name}" 发出了暂不支持的服务端请求 {message.get("method")}'
            )

    def _close_http_transport(self, *, send_delete: bool) -> None:
        with self._state_lock:
            client = self._http_client
            session_id = self._session_id
            delete_headers = self._http_headers(session_id=session_id) if session_id else {}
            self._http_client = None
            self._session_id = ""
        if client is None:
            return
        if send_delete and session_id:
            try:
                client.request(
                    "DELETE",
                    self.config.url,
                    headers=delete_headers,
                    timeout=httpx.Timeout(min(2.0, self.config.call_timeout)),
                )
            except (httpx.HTTPError, OSError, RuntimeError):
                pass
        client.close()


class MCPHost:
    """Own multiple MCP clients without allowing one failed server to block others."""

    def __init__(self, configs: Iterable[MCPServerConfig | Mapping[str, Any]]):
        ordered: dict[str, MCPServerConfig] = {}
        for item in configs:
            config = _coerce_config(item)
            if config.name in ordered:
                raise ValueError(f'重复的 MCP 服务名称："{config.name}"')
            ordered[config.name] = config
        self._configs = ordered
        self._clients: dict[str, MCPClient] = {}
        self._connecting: dict[str, MCPClient] = {}
        self._tool_index: dict[str, tuple[MCPClient, MCPTool]] = {}
        self._failures: dict[str, str] = {}
        self._disconnected: set[str] = set()
        self._server_locks = {name: threading.Lock() for name in ordered}
        self._lock = threading.RLock()
        self._closed = False

    def connect_all(self) -> list[dict[str, Any]]:
        """Connect every configured server concurrently and return all statuses."""

        with self._lock:
            if self._closed:
                raise MCPConnectionError("MCP 主机已关闭")
            names = list(self._configs)
        if names:
            with ThreadPoolExecutor(max_workers=min(8, len(names)), thread_name_prefix="mcp-connect") as pool:
                futures = [pool.submit(self._connect_for_all, name) for name in names]
                for future in futures:
                    # _connect_for_all contains failures by design; this protects
                    # other services even from an unexpected implementation bug.
                    try:
                        future.result()
                    except BaseException:
                        continue
        return self.status()

    def connect(self, name: str) -> list[MCPTool]:
        config = self._configs.get(name)
        if config is None:
            raise MCPError(f'没有配置名为 "{name}" 的 MCP 服务')
        server_lock = self._server_locks[name]
        with server_lock:
            with self._lock:
                if self._closed:
                    raise MCPConnectionError("MCP 主机已关闭")
                existing = self._clients.get(name)
            if existing is not None and existing.connected:
                tools = existing.list_tools()
                if not self._install_tools(existing, tools):
                    raise MCPConnectionError("MCP 主机已关闭")
                return tools
            if existing is not None:
                with self._lock:
                    self._remove_server_tools(name)
                existing.close()
            client = _client_for_config(config)
            with self._lock:
                if self._closed:
                    raise MCPConnectionError("MCP 主机已关闭")
                self._connecting[name] = client
            try:
                client.start()
                tools = client.list_tools()
            except BaseException as exc:
                try:
                    client.close()
                except Exception:
                    pass
                with self._lock:
                    self._connecting.pop(name, None)
                    if not self._closed:
                        self._failures[name] = _bounded_diagnostic(redact_sensitive(str(exc)))
                    self._remove_server_tools(name)
                raise
            with self._lock:
                self._connecting.pop(name, None)
                if self._closed:
                    client.close()
                    raise MCPConnectionError("MCP 主机已关闭")
                self._clients[name] = client
                self._failures.pop(name, None)
                self._disconnected.discard(name)
                self._install_tools(client, tools)
            return tools

    def reconnect(self, name: str) -> list[MCPTool]:
        self.disconnect(name)
        return self.connect(name)

    def disconnect(self, name: str) -> bool:
        if name not in self._configs:
            raise MCPError(f'没有配置名为 "{name}" 的 MCP 服务')
        server_lock = self._server_locks[name]
        with server_lock:
            with self._lock:
                client = self._clients.pop(name, None)
                self._failures.pop(name, None)
                self._disconnected.add(name)
                self._remove_server_tools(name)
            if client is not None:
                client.close()
                return True
            return False

    def tool_definitions(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            all_clients = list(self._clients.values())
            clients = [client for client in all_clients if client.connected]
            for client in all_clients:
                if not client.connected:
                    self._remove_server_tools(client.name)
        for client in clients:
            try:
                tools = client.list_tools(refresh=refresh)
            except MCPError as exc:
                with self._lock:
                    self._failures[client.name] = _bounded_diagnostic(str(exc))
                continue
            self._install_tools(client, tools)
        with self._lock:
            tools = [item[1] for item in self._tool_index.values()]
        tools.sort(key=lambda item: item.name)
        return [tool.definition() for tool in tools]

    def openai_tool_definitions(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        self.tool_definitions(refresh=refresh)
        with self._lock:
            tools = [item[1] for item in self._tool_index.values()]
        tools.sort(key=lambda item: item.name)
        return [tool.openai_definition() for tool in tools]

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            target = self._tool_index.get(name)
        if target is None:
            # A server may have sent notifications/tools/list_changed since the
            # last provider request. Refresh once before declaring it unknown.
            self.tool_definitions()
            with self._lock:
                target = self._tool_index.get(name)
        if target is None:
            raise MCPError(f'没有已连接的 MCP 工具 "{name}"')
        client, tool = target
        return client.call_tool(tool.raw_name, arguments, timeout=timeout)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            clients = dict(self._clients)
            failures = dict(self._failures)
            disconnected = set(self._disconnected)
            closed = self._closed
            names = list(self._configs)
        output: list[dict[str, Any]] = []
        for name in names:
            client = clients.get(name)
            if client is not None:
                item = client.status()
                if name in failures and not item.get("lastError"):
                    item["lastError"] = failures[name]
                output.append(item)
                continue
            state = (
                "closed"
                if closed
                else "disconnected"
                if name in disconnected
                else "failed"
                if name in failures
                else "configured"
            )
            output.append(
                {
                    "name": name,
                    "state": state,
                    "connected": False,
                    "message": _state_message(state),
                    "protocolVersion": None,
                    "serverInfo": {},
                    "toolCount": 0,
                    "pendingCalls": 0,
                    "lastError": failures.get(name),
                    "stderr": None,
                }
            )
        return output

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = list(dict.fromkeys([*self._clients.values(), *self._connecting.values()]))
            self._clients.clear()
            self._connecting.clear()
            self._tool_index.clear()
        if clients:
            with ThreadPoolExecutor(max_workers=min(8, len(clients)), thread_name_prefix="mcp-close") as pool:
                list(pool.map(lambda client: client.close(), clients))

    def __enter__(self) -> MCPHost:
        self.connect_all()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _connect_for_all(self, name: str) -> None:
        try:
            self.connect(name)
        except BaseException:
            return

    def _install_tools(self, client: MCPClient, tools: Iterable[MCPTool]) -> bool:
        with self._lock:
            if self._closed or self._clients.get(client.name) is not client:
                return False
            self._remove_server_tools(client.name)
            for tool in tools:
                existing = self._tool_index.get(tool.name)
                if existing is not None and existing[0].name != client.name:
                    # Stable hashed normalization should make this practically
                    # impossible, but fail closed rather than route to a server
                    # different from the model-visible namespace.
                    raise MCPProtocolError(f'MCP 工具名称冲突："{tool.name}"')
                self._tool_index[tool.name] = (client, tool)
            return True

    def _remove_server_tools(self, server: str) -> None:
        stale = [name for name, (client, _tool) in self._tool_index.items() if client.name == server]
        for name in stale:
            self._tool_index.pop(name, None)


def _coerce_config(config: MCPServerConfig | Mapping[str, Any]) -> MCPServerConfig:
    if isinstance(config, MCPServerConfig):
        return config
    if isinstance(config, Mapping):
        return MCPServerConfig(**dict(config))
    raise TypeError("MCP 配置必须是 MCPServerConfig 或映射")


def _client_for_config(config: MCPServerConfig) -> MCPClient:
    if config.transport == "streamable-http":
        return MCPHTTPClient(config)
    return MCPClient(config)


def _config_secret_values(config: MCPServerConfig) -> tuple[str, ...]:
    values = {
        value
        for key, value in config.env.items()
        if value and _SENSITIVE_KEY_RE.search(key)
    }
    args = config.args
    for index, argument in enumerate(args):
        if not _SENSITIVE_KEY_RE.search(argument):
            continue
        if "=" in argument:
            candidate = argument.split("=", 1)[1].strip("\"'")
            if candidate:
                values.add(candidate)
        elif index + 1 < len(args):
            candidate = args[index + 1].strip("\"'")
            if candidate and not candidate.startswith("-"):
                values.add(candidate)
    for value in config.headers.values():
        if value:
            values.add(value)
    query = urlsplit(config.url).query if config.url else ""
    for _key, value in parse_qsl(query, keep_blank_values=False):
        if value:
            values.add(value)
    for component in query.split("&"):
        _separator, _equals, raw_value = component.partition("=")
        if raw_value:
            values.add(raw_value)
    return tuple(sorted(values, key=len, reverse=True))


def _http_deadline_error(server: str, operation: str) -> MCPTimeoutError:
    return MCPTimeoutError(f'MCP 服务 "{server}" 调用 {operation} 超时（已达到总时限）')


class _HTTPResponseDeadline:
    """Close a streaming response at an absolute deadline to unblock sync reads."""

    def __init__(self, response: httpx.Response, deadline: float, server: str, operation: str):
        self.response = response
        self.deadline = deadline
        self.server = server
        self.operation = operation
        self.expired = threading.Event()
        self.timer: threading.Timer | None = None

    def __enter__(self) -> _HTTPResponseDeadline:
        self.check()
        self.timer = threading.Timer(max(0.0, self.deadline - time.monotonic()), self._expire)
        self.timer.daemon = True
        self.timer.start()
        return self

    def __exit__(self, exc_type: Any, _exc: Any, _tb: Any) -> bool:
        if self.timer is not None:
            self.timer.cancel()
        if exc_type is not None and issubclass(exc_type, MCPTimeoutError):
            return False
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            raise _http_deadline_error(self.server, self.operation)
        return False

    def check(self) -> None:
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            self.expired.set()
            raise _http_deadline_error(self.server, self.operation)

    def _expire(self) -> None:
        self.expired.set()
        try:
            self.response.close()
        except Exception:
            pass


def _read_bounded_http_bytes(
    response: httpx.Response,
    limit: int,
    *,
    deadline: float | None = None,
    server: str = "MCP",
    operation: str = "HTTP",
) -> bytes:
    absolute_deadline = deadline if deadline is not None else time.monotonic() + 86_400.0
    with _HTTPResponseDeadline(response, absolute_deadline, server, operation) as response_deadline:
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    raise MCPProtocolError(f"MCP HTTP 响应超过 {limit} 字节")
            except ValueError:
                pass
        body = bytearray()
        for chunk in response.iter_bytes():
            response_deadline.check()
            if len(body) + len(chunk) > limit:
                raise MCPProtocolError(f"MCP HTTP 响应超过 {limit} 字节")
            body.extend(chunk)
        return bytes(body)


def _read_bounded_http_body(
    response: httpx.Response,
    limit: int,
    *,
    deadline: float | None = None,
    server: str = "MCP",
    operation: str = "HTTP",
) -> str:
    try:
        body = _read_bounded_http_bytes(
            response,
            limit,
            deadline=deadline,
            server=server,
            operation=operation,
        )
    except MCPProtocolError:
        return "响应体过大"
    return body.decode("utf-8", errors="replace").strip()[:MAX_DIAGNOSTIC_CHARS]


def _canonical_object(value: Mapping[str, Any]) -> dict[str, Any]:
    # A JSON round trip both detaches the untrusted server object and rejects
    # values that cannot be forwarded in a provider request.
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MCPProtocolError(f"MCP 工具 schema 不是有效 JSON：{exc}") from exc
    if not isinstance(decoded, dict):
        raise MCPProtocolError("MCP 工具 schema 必须是对象")
    return decoded


def _sanitize_value(value: Any, secrets: Iterable[str], key: str = "") -> Any:
    if key and _SENSITIVE_KEY_RE.search(key):
        return "***"
    if isinstance(value, str):
        return redact_sensitive(value, secrets)
    if isinstance(value, Mapping):
        return {str(k): _sanitize_value(v, secrets, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, secrets) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive(str(value), secrets)


def _bounded_diagnostic(message: str) -> str:
    compact = " ".join(str(message).split())
    if len(compact) <= MAX_DIAGNOSTIC_CHARS:
        return compact
    return compact[: MAX_DIAGNOSTIC_CHARS - 3] + "..."


def _state_message(state: str) -> str:
    return {
        "configured": "尚未连接",
        "starting": "正在连接",
        "connected": "已连接",
        "failed": "连接失败",
        "disconnected": "已断开",
        "closed": "已关闭",
    }.get(state, state)


def _create_windows_kill_job(process: subprocess.Popen[bytes]) -> int | None:
    """Attach the server to a kill-on-close Job Object when Windows permits it."""

    if os.name != "nt" or not hasattr(process, "_handle"):
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _kill_process_tree(process: subprocess.Popen[bytes], windows_job: int | None) -> None:
    if os.name == "nt":
        if windows_job:
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle(wintypes.HANDLE(windows_job))
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        if process.poll() is None:
            try:
                run_hidden(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                try:
                    process.terminate()
                except OSError:
                    pass
        return

    # start_new_session=True makes the server PID its process-group ID. Killing
    # the group also reaps launchers and grandchildren that inherited stdio.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=1.0)
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        # The launcher may already have exited while a grandchild kept the
        # process group alive. Probe the group, not only ``process.poll()``.
        os.killpg(process.pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


__all__ = [
    "MCPClient",
    "MCPConnectionError",
    "MCPError",
    "MCPHost",
    "MCPProtocolError",
    "MCPRemoteError",
    "MCPServerConfig",
    "MCPTimeoutError",
    "MCPTool",
    "PROTOCOL_VERSION",
    "model_tool_name",
    "normalize_tool_component",
    "redact_sensitive",
]
