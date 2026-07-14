from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from deepseekfathom._core import cli


class FakeExtensionCatalog:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.home = self.workspace / "home"
        self.skill_roots = (self.workspace / "plugin-skills",)
        self.instruction_files = (self.workspace / "PLUGIN.md",)
        self.mcp_specs = ()
        self.report = SimpleNamespace(
            plugins=(),
            hooks=SimpleNamespace(hooks=(), project_trusted=False),
        )
        self.runner = object()

    def active_mcp_configs(self):
        return []

    def diagnostics(self):
        return {
            "summary": {},
            "mcp": {"entries": []},
            "plugins": {"entries": []},
            "hooks": {"entries": []},
        }

    def new_hook_runner(self):
        return self.runner

    def refresh(self):
        return SimpleNamespace(to_dict=lambda: self.diagnostics())


class FakeMCPHost:
    instances: list["FakeMCPHost"] = []

    def __init__(self, _configs):
        self.connected = False
        self.connect_all_calls = 0
        self.connect_calls: list[str] = []
        self.closed = False
        self.__class__.instances.append(self)

    def connect_all(self):
        self.connect_all_calls += 1
        self.connected = True
        return self.status()

    def connect(self, name):
        self.connect_calls.append(name)
        self.connected = True
        return []

    def disconnect(self, _name):
        self.connected = False

    def reconnect(self, name):
        self.connect(name)

    def status(self):
        if not self.connected:
            return []
        return [{"name": "demo", "connected": True, "state": "connected", "toolCount": 1}]

    def tool_definitions(self):
        if not self.connected:
            return []
        return [{
            "name": "mcp__demo__read",
            "description": "read from demo",
            "schema": {"type": "object", "properties": {}},
            "origin": {"server": "demo", "tool": "read"},
            "read_only": True,
        }]

    def call_tool(self, _name, _arguments):
        return {"content": [{"type": "text", "text": "ok"}]}

    def close(self):
        self.closed = True


@pytest.fixture
def fake_runtime(monkeypatch, tmp_path):
    FakeMCPHost.instances.clear()
    monkeypatch.setattr(cli, "ExtensionRuntime", FakeExtensionCatalog)
    monkeypatch.setattr(cli, "MCPHost", FakeMCPHost)
    runtime = cli.CliExtensionRuntime(tmp_path)
    yield runtime
    runtime.close()


def test_cli_extension_runtime_is_lazy_and_tools_appear_after_connect(fake_runtime):
    host = fake_runtime.host
    assert host.connect_all_calls == 0
    initial = fake_runtime.agent_kwargs()
    assert {item.name for item in initial["extra_tool_contracts"]} == {
        "configure_mcp_server",
        "list_mcp_servers",
    }

    fake_runtime.connect_mcp("connect_all")
    connected = fake_runtime.agent_kwargs()
    contracts = {item.name: item for item in connected["extra_tool_contracts"]}
    assert "mcp__demo__read" in contracts
    assert connected["extra_skill_roots"] == fake_runtime.extensions.skill_roots
    assert connected["extra_instruction_files"] == fake_runtime.extensions.instruction_files
    assert connected["hook_runner"] is fake_runtime.extensions.runner
    assert contracts["mcp__demo__read"].handler({}).output == "ok"


def test_bare_mcp_connects_and_explicit_list_does_not(capsys):
    calls: list[tuple[str, str]] = []
    runtime = SimpleNamespace(
        connect_mcp=lambda action, name="": calls.append((action, name)),
        diagnostics=lambda: {"mcp": {"entries": []}},
    )
    settings = SimpleNamespace(workspace=Path.cwd())
    assert cli.handle_extension_prompt("/mcp", runtime, settings)
    assert calls == [("connect_all", "")]
    calls.clear()
    assert cli.handle_extension_prompt("/mcp list", runtime, settings)
    assert calls == []
    assert "no configured servers" in capsys.readouterr().out


def test_cli_mcp_subcommand_defaults_to_connect_all(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "install_terminal_safety", lambda: None)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(workspace=tmp_path))
    monkeypatch.setattr(cli, "extensions_command", lambda _settings, args: seen.setdefault("action", args.action) and 0)
    assert cli.main(["mcp"]) == 0
    assert seen["action"] == "connect-all"


def test_refresh_restores_connected_mcp_without_blocking(fake_runtime):
    fake_runtime.connect_mcp("connect_all")
    previous = fake_runtime.host
    fake_runtime.refresh()
    thread = fake_runtime._mcp_connect_thread
    assert thread is not None
    thread.join(timeout=1)
    assert previous.closed
    assert fake_runtime.host.connect_calls == ["demo"]


def test_hook_toggle_uses_stable_id_and_enforces_ownership(monkeypatch, fake_runtime):
    captured = {}
    monkeypatch.setattr(cli, "set_hook_enabled", lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs))
    fake_runtime.refresh = lambda **_kwargs: {}

    global_hook = SimpleNamespace(
        hook_id="global-123",
        scope="global",
        source=fake_runtime.extensions.home / "settings.json",
        event="PreToolUse",
        match="read_file",
    )
    fake_runtime.extensions.report.hooks = SimpleNamespace(hooks=(global_hook,), project_trusted=False)
    fake_runtime.set_hook("global-123", False)
    assert captured["kwargs"]["hook_id"] == "global-123"
    assert captured["args"][3] is False

    plugin_hook = SimpleNamespace(**{**global_hook.__dict__, "hook_id": "plugin-1", "scope": "plugin"})
    fake_runtime.extensions.report.hooks = SimpleNamespace(hooks=(plugin_hook,), project_trusted=True)
    with pytest.raises(ValueError, match="plugin hooks"):
        fake_runtime.set_hook("plugin-1", False)

    project_hook = SimpleNamespace(**{**global_hook.__dict__, "hook_id": "project-1", "scope": "project"})
    fake_runtime.extensions.report.hooks = SimpleNamespace(hooks=(project_hook,), project_trusted=False)
    with pytest.raises(ValueError, match="trust"):
        fake_runtime.set_hook("project-1", True)


def test_mcp_confirmation_redacts_secret_values(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    arguments = {
        "name": "demo",
        "transport": "http",
        "url": "https://user:password@example.test/mcp?token=url-secret",
        "headers": {"Authorization": "Bearer header-secret"},
        "env": {"API_TOKEN": "env-secret"},
        "args": ["--token", "argument-secret"],
    }
    assert cli.confirm_mcp_configuration(arguments)
    output = capsys.readouterr().out
    assert "example.test" in output
    assert "Authorization" in output
    assert "API_TOKEN" in output
    for secret in ("password", "url-secret", "header-secret", "env-secret", "argument-secret"):
        assert secret not in output


def test_headless_auto_approval_still_denies_mcp_persistence(monkeypatch):
    monkeypatch.setattr(cli, "confirm_mcp_configuration", lambda _arguments: pytest.fail("must not prompt"))
    approver = cli.cli_approver(auto_approve=True, allow_mandatory_prompt=False)
    assert approver("read_file", {})
    assert not approver("configure_mcp_server", {"name": "demo"})
