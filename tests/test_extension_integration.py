from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import threading

import pytest

from deepseekfathom._core.agent import FathomAgent, compact_context_messages, filter_internal_automation_messages, parse_tool_call
from deepseekfathom._core.config import Settings
from deepseekfathom._core.desktop.app import DesktopApi, mcp_result_to_tool_result, serialize_messages
from deepseekfathom._core.extensions import UserMCPConfigError, get_user_mcp_server
from deepseekfathom._core.hooks import HookConfig, HookOutcome, HookRunner, POST_LLM_CALL, POST_TOOL_USE, PRE_TOOL_USE, SESSION_START
from deepseekfathom._core.messages import Message
from deepseekfathom._core.policy import ThinkingMode
from deepseekfathom._core.provider import NativeToolCall, openai_tool_definitions
from deepseekfathom._core.session import Session
from deepseekfathom._core.tool_contracts import ToolContract


def settings(workspace: Path) -> Settings:
    return Settings(
        api_key="test",
        base_url="https://example.invalid",
        model="test-model",
        workspace=workspace,
        max_tool_rounds=8,
        max_tokens=1000,
        request_timeout=10,
        default_mode="root",
        default_thinking="fast",
    )


def test_dynamic_contract_reaches_native_provider_and_executes(tmp_path: Path):
    name = "mcp__files__lookup"
    calls: list[dict] = []

    class Client:
        supports_native_tools = True

        def __init__(self):
            self.last_tool_calls = []
            self.runtime_tool_contracts = {}
            self.round = 0

        def chat(self, _messages, *, tool_names=None):
            self.round += 1
            assert name in set(tool_names or ())
            assert name in self.runtime_tool_contracts
            if self.round == 1:
                self.last_tool_calls = [NativeToolCall(name, {"query": "README"}, "call-1")]
                return ""
            self.last_tool_calls = []
            return "MCP 查询完成。"

    contract = ToolContract(
        name=name,
        description="Search files through MCP",
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=lambda arguments: calls.append(arguments) or {"ok": True, "output": "README.md"},
        origin="mcp:files",
    )
    client = Client()
    result = FathomAgent(
        settings(tmp_path),
        mode="root",
        client=client,
        extra_tool_contracts=[contract],
    ).run("查找 README", require_todo=False)

    assert result.answer == "MCP 查询完成。"
    assert calls == [{"query": "README"}]


def test_dynamic_mcp_name_works_in_text_fallback(tmp_path: Path):
    name = "mcp__clock__now"

    class Client:
        supports_native_tools = False
        last_tool_calls = []

        def __init__(self):
            self.round = 0

        def chat(self, _messages):
            self.round += 1
            if self.round == 1:
                return '{"tool":"mcp__clock__now","arguments":{}}'
            return "现在是测试时间。"

    result = FathomAgent(
        settings(tmp_path),
        mode="root",
        client=Client(),
        extra_tool_contracts=[ToolContract(
            name=name,
            description="Read time",
            schema={"type": "object"},
            handler=lambda _arguments: "12:34",
            origin="mcp:clock",
            read_only=True,
        )],
    ).run("几点了", require_todo=False)

    assert result.answer == "现在是测试时间。"
    assert parse_tool_call('{"tool":"mcp__clock__now","arguments":{}}') == (name, {})


def test_provider_definitions_prefer_runtime_contract():
    name = "mcp__demo__echo"
    contract = ToolContract(
        name=name,
        description="Dynamic echo",
        schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )

    definitions = openai_tool_definitions(["read_file", name], {name: contract})

    dynamic = next(item for item in definitions if item["function"]["name"] == name)
    assert dynamic["function"]["description"] == "Dynamic echo"
    assert dynamic["function"]["parameters"]["properties"]["text"]["type"] == "string"


def test_untrusted_extension_tool_is_hidden_in_plan_mode(tmp_path: Path):
    agent = FathomAgent(
        settings(tmp_path),
        mode="plan",
        client=type("Client", (), {"supports_native_tools": True, "runtime_tool_contracts": {}})(),
        extra_tool_contracts=[ToolContract(
            name="mcp__demo__read",
            description="Untrusted read hint",
            schema={"type": "object"},
            handler=lambda _arguments: "ok",
            origin="mcp:demo",
            read_only=True,
            trusted_read_only=False,
        )],
    )

    assert "mcp__demo__read" not in agent._native_tool_names()


def test_untrusted_extension_text_call_cannot_bypass_plan_or_review_policy(tmp_path: Path):
    name = "mcp__demo__write"
    calls: list[dict] = []

    class Client:
        supports_native_tools = False
        last_tool_calls = []

        def __init__(self):
            self.round = 0

        def chat(self, _messages):
            self.round += 1
            if self.round == 1:
                return '{"tool":"mcp__demo__write","arguments":{"value":1}}'
            return "Blocked: this MCP tool is disabled in the current mode."

    contract = ToolContract(
        name=name,
        description="Untrusted write tool",
        schema={"type": "object"},
        handler=lambda arguments: calls.append(arguments) or "changed",
        origin="mcp:demo",
        read_only=False,
        trusted_read_only=False,
    )
    for mode in ("plan", "review"):
        result = FathomAgent(
            settings(tmp_path),
            mode=mode,
            client=Client(),
            approve=lambda _name, _arguments: True,
            extra_tool_contracts=[contract],
        ).run("只检查，不要修改", require_todo=False)

        assert result.answer.startswith("Blocked:")
    assert calls == []


def test_untrusted_extension_native_call_cannot_bypass_execution_policy(tmp_path: Path):
    name = "mcp__demo__write"
    calls: list[dict] = []

    class Client:
        supports_native_tools = True

        def __init__(self):
            self.last_tool_calls = []
            self.runtime_tool_contracts = {}
            self.round = 0

        def chat(self, _messages, *, tool_names=None):
            self.round += 1
            assert name not in set(tool_names or ())
            if self.round == 1:
                self.last_tool_calls = [NativeToolCall(name, {"value": 1}, "call-unsafe")]
                return ""
            self.last_tool_calls = []
            return "Blocked: this MCP tool is disabled in the current mode."

    result = FathomAgent(
        settings(tmp_path),
        mode="plan",
        client=Client(),
        extra_tool_contracts=[ToolContract(
            name=name,
            description="Untrusted write tool",
            schema={"type": "object"},
            handler=lambda arguments: calls.append(arguments) or "changed",
            origin="mcp:demo",
            read_only=False,
            trusted_read_only=False,
        )],
    ).run("只检查，不要修改", require_todo=False)

    assert result.answer.startswith("Blocked:")
    assert calls == []


def test_session_start_hook_context_is_temporary_system_context(tmp_path: Path):
    context = tmp_path / "startup.txt"
    context.write_text("temporary-hook-context-42", encoding="utf-8")
    runner = HookRunner([HookConfig(SESSION_START, context_file=context)], tmp_path)
    seen: list[list[Message]] = []

    class Client:
        supports_native_tools = False
        last_tool_calls = []

        def chat(self, messages):
            seen.append(list(messages))
            return "完成。"

    session = Session(tmp_path)
    FathomAgent(settings(tmp_path), mode="root", client=Client(), hook_runner=runner).run(
        "开始",
        session=session,
        require_todo=False,
    )

    assert any(
        message.role == "system" and "temporary-hook-context-42" in message.content
        for message in seen[0]
    )
    assert all("temporary-hook-context-42" not in message.content for message in session.messages)
    assert [message.content for message in session.messages if message.role == "user"] == ["开始"]


def test_post_llm_hook_stdout_cannot_replace_the_assistant_answer(tmp_path: Path):
    def spawn(hook, _stdin):
        return HookOutcome(hook, "pass", 0, stdout="audit log from hook")

    runner = HookRunner(
        [HookConfig(POST_LLM_CALL, "audit", description="observer")],
        tmp_path,
        spawner=spawn,
    )

    class Client:
        supports_native_tools = False
        last_tool_calls = []

        def chat(self, _messages):
            return "真实模型回答。"

    session = Session(tmp_path, persist=False)
    result = FathomAgent(
        settings(tmp_path),
        mode="root",
        client=Client(),
        hook_runner=runner,
    ).run("请回答", session=session, require_todo=False)

    assert result.answer == "真实模型回答。"
    assert session.messages[-1].content == "真实模型回答。"


def test_pre_and_post_hooks_do_not_run_when_confirmation_is_denied(tmp_path: Path):
    called: list[str] = []

    def spawn(hook, _stdin):
        called.append(hook.event)
        return HookOutcome(hook, "pass", 0)

    runner = HookRunner([
        HookConfig(PRE_TOOL_USE, "pre"),
        HookConfig(POST_TOOL_USE, "post"),
    ], tmp_path, spawner=spawn)

    class Client:
        supports_native_tools = True

        def __init__(self):
            self.last_tool_calls = []
            self.runtime_tool_contracts = {}
            self.round = 0

        def chat(self, _messages, *, tool_names=None):
            self.round += 1
            if self.round == 1:
                self.last_tool_calls = [NativeToolCall("write_file", {"path": "x.txt", "content": "x"})]
                return ""
            self.last_tool_calls = []
            return "Blocked: confirmation was denied."

    FathomAgent(
        settings(tmp_path),
        mode="agent",
        client=Client(),
        approve=lambda _name, _arguments: False,
        hook_runner=runner,
    ).run("写文件", require_todo=False)

    assert called == []
    assert not (tmp_path / "x.txt").exists()


def test_mcp_result_converts_text_and_bounded_image():
    encoded = "iVBORw0KGgo="
    result = mcp_result_to_tool_result({
        "content": [
            {"type": "text", "text": "done"},
            {"type": "image", "mimeType": "image/png", "data": encoded},
        ],
        "isError": False,
    })

    assert result.ok is True
    assert result.output == "done"
    assert result.images == [f"data:image/png;base64,{encoded}"]


def test_desktop_send_is_idempotent_by_client_request_id(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path / "workspace"))
    api = DesktopApi()
    calls: list[str] = []

    def start(prompt, **_kwargs):
        calls.append(prompt)
        return {"ok": True, "sessionId": "s1", "turnId": "t1"}

    monkeypatch.setattr(api, "_start_turn", start)
    payload = {"prompt": "只执行一次", "clientRequestId": "request-1"}

    first = api.send(payload)
    second = api.send(payload)

    assert first == {"ok": True, "sessionId": "s1", "turnId": "t1"}
    assert second == {**first, "duplicate": True}
    assert calls == ["只执行一次"]
    api._shutdown_extensions()


def test_desktop_send_forwards_presentation_metadata(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path / "workspace"))
    api = DesktopApi()
    captured: dict[str, object] = {}

    def start(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return {"ok": True, "sessionId": "s1", "turnId": "t1"}

    monkeypatch.setattr(api, "_start_turn", start)
    result = api.send({
        "prompt": "Use skill review: inspect this",
        "displayPrompt": "/skill review inspect this",
        "uiKind": "command",
    })

    assert result["ok"] is True
    assert captured["prompt"] == "Use skill review: inspect this"
    assert captured["display_prompt"] == "/skill review inspect this"
    assert captured["ui_kind"] == "command"
    api._shutdown_extensions()


def test_local_slash_command_is_persisted_once_and_hidden_from_model(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path / "workspace"))
    api = DesktopApi()
    payload = {
        "command": "/mcp",
        "uiKind": "command",
        "modelVisible": False,
        "clientRequestId": "slash-once",
    }

    first = api.record_slash_command(payload)
    second = api.record_slash_command(payload)

    assert first["ok"] is True
    assert second == {**first, "duplicate": True}
    assert api.session is not None
    assert len(api.session.messages) == 1
    message = api.session.messages[0]
    assert message.content == "/mcp"
    assert message.display_content == "/mcp"
    assert message.ui_kind == "command"
    assert message.model_visible is False
    assert serialize_messages([message]) == [{
        "role": "user",
        "content": "/mcp",
        "srcIndex": 0,
        "modelVisible": False,
        "uiKind": "command",
        "displayContent": "/mcp",
    }]
    api._shutdown_extensions()


def test_desktop_exposes_max_after_xhigh_and_max_is_stronger(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path / "workspace"))
    api = DesktopApi()
    boot = api.boot()

    assert boot["thinkingModes"][-2:] == ["ultra", "max"]
    assert boot["thinkingLabels"]["ultra"] == "XHigh"
    assert boot["thinkingLabels"]["max"] == "Max"
    assert ThinkingMode.resolve("max").reasoning_effort == "max"
    assert ThinkingMode.resolve("max").deliberation_passes > ThinkingMode.resolve("ultra").deliberation_passes
    api._shutdown_extensions()


def test_mcp_management_contracts_are_real_redacted_and_deferred(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path / "workspace"))
    api = DesktopApi()
    contracts = {contract.name: contract for contract in api._mcp_management_contracts()}

    assert set(contracts) == {"list_mcp_servers", "configure_mcp_server"}
    assert contracts["list_mcp_servers"].read_only is True
    assert contracts["configure_mcp_server"].always_confirm is True
    secret = "Bearer do-not-display"
    saved = contracts["configure_mcp_server"].handler({
        "name": "docs",
        "transport": "http",
        "url": "https://example.test/mcp",
        "headers": {"Authorization": secret},
    })

    assert saved is not None and saved.ok is True
    assert secret not in saved.output
    assert "Authorization" in saved.output
    assert api._pending_mcp_refresh is True
    assert get_user_mcp_server("docs", api._extensions.home)["headers"]["Authorization"] == secret
    api._shutdown_extensions()


def test_mcp_configure_does_not_treat_a_corrupt_config_as_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path / "workspace"))
    api = DesktopApi()
    config_path = api._extensions.home / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(UserMCPConfigError, match="不是有效 JSON"):
        api._configure_mcp_server_tool({
            "name": "docs",
            "transport": "http",
            "url": "https://example.test/mcp",
        })
    api._shutdown_extensions()


def test_always_confirm_contract_is_not_bypassed_in_root_mode(tmp_path: Path):
    calls: list[dict] = []

    class Client:
        supports_native_tools = True

        def __init__(self):
            self.last_tool_calls = []
            self.runtime_tool_contracts = {}
            self.round = 0

        def chat(self, _messages, *, tool_names=None):
            self.round += 1
            if self.round == 1:
                self.last_tool_calls = [NativeToolCall("configure_mcp_server", {"name": "blocked"})]
                return ""
            self.last_tool_calls = []
            return "Configuration was not approved."

    contract = ToolContract(
        name="configure_mcp_server",
        description="Persist MCP configuration",
        schema={"type": "object", "properties": {"name": {"type": "string"}}},
        handler=lambda arguments: calls.append(arguments) or "saved",
        origin="native:mcp-manager",
        always_confirm=True,
    )
    result = FathomAgent(
        settings(tmp_path),
        mode="root",
        client=Client(),
        approve=lambda _name, _arguments: False,
        extra_tool_contracts=[contract],
    ).run("配置 MCP", require_todo=False)

    assert calls == []
    assert "not approved" in result.answer


def test_desktop_turn_start_claim_is_atomic(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path / "workspace"))
    api = DesktopApi()
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()

    def hold_worker(*_args):
        started.set()
        release.wait(timeout=2)
        finished.set()

    monkeypatch.setattr(api, "_run_agent_turn", hold_worker)
    barrier = threading.Barrier(2)

    def start(label: str):
        barrier.wait(timeout=2)
        return api._start_turn(label)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, ["first", "second"]))
        assert started.wait(timeout=2)
        assert sum(result.get("ok") is True for result in results) == 1
        rejected = next(result for result in results if result.get("ok") is not True)
        assert rejected["error"] == "turn already running"
    finally:
        release.set()
        finished.wait(timeout=2)
        with api._turn_state_lock:
            api._running = False
        api._shutdown_extensions()


def test_extension_actions_are_rejected_before_mutation_while_turn_runs(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path / "workspace"))
    api = DesktopApi()
    called: list[str] = []
    monkeypatch.setattr(api._mcp_host, "connect_all", lambda: called.append("connect"))
    with api._turn_state_lock:
        api._running = True

    try:
        result = api.extension_action({"kind": "mcp", "action": "connect_all"})
        assert result == {"ok": False, "error": "回复生成期间不能修改扩展"}
        assert called == []
    finally:
        with api._turn_state_lock:
            api._running = False
        api._shutdown_extensions()


def test_turn_cannot_start_while_extension_mutation_is_in_progress(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path / "workspace"))
    api = DesktopApi()
    entered = threading.Event()
    release = threading.Event()

    def connect_all():
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(api._mcp_host, "connect_all", connect_all)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            action = pool.submit(api.extension_action, {"kind": "mcp", "action": "connect_all"})
            assert entered.wait(timeout=2)
            assert api._start_turn("must wait") == {"ok": False, "error": "extensions are updating"}
            release.set()
            assert action.result(timeout=2)["ok"] is True
    finally:
        release.set()
        api._shutdown_extensions()


def test_desktop_can_trust_project_mcp_and_toggle_one_hook_by_id(monkeypatch, tmp_path: Path):
    home = tmp_path / "config"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(home))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(workspace))
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"local": {"command": "local-server"}}}),
        encoding="utf-8",
    )
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [
            {"command": "first", "match": "*"},
            {"command": "second", "match": "*"},
        ]}}),
        encoding="utf-8",
    )
    api = DesktopApi()

    try:
        status = api.extension_status()
        assert status["mcp"]["projectTrusted"] is False
        trusted = api.extension_action({"kind": "mcp", "action": "trust_project"})
        assert trusted["mcp"]["projectTrusted"] is True

        hook_id = api.extension_status()["hooks"]["entries"][1]["id"]
        changed = api.extension_action({
            "kind": "hooks",
            "name": hook_id,
            "action": "disable",
            "enabled": False,
        })
        assert changed["hooks"]["entries"][0]["enabled"] is True
        assert changed["hooks"]["entries"][1]["enabled"] is False
    finally:
        api._shutdown_extensions()


def test_desktop_user_mcp_crud_is_scoped_refreshes_and_keeps_headers_private(monkeypatch, tmp_path: Path):
    home = tmp_path / "config"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home.mkdir()
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(home))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(workspace))
    (home / "config.json").write_text(
        json.dumps({"api_key": "keep-key", "mcpServers": {"keep": {"command": "keep-server"}}}),
        encoding="utf-8",
    )
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"project-only": {"command": "project-server"}}}),
        encoding="utf-8",
    )
    api = DesktopApi()
    monkeypatch.setattr(api, "_start_mcp_background", lambda: None)
    secret = "Bearer local-editor-secret"

    try:
        saved = api.extension_action({
            "kind": "mcp",
            "action": "save",
            "name": "remote",
            "originalName": "",
            "config": {
                "type": "http",
                "url": "https://mcp.example.test/rpc",
                "headers": {"Authorization": secret},
            },
        })
        assert saved["ok"] is True
        assert saved["name"] == "remote"
        assert secret not in json.dumps(saved, ensure_ascii=False)
        remote_status = next(item for item in saved["extensions"]["mcp"]["entries"] if item["name"] == "remote")
        assert remote_status["state"] == "configured"
        assert remote_status["connected"] is False
        assert "尚未启用 HTTP" not in json.dumps(remote_status, ensure_ascii=False)
        on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert on_disk["api_key"] == "keep-key"
        assert "keep" in on_disk["mcpServers"]

        fetched = api.extension_action({"kind": "mcp", "action": "get", "name": "remote"})
        assert fetched["ok"] is True
        assert fetched["server"]["headers"]["Authorization"] == secret

        renamed_server = dict(fetched["server"])
        renamed_server.pop("name", None)
        renamed = api.extension_action({
            "kind": "mcp",
            "action": "save",
            "name": "remote-renamed",
            "originalName": "remote",
            "config": renamed_server,
        })
        assert renamed["ok"] is True
        on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert "remote" not in on_disk["mcpServers"]
        assert "remote-renamed" in on_disk["mcpServers"]

        project_delete = api.extension_action({"kind": "mcp", "action": "delete", "name": "project-only"})
        assert project_delete["ok"] is False
        assert "project-only" in (workspace / ".mcp.json").read_text(encoding="utf-8")

        deleted = api.extension_action({"kind": "mcp", "action": "delete", "name": "remote-renamed"})
        assert deleted["ok"] is True
        on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert set(on_disk["mcpServers"]) == {"keep"}
    finally:
        api._shutdown_extensions()


def test_desktop_user_mcp_save_reports_success_when_hot_refresh_fails(monkeypatch, tmp_path: Path):
    home = tmp_path / "config"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(home))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(workspace))
    api = DesktopApi()
    monkeypatch.setattr(api, "_refresh_extensions_when_idle", lambda: {"ok": False, "error": "refresh failed"})
    monkeypatch.setattr(api, "extension_status", lambda: {"mcp": {"entries": []}})

    try:
        result = api.extension_action({
            "kind": "mcp",
            "action": "save",
            "server": {"name": "saved", "transport": "stdio", "command": "saved-server"},
        })

        assert result["ok"] is True
        assert result["name"] == "saved"
        assert "重启应用后生效" in result["warning"]
        on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert on_disk["mcpServers"]["saved"]["command"] == "saved-server"
    finally:
        api._shutdown_extensions()


def test_project_hook_toggle_never_implicitly_trusts_other_hooks(monkeypatch, tmp_path: Path):
    home = tmp_path / "config"
    workspace = tmp_path / "workspace"
    project_settings = workspace / ".deepseekfathom" / "settings.json"
    project_settings.parent.mkdir(parents=True)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(home))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(workspace))
    project_settings.write_text(
        json.dumps({"hooks": {"Stop": [
            {"command": "first", "match": "*"},
            {"command": "second", "match": "*"},
        ]}}),
        encoding="utf-8",
    )
    api = DesktopApi()

    try:
        status = api.extension_status()
        assert status["hooks"]["projectDefined"] is True
        assert status["hooks"]["projectTrusted"] is False
        assert [item["enabled"] for item in status["hooks"]["entries"]] == [False, False]

        blocked = api.extension_action({
            "kind": "hooks",
            "name": status["hooks"]["entries"][0]["id"],
            "action": "enable",
            "enabled": True,
        })
        assert blocked == {"ok": False, "error": "请先明确授权当前项目的 Hooks，再启用单条 Hook"}
        assert api.extension_status()["hooks"]["projectTrusted"] is False

        trusted = api.extension_action({"kind": "hooks", "action": "trust_project"})
        assert trusted["hooks"]["projectTrusted"] is True
        assert [item["enabled"] for item in trusted["hooks"]["entries"]] == [True, True]
    finally:
        api._shutdown_extensions()


def test_legacy_adjacent_assistant_recovery_is_not_reused_as_context():
    messages = [
        Message("user", "检查本机"),
        Message("assistant", "本机检查完成。"),
        Message("assistant", "旧版内部恢复又回答了一次。"),
        Message("user", "下一步"),
    ]

    filtered = filter_internal_automation_messages(messages)

    assert [message.content for message in filtered] == ["检查本机", "本机检查完成。", "下一步"]


def test_force_session_start_hook_runs_for_resumed_conversation(tmp_path: Path):
    context = tmp_path / "resume.txt"
    context.write_text("resume-context-only", encoding="utf-8")
    runner = HookRunner([HookConfig(SESSION_START, context_file=context)], tmp_path)
    observed: list[list[Message]] = []

    class Client:
        supports_native_tools = False
        last_tool_calls = []

        def chat(self, messages):
            observed.append(list(messages))
            return "继续完成。"

    session = Session(
        tmp_path,
        messages=[Message("system", "old"), Message("user", "旧问题"), Message("assistant", "旧回答")],
        persist=False,
    )
    FathomAgent(
        settings(tmp_path),
        mode="root",
        client=Client(),
        hook_runner=runner,
        force_session_start_hook=True,
    ).run("继续", session=session, require_todo=False)

    assert any("resume-context-only" in message.content for message in observed[0])
    assert all("resume-context-only" not in message.content for message in session.messages)


def test_precompact_callback_runs_only_when_compaction_is_real():
    messages = [Message("system", "system")]
    for index in range(10):
        messages.extend([
            Message("user", f"question {index} " + "x" * 500),
            Message("assistant", f"answer {index} " + "y" * 500),
        ])
    payloads: list[dict] = []

    compacted = compact_context_messages(
        messages,
        "tiny",
        force=True,
        pre_compact=lambda payload: payloads.append(payload) or "Keep file paths.",
    )

    assert compacted != messages
    assert len(payloads) == 1
    assert payloads[0]["messageCount"] == len(messages)
