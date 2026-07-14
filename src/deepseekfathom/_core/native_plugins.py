from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from .config import config_home


STATE_FILENAME = "native-plugins.json"
STATE_VERSION = 1
_STATE_LOCK = threading.RLock()


@dataclass(frozen=True)
class NativeCommand:
    name: str
    title: str
    description: str
    prompt: str
    mode: str = "plan"
    thinking: str = "balanced"
    handler: str = "agent"

    def to_public_dict(self, plugin: str) -> dict[str, Any]:
        return {
            "name": self.name,
            "key": f"/{self.name}",
            "title": self.title,
            "description": self.description,
            "prompt": self.prompt,
            "mode": self.mode,
            "thinking": self.thinking,
            "handler": self.handler,
            "plugin": plugin,
        }


@dataclass(frozen=True)
class NativePlugin:
    name: str
    version: str
    description: str
    commands: tuple[NativeCommand, ...]

    def to_public_dict(self, enabled: bool) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "enabled": enabled,
            "scope": "official",
            "source": "DeepSeekFathom 内置",
            "manifestKind": "native",
            "skills": 0,
            "instructions": 0,
            "hooks": 0,
            "mcpServers": 0,
            "capabilities": {"原生命令": len(self.commands)},
            "warnings": [],
            "error": None,
        }


NATIVE_PLUGINS: tuple[NativePlugin, ...] = (
    NativePlugin(
        "code-review",
        "1.0.0",
        "使用独立只读审查会话检查上一轮或当前工作区改动。",
        (
            NativeCommand(
                "review",
                "代码审查",
                "独立 AI 审查当前代码改动",
                "Review the current workspace changes. Report findings first, ordered by severity, with exact file and line references. Distinguish correctness bugs, security risks, regressions, and missing tests. Do not modify files.",
                mode="review",
                thinking="deep",
                handler="review",
            ),
        ),
    ),
    NativePlugin(
        "test-doctor",
        "1.0.0",
        "识别项目测试框架，运行最相关测试并定位失败根因。",
        (
            NativeCommand(
                "test",
                "测试诊断",
                "识别并运行项目测试",
                "Inspect this project's test setup, choose the most relevant existing test command, run it after any required approval, diagnose failures from evidence, and propose or implement the smallest correct fix requested by the user.",
                mode="agent",
                thinking="balanced",
            ),
        ),
    ),
    NativePlugin(
        "security-audit",
        "1.0.0",
        "离线检查密钥泄露、危险配置、注入风险与依赖边界。",
        (
            NativeCommand(
                "security",
                "安全审查",
                "只读安全检查",
                "Perform a read-only security audit of the current workspace. Prioritize exploitable issues, secret exposure, command or path injection, unsafe deserialization, authentication mistakes, and missing security tests. Findings first with file and line references. Do not modify files.",
                mode="review",
                thinking="deep",
            ),
        ),
    ),
    NativePlugin(
        "commit-assistant",
        "1.0.0",
        "根据真实改动生成准确的提交标题与正文，不会自动提交。",
        (
            NativeCommand(
                "commit",
                "提交助手",
                "根据改动起草提交信息",
                "Inspect the current Git status and diff, then draft one concise commit subject and an optional body that accurately describes the change and validation. Do not commit, stage, or modify files.",
                mode="plan",
                thinking="fast",
            ),
        ),
    ),
    NativePlugin(
        "release-notes",
        "1.0.0",
        "根据提交与工作区差异生成面向用户的更新记录。",
        (
            NativeCommand(
                "release-notes",
                "更新记录",
                "从提交和差异生成发布说明",
                "Inspect recent commits and the current diff, then draft user-facing release notes grouped by features, fixes, and compatibility. State versions only when verified. Do not edit or publish anything.",
                mode="plan",
                thinking="balanced",
            ),
        ),
    ),
    NativePlugin(
        "workspace-inspector",
        "1.0.0",
        "快速总结项目结构、技术栈、入口、测试与当前改动。",
        (
            NativeCommand(
                "workspace",
                "项目体检",
                "总结项目结构与当前状态",
                "Inspect the workspace and provide a compact engineering map: primary languages and frameworks, entry points, build and test commands, important modules, current Git changes, and the three highest-value next actions. Do not modify files.",
                mode="plan",
                thinking="fast",
            ),
        ),
    ),
)


def native_plugin_state_path(home: Path | None = None) -> Path:
    root = Path(home).expanduser() if home is not None else config_home()
    return root.resolve() / STATE_FILENAME


def _known_plugins() -> dict[str, NativePlugin]:
    return {plugin.name: plugin for plugin in NATIVE_PLUGINS}


def load_native_plugin_state(home: Path | None = None) -> dict[str, bool]:
    path = native_plugin_state_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("plugins", {}), dict):
        return {}
    known = _known_plugins()
    return {
        str(name): bool(enabled)
        for name, enabled in raw["plugins"].items()
        if str(name) in known and isinstance(enabled, bool)
    }


def native_plugin_enabled(name: str, home: Path | None = None) -> bool:
    if name not in _known_plugins():
        raise ValueError(f"unknown native plugin: {name}")
    return load_native_plugin_state(home).get(name, True)


def set_native_plugin_enabled(name: str, enabled: bool, home: Path | None = None) -> Path:
    if name not in _known_plugins():
        raise ValueError(f"unknown native plugin: {name}")
    path = native_plugin_state_path(home)
    with _STATE_LOCK:
        state = load_native_plugin_state(home)
        state[name] = bool(enabled)
        payload = {
            "version": STATE_VERSION,
            "plugins": {key: state[key] for key in sorted(state)},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return path


def native_plugin_entries(home: Path | None = None) -> list[dict[str, Any]]:
    state = load_native_plugin_state(home)
    return [plugin.to_public_dict(state.get(plugin.name, True)) for plugin in NATIVE_PLUGINS]


def enabled_native_commands(home: Path | None = None) -> list[dict[str, Any]]:
    state = load_native_plugin_state(home)
    commands: list[dict[str, Any]] = []
    for plugin in NATIVE_PLUGINS:
        if not state.get(plugin.name, True):
            continue
        commands.extend(command.to_public_dict(plugin.name) for command in plugin.commands)
    return commands


def resolve_native_command(name: str, home: Path | None = None) -> NativeCommand | None:
    normalized = str(name or "").strip().lower().lstrip("/")
    state = load_native_plugin_state(home)
    for plugin in NATIVE_PLUGINS:
        if not state.get(plugin.name, True):
            continue
        for command in plugin.commands:
            if command.name == normalized:
                return command
    return None


def is_native_plugin(name: str) -> bool:
    return str(name or "") in _known_plugins()
