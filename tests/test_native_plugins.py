from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseekfathom._core.native_plugins import (
    enabled_native_commands,
    is_native_plugin,
    native_plugin_entries,
    resolve_native_command,
    set_native_plugin_enabled,
)


def test_official_native_plugins_are_enabled_and_expose_real_commands(tmp_path: Path) -> None:
    entries = native_plugin_entries(tmp_path)
    commands = enabled_native_commands(tmp_path)

    assert {entry["name"] for entry in entries} == {
        "code-review",
        "test-doctor",
        "security-audit",
        "commit-assistant",
        "release-notes",
        "workspace-inspector",
    }
    assert all(entry["enabled"] is True and entry["scope"] == "official" for entry in entries)
    assert {command["key"] for command in commands} == {
        "/review",
        "/test",
        "/security",
        "/commit",
        "/release-notes",
        "/workspace",
    }
    assert resolve_native_command("/review", tmp_path).handler == "review"
    assert resolve_native_command("test", tmp_path).mode == "agent"
    assert next(command for command in commands if command["key"] == "/test")["prompt"].startswith("Inspect")


def test_native_plugin_toggle_is_persistent_and_scoped(tmp_path: Path) -> None:
    path = set_native_plugin_enabled("security-audit", False, tmp_path)

    assert path == tmp_path / "native-plugins.json"
    assert "/security" not in {item["key"] for item in enabled_native_commands(tmp_path)}
    assert "/review" in {item["key"] for item in enabled_native_commands(tmp_path)}
    security = next(item for item in native_plugin_entries(tmp_path) if item["name"] == "security-audit")
    assert security["enabled"] is False
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted == {"plugins": {"security-audit": False}, "version": 1}

    set_native_plugin_enabled("security-audit", True, tmp_path)
    assert resolve_native_command("security", tmp_path) is not None


def test_unknown_native_plugin_cannot_modify_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown native plugin"):
        set_native_plugin_enabled("not-installed", False, tmp_path)
    assert not (tmp_path / "native-plugins.json").exists()
    assert is_native_plugin("code-review") is True
    assert is_native_plugin("not-installed") is False
