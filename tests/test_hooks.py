from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

import pytest

from deepseekfathom._core import hooks
from deepseekfathom._core.hooks import (
    HookConfig,
    HookOutcome,
    HookRunner,
    POST_TOOL_USE,
    PRE_TOOL_USE,
    SESSION_START,
    STOP,
    USER_PROMPT_SUBMIT,
    hook_decision,
    inspect_hooks,
    is_project_trusted,
    parse_session_start_output,
    set_hook_enabled,
    save_hook_settings,
    trust_project,
)


def write_settings(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def python_command(source: str) -> str:
    values = [sys.executable, "-c", source]
    return subprocess.list2cmdline(values) if sys.platform == "win32" else shlex.join(values)


def test_project_hooks_are_discovered_but_inactive_until_trusted(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    write_settings(home / "settings.json", {"hooks": {"Stop": [{"command": "echo global"}]}})
    write_settings(project / ".deepseekfathom" / "settings.json", {
        "hooks": {"PreToolUse": [{"match": "read_.*", "command": "echo project"}]}
    })

    inspection = inspect_hooks(project, home)

    assert inspection.project_defined is True
    assert inspection.project_trusted is False
    assert [hook.scope for hook in inspection.hooks] == ["project", "global"]
    assert [hook.scope for hook in inspection.active] == ["global"]
    assert any(issue.code == "hook.untrusted_project" for issue in inspection.issues)

    trust_project(project, home, "hooks")
    trusted = inspect_hooks(project, home)
    assert trusted.project_trusted is True
    assert [hook.scope for hook in trusted.active] == ["project", "global"]
    assert is_project_trusted(project, home, "mcp") is False


def test_hook_settings_save_is_atomic_and_validated(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    path = save_hook_settings("global", [
        HookConfig(PRE_TOOL_USE, "echo check", match="read_.*", env={"MODE": "check"}),
        {"event": STOP, "command": "echo done", "timeout": 1000},
    ], project, home)

    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert list(parsed["hooks"]) == [PRE_TOOL_USE, STOP]
    assert parsed["hooks"][PRE_TOOL_USE][0]["env"] == {"MODE": "check"}
    assert not list(home.glob(".settings.json.tmp-*"))

    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_hook_settings("global", [{"event": PRE_TOOL_USE, "command": "guard", "match": "["}], project, home)
    assert path.read_bytes() == before


def test_malformed_settings_and_invalid_matcher_are_diagnostic_only(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{broken", encoding="utf-8")
    inspection = inspect_hooks(tmp_path, home)
    assert any(issue.code == "hook.malformed_settings" for issue in inspection.issues)

    write_settings(home / "settings.json", {"hooks": {"PreToolUse": [{"match": "[", "command": "guard"}]}})
    inspection = inspect_hooks(tmp_path, home)
    assert not inspection.hooks
    assert any(issue.code == "hook.invalid_matcher" for issue in inspection.issues)


@pytest.mark.parametrize(
    "event,exit_code,timed_out,spawn_error,expected",
    [
        (PRE_TOOL_USE, 2, False, False, "block"),
        (USER_PROMPT_SUBMIT, 1, True, False, "block"),
        (POST_TOOL_USE, 2, False, False, "warn"),
        (STOP, 1, True, False, "warn"),
        (PRE_TOOL_USE, 1, False, False, "warn"),
        (PRE_TOOL_USE, -1, False, True, "error"),
        (STOP, 0, False, False, "pass"),
    ],
)
def test_only_gating_events_can_block(event: str, exit_code: int, timed_out: bool, spawn_error: bool, expected: str):
    assert hook_decision(event, exit_code, timed_out=timed_out, spawn_error=spawn_error) == expected


def test_runner_filters_matchers_and_stops_at_first_block(tmp_path: Path):
    called: list[str] = []

    def spawn(hook: HookConfig, _stdin: str) -> HookOutcome:
        called.append(hook.description)
        decision = "block" if hook.description == "block" else "pass"
        return HookOutcome(hook, decision, 2 if decision == "block" else 0, stderr="denied" if decision == "block" else "")

    runner = HookRunner([
        HookConfig(PRE_TOOL_USE, "first", match="write_.*", description="skip"),
        HookConfig(PRE_TOOL_USE, "second", match="read_.*", description="block"),
        HookConfig(PRE_TOOL_USE, "third", match="read_.*", description="after"),
    ], tmp_path, spawner=spawn)

    report = runner.pre_tool_use("read_file", {"path": "a.txt"})

    assert report.blocked is True
    assert called == ["block"]
    assert "denied" in report.block_message


def test_runner_executes_hook_with_json_stdin(tmp_path: Path):
    command = python_command("import json,sys; p=json.load(sys.stdin); print(p['event'] + ':' + p['prompt'])")
    runner = HookRunner([HookConfig(USER_PROMPT_SUBMIT, command, timeout_ms=2_000)], tmp_path)

    report = runner.user_prompt_submit("你好", 1)

    assert report.blocked is False
    assert report.outcomes[0].decision == "pass"
    assert report.outcomes[0].stdout == "UserPromptSubmit:你好"


def test_hook_timeout_blocks_pre_tool_but_not_stop(tmp_path: Path):
    command = python_command("import time; time.sleep(1)")
    pre = HookRunner([HookConfig(PRE_TOOL_USE, command, timeout_ms=30)], tmp_path).pre_tool_use("read_file", {})
    stop = HookRunner([HookConfig(STOP, command, timeout_ms=30)], tmp_path).stop("done", 1)

    assert pre.blocked is True
    assert pre.outcomes[0].timed_out is True
    assert stop.blocked is False
    assert stop.outcomes[0].decision == "warn"


def test_hook_output_is_capped_without_blocking_large_writes(tmp_path: Path):
    command = python_command("import sys; sys.stdout.write('x' * 300000); sys.stderr.write('y' * 300000)")
    report = HookRunner([HookConfig(STOP, command, timeout_ms=3_000)], tmp_path).stop("done", 1)

    outcome = report.outcomes[0]
    assert outcome.decision == "pass"
    assert outcome.truncated is True
    assert len(outcome.stdout.encode("utf-8")) == hooks.OUTPUT_CAP_BYTES
    assert len(outcome.stderr.encode("utf-8")) == hooks.OUTPUT_CAP_BYTES


def test_session_start_context_is_bounded_and_never_a_user_turn(tmp_path: Path):
    body = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "Use project rules.",
        }
    }
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps(body), encoding="utf-8")
    runner = HookRunner([HookConfig(SESSION_START, context_file=context_file, scope="plugin")], tmp_path)

    report = runner.session_start()

    assert report.session_contexts() == ["Use project rules."]
    assert parse_session_start_output("plain startup note") == "plain startup note"
    assert all("role" not in context for context in report.session_contexts())


def test_windows_hook_uses_hidden_process_wrapper(monkeypatch, tmp_path: Path):
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 77
        returncode = 0

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            captured["timeout"] = timeout
            return None, None

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        kwargs["stdout"].write(b"ok")
        return FakeProcess()

    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks, "popen_hidden", fake_popen)
    report = HookRunner([HookConfig(STOP, "echo ok")], tmp_path).stop("done", 1)

    assert report.outcomes[0].stdout == "ok"
    assert captured["command"] == "echo ok"
    assert captured["kwargs"]["stdin"] is subprocess.PIPE
    assert captured["kwargs"]["shell"] is True
    assert "start_new_session" not in captured["kwargs"]


def test_set_hook_enabled_preserves_other_settings(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    path = home / "settings.json"
    write_settings(path, {
        "theme": "dark",
        "hooks": {
            "Stop": [
                {"command": "first", "match": "*"},
                {"command": "second", "match": "other"},
            ],
        },
    })

    set_hook_enabled(path, STOP, "*", False, workspace, home)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["theme"] == "dark"
    assert raw["hooks"]["Stop"][0]["enabled"] is False
    inspection = inspect_hooks(workspace, home)
    assert [hook.enabled for hook in inspection.hooks] == [False, True]


def test_set_hook_enabled_by_stable_id_changes_only_one_duplicate(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    path = home / "settings.json"
    write_settings(path, {
        "hooks": {
            "Stop": [
                {"command": "first", "match": "*"},
                {"command": "second", "match": "*"},
            ],
        },
    })
    before = inspect_hooks(workspace, home).hooks

    assert len({hook.hook_id for hook in before}) == 2
    set_hook_enabled(path, STOP, "*", False, workspace, home, hook_id=before[1].hook_id)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "enabled" not in raw["hooks"]["Stop"][0]
    assert raw["hooks"]["Stop"][1]["enabled"] is False
    after = inspect_hooks(workspace, home).hooks
    assert after[0].hook_id == before[0].hook_id
    assert after[1].hook_id == before[1].hook_id
    assert [hook.enabled for hook in after] == [True, False]
