from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterable

from .plugins import PluginHook, extension_home
from .processes import popen_hidden, run_hidden


PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
PERMISSION_REQUEST = "PermissionRequest"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
STOP = "Stop"
POST_LLM_CALL = "PostLLMCall"
SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
SUBAGENT_STOP = "SubagentStop"
NOTIFICATION = "Notification"
PRE_COMPACT = "PreCompact"
EVENTS = (
    PRE_TOOL_USE,
    POST_TOOL_USE,
    PERMISSION_REQUEST,
    USER_PROMPT_SUBMIT,
    STOP,
    POST_LLM_CALL,
    SESSION_START,
    SESSION_END,
    SUBAGENT_STOP,
    NOTIFICATION,
    PRE_COMPACT,
)
BLOCKING_EVENTS = frozenset({PRE_TOOL_USE, USER_PROMPT_SUBMIT})
TOOL_MATCH_EVENTS = frozenset({PRE_TOOL_USE, POST_TOOL_USE, PERMISSION_REQUEST})
GLOBAL_SETTINGS_FILENAME = "settings.json"
PROJECT_SETTINGS_RELATIVE = Path(".deepseek-tulagent") / "settings.json"
TRUST_FILENAME = "trust.json"
MAX_SETTINGS_BYTES = 1_000_000
OUTPUT_CAP_BYTES = 256 * 1024
SESSION_CONTEXT_ITEM_CHARS = 10_000
SESSION_CONTEXT_TOTAL_CHARS = 20_000
_TRUST_LOCK = threading.RLock()


@dataclass(frozen=True)
class HookConfig:
    event: str
    command: str = ""
    match: str = "*"
    description: str = ""
    timeout_ms: int | None = None
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    context_file: Path | None = None
    scope: str = "global"
    source: Path | None = None
    enabled: bool = True
    plugin: str = ""
    hook_id: str = ""

    @property
    def timeout_seconds(self) -> float:
        if self.timeout_ms is not None and self.timeout_ms > 0:
            return self.timeout_ms / 1000.0
        return 5.0 if self.event in {PRE_TOOL_USE, PERMISSION_REQUEST, USER_PROMPT_SUBMIT} else 30.0


@dataclass(frozen=True)
class HookIssue:
    severity: str
    code: str
    message: str
    source: str
    event: str = ""


@dataclass(frozen=True)
class HookInspection:
    hooks: tuple[HookConfig, ...]
    issues: tuple[HookIssue, ...]
    project_defined: bool
    project_trusted: bool

    @property
    def active(self) -> tuple[HookConfig, ...]:
        return tuple(hook for hook in self.hooks if hook.enabled)


@dataclass(frozen=True)
class HookOutcome:
    hook: HookConfig
    decision: str
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False
    duration_ms: int = 0


@dataclass(frozen=True)
class HookReport:
    event: str
    outcomes: tuple[HookOutcome, ...]
    blocked: bool = False
    block_message: str = ""

    def session_contexts(self) -> list[str]:
        if self.event != SESSION_START:
            return []
        contexts: list[str] = []
        remaining = SESSION_CONTEXT_TOTAL_CHARS
        for outcome in self.outcomes:
            if outcome.decision != "pass" or not outcome.stdout.strip() or remaining <= 0:
                continue
            context = parse_session_start_output(outcome.stdout, self.event)
            if not context:
                continue
            context = context[: min(SESSION_CONTEXT_ITEM_CHARS, remaining)]
            contexts.append(context)
            remaining -= len(context)
        return contexts

    def pre_compact_guidance(self) -> str:
        if self.event != PRE_COMPACT:
            return ""
        return "\n".join(
            outcome.stdout.strip()
            for outcome in self.outcomes
            if outcome.decision == "pass" and outcome.stdout.strip()
        )


def global_settings_path(home: Path | None = None) -> Path:
    return extension_home(home) / GLOBAL_SETTINGS_FILENAME


def project_settings_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / PROJECT_SETTINGS_RELATIVE


def trust_path(home: Path | None = None) -> Path:
    return extension_home(home) / TRUST_FILENAME


def canonical_project_key(workspace: Path) -> str:
    return os.path.normcase(str(Path(workspace).expanduser().resolve()))


def is_project_trusted(workspace: Path, home: Path | None = None, capability: str = "hooks") -> bool:
    data = _read_trust(home)
    value = data.get("projects", {}).get(canonical_project_key(workspace))
    if isinstance(value, bool):
        return value if capability == "hooks" else False
    return bool(value.get(capability)) if isinstance(value, dict) else False


def trust_project(workspace: Path, home: Path | None = None, capability: str = "hooks") -> Path:
    if capability not in {"hooks", "mcp", "plugins"}:
        raise ValueError(f"unsupported project trust capability: {capability}")
    key = canonical_project_key(workspace)
    with _TRUST_LOCK:
        data = _read_trust(home)
        projects = data.setdefault("projects", {})
        current = projects.get(key)
        if isinstance(current, bool):
            current = {"hooks": current}
        if not isinstance(current, dict):
            current = {}
        current[capability] = True
        projects[key] = current
        _atomic_write_json(trust_path(home), data)
    return trust_path(home)


def revoke_project_trust(workspace: Path, home: Path | None = None, capability: str = "hooks") -> Path:
    key = canonical_project_key(workspace)
    with _TRUST_LOCK:
        data = _read_trust(home)
        projects = data.setdefault("projects", {})
        current = projects.get(key)
        if isinstance(current, bool):
            current = {"hooks": current}
        if isinstance(current, dict):
            current.pop(capability, None)
            if current:
                projects[key] = current
            else:
                projects.pop(key, None)
        _atomic_write_json(trust_path(home), data)
    return trust_path(home)


def _read_trust(home: Path | None = None) -> dict[str, Any]:
    path = trust_path(home)
    if not path.exists():
        return {"version": 1, "projects": {}}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"version": 1, "projects": {}}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("projects", {}), dict):
        return {"version": 1, "projects": {}}
    parsed.setdefault("version", 1)
    parsed.setdefault("projects", {})
    return parsed


def inspect_hooks(
    workspace: Path,
    home: Path | None = None,
    *,
    plugin_hooks: Iterable[tuple[str, PluginHook]] = (),
) -> HookInspection:
    workspace = Path(workspace).resolve()
    project_path = project_settings_path(workspace)
    global_path = global_settings_path(home)
    trusted = is_project_trusted(workspace, home, "hooks")
    hooks: list[HookConfig] = []
    issues: list[HookIssue] = []

    project_hooks, project_issues, project_defined = _read_settings(
        project_path,
        scope="project",
        default_cwd=workspace,
        enabled=trusted,
    )
    hooks.extend(project_hooks)
    issues.extend(project_issues)
    if project_defined and not trusted:
        issues.append(HookIssue(
            "warning",
            "hook.untrusted_project",
            "Project hooks are present but will not run until this workspace is explicitly trusted.",
            _display_path(project_path, workspace, extension_home(home)),
        ))

    for plugin_index, (plugin_name, plugin_hook) in enumerate(plugin_hooks):
        hook, issue = _from_plugin_hook(plugin_name, plugin_hook, plugin_index)
        if issue is not None:
            issues.append(issue)
        elif hook is not None:
            hooks.append(hook)

    global_hooks, global_issues, _ = _read_settings(
        global_path,
        scope="global",
        default_cwd=workspace,
        enabled=True,
    )
    hooks.extend(global_hooks)
    issues.extend(global_issues)
    return HookInspection(tuple(hooks), tuple(issues), project_defined, trusted)


def load_hooks(
    workspace: Path,
    home: Path | None = None,
    *,
    plugin_hooks: Iterable[tuple[str, PluginHook]] = (),
) -> list[HookConfig]:
    return list(inspect_hooks(workspace, home, plugin_hooks=plugin_hooks).active)


def save_hook_settings(
    scope: str,
    hooks: Iterable[HookConfig | dict[str, Any]],
    workspace: Path,
    home: Path | None = None,
) -> Path:
    """Validate and atomically save user-authored hooks for one scope.

    Plugin hooks are package-owned and cannot be written through this function.
    Project settings remain inert until the separate trust action is performed.
    """

    if scope not in {"global", "project"}:
        raise ValueError("hook scope must be global or project")
    path = global_settings_path(home) if scope == "global" else project_settings_path(workspace)
    mapping: dict[str, list[dict[str, Any]]] = {}
    for value in hooks:
        if isinstance(value, HookConfig):
            event = value.event
            raw: dict[str, Any] = {
                "command": value.command,
                "match": value.match,
                "description": value.description,
                "timeout": value.timeout_ms,
                "cwd": str(value.cwd) if value.cwd is not None else None,
                "env": dict(value.env) or None,
                "contextFile": str(value.context_file) if value.context_file is not None else None,
                "enabled": value.enabled,
            }
        elif isinstance(value, dict):
            event = value.get("event")
            raw = dict(value)
            raw.pop("event", None)
        else:
            raise ValueError("hook entries must be HookConfig objects or mappings")
        if not isinstance(event, str) or event not in EVENTS:
            raise ValueError(f"unknown hook event: {event}")
        raw = {key: item for key, item in raw.items() if item not in (None, "", {}) or key == "command"}
        mapping.setdefault(event, []).append(raw)
    payload = {"hooks": {event: mapping[event] for event in EVENTS if event in mapping}}

    # Validate the exact serialized shape before replacing the user's file.
    entries = _settings_entries(payload)
    for event, raw in entries:
        if not isinstance(raw, dict):
            raise ValueError(f"hook {event} must be an object")
        command = raw.get("command", "")
        context_file = raw.get("contextFile", "")
        if not isinstance(command, str) or not isinstance(context_file, str) or (not command.strip() and not context_file.strip()):
            raise ValueError(f"hook {event} requires command or contextFile")
        matcher = raw.get("match", "*")
        if event in TOOL_MATCH_EVENTS and isinstance(matcher, str) and matcher not in {"", "*"}:
            try:
                re.compile(matcher)
            except re.error as exc:
                raise ValueError(f"hook {event} matcher is invalid: {exc}") from exc
        if raw.get("timeout") is not None and _timeout_value(raw.get("timeout")) is None:
            raise ValueError(f"hook {event} timeout must be positive")
        env = raw.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in env.items()):
            raise ValueError(f"hook {event} env must contain string keys and values")
        if "enabled" in raw and not isinstance(raw.get("enabled"), bool):
            raise ValueError(f"hook {event} enabled must be boolean")
    _atomic_write_json(path, payload)
    return path


def set_hook_enabled(
    source: Path,
    event: str,
    matcher: str,
    enabled: bool,
    workspace: Path,
    home: Path | None = None,
    *,
    hook_id: str = "",
) -> Path:
    """Toggle one user/project hook while preserving unrelated settings."""

    path = Path(source).expanduser().resolve()
    allowed = {
        global_settings_path(home).resolve(),
        project_settings_path(workspace).resolve(),
    }
    if path not in allowed:
        raise ValueError("only global or project hook settings can be edited")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read hook settings: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("hook settings must be an object")
    mapping = parsed.get("hooks", parsed)
    if not isinstance(mapping, dict):
        raise ValueError("hooks must be an object keyed by event")
    values = mapping.get(event)
    if not isinstance(values, list):
        raise ValueError(f"hook event not found: {event}")
    scope = "global" if path == global_settings_path(home).resolve() else "project"
    changed = 0
    for entry_index, (entry_event, raw) in enumerate(_settings_entries(parsed)):
        if entry_event != event or not isinstance(raw, dict):
            continue
        raw_match = raw.get("match", raw.get("matcher", "*"))
        raw_match = str(raw_match or "*")
        candidate_id = _stable_hook_id(
            scope,
            path,
            event,
            entry_index,
            str(raw.get("command") or "").strip(),
            raw_match,
            str(raw.get("contextFile") or "").strip(),
        )
        if hook_id and candidate_id != hook_id:
            continue
        if not hook_id and raw_match != str(matcher or "*"):
            continue
        raw["enabled"] = bool(enabled)
        changed += 1
        break
    if not changed:
        target = hook_id or f"{event}/{matcher}"
        raise ValueError(f"matching hook not found: {target}")
    _atomic_write_json(path, parsed)
    return path


def _read_settings(
    path: Path,
    *,
    scope: str,
    default_cwd: Path,
    enabled: bool,
) -> tuple[list[HookConfig], list[HookIssue], bool]:
    if not path.exists():
        return [], [], False
    source = str(path)
    try:
        with path.open("rb") as handle:
            body = handle.read(MAX_SETTINGS_BYTES + 1)
        if len(body) > MAX_SETTINGS_BYTES:
            raise ValueError(f"settings exceed {MAX_SETTINGS_BYTES} bytes")
        parsed = json.loads(body.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return [], [HookIssue("error", "hook.malformed_settings", str(exc), source)], True
    try:
        entries = _settings_entries(parsed)
    except ValueError as exc:
        return [], [HookIssue("error", "hook.malformed_settings", str(exc), source)], True
    hooks: list[HookConfig] = []
    issues: list[HookIssue] = []
    for entry_index, (event, raw) in enumerate(entries):
        if event not in EVENTS:
            issues.append(HookIssue("warning", "hook.unknown_event", f"Unknown hook event: {event}", source, event))
            continue
        if not isinstance(raw, dict):
            issues.append(HookIssue("warning", "hook.invalid_entry", "Hook entry must be an object.", source, event))
            continue
        command = raw.get("command")
        context_file = raw.get("contextFile")
        if not isinstance(command, str):
            command = ""
        if not isinstance(context_file, str):
            context_file = ""
        command = command.strip()
        context_file = context_file.strip()
        if not command and not context_file:
            issues.append(HookIssue("warning", "hook.missing_command", "Hook command is empty.", source, event))
            continue
        matcher = raw.get("match", raw.get("matcher", "*"))
        matcher = matcher.strip() if isinstance(matcher, str) else "*"
        if event in TOOL_MATCH_EVENTS and matcher not in {"", "*"}:
            try:
                re.compile(matcher)
            except re.error as exc:
                issues.append(HookIssue("warning", "hook.invalid_matcher", f"Invalid hook matcher: {exc}", source, event))
                continue
        timeout = _timeout_value(raw.get("timeout"))
        if raw.get("timeout") is not None and timeout is None:
            issues.append(HookIssue("warning", "hook.invalid_timeout", "Hook timeout must be a positive number of milliseconds.", source, event))
            continue
        cwd = default_cwd
        if isinstance(raw.get("cwd"), str) and raw["cwd"].strip():
            candidate = Path(raw["cwd"].strip()).expanduser()
            cwd = candidate.resolve() if candidate.is_absolute() else (default_cwd / candidate).resolve()
        context_path: Path | None = None
        if context_file:
            candidate = Path(context_file).expanduser()
            context_path = candidate.resolve() if candidate.is_absolute() else (default_cwd / candidate).resolve()
        env = raw.get("env") or {}
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            issues.append(HookIssue("warning", "hook.invalid_env", "Hook env must contain string keys and values.", source, event))
            continue
        raw_enabled = raw.get("enabled", True)
        if not isinstance(raw_enabled, bool):
            issues.append(HookIssue("warning", "hook.invalid_enabled", "Hook enabled must be boolean.", source, event))
            continue
        hooks.append(HookConfig(
            event=event,
            command=command,
            match=matcher or "*",
            description=raw.get("description", "").strip() if isinstance(raw.get("description"), str) else "",
            timeout_ms=timeout,
            cwd=cwd,
            env=dict(env),
            context_file=context_path,
            scope=scope,
            source=path.resolve(),
            enabled=enabled and raw_enabled,
            hook_id=_stable_hook_id(
                scope,
                path.resolve(),
                event,
                entry_index,
                command,
                matcher or "*",
                context_file,
            ),
        ))
    return hooks, issues, bool(entries)


def _settings_entries(parsed: Any) -> list[tuple[str, Any]]:
    if isinstance(parsed, list):
        entries: list[tuple[str, Any]] = []
        for raw in parsed:
            if not isinstance(raw, dict) or not isinstance(raw.get("event"), str):
                raise ValueError("hook list entries require an event")
            entries.append((raw["event"].strip(), raw))
        return entries
    if not isinstance(parsed, dict):
        raise ValueError("hook settings must be an object or array")
    mapping = parsed.get("hooks", parsed)
    if not isinstance(mapping, dict):
        raise ValueError("hooks must be an object keyed by event")
    entries = []
    for event, values in mapping.items():
        if not isinstance(values, list):
            raise ValueError(f"hooks.{event} must be an array")
        entries.extend((str(event).strip(), value) for value in values)
    return entries


def _from_plugin_hook(
    plugin_name: str,
    hook: PluginHook,
    entry_index: int,
) -> tuple[HookConfig | None, HookIssue | None]:
    source = str(hook.context_file or hook.cwd or "<plugin>")
    if hook.event not in EVENTS:
        return None, HookIssue("warning", "hook.unknown_event", f"Unknown plugin hook event: {hook.event}", source, hook.event)
    if hook.event in TOOL_MATCH_EVENTS and hook.match not in {"", "*"}:
        try:
            re.compile(hook.match)
        except re.error as exc:
            return None, HookIssue("warning", "hook.invalid_matcher", f"Invalid plugin hook matcher: {exc}", source, hook.event)
    return HookConfig(
        event=hook.event,
        command=hook.command,
        match=hook.match or "*",
        description=hook.description,
        timeout_ms=hook.timeout_ms,
        cwd=hook.cwd,
        env=dict(hook.env),
        context_file=hook.context_file,
        scope="plugin",
        source=hook.context_file or hook.cwd,
        enabled=True,
        plugin=plugin_name,
        hook_id=_stable_hook_id(
            "plugin",
            hook.context_file or hook.cwd or plugin_name,
            hook.event,
            entry_index,
            hook.command,
            hook.match or "*",
            str(hook.context_file or ""),
        ),
    ), None


def _stable_hook_id(
    scope: str,
    source: Path | str,
    event: str,
    entry_index: int,
    command: str,
    matcher: str,
    context_file: str,
) -> str:
    payload = "\0".join((
        scope,
        str(source),
        event,
        str(entry_index),
        command,
        matcher,
        context_file,
    ))
    return f"hook-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def _timeout_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class HookRunner:
    def __init__(
        self,
        hooks: Iterable[HookConfig],
        workspace: Path,
        *,
        notifier: Callable[[HookOutcome], None] | None = None,
        spawner: Callable[[HookConfig, str], HookOutcome] | None = None,
    ) -> None:
        self.hooks = tuple(hooks)
        self.workspace = Path(workspace).resolve()
        self.notifier = notifier
        self.spawner = spawner or self._spawn

    @property
    def enabled(self) -> bool:
        return any(hook.enabled for hook in self.hooks)

    def has(self, event: str) -> bool:
        return any(hook.enabled and hook.event == event for hook in self.hooks)

    def run(self, event: str, payload: dict[str, Any] | None = None) -> HookReport:
        if event not in EVENTS:
            raise ValueError(f"unknown hook event: {event}")
        body = dict(payload or {})
        body["event"] = event
        body.setdefault("cwd", str(self.workspace))
        tool_name = str(body.get("toolName") or "")
        stdin = json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"
        outcomes: list[HookOutcome] = []
        blocked = False
        block_message = ""
        for hook in self.hooks:
            if not hook.enabled or hook.event != event or not hook_matches(hook, tool_name):
                continue
            outcome = self._read_context(hook) if hook.context_file is not None else self.spawner(hook, stdin)
            outcomes.append(outcome)
            if outcome.decision != "pass" and self.notifier is not None:
                self.notifier(outcome)
            if outcome.decision == "block":
                blocked = True
                block_message = format_hook_outcome(outcome)
                break
        return HookReport(event, tuple(outcomes), blocked, block_message)

    def pre_tool_use(self, name: str, arguments: Any) -> HookReport:
        return self.run(PRE_TOOL_USE, {"toolName": name, "toolArgs": arguments})

    def post_tool_use(self, name: str, arguments: Any, result: str) -> HookReport:
        return self.run(POST_TOOL_USE, {"toolName": name, "toolArgs": arguments, "toolResult": result})

    def permission_request(self, name: str, arguments: Any, subject: str = "") -> HookReport:
        return self.run(PERMISSION_REQUEST, {"toolName": name, "toolArgs": arguments, "subject": subject})

    def user_prompt_submit(self, prompt: str, turn: int) -> HookReport:
        return self.run(USER_PROMPT_SUBMIT, {"prompt": prompt, "turn": turn})

    def session_start(self) -> HookReport:
        return self.run(SESSION_START)

    def session_end(self) -> HookReport:
        return self.run(SESSION_END)

    def stop(self, last_assistant_text: str, turn: int) -> HookReport:
        return self.run(STOP, {"lastAssistantText": last_assistant_text, "turn": turn})

    def post_llm_call(self, reasoning: str, turn: int) -> HookReport:
        return self.run(POST_LLM_CALL, {"reasoning": reasoning, "turn": turn})

    def subagent_stop(self, last_assistant_text: str) -> HookReport:
        return self.run(SUBAGENT_STOP, {"lastAssistantText": last_assistant_text})

    def notification(self, message: str) -> HookReport:
        return self.run(NOTIFICATION, {"message": message})

    def pre_compact(self, trigger: str) -> HookReport:
        return self.run(PRE_COMPACT, {"trigger": trigger})

    def _read_context(self, hook: HookConfig) -> HookOutcome:
        start = time.monotonic()
        try:
            body = hook.context_file.read_bytes() if hook.context_file is not None else b""
            clipped, truncated = _clip_output(body)
            return HookOutcome(hook, "pass", 0, clipped, truncated=truncated, duration_ms=_elapsed_ms(start))
        except OSError as exc:
            return HookOutcome(hook, "error", stderr=str(exc), duration_ms=_elapsed_ms(start))

    def _spawn(self, hook: HookConfig, stdin: str) -> HookOutcome:
        start = time.monotonic()
        command, use_shell = _shell_command(hook.command)
        env = os.environ.copy()
        env.update(hook.env)
        cwd = str(hook.cwd or self.workspace)
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "env": env,
            "stdin": subprocess.PIPE,
        }
        if use_shell:
            kwargs["shell"] = True
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            kwargs["stdout"] = stdout_file
            kwargs["stderr"] = stderr_file
            try:
                process = popen_hidden(command, **kwargs)
            except (OSError, subprocess.SubprocessError) as exc:
                return HookOutcome(hook, "error", stderr=str(exc), duration_ms=_elapsed_ms(start))
            timed_out = False
            spawn_error = ""
            try:
                process.communicate(input=stdin.encode("utf-8"), timeout=hook.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(process)
                try:
                    process.communicate(timeout=1)
                except (subprocess.SubprocessError, OSError):
                    pass
            except (OSError, subprocess.SubprocessError) as exc:
                spawn_error = str(exc)
                _kill_process_tree(process)
            stdout, out_truncated = _read_capped_file(stdout_file)
            stderr, err_truncated = _read_capped_file(stderr_file)
            return_code = int(process.returncode) if isinstance(process.returncode, int) else -1
        decision = hook_decision(hook.event, return_code, timed_out=timed_out, spawn_error=bool(spawn_error))
        if timed_out and not stderr:
            stderr = f"hook timed out after {hook.timeout_seconds:g}s"
        if spawn_error and not stderr:
            stderr = spawn_error
        return HookOutcome(
            hook=hook,
            decision=decision,
            exit_code=return_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=out_truncated or err_truncated,
            duration_ms=_elapsed_ms(start),
        )


def hook_matches(hook: HookConfig, tool_name: str) -> bool:
    if hook.event not in TOOL_MATCH_EVENTS or hook.match in {"", "*"}:
        return True
    try:
        return re.fullmatch(hook.match, tool_name) is not None
    except re.error:
        return False


def hook_decision(event: str, exit_code: int, *, timed_out: bool = False, spawn_error: bool = False) -> str:
    if spawn_error:
        return "error"
    if timed_out:
        return "block" if event in BLOCKING_EVENTS else "warn"
    if exit_code == 0:
        return "pass"
    if exit_code == 2 and event in BLOCKING_EVENTS:
        return "block"
    return "warn"


def format_hook_outcome(outcome: HookOutcome) -> str:
    detail = outcome.stderr.strip() or outcome.stdout.strip()
    target = outcome.hook.command or f"context:{outcome.hook.context_file}"
    text = f"hook [{outcome.hook.scope}/{outcome.hook.event}] {target[:80]}: {outcome.decision}"
    return f"{text}: {detail}" if detail else text


def parse_session_start_output(stdout: str, event: str = SESSION_START) -> str:
    text = stdout.strip()
    if not text:
        return ""
    if not text.startswith("{"):
        return text if event == SESSION_START else ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return ""
    specific = parsed.get("hookSpecificOutput") if isinstance(parsed, dict) else None
    if not isinstance(specific, dict) or specific.get("hookEventName") != event:
        return ""
    context = specific.get("additionalContext")
    return context.strip() if isinstance(context, str) else ""


def _shell_command(command: str) -> tuple[str | list[str], bool]:
    if sys.platform == "win32":
        # Python's Windows shell adapter preserves nested quotes more reliably than
        # passing the whole command as the final argv item to cmd.exe. The process
        # still goes through popen_hidden, so the implicit cmd.exe cannot flash a
        # console window in the desktop build.
        return command, True
    return [os.environ.get("SHELL") or "/bin/sh", "-c", command], False


def _kill_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            run_hidden(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _clip_output(body: bytes) -> tuple[str, bool]:
    truncated = len(body) > OUTPUT_CAP_BYTES
    return body[:OUTPUT_CAP_BYTES].decode("utf-8", errors="replace").strip(), truncated


def _read_capped_file(handle: Any) -> tuple[str, bool]:
    handle.flush()
    handle.seek(0)
    return _clip_output(handle.read(OUTPUT_CAP_BYTES + 1))


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.monotonic() - start) * 1000))


def _display_path(path: Path, workspace: Path, home: Path) -> str:
    try:
        return "<workspace>/" + path.resolve().relative_to(workspace).as_posix()
    except ValueError:
        pass
    try:
        return "~/" + path.resolve().relative_to(home.parent).as_posix()
    except ValueError:
        return path.name


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
