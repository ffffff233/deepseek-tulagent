from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from deepseekfathom._core.extensions import (
    ExtensionRuntime,
    UserMCPConfigError,
    delete_user_mcp_server,
    get_user_mcp_server,
    inspect_extensions,
    save_user_mcp_server,
)
from deepseekfathom._core.hooks import trust_project
from deepseekfathom._core.plugins import InstalledPlugin, upsert_plugin_state


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_static_extension_inspection_never_starts_process_and_redacts_secrets(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_json(home / "config.json", {
        "api_key": "sk-never-report-this",
        "mcpServers": {
            "global": {
                "command": "npx",
                "args": ["-y", "server"],
                "env": {"TOKEN": "env-secret"},
                "headers": {"Authorization": "Bearer header-secret"},
            }
        },
    })
    write_json(workspace / ".mcp.json", {
        "mcpServers": {"project": {"command": "project-server", "env": {"PASSWORD": "project-secret"}}}
    })

    def fail_process(*_args, **_kwargs):
        raise AssertionError("static inspection must not start external processes")

    monkeypatch.setattr(subprocess, "Popen", fail_process)
    report = inspect_extensions(workspace, home)
    public = report.to_dict()
    serialized = json.dumps(public, ensure_ascii=False)

    assert report.static is True
    assert [server.name for server in report.mcp_servers] == ["global", "project"]
    assert report.mcp_servers[0].active is True
    assert report.mcp_servers[1].active is False
    assert public["mcp"]["live"] is False
    assert public["mcp"]["projectDefined"] is True
    assert public["mcp"]["projectTrusted"] is False
    assert public["mcp"]["entries"][0]["envKeys"] == ["TOKEN"]
    assert "sk-never" not in serialized
    assert "env-secret" not in serialized
    assert "header-secret" not in serialized
    assert "project-secret" not in serialized
    assert any(issue["code"] == "mcp.untrusted_project" for issue in public["issues"])


def test_extension_report_exposes_stable_hook_ids_and_mcp_trust(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_json(workspace / ".mcp.json", {"mcpServers": {"local": {"command": "local-server"}}})
    write_json(home / "settings.json", {"hooks": {"Stop": [{"command": "first"}, {"command": "second"}]}})

    first = inspect_extensions(workspace, home).to_dict()
    second = inspect_extensions(workspace, home).to_dict()

    first_ids = [item["id"] for item in first["hooks"]["entries"]]
    assert first_ids == [item["id"] for item in second["hooks"]["entries"]]
    assert len(set(first_ids)) == 2
    assert first["mcp"]["projectDefined"] is True
    assert first["mcp"]["projectTrusted"] is False

    trust_project(workspace, home, "mcp")
    trusted = inspect_extensions(workspace, home).to_dict()
    assert trusted["mcp"]["projectTrusted"] is True
    assert trusted["mcp"]["entries"][0]["active"] is True


def test_mcp_priority_is_global_then_project_then_plugin(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_json(home / "config.json", {"mcpServers": {"shared": {"command": "global-server"}}})
    write_json(workspace / ".mcp.json", {"mcpServers": {"shared": {"command": "project-server"}}})
    trust_project(workspace, home, "mcp")
    plugin_root = home / "plugins" / "pack"
    write_json(plugin_root / "deepseekfathom-plugin.json", {
        "name": "pack",
        "mcpServers": {"shared": {"command": "plugin-server"}, "plugin-only": {"command": "only-server"}},
    })
    upsert_plugin_state(InstalledPlugin("pack", "plugins/pack", enabled=True), home)

    report = inspect_extensions(workspace, home)

    by_name = {server.name: server for server in report.mcp_servers}
    assert by_name["shared"].command == "global-server"
    assert by_name["plugin-only"].plugin == "pack"
    assert sum(issue.code == "mcp.shadowed_server" for issue in report.issues) == 2


def test_enabled_plugin_contributes_static_skills_instructions_hooks_and_mcp(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = home / "plugins" / "workflow"
    (root / "skills" / "review").mkdir(parents=True)
    (root / "skills" / "review" / "SKILL.md").write_text("review", encoding="utf-8")
    (root / "AGENTS.md").write_text("Use tests.", encoding="utf-8")
    write_json(root / "deepseekfathom-plugin.json", {
        "name": "workflow",
        "skills": "skills",
        "hooks": {"Stop": [{"command": "echo done", "env": {"HOOK_TOKEN": "secret"}}]},
        "mcpServers": {"helper": {"command": "helper-server"}},
    })
    upsert_plugin_state(InstalledPlugin("workflow", "plugins/workflow", enabled=True), home)

    report = inspect_extensions(workspace, home)
    public = report.to_dict()

    assert report.skill_roots == ((root / "skills").resolve(),)
    assert (root / "AGENTS.md").resolve() in report.instruction_files
    assert [hook.scope for hook in report.hooks.active] == ["plugin"]
    assert [server.name for server in report.mcp_servers] == ["helper"]
    assert public["hooks"]["entries"][0]["envKeys"] == ["HOOK_TOKEN"]
    assert "secret" not in json.dumps(public)


def test_malformed_mcp_and_plugin_state_are_reported_without_writes(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    config = home / "config.json"
    state = home / "plugin-packages.json"
    config.write_text("{bad", encoding="utf-8")
    state.write_text("{also-bad", encoding="utf-8")

    report = inspect_extensions(workspace, home)

    assert any(issue.code == "mcp.malformed_config" for issue in report.issues)
    assert any(issue.code == "plugin.invalid_state" for issue in report.issues)
    assert config.read_text(encoding="utf-8") == "{bad"
    assert state.read_text(encoding="utf-8") == "{also-bad"


def test_invalid_plugin_state_entry_is_a_diagnostic_not_a_crash(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_json(home / "plugin-packages.json", {
        "version": 1,
        "plugins": [{"name": "../escape", "root": "plugins/escape", "enabled": True}],
    })

    report = inspect_extensions(workspace, home)

    assert any(issue.code == "plugin.invalid_state" for issue in report.issues)
    assert not report.plugins


def test_project_plugin_is_visible_but_disabled(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    root = workspace / ".deepseekfathom" / "plugins" / "local-pack"
    write_json(root / "deepseekfathom-plugin.json", {
        "name": "local-pack",
        "hooks": {"Stop": [{"command": "echo must-not-run"}]},
        "mcpServers": {"unsafe": {"command": "must-not-start"}},
    })

    report = inspect_extensions(workspace, home)

    assert len(report.plugins) == 1
    assert report.plugins[0].installed.enabled is False
    assert not report.hooks.active
    assert not report.mcp_servers
    assert any(issue.code == "plugin.project_discovered" for issue in report.issues)


def test_extension_runtime_refreshes_catalog_and_hook_runner_without_starting_mcp(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_json(home / "config.json", {"mcpServers": {"clock": {"command": "clock-server"}}})

    def fail_process(*_args, **_kwargs):
        raise AssertionError("ExtensionRuntime construction and refresh must stay static")

    monkeypatch.setattr(subprocess, "Popen", fail_process)
    runtime = ExtensionRuntime(workspace, home)
    original_runner = runtime.hook_runner
    assert [spec.name for spec in runtime.mcp_specs] == ["clock"]
    assert runtime.skill_roots == ()
    assert runtime.instruction_files == ()

    write_json(home / "settings.json", {"hooks": {"Stop": [{"command": "echo done"}]}})
    runtime.refresh()

    assert runtime.hook_runner is not original_runner
    assert len(runtime.hook_runner.hooks) == 1
    configs = runtime.active_mcp_configs()
    assert len(configs) == 1 and configs[0].name == "clock"


def test_user_mcp_crud_preserves_unrelated_config_and_renames_atomically(tmp_path: Path):
    home = tmp_path / "home"
    config = home / "config.json"
    write_json(config, {
        "api_key": "keep-api-key",
        "theme": "dark",
        "mcpServers": {
            "existing": {"command": "existing-server", "env": {"KEEP": "yes"}},
            "other": {"command": "other-server"},
        },
    })

    saved = save_user_mcp_server({
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.test/rpc",
        "headers": {"Authorization": "Bearer edit-only-secret", "X-Tenant": "demo"},
        "enabled": True,
        "callTimeoutMs": 45_000,
    }, home)

    assert saved["name"] == "remote"
    assert saved["headerKeys"] == ["Authorization", "X-Tenant"]
    assert "edit-only-secret" not in json.dumps(saved)
    on_disk = json.loads(config.read_text(encoding="utf-8"))
    assert on_disk["api_key"] == "keep-api-key"
    assert on_disk["theme"] == "dark"
    assert on_disk["mcpServers"]["existing"]["env"] == {"KEEP": "yes"}
    assert on_disk["mcpServers"]["other"] == {"command": "other-server"}

    editable = get_user_mcp_server("remote", home)
    assert editable["transport"] == "http"
    assert editable["headers"]["Authorization"] == "Bearer edit-only-secret"

    renamed = save_user_mcp_server({
        **editable,
        "name": "remote-renamed",
    }, home, original_name="remote")
    assert renamed["name"] == "remote-renamed"
    on_disk = json.loads(config.read_text(encoding="utf-8"))
    assert "remote" not in on_disk["mcpServers"]
    assert "remote-renamed" in on_disk["mcpServers"]

    assert delete_user_mcp_server("remote-renamed", home) == "remote-renamed"
    on_disk = json.loads(config.read_text(encoding="utf-8"))
    assert set(on_disk["mcpServers"]) == {"existing", "other"}
    assert on_disk["api_key"] == "keep-api-key"


def test_user_mcp_get_is_user_scoped_and_diagnostics_never_expose_header_values(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_json(workspace / ".mcp.json", {
        "mcpServers": {"project-only": {"command": "project-server"}},
    })
    save_user_mcp_server({
        "name": "remote",
        "transport": "http",
        "url": "http://127.0.0.1:4321/mcp",
        "headers": {"Authorization": "Bearer must-stay-private"},
    }, home)

    with pytest.raises(UserMCPConfigError, match="找不到用户 MCP"):
        get_user_mcp_server("project-only", home)
    with pytest.raises(UserMCPConfigError, match="找不到用户 MCP"):
        delete_user_mcp_server("project-only", home)

    report = inspect_extensions(workspace, home)
    public = report.to_dict()
    serialized = json.dumps(public, ensure_ascii=False)
    remote = next(item for item in public["mcp"]["entries"] if item["name"] == "remote")
    assert remote["headerKeys"] == ["Authorization"]
    assert "must-stay-private" not in serialized
    runtime = next(config for config in ExtensionRuntime(workspace, home).active_mcp_configs() if config.name == "remote")
    assert runtime.transport == "streamable-http"
    assert runtime.headers["Authorization"] == "Bearer must-stay-private"


@pytest.mark.parametrize("transport", ["http", "streamable-http", "streamable_http"])
def test_user_mcp_crud_normalizes_http_transport_aliases(tmp_path: Path, transport: str):
    home = tmp_path / "home"

    save_user_mcp_server({
        "name": "remote",
        "transport": transport,
        "url": "https://example.test/mcp",
    }, home)

    on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["remote"]["type"] == "http"
    assert get_user_mcp_server("remote", home)["transport"] == "http"


def test_user_stdio_headers_remain_editable_but_are_not_sent_to_the_process(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "Bearer compatibility-secret"

    save_user_mcp_server({
        "name": "local",
        "transport": "stdio",
        "command": "local-server",
        "headers": {"Authorization": secret},
    }, home)

    editable = get_user_mcp_server("local", home)
    assert editable["headers"]["Authorization"] == secret
    public = inspect_extensions(workspace, home).to_dict()
    assert public["mcp"]["entries"][0]["headerKeys"] == ["Authorization"]
    assert secret not in json.dumps(public, ensure_ascii=False)
    runtime = ExtensionRuntime(workspace, home).active_mcp_configs()[0]
    assert runtime.transport == "stdio"
    assert runtime.headers == {}


@pytest.mark.parametrize("server", [
    {"name": "bad", "transport": "http", "url": "file:///tmp/mcp"},
    {"name": "bad", "transport": "http", "url": "https://user:pass@example.test/mcp"},
    {"name": "bad", "transport": "http", "url": "https://example.test/mcp", "headers": {"Content-Length": "4"}},
    {"name": "bad", "transport": "http", "url": "https://example.test/mcp", "headers": {"X-Test": "one\r\ntwo"}},
    {"name": "bad", "transport": "stdio", "command": "../outside/server"},
    {"name": "bad", "transport": "stdio", "command": "server", "env": {"__proto__": "blocked"}},
    {"name": "bad", "transport": "stdio", "command": "server", "args": ["x" * 20_000]},
    {"name": "bad", "transport": "stdio", "command": "server", "path": "other-config.json"},
])
def test_user_mcp_save_rejects_dangerous_or_oversized_input_without_writing(tmp_path: Path, server: dict):
    home = tmp_path / "home"
    config = home / "config.json"
    write_json(config, {"api_key": "keep", "mcpServers": {"safe": {"command": "safe-server"}}})
    before = config.read_bytes()

    with pytest.raises(UserMCPConfigError):
        save_user_mcp_server(server, home)

    assert config.read_bytes() == before


def test_user_mcp_rename_rejects_case_insensitive_collision_without_writing(tmp_path: Path):
    home = tmp_path / "home"
    config = home / "config.json"
    write_json(config, {
        "mcpServers": {
            "first": {"command": "first-server"},
            "Second": {"command": "second-server"},
        }
    })
    before = config.read_bytes()

    with pytest.raises(UserMCPConfigError, match="已被占用"):
        save_user_mcp_server(
            {"name": "second", "transport": "stdio", "command": "replacement"},
            home,
            original_name="first",
        )

    assert config.read_bytes() == before


def test_hand_written_http_mcp_validation_redacts_header_values_from_errors(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "never-echo-this-header-value"
    write_json(home / "config.json", {
        "mcpServers": {
            "bad-header": {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Mcp-Session-Id": secret},
            },
            "bad-url": {
                "type": "http",
                "url": "https://user:password@example.test/mcp",
            },
        }
    })

    public = inspect_extensions(workspace, home).to_dict()
    serialized = json.dumps(public, ensure_ascii=False)

    assert {item["name"] for item in public["issues"] if item["code"] == "mcp.invalid_server"} == {
        "bad-header",
        "bad-url",
    }
    assert secret not in serialized
    assert "password" not in serialized
