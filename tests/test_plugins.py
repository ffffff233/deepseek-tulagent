from __future__ import annotations

import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from deepseek_tulagent import plugins as plugin_module
from deepseek_tulagent.plugins import (
    InstalledPlugin,
    PluginError,
    PluginState,
    PluginStateError,
    discover_installed_plugins,
    discover_project_plugins,
    install_local_plugin,
    load_plugin_state,
    parse_plugin,
    save_plugin_state,
    set_plugin_enabled,
    state_path,
    uninstall_plugin,
    upsert_plugin_state,
)


def write_manifest(root: Path, payload: dict, name: str = "deepseekfathom-plugin.json") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_parse_native_plugin_inventory_and_relative_paths(tmp_path: Path):
    root = tmp_path / "source"
    (root / "skills" / "review").mkdir(parents=True)
    (root / "skills" / "review" / "SKILL.md").write_text("---\ndescription: Review code\n---\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "RULES.md").write_text("rules", encoding="utf-8")
    (root / "hooks").mkdir()
    (root / "hooks" / "start.py").write_text("print('ok')", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "server.exe").write_bytes(b"")
    write_manifest(root, {
        "name": "quality",
        "version": "1.2.3",
        "description": "Quality tools",
        "skills": "skills",
        "instructions": ["docs/RULES.md"],
        "hooks": {
            "SessionStart": [{"command": "hooks/start.py", "timeout": 1200}],
        },
        "mcpServers": {
            "helper": {
                "command": "bin/server.exe",
                "args": ["--stdio"],
                "env": {"TOKEN_ENV": "value"},
                "trusted_read_only_tools": ["search"],
            }
        },
    })

    manifest = parse_plugin(root)

    assert manifest.name == "quality"
    assert manifest.version == "1.2.3"
    assert manifest.skills == ((root / "skills").resolve(),)
    assert (root / "docs" / "RULES.md").resolve() in manifest.instructions
    assert manifest.hooks[0].event == "SessionStart"
    assert manifest.hooks[0].command == str((root / "hooks" / "start.py").resolve())
    assert manifest.mcp_servers[0].command == str((root / "bin" / "server.exe").resolve())
    assert manifest.mcp_servers[0].trusted_read_only_tools == ("search",)


def test_hook_shell_command_with_script_path_is_not_misread_as_one_filename(tmp_path: Path):
    root = tmp_path / "source"
    write_manifest(root, {
        "name": "shell-hook",
        "hooks": {"Stop": [{"command": "node hooks/stop.js"}]},
    })

    manifest = parse_plugin(root)

    assert manifest.hooks[0].command == "node hooks/stop.js"


@pytest.mark.parametrize(
    "field,payload",
    [
        ("skill", {"skills": "../outside"}),
        ("instruction", {"instructions": ["..\\outside.md"]}),
        ("hook-context", {"hooks": {"SessionStart": [{"contextFile": "../secret"}]}}),
        ("hook-cwd", {"hooks": {"Stop": [{"command": "echo ok", "cwd": "../../"}]}}),
        ("mcp-command", {"mcpServers": {"x": {"command": "../server.exe"}}}),
    ],
)
def test_manifest_rejects_paths_outside_plugin_root(tmp_path: Path, field: str, payload: dict):
    root = tmp_path / field
    base = {"name": "safe"}
    base.update(payload)
    write_manifest(root, base)

    with pytest.raises(PluginError, match="escapes the plugin root"):
        parse_plugin(root)


def test_manifest_rejects_symlink_escape_when_supported(tmp_path: Path):
    root = tmp_path / "source"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    try:
        (root / "skills").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")
    write_manifest(root, {"name": "unsafe", "skills": "skills"})

    with pytest.raises(PluginError, match="escapes the plugin root"):
        parse_plugin(root)


def test_codex_manifest_discovers_conventional_skills(tmp_path: Path):
    root = tmp_path / "codex"
    (root / "skills" / "plan").mkdir(parents=True)
    (root / "skills" / "plan" / "SKILL.md").write_text("plan", encoding="utf-8")
    write_manifest(root, {"name": "codex-pack", "version": "1"}, ".codex-plugin/plugin.json")

    manifest = parse_plugin(root)

    assert manifest.manifest_kind == "codex"
    assert manifest.skills == ((root / "skills").resolve(),)


def test_claude_manifest_maps_instructions_and_command_hooks(tmp_path: Path):
    root = tmp_path / "claude"
    write_manifest(root, {"name": "claude-pack"}, ".claude-plugin/plugin.json")
    (root / "CLAUDE.md").write_text("Use the plugin workflow.", encoding="utf-8")
    write_manifest(root, {
        "hooks": {
            "PreToolUse": [{
                "matcher": "read_.*",
                "hooks": [
                    {"type": "command", "command": "node guard.js", "timeout": 3},
                    {"type": "prompt", "command": "ignored"},
                ],
            }]
        }
    }, ".claude/settings.json")

    manifest = parse_plugin(root)

    assert (root / "CLAUDE.md").resolve() in manifest.instructions
    assert manifest.hooks[0].event == "PreToolUse"
    assert manifest.hooks[0].match == "read_.*"
    assert manifest.hooks[0].timeout_ms == 3_000
    assert any("unsupported hook type" in warning for warning in manifest.warnings)


def test_plugin_state_is_sorted_atomic_and_enableable(tmp_path: Path):
    home = tmp_path / "home"
    first = InstalledPlugin("zeta", "plugins/zeta", enabled=False)
    second = InstalledPlugin("alpha", "plugins/alpha", enabled=True)

    save_plugin_state(PluginState(1, (first, second)), home)
    set_plugin_enabled("zeta", True, home)
    loaded = load_plugin_state(home)

    assert [item.name for item in loaded.plugins] == ["alpha", "zeta"]
    assert all(item.enabled for item in loaded.plugins)
    assert not list(home.glob(".plugin-packages.json.tmp-*"))
    raw = json.loads(state_path(home).read_text(encoding="utf-8"))
    assert [item["name"] for item in raw["plugins"]] == ["alpha", "zeta"]


def test_concurrent_plugin_state_updates_do_not_lose_entries(tmp_path: Path):
    home = tmp_path / "home"

    def add(index: int) -> None:
        upsert_plugin_state(InstalledPlugin(f"plugin-{index}", f"plugins/plugin-{index}"), home)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(24)))

    assert len(load_plugin_state(home).plugins) == 24
    assert not list(home.glob(".plugin-packages.json.tmp-*"))


def test_corrupt_plugin_state_is_reported_not_silently_overwritten(tmp_path: Path):
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "{broken"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(PluginStateError):
        load_plugin_state(tmp_path)

    assert path.read_text(encoding="utf-8") == original


def test_discovery_keeps_disabled_plugins_visible(tmp_path: Path):
    home = tmp_path / "home"
    root = home / "plugins" / "demo"
    write_manifest(root, {"name": "demo", "version": "2"})
    upsert_plugin_state(InstalledPlugin("demo", "plugins/demo", enabled=False), home)

    packages = discover_installed_plugins(home)

    assert len(packages) == 1
    assert packages[0].manifest is not None
    assert packages[0].installed.enabled is False


def test_project_discovery_never_auto_enables_plugin(tmp_path: Path):
    root = tmp_path / ".deepseek-tulagent" / "plugins" / "project-pack"
    write_manifest(root, {"name": "project-pack"})

    packages = discover_project_plugins(tmp_path)

    assert len(packages) == 1
    assert packages[0].scope == "project"
    assert packages[0].installed.enabled is False


def test_local_plugin_update_only_replaces_its_owned_root(tmp_path: Path):
    home = tmp_path / "home"
    sessions = home / "sessions" / "conversation.jsonl"
    user_skill = home / "skills" / "mine" / "SKILL.md"
    config = home / "config.json"
    for path, body in ((sessions, "chat"), (user_skill, "skill"), (config, "{\"api_key\":\"secret\"}")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    source1 = tmp_path / "source1"
    source2 = tmp_path / "source2"
    write_manifest(source1, {"name": "demo", "version": "1"})
    write_manifest(source2, {"name": "demo", "version": "2"})
    (source1 / "payload.txt").write_text("old", encoding="utf-8")
    (source2 / "payload.txt").write_text("new", encoding="utf-8")

    install_local_plugin(source1, home)
    installed = install_local_plugin(source2, home, replace=True)

    assert installed.version == "2"
    assert (home / "plugins" / "demo" / "payload.txt").read_text(encoding="utf-8") == "new"
    assert sessions.read_text(encoding="utf-8") == "chat"
    assert user_skill.read_text(encoding="utf-8") == "skill"
    assert config.read_text(encoding="utf-8") == "{\"api_key\":\"secret\"}"

    removed = uninstall_plugin("demo", home)
    assert removed is not None
    assert not (home / "plugins" / "demo").exists()
    assert sessions.exists() and user_skill.exists() and config.exists()


def test_failed_plugin_update_before_swap_keeps_previous_install(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    source1 = tmp_path / "source1"
    source2 = tmp_path / "source2"
    write_manifest(source1, {"name": "demo", "version": "1"})
    write_manifest(source2, {"name": "demo", "version": "2"})
    (source1 / "payload.txt").write_text("working", encoding="utf-8")
    install_local_plugin(source1, home)

    def fail_copy(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(plugin_module.shutil, "copytree", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        install_local_plugin(source2, home, replace=True)

    assert (home / "plugins" / "demo" / "payload.txt").read_text(encoding="utf-8") == "working"
    assert load_plugin_state(home).plugins[0].version == "1"
