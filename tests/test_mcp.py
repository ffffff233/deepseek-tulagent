from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import httpx
import pytest

import deepseek_tulagent.mcp as mcp_module
from deepseek_tulagent.mcp import (
    MCPClient,
    MCPConnectionError,
    MCPHost,
    MCPProtocolError,
    MCPRemoteError,
    MCPServerConfig,
    MCPTimeoutError,
    model_tool_name,
    redact_sensitive,
)


FAKE_MCP_SERVER = r'''
import json
import os
import subprocess
import sys
import threading
import time

write_lock = threading.Lock()
initialized = False
mode = os.environ.get("FAKE_MODE", "normal")
secret = os.environ.get("FAKE_SECRET", "")
log_path = os.environ.get("FAKE_LOG", "")


def send(message):
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    with write_lock:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def result(request_id, value):
    send({"jsonrpc": "2.0", "id": request_id, "result": value})


def error(request_id, code, message, data=None):
    body = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    send({"jsonrpc": "2.0", "id": request_id, "error": body})


def delayed(request_id, value, delay):
    time.sleep(delay)
    result(request_id, {"content": [{"type": "text", "text": value}]})


def log(message):
    if not log_path:
        return
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(message, ensure_ascii=False) + "\n")


TOOLS_PAGE_ONE = [
    {
        "name": "echo",
        "description": "Echo text from the fake server",
        "inputSchema": {
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
            "type": "object",
        },
    },
    {
        "name": "read_hint",
        "description": "Read-only annotation test",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    },
]
TOOLS_PAGE_TWO = [
    {"name": "delay", "description": "Respond later", "inputSchema": {"type": "object"}},
    {"name": "hang", "description": "Never respond", "inputSchema": {"type": "object"}},
    {"name": "explode", "description": "Return a secret error", "inputSchema": {"type": "object"}},
    {"name": "announce_change", "description": "Send list changed", "inputSchema": {"type": "object"}},
    {"name": "spawn_child", "description": "Spawn a descendant", "inputSchema": {"type": "object"}},
]


for line in sys.stdin:
    try:
        message = json.loads(line)
    except Exception:
        continue
    log(message)
    method = message.get("method", "")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        if mode == "bad_init":
            result(request_id, {"capabilities": {"tools": {}}})
            continue
        if params.get("protocolVersion") != "2025-06-18":
            error(request_id, -32602, "unexpected protocol")
            continue
        client_info = params.get("clientInfo") or {}
        if client_info.get("name") != "DeepSeekFathom":
            error(request_id, -32602, "unexpected client")
            continue
        result(request_id, {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "fake-mcp", "version": "1.0", "api_key": secret},
        })
        continue

    if method == "notifications/initialized":
        initialized = True
        continue

    if method == "notifications/cancelled":
        continue

    if method == "tools/list":
        if not initialized:
            error(request_id, -32000, "not initialized")
            continue
        cursor = params.get("cursor")
        if mode == "cycle":
            result(request_id, {"tools": TOOLS_PAGE_ONE if cursor is None else [], "nextCursor": "same"})
        elif cursor is None:
            result(request_id, {"tools": TOOLS_PAGE_ONE, "nextCursor": "page-2"})
        elif cursor == "page-2":
            result(request_id, {"tools": TOOLS_PAGE_TWO})
        else:
            error(request_id, -32602, "bad cursor")
        continue

    if method != "tools/call":
        if request_id is not None:
            error(request_id, -32601, "method not found")
        continue

    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "echo":
        result(request_id, {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]})
    elif name == "read_hint":
        result(request_id, {"content": [{"type": "text", "text": "read"}]})
    elif name == "delay":
        worker = threading.Thread(
            target=delayed,
            args=(request_id, str(arguments.get("value", "")), float(arguments.get("delay", 0))),
            daemon=True,
        )
        worker.start()
    elif name == "hang":
        pass
    elif name == "explode":
        sys.stderr.write("token=" + secret + "\n")
        sys.stderr.flush()
        error(request_id, 4100, "Authorization=" + secret, {"api_key": secret})
    elif name == "announce_change":
        result(request_id, {"content": [{"type": "text", "text": "changed"}]})
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}})
    elif name == "spawn_child":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result(request_id, {"content": [{"type": "text", "text": str(child.pid)}]})
    else:
        error(request_id, -32602, "unknown tool")
'''


def write_fake_server(tmp_path: Path) -> Path:
    path = tmp_path / "fake_mcp_server.py"
    path.write_text(FAKE_MCP_SERVER, encoding="utf-8")
    return path


def fake_config(
    tmp_path: Path,
    *,
    name: str = "fake",
    mode: str = "normal",
    secret: str = "",
    call_timeout: float = 2.0,
) -> tuple[MCPServerConfig, Path]:
    script = write_fake_server(tmp_path)
    log_path = tmp_path / f"{name}.jsonl"
    config = MCPServerConfig(
        name=name,
        command=sys.executable,
        args=(str(script),),
        env={"FAKE_MODE": mode, "FAKE_SECRET": secret, "FAKE_LOG": str(log_path)},
        startup_timeout=3.0,
        call_timeout=call_timeout,
    )
    return config, log_path


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def result_text(result: dict) -> str:
    return "".join(
        item.get("text", "")
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    )


class FakeStreamableMCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *args):
        return

    def _send(self, status, body=b"", content_type="application/json", extra_headers=None):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json_response(self, request_id, result, *, session=False):
        body = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Mcp-Session-Id": self.server.session_id} if session else None
        self._send(200, body, extra_headers=headers)

    def _sse_response(self, request_id, result):
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        )
        body = f"event: message\ndata: {payload}\n\n".encode("utf-8")
        self._send(200, body, "text/event-stream; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            message = json.loads(body)
        except json.JSONDecodeError:
            self._send(400, b"bad json", "text/plain")
            return
        method = message.get("method")
        with self.server.state_lock:
            self.server.requests.append({
                "method": method,
                "accept": self.headers.get("Accept"),
                "session": self.headers.get("Mcp-Session-Id"),
                "protocol": self.headers.get("MCP-Protocol-Version"),
                "authorization": self.headers.get("Authorization"),
                "params": message.get("params") or {},
            })

        if self.path.startswith("/reject"):
            detail = (
                f"Authorization={self.server.secret}; custom={self.headers.get('X-Custom')}; url={self.path}"
            ).encode("utf-8")
            self._send(401, detail, "text/plain")
            return
        if self.headers.get("Authorization") != f"Bearer {self.server.secret}":
            self._send(401, b"unauthorized", "text/plain")
            return
        request_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            self._json_response(request_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-http", "version": "1"},
            }, session=True)
            return
        if method == "tools/call" and self.server.expire_tool_calls:
            with self.server.state_lock:
                self.server.forced_expirations += 1
                self.server.session_id = f"forced-expiry-{self.server.forced_expirations}"
        if self.headers.get("Mcp-Session-Id") != self.server.session_id:
            barrier = self.server.expired_barrier
            if barrier is not None:
                barrier.wait(timeout=2.0)
            self._send(404, b"missing session", "text/plain")
            return
        if method in {"notifications/initialized", "notifications/cancelled"}:
            self._send(202)
            return
        if method == "tools/list":
            if params.get("cursor") == "page-2":
                self._sse_response(request_id, {"tools": [
                    {"name": "slow", "description": "Slow response", "inputSchema": {"type": "object"}},
                ]})
            else:
                self._json_response(request_id, {
                    "tools": [{
                        "name": "echo",
                        "description": "Echo over Streamable HTTP",
                        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                    }],
                    "nextCursor": "page-2",
                })
            return
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "slow":
                time.sleep(float(arguments.get("delay", 0.2)))
                self._json_response(request_id, {"content": [{"type": "text", "text": "slow"}]})
                return
            self._sse_response(request_id, {
                "content": [{"type": "text", "text": str(arguments.get("text", ""))}],
            })
            return
        self._send(400, b"unknown method", "text/plain")

    def do_DELETE(self):
        with self.server.state_lock:
            self.server.deletes.append({
                "session": self.headers.get("Mcp-Session-Id"),
                "protocol": self.headers.get("MCP-Protocol-Version"),
                "authorization": self.headers.get("Authorization"),
            })
        self._send(405)


@contextmanager
def fake_streamable_mcp_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeStreamableMCPHandler)
    server.daemon_threads = True
    server.secret = "http-secret-value-123"
    server.session_id = "session-abc-123"
    server.requests = []
    server.deletes = []
    server.expire_tool_calls = False
    server.forced_expirations = 0
    server.expired_barrier = None
    server.state_lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return predicate()


def pid_exists(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def test_initialize_paginated_tools_and_dynamic_definitions(tmp_path: Path):
    config, log_path = fake_config(tmp_path)
    client = MCPClient(config)
    try:
        client.start()
        tools = client.list_tools()
        assert {tool.raw_name for tool in tools} == {
            "echo",
            "read_hint",
            "delay",
            "hang",
            "explode",
            "announce_change",
            "spawn_child",
        }
        definitions = client.tool_definitions()
        echo = next(item for item in definitions if item["name"] == "mcp__fake__echo")
        assert echo == {
            "name": "mcp__fake__echo",
            "description": "Echo text from the fake server",
            "schema": {
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "type": "object",
            },
            "origin": {"kind": "mcp", "server": "fake", "tool": "echo"},
            "read_only": False,
        }
        readonly = next(item for item in definitions if item["name"] == "mcp__fake__read_hint")
        assert readonly["read_only"] is True
        assert result_text(client.call_tool("mcp__fake__echo", {"text": "hello"})) == "hello"

        status = client.status()
        assert status["state"] == "connected"
        assert status["message"] == "已连接"
        assert status["toolCount"] == 7
        messages = read_log(log_path)
        assert messages[0]["method"] == "initialize"
        assert messages[0]["params"]["clientInfo"]["name"] == "DeepSeekFathom"
        assert any(item.get("method") == "notifications/initialized" for item in messages)
        list_calls = [item for item in messages if item.get("method") == "tools/list"]
        assert [item["params"] for item in list_calls] == [{}, {"cursor": "page-2"}]
    finally:
        client.close()


def test_streamable_http_host_supports_json_sse_session_timeout_and_delete():
    with fake_streamable_mcp_server() as (server, url):
        config = MCPServerConfig(
            name="remote",
            transport="streamable-http",
            url=url,
            headers={"Authorization": f"Bearer {server.secret}"},
            startup_timeout=2.0,
            call_timeout=1.0,
        )
        host = MCPHost([config])
        try:
            tools = host.connect("remote")
            assert {tool.raw_name for tool in tools} == {"echo", "slow"}
            echo_name = model_tool_name("remote", "echo")
            slow_name = model_tool_name("remote", "slow")
            assert result_text(host.call_tool(echo_name, {"text": "over http"})) == "over http"
            with pytest.raises(MCPTimeoutError, match="tools/call"):
                host.call_tool(slow_name, {"delay": 0.2}, timeout=0.05)
            assert result_text(host.call_tool(echo_name, {"text": "still alive"})) == "still alive"
        finally:
            host.close()

        with server.state_lock:
            requests = list(server.requests)
            deletes = list(server.deletes)
        assert requests[0]["method"] == "initialize"
        assert requests[0]["session"] is None
        assert requests[0]["protocol"] == "2025-06-18"
        assert all(
            "application/json" in request["accept"] and "text/event-stream" in request["accept"]
            for request in requests
        )
        assert all(
            request["session"] == server.session_id
            for request in requests[1:]
        )
        assert all(
            request["protocol"] == "2025-06-18"
            for request in requests[1:]
        )
        assert deletes == [{
            "session": server.session_id,
            "protocol": "2025-06-18",
            "authorization": f"Bearer {server.secret}",
        }]


def test_streamable_http_expired_session_reinitializes_and_replays_once():
    with fake_streamable_mcp_server() as (server, url):
        config = MCPServerConfig(
            name="remote",
            transport="http",
            url=url,
            headers={"Authorization": f"Bearer {server.secret}"},
        )
        host = MCPHost([config])
        try:
            host.connect("remote")
            echo_name = model_tool_name("remote", "echo")
            server.session_id = "replacement-session"
            assert result_text(host.call_tool(echo_name, {"text": "self-healed"})) == "self-healed"
            assert host.status()[0]["state"] == "connected"
            host.tool_definitions()
            with server.state_lock:
                initialize_calls = [item for item in server.requests if item["method"] == "initialize"]
                list_calls = [item for item in server.requests if item["method"] == "tools/list"]
            assert len(initialize_calls) == 2
            assert len(list_calls) == 4
        finally:
            host.close()


def test_streamable_http_replays_only_once_when_rebuilt_session_also_expires():
    with fake_streamable_mcp_server() as (server, url):
        config = MCPServerConfig(
            name="remote",
            transport="http",
            url=url,
            headers={"Authorization": f"Bearer {server.secret}"},
        )
        host = MCPHost([config])
        try:
            host.connect("remote")
            server.expire_tool_calls = True
            echo_name = model_tool_name("remote", "echo")
            with pytest.raises(MCPConnectionError, match="重建后仍然失效"):
                host.call_tool(echo_name, {"text": "never loops"})
            with server.state_lock:
                initialize_count = sum(item["method"] == "initialize" for item in server.requests)
                tool_call_count = sum(item["method"] == "tools/call" for item in server.requests)
            assert initialize_count == 2
            assert tool_call_count == 2
            assert host.status()[0]["state"] == "failed"
        finally:
            host.close()


def test_streamable_http_concurrent_expiry_uses_one_session_rebuild():
    with fake_streamable_mcp_server() as (server, url):
        config = MCPServerConfig(
            name="remote",
            transport="http",
            url=url,
            headers={"Authorization": f"Bearer {server.secret}"},
        )
        host = MCPHost([config])
        try:
            host.connect("remote")
            echo_name = model_tool_name("remote", "echo")
            server.session_id = "concurrent-replacement"
            server.expired_barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(host.call_tool, echo_name, {"text": value})
                    for value in ("first", "second")
                ]
                assert {result_text(future.result(timeout=4.0)) for future in futures} == {"first", "second"}
            with server.state_lock:
                initialize_count = sum(item["method"] == "initialize" for item in server.requests)
            assert initialize_count == 2
        finally:
            host.close()


def test_streamable_http_tool_cache_rejects_response_from_stale_generation():
    config = MCPServerConfig(name="stale-tools", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    client._session_generation = 5
    client._http_client = httpx.Client()
    decode_started = threading.Event()
    release_decode = threading.Event()
    original_decode_tool = client._decode_tool

    def fake_post(message, *, deadline, expected_id, include_generation=False):
        assert message.get("method") == "tools/list"
        assert include_generation is True
        return ([{
            "jsonrpc": "2.0",
            "id": expected_id,
            "result": {
                "tools": [{"name": "stale", "inputSchema": {"type": "object"}}],
            },
        }], 5)

    def blocking_decode_tool(item):
        tool = original_decode_tool(item)
        decode_started.set()
        assert release_decode.wait(timeout=2.0)
        return tool

    client._post_message = fake_post
    client._decode_tool = blocking_decode_tool
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.list_tools, refresh=True)
            assert decode_started.wait(timeout=1.0)
            with client._state_lock:
                client._session_generation = 6
            release_decode.set()
            with pytest.raises(MCPConnectionError, match="会话在读取工具列表期间发生变化"):
                future.result(timeout=1.0)
        assert client._tools is None
    finally:
        release_decode.set()
        client.close()


def test_streamable_http_paginated_tool_list_cannot_mix_generations():
    config = MCPServerConfig(name="paged-tools", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    client._session_generation = 8
    client._http_client = httpx.Client()
    cursors: list[str | None] = []

    def fake_post(message, *, deadline, expected_id, include_generation=False):
        assert message.get("method") == "tools/list"
        assert include_generation is True
        cursor = message.get("params", {}).get("cursor")
        cursors.append(cursor)
        if cursor is None:
            return ([{
                "jsonrpc": "2.0",
                "id": expected_id,
                "result": {
                    "tools": [{"name": "page_one", "inputSchema": {"type": "object"}}],
                    "nextCursor": "page-2",
                },
            }], 8)
        with client._state_lock:
            client._session_generation = 9
        return ([{
            "jsonrpc": "2.0",
            "id": expected_id,
            "result": {
                "tools": [{"name": "page_two", "inputSchema": {"type": "object"}}],
            },
        }], 9)

    client._post_message = fake_post
    try:
        with pytest.raises(MCPConnectionError, match="工具列表跨越了不同 HTTP 会话"):
            client.list_tools(refresh=True)
        assert cursors == [None, "page-2"]
        assert client._tools is None
    finally:
        client.close()


def test_streamable_http_startup_messages_share_one_absolute_deadline():
    config = MCPServerConfig(
        name="startup-deadline",
        transport="http",
        url="https://example.test/mcp",
        startup_timeout=0.06,
        call_timeout=5.0,
    )
    client = mcp_module.MCPHTTPClient(config)
    client._http_client = httpx.Client()
    calls: list[tuple[str, float]] = []

    def fake_post(message, *, deadline, expected_id):
        method = str(message.get("method") or "")
        calls.append((method, deadline))
        if method == "initialize":
            return [{
                "jsonrpc": "2.0",
                "id": expected_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "startup-deadline", "version": "1"},
                },
            }]
        assert method == "notifications/initialized"
        while time.monotonic() < deadline:
            time.sleep(0.001)
        raise MCPTimeoutError("initialized reached startup deadline")

    client._post_message = fake_post
    started = time.monotonic()
    with pytest.raises(MCPTimeoutError, match="startup deadline"):
        client.start()

    assert time.monotonic() - started < 0.5
    assert [method for method, _ in calls] == ["initialize", "notifications/initialized"]
    assert calls[1][1] == calls[0][1]
    assert client.status()["state"] == "failed"


def test_streamable_http_recovery_lock_wait_respects_request_deadline():
    config = MCPServerConfig(name="locked", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    client._session_id = "session-current"
    client._session_generation = 4
    client._reconnect_lock.acquire()

    started = time.monotonic()
    try:
        with pytest.raises(MCPTimeoutError, match="总时限"):
            client._recover_http_session(
                "session-current",
                4,
                deadline=started + 0.05,
                operation="tools/call",
            )
    finally:
        client._reconnect_lock.release()

    assert time.monotonic() - started < 0.3
    assert client._session_generation == 4
    assert client._session_id == "session-current"
    assert client._state == "connected"


def test_streamable_http_late_session_headers_cannot_overwrite_current_generation():
    config = MCPServerConfig(name="headers", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    client._session_id = "session-current"
    client._session_generation = 3

    stale_response = httpx.Response(200, headers={"Mcp-Session-Id": "session-stale"})
    client._capture_session_id(stale_response, sent_generation=2)
    assert client._session_id == "session-current"

    client._state = "closed"
    client._session_id = ""
    closed_response = httpx.Response(200, headers={"Mcp-Session-Id": "session-after-close"})
    client._capture_session_id(closed_response, sent_generation=3)
    assert client._session_id == ""

    client._state = "starting"
    current_response = httpx.Response(200, headers={"Mcp-Session-Id": "session-new"})
    client._capture_session_id(current_response, sent_generation=3)
    assert client._session_id == "session-new"


def test_streamable_http_timeout_after_deadline_skips_cancel_notification():
    config = MCPServerConfig(name="expired", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    methods: list[str] = []

    def fake_post(message, *, deadline, expected_id):
        methods.append(str(message.get("method") or ""))
        while time.monotonic() < deadline:
            time.sleep(0.001)
        raise MCPTimeoutError("request deadline expired")

    client._post_message = fake_post
    with pytest.raises(MCPTimeoutError, match="deadline expired"):
        client._request("tools/call", {"name": "echo"}, timeout=0.03)

    assert methods == ["tools/call"]


def test_streamable_http_early_timeout_cancel_uses_original_deadline():
    config = MCPServerConfig(name="cancel", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    calls: list[tuple[str, float]] = []

    def fake_post(message, *, deadline, expected_id):
        method = str(message.get("method") or "")
        calls.append((method, deadline))
        if method == "tools/call":
            raise MCPTimeoutError("early transport timeout")
        assert method == "notifications/cancelled"
        return []

    client._post_message = fake_post
    with pytest.raises(MCPTimeoutError, match="early transport timeout"):
        client._request("tools/call", {"name": "echo"}, timeout=1.0)

    assert [method for method, _ in calls] == ["tools/call", "notifications/cancelled"]
    assert calls[1][1] == calls[0][1]


def test_streamable_http_recovery_and_replay_share_one_total_deadline():
    config = MCPServerConfig(name="deadline", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    client._session_id = "session-old"
    methods: list[str] = []
    deadlines: list[float] = []
    tool_calls = 0

    def fake_post(message, *, deadline, expected_id):
        nonlocal tool_calls
        method = str(message.get("method") or "")
        methods.append(method)
        if method == "notifications/cancelled":
            return []
        deadlines.append(deadline)
        if method == "tools/call":
            tool_calls += 1
            if tool_calls == 1:
                raise mcp_module._MCPHTTPSessionExpired("deadline", "session-old", 0)
            while time.monotonic() < deadline:
                time.sleep(0.002)
            raise MCPTimeoutError("replay reached the original total deadline")
        if method == "initialize":
            time.sleep(0.025)
            with client._state_lock:
                client._session_id = "session-new"
            return [{
                "jsonrpc": "2.0",
                "id": expected_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "deadline", "version": "1"},
                },
            }]
        if method == "notifications/initialized":
            time.sleep(0.025)
            return []
        raise AssertionError(method)

    client._post_message = fake_post
    started = time.monotonic()
    with pytest.raises(MCPTimeoutError, match="original total deadline"):
        client._request("tools/call", {"name": "echo"}, timeout=0.08)

    assert time.monotonic() - started < 0.3
    assert methods[:4] == ["tools/call", "initialize", "notifications/initialized", "tools/call"]
    assert len(set(deadlines)) == 1
    assert client._state == "connected"


def test_streamable_http_failed_initialized_never_exposes_half_rebuilt_session_to_waiters():
    config = MCPServerConfig(name="half", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    client._session_id = "session-old"
    expired_barrier = threading.Barrier(2)
    lock = threading.Lock()
    tool_calls = 0
    replay_calls = 0

    def fake_post(message, *, deadline, expected_id):
        nonlocal tool_calls, replay_calls
        method = str(message.get("method") or "")
        if method == "notifications/cancelled":
            return []
        if method == "tools/call":
            with lock:
                tool_calls += 1
                call_number = tool_calls
            if call_number <= 2:
                expired_barrier.wait(timeout=2.0)
                raise mcp_module._MCPHTTPSessionExpired("half", "session-old", 0)
            with lock:
                replay_calls += 1
            return [{"jsonrpc": "2.0", "id": expected_id, "result": {"unexpected": True}}]
        if method == "initialize":
            with client._state_lock:
                client._session_id = "session-half"
            return [{
                "jsonrpc": "2.0",
                "id": expected_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "half", "version": "1"},
                },
            }]
        if method == "notifications/initialized":
            raise MCPConnectionError("initialized failed deterministically")
        raise AssertionError(method)

    client._post_message = fake_post
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client._request, "tools/call", {"worker": index}, 1.0) for index in range(2)]
        for future in futures:
            with pytest.raises(MCPConnectionError):
                future.result(timeout=2.0)

    assert replay_calls == 0
    assert client._session_generation == 1
    assert client.status()["state"] == "failed"


def test_streamable_http_stale_second_404_cannot_fail_a_newer_connected_generation():
    config = MCPServerConfig(name="generation", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    client._session_id = "session-a"
    stale_replay_started = threading.Event()
    release_stale_replay = threading.Event()
    lock = threading.Lock()
    calls: dict[str, int] = {"stale": 0, "fresh": 0}
    initialize_count = 0

    def fake_post(message, *, deadline, expected_id):
        nonlocal initialize_count
        method = str(message.get("method") or "")
        if method == "notifications/cancelled":
            return []
        if method == "initialize":
            with lock:
                initialize_count += 1
                session_id = "session-b" if initialize_count == 1 else "session-c"
            with client._state_lock:
                client._session_id = session_id
            return [{
                "jsonrpc": "2.0",
                "id": expected_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "generation", "version": str(initialize_count)},
                },
            }]
        if method == "notifications/initialized":
            return []
        if method == "tools/call":
            label = str(message.get("params", {}).get("label"))
            with lock:
                calls[label] += 1
                call_number = calls[label]
            with client._state_lock:
                session_id = client._session_id
                generation = client._session_generation
            if call_number == 1:
                raise mcp_module._MCPHTTPSessionExpired("generation", session_id, generation)
            if label == "stale":
                stale_replay_started.set()
                assert release_stale_replay.wait(timeout=2.0)
                raise mcp_module._MCPHTTPSessionExpired("generation", "session-b", 1)
            return [{"jsonrpc": "2.0", "id": expected_id, "result": {"ok": True}}]
        raise AssertionError(method)

    client._post_message = fake_post
    with ThreadPoolExecutor(max_workers=1) as pool:
        stale = pool.submit(client._request, "tools/call", {"label": "stale"}, 2.0)
        assert stale_replay_started.wait(timeout=1.0)
        fresh = client._request("tools/call", {"label": "fresh"}, timeout=1.0)
        assert fresh == {"ok": True}
        release_stale_replay.set()
        with pytest.raises(MCPConnectionError, match="重建后仍然失效"):
            stale.result(timeout=1.0)

    assert client._session_generation == 2
    assert client._session_id == "session-c"
    assert client._state == "connected"


def test_streamable_http_second_404_cannot_overwrite_closed_state():
    config = MCPServerConfig(name="close-race", transport="http", url="https://example.test/mcp")
    client = mcp_module.MCPHTTPClient(config)
    client._state = "connected"
    client._session_id = "session-a"
    replay_started = threading.Event()
    release_replay = threading.Event()
    close_state_set = threading.Event()
    release_close = threading.Event()
    tool_calls = 0

    def fake_post(message, *, deadline, expected_id):
        nonlocal tool_calls
        method = str(message.get("method") or "")
        if method == "tools/call":
            tool_calls += 1
            if tool_calls == 1:
                raise mcp_module._MCPHTTPSessionExpired("close-race", "session-a", 0)
            replay_started.set()
            assert release_replay.wait(timeout=2.0)
            raise mcp_module._MCPHTTPSessionExpired("close-race", "session-b", 1)
        if method == "initialize":
            with client._state_lock:
                client._session_id = "session-b"
            return [{
                "jsonrpc": "2.0",
                "id": expected_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "close-race", "version": "1"},
                },
            }]
        if method == "notifications/initialized":
            return []
        raise AssertionError(method)

    original_fail_all = client._fail_all

    def blocking_fail_all(error):
        close_state_set.set()
        assert release_close.wait(timeout=2.0)
        original_fail_all(error)

    client._post_message = fake_post
    client._fail_all = blocking_fail_all
    with ThreadPoolExecutor(max_workers=2) as pool:
        request_future = pool.submit(client._request, "tools/call", {"name": "echo"}, 2.0)
        assert replay_started.wait(timeout=1.0)
        close_future = pool.submit(client.close)
        assert close_state_set.wait(timeout=1.0)
        try:
            release_replay.set()
            with pytest.raises(MCPConnectionError, match="重建后仍然失效"):
                request_future.result(timeout=1.0)
            assert client.status()["state"] == "closed"
        finally:
            release_replay.set()
            release_close.set()
        close_future.result(timeout=1.0)

    assert client.status()["state"] == "closed"


def test_streamable_http_close_during_rebuild_cannot_restore_connected_state():
    with fake_streamable_mcp_server() as (server, url):
        config = MCPServerConfig(
            name="remote",
            transport="http",
            url=url,
            headers={"Authorization": f"Bearer {server.secret}"},
        )
        host = MCPHost([config])
        host.connect("remote")
        client = host._clients["remote"]
        original_post_message = client._post_message
        rebuild_reached = threading.Event()
        release_rebuild = threading.Event()

        def blocking_post_message(message, *, deadline, expected_id):
            if message.get("method") == "notifications/initialized" and client._state == "starting":
                rebuild_reached.set()
                assert release_rebuild.wait(timeout=2.0)
            return original_post_message(message, deadline=deadline, expected_id=expected_id)

        client._post_message = blocking_post_message
        server.session_id = "close-race-replacement"
        echo_name = model_tool_name("remote", "echo")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(host.call_tool, echo_name, {"text": "close race"})
            assert rebuild_reached.wait(timeout=3.0)
            try:
                host.close()
            finally:
                release_rebuild.set()
            with pytest.raises(MCPConnectionError):
                future.result(timeout=3.0)

        assert client.status()["state"] == "closed"
        assert host.status()[0]["state"] == "closed"


def test_streamable_http_redacts_sensitive_headers_from_http_errors():
    with fake_streamable_mcp_server() as (server, url):
        query_secret = "query-secret-value-456"
        custom_secret = "custom-header-secret-789"
        config = MCPServerConfig(
            name="reject",
            transport="streamable_http",
            url=url + f"/reject?token={query_secret}",
            headers={
                "Authorization": f"Bearer {server.secret}",
                "X-Custom": custom_secret,
            },
        )
        host = MCPHost([config])
        try:
            with pytest.raises(MCPConnectionError) as captured:
                host.connect("reject")
            diagnostic = str(captured.value) + json.dumps(host.status(), ensure_ascii=False)
            assert server.secret not in diagnostic
            assert f"Bearer {server.secret}" not in diagnostic
            assert query_secret not in diagnostic
            assert custom_secret not in diagnostic
        finally:
            host.close()


def test_streamable_http_config_rejects_url_credentials_and_reserved_headers():
    with pytest.raises(ValueError, match="username/password"):
        MCPServerConfig(
            name="remote",
            transport="http",
            url="https://user:password@example.test/mcp",
        )
    for header in (
        "Host",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Accept",
        "Content-Type",
        "Mcp-Session-Id",
        "MCP-Protocol-Version",
    ):
        with pytest.raises(ValueError, match="运行时保留"):
            MCPServerConfig(
                name="remote",
                transport="http",
                url="https://example.test/mcp",
                headers={header: "attacker-controlled"},
            )
    with pytest.raises(ValueError, match="名称无效"):
        MCPServerConfig(
            name="remote",
            transport="http",
            url="https://example.test/mcp",
            headers={"Bad Header": "value"},
        )


def test_streamable_http_sse_heartbeat_cannot_extend_total_deadline():
    class HeartbeatStream(httpx.SyncByteStream):
        def __init__(self):
            self.closed = threading.Event()

        def __iter__(self):
            while not self.closed.is_set():
                time.sleep(0.01)
                yield b": keepalive\n\n"

        def close(self):
            self.closed.set()

    config = MCPServerConfig(
        name="heartbeat",
        transport="http",
        url="https://example.test/mcp",
        max_message_bytes=4096,
    )
    client = mcp_module.MCPHTTPClient(config)
    stream = HeartbeatStream()
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        stream=stream,
        request=httpx.Request("POST", config.url),
    )
    started = time.monotonic()
    try:
        with pytest.raises(MCPTimeoutError, match="总时限"):
            client._read_http_messages(
                response,
                expected_id=1,
                deadline=started + 0.08,
                operation="tools/call",
            )
    finally:
        response.close()

    assert time.monotonic() - started < 0.5
    assert stream.closed.is_set()


def test_streamable_http_sse_rejects_oversized_unterminated_line_before_buffering_more():
    class UnterminatedStream(httpx.SyncByteStream):
        def __init__(self):
            self.yielded = 0
            self.closed = False

        def __iter__(self):
            while self.yielded < 100:
                self.yielded += 1
                yield b"x" * 40

        def close(self):
            self.closed = True

    config = MCPServerConfig(
        name="oversized",
        transport="http",
        url="https://example.test/mcp",
        max_message_bytes=64,
    )
    client = mcp_module.MCPHTTPClient(config)
    stream = UnterminatedStream()
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        stream=stream,
        request=httpx.Request("POST", config.url),
    )
    try:
        with pytest.raises(MCPProtocolError, match="超过 64 字节"):
            client._read_http_messages(
                response,
                expected_id=1,
                deadline=time.monotonic() + 1.0,
                operation="tools/call",
            )
    finally:
        response.close()

    assert stream.yielded == 2
    assert stream.closed is True


def test_list_changed_notification_invalidates_tool_cache(tmp_path: Path):
    config, log_path = fake_config(tmp_path)
    with MCPClient(config) as client:
        client.list_tools()
        assert result_text(client.call_tool("announce_change")) == "changed"
        assert wait_until(lambda: client.tools == [])
        assert len(client.tool_definitions()) == 7
        list_calls = [item for item in read_log(log_path) if item.get("method") == "tools/list"]
        assert len(list_calls) == 4


def test_concurrent_pending_calls_timeout_without_poisoning_connection(tmp_path: Path):
    config, _ = fake_config(tmp_path, call_timeout=1.0)
    with MCPClient(config) as client:
        client.list_tools()
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(client.call_tool, "delay", {"value": "slow", "delay": 0.20})
            fast = pool.submit(client.call_tool, "delay", {"value": "fast", "delay": 0.02})
            assert result_text(fast.result(timeout=1.0)) == "fast"
            assert result_text(slow.result(timeout=1.0)) == "slow"

        with pytest.raises(MCPTimeoutError, match="tools/call"):
            client.call_tool("hang", timeout=0.08)
        assert client.status()["pendingCalls"] == 0
        assert result_text(client.call_tool("echo", {"text": "still alive"})) == "still alive"
        assert "超时" in (client.status()["lastError"] or "")


def test_remote_errors_and_status_redact_configured_secrets(tmp_path: Path):
    secret = "super-secret-value-123"
    config, _ = fake_config(tmp_path, secret=secret)
    with MCPClient(config) as client:
        client.list_tools()
        with pytest.raises(MCPRemoteError) as captured:
            client.call_tool("explode")
        assert secret not in str(captured.value)
        assert captured.value.data == {"api_key": "***"}
        assert wait_until(lambda: bool(client.status()["stderr"]))
        serialized = json.dumps(client.status(), ensure_ascii=False)
        assert secret not in serialized
        assert "***" in serialized

    text = 'Authorization: Bearer abcdefghijklmnop "api_key":"sk-abcdefghijk"'
    redacted = redact_sensitive(text)
    assert "abcdefghijklmnop" not in redacted
    assert "sk-abcdefghijk" not in redacted


def test_host_isolates_failed_servers_and_supports_reconnect(tmp_path: Path):
    good, _ = fake_config(tmp_path, name="good")
    bad = MCPServerConfig(name="broken", command=str(tmp_path / "does-not-exist.exe"), startup_timeout=0.2)
    host = MCPHost([good, bad])
    try:
        statuses = {item["name"]: item for item in host.connect_all()}
        assert statuses["good"]["state"] == "connected"
        assert statuses["broken"]["state"] == "failed"
        definitions = host.tool_definitions()
        assert definitions
        assert all(item["origin"]["server"] == "good" for item in definitions)
        echo_name = model_tool_name("good", "echo")
        assert result_text(host.call_tool(echo_name, {"text": "host"})) == "host"

        assert host.disconnect("good") is True
        assert {item["name"]: item["state"] for item in host.status()}["good"] == "disconnected"
        host.reconnect("good")
        assert result_text(host.call_tool(echo_name, {"text": "again"})) == "again"
    finally:
        host.close()
    assert all(item["state"] == "closed" for item in host.status())


def test_host_close_reclaims_client_that_fails_while_connecting(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    instances = []

    class FailingClient:
        def __init__(self, config):
            self.config = config
            instances.append(self)

        @property
        def name(self):
            return self.config.name

        def start(self):
            started.set()
            assert release.wait(timeout=2.0)
            raise MCPProtocolError("deterministic startup failure")

        def close(self):
            closed.set()

    monkeypatch.setattr(mcp_module, "MCPClient", FailingClient)
    host = MCPHost([MCPServerConfig(name="racing", command="unused")])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(host.connect, "racing")
        assert started.wait(timeout=2.0)
        try:
            host.close()
            assert closed.is_set()
        finally:
            release.set()
        with pytest.raises(MCPProtocolError, match="deterministic startup failure"):
            future.result(timeout=2.0)

    assert instances
    assert host._clients == {}
    assert host._connecting == {}
    assert host.status()[0]["state"] == "closed"


def test_stdio_close_during_initialize_cannot_restore_connected_or_failed_state():
    client = MCPClient(MCPServerConfig(name="stdio-close-race", command="unused"))
    initialize_started = threading.Event()
    release_initialize = threading.Event()
    shutdown_called = threading.Event()

    class AliveProcess:
        @staticmethod
        def poll():
            return None

    def fake_spawn():
        client._process = AliveProcess()

    def blocking_request(method, params, timeout, *, allow_starting=False):
        assert method == "initialize"
        assert allow_starting is True
        initialize_started.set()
        assert release_initialize.wait(timeout=2.0)
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "stdio-close-race", "version": "1"},
        }

    client._spawn = fake_spawn
    client._request = blocking_request
    client._notify = lambda method, params: None
    client._shutdown_process = shutdown_called.set

    with ThreadPoolExecutor(max_workers=1) as pool:
        start_future = pool.submit(client.start)
        assert initialize_started.wait(timeout=1.0)
        client.close()
        assert client.status()["state"] == "closed"
        release_initialize.set()
        with pytest.raises(MCPConnectionError, match="初始化期间关闭"):
            start_future.result(timeout=1.0)

    assert shutdown_called.is_set()
    assert client.status()["state"] == "closed"
    assert client.status()["lastError"] is None


def test_cyclic_tools_pagination_fails_with_protocol_diagnostic(tmp_path: Path):
    config, _ = fake_config(tmp_path, mode="cycle")
    with MCPClient(config) as client:
        with pytest.raises(MCPProtocolError, match="循环分页游标"):
            client.list_tools()
        assert "循环分页游标" in (client.status()["lastError"] or "")


def test_bad_initialize_is_rejected_and_process_is_closed(tmp_path: Path):
    config, _ = fake_config(tmp_path, mode="bad_init")
    client = MCPClient(config)
    with pytest.raises(MCPProtocolError, match="protocolVersion"):
        client.start()
    assert client.status()["state"] == "failed"
    process = client._process
    assert process is not None
    assert wait_until(lambda: process.poll() is not None)
    client.close()


def test_close_kills_fake_server_descendant_process(tmp_path: Path):
    config, _ = fake_config(tmp_path)
    client = MCPClient(config).start()
    try:
        client.list_tools()
        child_pid = int(result_text(client.call_tool("spawn_child")))
        assert wait_until(lambda: pid_exists(child_pid))
    finally:
        client.close()
    assert wait_until(lambda: not pid_exists(child_pid), timeout=5.0)


def test_mcp_subprocess_uses_hidden_launcher(tmp_path: Path, monkeypatch):
    config, _ = fake_config(tmp_path)
    calls = []
    real_popen_hidden = mcp_module.popen_hidden

    def tracking_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return real_popen_hidden(*args, **kwargs)

    monkeypatch.setattr(mcp_module, "popen_hidden", tracking_popen)
    with MCPClient(config):
        pass
    assert len(calls) == 1
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["stdin"] is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher compatibility")
def test_windows_batch_server_uses_hidden_comspec_wrapper(tmp_path: Path, monkeypatch):
    script = write_fake_server(tmp_path)
    launcher = tmp_path / "fake server.cmd"
    launcher.write_text('@echo off\n"%~1" "%~2"\n', encoding="ascii")
    config = MCPServerConfig(
        name="batch",
        command=str(launcher),
        args=(sys.executable, str(script)),
        env={"FAKE_LOG": str(tmp_path / "batch.jsonl")},
        startup_timeout=3.0,
        call_timeout=2.0,
    )
    calls = []
    real_popen_hidden = mcp_module.popen_hidden

    def tracking_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return real_popen_hidden(*args, **kwargs)

    monkeypatch.setattr(mcp_module, "popen_hidden", tracking_popen)
    with MCPClient(config) as client:
        assert result_text(client.call_tool("echo", {"text": "batch works"})) == "batch works"

    command = calls[0][0][0]
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    batch_line = subprocess.list2cmdline([str(launcher), sys.executable, str(script)])
    expected = subprocess.list2cmdline([comspec, "/d", "/s", "/c"]) + f' "{batch_line}"'
    assert command == expected
    assert calls[0][1]["shell"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows PATH batch launcher compatibility")
def test_windows_bare_command_resolves_npx_style_cmd_launcher(tmp_path: Path, monkeypatch):
    script = write_fake_server(tmp_path)
    launcher = tmp_path / "fake-npx.cmd"
    launcher.write_text('@echo off\n"%~1" "%~2"\n', encoding="ascii")
    path = str(tmp_path) + os.pathsep + os.environ.get("PATH", "")
    config = MCPServerConfig(
        name="bare-batch",
        command="fake-npx",
        args=(sys.executable, str(script)),
        env={"FAKE_LOG": str(tmp_path / "bare-batch.jsonl"), "PATH": path},
        startup_timeout=3.0,
        call_timeout=2.0,
    )
    calls = []
    real_popen_hidden = mcp_module.popen_hidden

    def tracking_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return real_popen_hidden(*args, **kwargs)

    monkeypatch.setattr(mcp_module, "popen_hidden", tracking_popen)
    with MCPClient(config) as client:
        assert result_text(client.call_tool("echo", {"text": "bare batch works"})) == "bare batch works"

    command = calls[0][0][0]
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    resolved = shutil.which("fake-npx", path=path)
    assert resolved is not None
    batch_line = subprocess.list2cmdline([resolved, sys.executable, str(script)])
    expected = subprocess.list2cmdline([comspec, "/d", "/s", "/c"]) + f' "{batch_line}"'
    assert command == expected
    assert calls[0][1]["shell"] is False
