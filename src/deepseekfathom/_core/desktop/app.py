from __future__ import annotations

from dataclasses import asdict, replace as replace_settings
import base64
import binascii
from email.message import Message as EmailMessage
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from . import DESKTOP_VERSION
from ..agent import FathomAgent, compact_context_messages, context_window_info, estimate_message_tokens, summarize_arguments
from ..capabilities import collect_capability_report
from ..config import Settings, environment_value, get_settings, merge_file_config, migrate_legacy_data
from ..extensions import (
    ExtensionRuntime,
    UserMCPServerNotFoundError,
    delete_user_mcp_server,
    get_user_mcp_server,
    save_user_mcp_server,
)
from ..hooks import SESSION_END, set_hook_enabled, trust_project
from ..mcp import MCPError, MCPHost
from ..messages import Message, clone_message
from ..native_plugins import (
    enabled_native_commands,
    is_native_plugin,
    native_plugin_entries,
    resolve_native_command,
    set_native_plugin_enabled,
)
from ..policy import ThinkingMode
from ..processes import run_hidden
from ..provider import DeepSeekClient, UsageStats, apply_thinking_payload
from ..reviews import ChangeManifest, ChangeSnapshot, ChangeSnapshotService
from ..session import Session, SessionStore
from ..skills import SkillStore
from ..tool_contracts import ToolContract, normalize_tool_schema
from ..tools import ToolResult
from ..plugins import install_local_plugin, set_plugin_enabled


ASSET_DIR = Path(__file__).resolve().parent / "assets"
MAX_BROWSER_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_NETWORK_ATTACHMENT_BYTES = 100 * 1024 * 1024
MODES = ["plan", "review", "agent", "trusted", "yolo", "root"]
# Codex-style permission tiers exposed in the desktop UI (composer.permissionsDropdown:
# read-only / default-with-approval / full access), mapped onto the internal modes.
PERMISSION_TIERS = ["plan", "agent", "root"]
PERMISSION_LABELS = {
    "plan": "只读",
    "agent": "受限",
    "root": "完全访问",
}
PERMISSION_DESCRIPTIONS = {
    "plan": "只读：可以阅读文件和回答，不写文件、不执行命令",
    "agent": "受限：危险操作（写文件 / 执行命令 / 联网）会弹出批准请求，同意后才执行",
    "root": "完全访问：不受限制地执行命令、读写文件和访问网络",
}


def coerce_permission_tier(mode: str) -> str:
    """Map any legacy internal mode onto the three Codex-style tiers."""
    if mode in PERMISSION_TIERS:
        return mode
    return {"review": "agent", "trusted": "agent", "yolo": "root"}.get(mode, "root")
# Codex-style reasoning effort tiers exposed in the desktop UI, mapped onto the
# richer internal ThinkingMode set (CLI keeps the full list).
THINKING_TIERS = ThinkingMode.user_selectable_names()
THINKING_LABELS = {
    "fast": "Low",
    "balanced": "Medium",
    "deep": "High",
    "ultra": "XHigh",
    "max": "Max",
}


def _copy_missing_user_data(source: Path, target: Path) -> None:
    """Migrate legacy install-local data without replacing anything user-owned."""
    migrate_legacy_data(source, target)


def get_desktop_settings() -> Settings:
    settings = get_settings()
    if not getattr(sys, "frozen", False) or environment_value("DEEPSEEKFATHOM_WORKSPACE", "DSTUL_WORKSPACE"):
        return settings
    user_workspace = Path.home().resolve()
    _copy_missing_user_data(settings.workspace / ".deepseekfathom", user_workspace / ".deepseekfathom")
    return replace_settings(settings, workspace=user_workspace)


def desktop_window_geometry() -> tuple[int, int, tuple[int, int]]:
    """Choose logical window dimensions that fit the current Windows DPI/work area."""
    if sys.platform != "win32":
        return 1180, 780, (920, 620)
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        dpi = int(ctypes.windll.user32.GetDpiForSystem()) or 96
        scale = max(1.0, dpi / 96.0)
        work_width = max(640, rect.right - rect.left)
        work_height = max(480, rect.bottom - rect.top)
        width = max(640, min(1180, int((work_width - 36) / scale)))
        height = max(340, min(780, int((work_height - 120) / scale)))
        return width, height, (min(760, width), min(440, height))
    except (AttributeError, OSError, ValueError):
        return 1180, 720, (760, 440)


class DesktopApi:
    def __init__(self) -> None:
        self.settings = get_desktop_settings()
        self.mode = coerce_permission_tier(self.settings.default_mode)
        self.thinking = ThinkingMode.resolve(self.settings.default_thinking)
        if self.thinking.name not in THINKING_TIERS:
            self.thinking = ThinkingMode.resolve("fast")
        self.settings = self.settings.with_runtime(
            max_tokens=self.thinking.max_tokens,
            thinking_enabled=True,
            reasoning_effort=self.thinking.reasoning_effort,
        )
        self.session: Session | None = None
        # pywebview recursively exposes public js_api attributes. Keeping the native
        # Window public makes it walk window.native and can deadlock WebView2 startup.
        self._window: Any = None
        self._lock = threading.Lock()
        self._turn_state_lock = threading.RLock()
        self._session_navigation_lock = threading.Lock()
        self._session_navigation_id = 0
        self._running = False
        self._cancel_requested = False
        self._approvals: dict[str, dict[str, Any]] = {}
        self._active_turn_session_id: str | None = None
        self._active_turn_id: str | None = None
        self._pending_turn: dict[str, Any] | None = None
        self._pending_turn_lock = threading.Lock()
        self._client_request_lock = threading.Lock()
        self._client_request_results: dict[str, dict[str, Any]] = {}
        self._abandoned_turn_ids: set[str] = set()
        self._active_client: DeepSeekClient | None = None
        self._models_cache: dict[str, tuple[float, list[str]]] = {}
        self._last_usage = UsageStats()
        self._usage_total = UsageStats()
        self._usage_by_session: dict[str, UsageStats] = {}
        self._context_by_session: dict[str, dict[str, Any]] = {}
        self._review_service = ChangeSnapshotService(self.settings.workspace)
        self._extension_lock = threading.RLock()
        self._extension_mutating = False
        self._extensions = ExtensionRuntime(self.settings.workspace)
        self._mcp_host = MCPHost(self._extensions.active_mcp_configs())
        self._mcp_autostarted = False
        self._mcp_connect_thread: threading.Thread | None = None
        self._pending_mcp_refresh = False
        self._hook_session_start_pending: set[str] = set()
        self._hook_session_end_pending: set[str] = set()

    def bind_window(self, window: Any) -> None:
        self._window = window

    def boot(self) -> dict[str, Any]:
        self._start_mcp_background()
        return {
            "version": DESKTOP_VERSION,
            "workspace": str(self.settings.workspace),
            "baseUrl": self.settings.base_url,
            "model": self.settings.model,
            "providerFormat": self.settings.provider_format,
            "mode": self.mode if self.mode in PERMISSION_TIERS else "root",
            "thinking": self.thinking.name,
            "modes": list(PERMISSION_TIERS),
            "modeLabels": PERMISSION_LABELS,
            "modeDescriptions": PERMISSION_DESCRIPTIONS,
            "thinkingModes": [t for t in THINKING_TIERS if t in ThinkingMode.names()] or ThinkingMode.names(),
            "thinkingLabels": THINKING_LABELS,
            "skills": self._skill_catalog(),
            "sessionId": self.session.session_id if self.session else None,
            "apiKeySet": bool(self.settings.api_key),
            "running": self._running,
            "autoCompact": True,
            "contextWindowTokens": self.settings.context_window_tokens,
            "compactThresholdPercent": self.settings.compact_threshold_percent,
            "requestTimeout": self.settings.request_timeout,
            "compatFormats": ["deepseek", "openai", "openai-responses", "gemini", "anthropic"],
            "formatLabels": {
                "deepseek": "DeepSeek",
                "openai": "OpenAI (Chat)",
                "openai-responses": "OpenAI (Responses·最新)",
                "gemini": "Google Gemini",
                "anthropic": "Anthropic Claude",
            },
            "context": self.context_status(),
            "nativeCommands": enabled_native_commands(self._extensions.home),
        }

    def capability_diagnostics(self) -> dict[str, Any]:
        return collect_capability_report(self.settings.workspace, mode=self.mode)

    def _skill_catalog(self) -> list[dict[str, Any]]:
        with self._extension_lock:
            roots = self._extensions.skill_roots
        return [
            asdict(skill) | {"path": str(skill.path)}
            for skill in SkillStore(self.settings.workspace, extra_roots=roots).list()
        ]

    def _start_mcp_background(self) -> None:
        with self._extension_lock:
            if self._mcp_autostarted:
                return
            self._mcp_autostarted = True
            host = self._mcp_host
            if not host.status():
                return

            def connect() -> None:
                try:
                    host.connect_all()
                except MCPError:
                    # Per-server failures remain visible through extension_status().
                    pass

            thread = threading.Thread(target=connect, name="deepseekfathom-mcp-connect", daemon=True)
            self._mcp_connect_thread = thread
            thread.start()

    def extension_status(self) -> dict[str, Any]:
        with self._extension_lock:
            report = self._extensions.diagnostics()
            live = {item["name"]: item for item in self._mcp_host.status()}
        mcp = report.get("mcp") if isinstance(report.get("mcp"), dict) else {}
        entries = mcp.get("entries") if isinstance(mcp.get("entries"), list) else []
        merged_entries: list[dict[str, Any]] = []
        for entry in entries:
            item = dict(entry) if isinstance(entry, dict) else {}
            runtime = live.get(str(item.get("name") or ""))
            if runtime:
                item.update(runtime)
            elif not item.get("active"):
                item.update({"state": "disabled", "connected": False, "message": "已禁用或尚未信任"})
            else:
                item.update({"state": "configured", "connected": False, "message": "等待连接"})
            merged_entries.append(item)
        report["mcp"] = {**mcp, "live": True, "entries": merged_entries}
        plugin_report = report.get("plugins") if isinstance(report.get("plugins"), dict) else {}
        external_plugins = plugin_report.get("entries") if isinstance(plugin_report.get("entries"), list) else []
        official_plugins = native_plugin_entries(self._extensions.home)
        report["plugins"] = {
            **plugin_report,
            "entries": [*official_plugins, *external_plugins],
        }
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        report["summary"] = {
            **summary,
            "plugins": int(summary.get("plugins") or 0) + len(official_plugins),
            "enabledPlugins": int(summary.get("enabledPlugins") or 0)
            + sum(item.get("enabled") is not False for item in official_plugins),
        }
        report["nativeCommands"] = enabled_native_commands(self._extensions.home)
        report["availableSkills"] = self._skill_catalog()
        return report

    def refresh_extensions(self) -> dict[str, Any]:
        with self._turn_state_lock:
            if self._running:
                return {"ok": False, "error": "回复生成期间不能重新加载扩展"}
            if self._extension_mutating:
                return {"ok": False, "error": "扩展正在更新，请稍后重试"}
            self._extension_mutating = True
        try:
            return self._refresh_extensions_when_idle()
        finally:
            with self._turn_state_lock:
                self._extension_mutating = False

    def _refresh_extensions_when_idle(self) -> dict[str, Any]:
        try:
            with self._extension_lock:
                self._extensions.refresh()
                replacement = MCPHost(self._extensions.active_mcp_configs())
                previous = self._mcp_host
                self._mcp_host = replacement
                self._mcp_autostarted = False
            previous.close()
            self._start_mcp_background()
            return self.extension_status()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _finish_user_mcp_mutation(self, name: str, operation: str) -> dict[str, Any]:
        extensions = self._refresh_extensions_when_idle()
        if extensions.get("ok") is not False:
            return {"ok": True, "name": name, "extensions": extensions}
        try:
            current = self.extension_status()
        except Exception:
            current = {}
        return {
            "ok": True,
            "name": name,
            "warning": f"配置已{operation}，扩展重新加载失败；重启应用后生效",
            "extensions": current,
        }

    def extension_action(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._turn_state_lock:
            if self._running:
                return {"ok": False, "error": "回复生成期间不能修改扩展"}
            if self._extension_mutating:
                return {"ok": False, "error": "扩展正在更新，请稍后重试"}
            self._extension_mutating = True
        try:
            return self._extension_action_when_idle(data)
        finally:
            with self._turn_state_lock:
                self._extension_mutating = False

    def _extension_action_when_idle(self, data: dict[str, Any]) -> dict[str, Any]:
        kind = str(data.get("kind") or "").strip().lower()
        name = str(data.get("name") or "").strip()
        action = str(data.get("action") or "").strip().lower()
        enabled = bool(data.get("enabled", action in {"enable", "connect"}))
        try:
            if kind == "mcp":
                if action == "get":
                    return {
                        "ok": True,
                        "server": get_user_mcp_server(name, self._extensions.home),
                    }
                if action == "save":
                    server = data.get("server")
                    if isinstance(server, dict):
                        server = dict(server)
                        if "name" not in server and name:
                            server["name"] = name
                    else:
                        config = data.get("config")
                        if isinstance(config, dict):
                            server = dict(config)
                            if "name" in server and name and server["name"] != name:
                                return {"ok": False, "error": "MCP 名称与配置内容冲突"}
                            server["name"] = name
                    if not isinstance(server, dict):
                        return {"ok": False, "error": "MCP 保存内容必须是对象"}
                    original_name = data.get("originalName")
                    saved = save_user_mcp_server(
                        server,
                        self._extensions.home,
                        original_name=original_name,
                    )
                    return self._finish_user_mcp_mutation(saved["name"], "保存")
                if action == "delete":
                    deleted = delete_user_mcp_server(name, self._extensions.home)
                    return self._finish_user_mcp_mutation(deleted, "删除")
                if action == "trust_project":
                    trust_project(self.settings.workspace, self._extensions.home, "mcp")
                    return self._refresh_extensions_when_idle()
                if action == "connect_all":
                    with self._extension_lock:
                        self._mcp_host.connect_all()
                elif action == "connect":
                    with self._extension_lock:
                        self._mcp_host.connect(name)
                elif action == "disconnect":
                    with self._extension_lock:
                        self._mcp_host.disconnect(name)
                elif action == "reconnect":
                    with self._extension_lock:
                        self._mcp_host.reconnect(name)
                else:
                    return {"ok": False, "error": f"不支持的 MCP 操作：{action}"}
                return {"ok": True, "extensions": self.extension_status()}

            if kind in {"plugin", "plugins"}:
                if action == "reload":
                    return self._refresh_extensions_when_idle()
                if is_native_plugin(name):
                    set_native_plugin_enabled(name, enabled, self._extensions.home)
                    return {"ok": True, "extensions": self.extension_status()}
                package = next(
                    (item for item in self._extensions.report.plugins if item.installed.name == name),
                    None,
                )
                if package is None:
                    return {"ok": False, "error": f"找不到插件：{name}"}
                if package.scope == "project" and enabled:
                    install_local_plugin(Path(package.installed.root), self._extensions.home, enabled=True)
                elif package.scope == "project":
                    return {"ok": True, "extensions": self.extension_status()}
                else:
                    set_plugin_enabled(name, enabled, self._extensions.home)
                return self._refresh_extensions_when_idle()

            if kind in {"hook", "hooks"}:
                if action == "trust_project":
                    trust_project(self.settings.workspace, self._extensions.home, "hooks")
                    return self._refresh_extensions_when_idle()
                if action == "reload":
                    return self._refresh_extensions_when_idle()
                hook = self._find_hook_action(name)
                if hook is None:
                    return {"ok": False, "error": f"找不到 Hook：{name}"}
                if hook.scope == "plugin":
                    return {"ok": False, "error": "插件 Hook 随插件启停，不能在这里单独修改"}
                if hook.scope in {"project", "global"} and hook.source is not None:
                    if hook.scope == "project" and enabled and not self._extensions.report.hooks.project_trusted:
                        return {"ok": False, "error": "请先明确授权当前项目的 Hooks，再启用单条 Hook"}
                    set_hook_enabled(
                        hook.source,
                        hook.event,
                        hook.match,
                        enabled,
                        self.settings.workspace,
                        self._extensions.home,
                        hook_id=hook.hook_id,
                    )
                else:
                    return {"ok": False, "error": "该 Hook 由扩展包管理，不能单独修改"}
                return self._refresh_extensions_when_idle()
            return {"ok": False, "error": f"不支持的扩展类型：{kind}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _find_hook_action(self, action_name: str):
        public_hooks = self._extensions.diagnostics().get("hooks", {}).get("entries", [])
        configs = self._extensions.report.hooks.hooks
        for public, hook in zip(public_hooks, configs):
            candidate = "|".join(str(value) for value in (
                public.get("event") or "",
                public.get("source") or "",
                public.get("match") or "",
            ) if value)
            if action_name in {
                str(public.get("id") or ""),
                candidate,
                str(public.get("name") or ""),
                hook.event,
            }:
                return hook
        return None

    def _mcp_tool_contracts(self) -> list[ToolContract]:
        with self._extension_lock:
            host = self._mcp_host
            definitions = host.tool_definitions()
            specs = {spec.name: spec for spec in self._extensions.mcp_specs}
        contracts: list[ToolContract] = []
        schema_budget = 512_000
        for definition in definitions:
            name = str(definition.get("name") or "")
            origin = definition.get("origin") if isinstance(definition.get("origin"), dict) else {}
            server = str(origin.get("server") or "")
            raw_name = str(origin.get("tool") or "")
            spec = specs.get(server)
            trusted = bool(spec and (
                raw_name in spec.trusted_read_only_tools
                or name in spec.trusted_read_only_tools
            ))
            schema = normalize_tool_schema(definition.get("schema"))
            schema_bytes = len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if schema_bytes > schema_budget or len(contracts) >= 128:
                continue
            schema_budget -= schema_bytes
            contracts.append(ToolContract(
                name=name,
                description=str(definition.get("description") or name)[:1000],
                schema=schema,
                handler=lambda arguments, _host=host, _name=name: mcp_result_to_tool_result(
                    _host.call_tool(_name, arguments)
                ),
                origin=f"mcp:{server}",
                read_only=bool(definition.get("read_only")),
                trusted_read_only=trusted,
            ))
        return contracts

    def _mcp_management_contracts(self) -> list[ToolContract]:
        return [
            ToolContract(
                name="list_mcp_servers",
                description=(
                    "List configured MCP servers and their connection state without exposing header or environment values."
                ),
                schema={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._list_mcp_servers_tool,
                origin="native:mcp-manager",
                read_only=True,
                trusted_read_only=True,
            ),
            ToolContract(
                name="configure_mcp_server",
                description=(
                    "Persist one user-owned MCP server after an explicit user request to configure/save/install it. "
                    "This is not the /mcp connect command. Existing entries require originalName or replace=true."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Stable user MCP server name."},
                        "transport": {"type": "string", "enum": ["http", "stdio"]},
                        "url": {"type": "string", "description": "HTTP/HTTPS Streamable MCP URL."},
                        "command": {"type": "string", "description": "Local stdio executable or command."},
                        "args": {"type": "array", "items": {"type": "string"}, "maxItems": 256},
                        "env": {"type": "object", "additionalProperties": {"type": "string"}},
                        "cwd": {"type": "string"},
                        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                        "enabled": {"type": "boolean"},
                        "originalName": {"type": "string"},
                        "replace": {"type": "boolean"},
                    },
                    "required": ["name", "transport"],
                    "additionalProperties": False,
                },
                handler=self._configure_mcp_server_tool,
                origin="native:mcp-manager",
                read_only=False,
                trusted_read_only=False,
                always_confirm=True,
            ),
        ]

    def _list_mcp_servers_tool(self, _arguments: dict[str, Any]) -> ToolResult:
        report = self.extension_status()
        mcp = report.get("mcp") if isinstance(report.get("mcp"), dict) else {}
        entries = mcp.get("entries") if isinstance(mcp.get("entries"), list) else []
        safe_entries = [{
            "name": str(item.get("name") or ""),
            "transport": str(item.get("transport") or ""),
            "connected": bool(item.get("connected")),
            "state": str(item.get("state") or ""),
            "sourceScope": str(item.get("sourceScope") or ""),
            "urlHost": str(item.get("urlHost") or ""),
            "command": str(item.get("command") or ""),
            "headerKeys": list(item.get("headerKeys") or []),
            "envKeys": list(item.get("envKeys") or []),
        } for item in entries if isinstance(item, dict)]
        return ToolResult(True, json.dumps({"servers": safe_entries}, ensure_ascii=False))

    def _configure_mcp_server_tool(self, arguments: dict[str, Any]) -> ToolResult:
        allowed = {
            "name", "transport", "url", "command", "args", "env", "cwd", "headers", "enabled",
        }
        server = {key: arguments[key] for key in allowed if key in arguments}
        name = str(server.get("name") or "").strip()
        original_name = str(arguments.get("originalName") or "").strip() or None
        replace_existing = bool(arguments.get("replace"))
        exists = False
        if name:
            try:
                get_user_mcp_server(name, self._extensions.home)
            except UserMCPServerNotFoundError:
                exists = False
            else:
                exists = True
        if exists and original_name is None and not replace_existing:
            raise ValueError(
                f'用户 MCP 服务 "{name}" 已存在；更新时必须明确提供 originalName 或 replace=true'
            )
        if exists and replace_existing and original_name is None:
            original_name = name
        saved = save_user_mcp_server(
            server,
            self._extensions.home,
            original_name=original_name,
        )
        with self._turn_state_lock:
            self._pending_mcp_refresh = True
        result = {
            "ok": True,
            "name": saved["name"],
            "transport": saved["transport"],
            "headerKeys": list(saved.get("headerKeys") or []),
            "message": "配置已安全保存；当前回复结束后加载并连接，新工具从下一轮开始可用。",
        }
        return ToolResult(True, json.dumps(result, ensure_ascii=False))

    def _apply_pending_mcp_refresh(self) -> None:
        with self._turn_state_lock:
            if not self._pending_mcp_refresh:
                return
            self._pending_mcp_refresh = False
        refreshed = self._refresh_extensions_when_idle()
        self._emit("extensions:updated", {"extensions": refreshed})

    def _shutdown_extensions(self) -> None:
        try:
            session_id = self.session.session_id if self.session else None
            if not self._is_review_session(session_id):
                self._extensions.hook_runner.run(SESSION_END, {"sessionId": session_id})
        except Exception:
            pass
        try:
            with self._extension_lock:
                self._mcp_host.close()
        except Exception:
            pass

    def _run_session_end_hook(self, session_id: str | None) -> None:
        if not session_id or self._is_review_session(session_id):
            return
        try:
            self._extensions.new_hook_runner().run(SESSION_END, {"sessionId": session_id})
        except Exception:
            pass

    def _is_review_session(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        try:
            return isinstance(SessionStore(self.settings.workspace).metadata(session_id).get("review"), dict)
        except (OSError, ValueError):
            return False

    def _transition_session_hooks(self, previous_id: str | None, next_id: str | None) -> None:
        if previous_id and previous_id != next_id:
            if self._running and self._active_turn_session_id == previous_id:
                self._hook_session_end_pending.add(previous_id)
            else:
                self._run_session_end_hook(previous_id)
        if next_id and previous_id != next_id and not self._is_review_session(next_id):
            self._hook_session_start_pending.add(next_id)

    def configure(self, data: dict[str, Any]) -> dict[str, Any]:
        config: dict[str, Any] = {}
        for source, target in {
            "apiKey": "api_key",
            "model": "model",
            "providerFormat": "provider_format",
            "defaultMode": "default_mode",
            "defaultThinking": "default_thinking",
        }.items():
            value = data.get(source)
            if isinstance(value, str) and value.strip():
                config[target] = value.strip()
        base_url = data.get("baseUrl")
        if isinstance(base_url, str):
            config["base_url"] = base_url.strip().rstrip("/")
        request_timeout = parse_float_setting(data.get("requestTimeout"), minimum=15.0, maximum=300.0)
        if request_timeout is not None:
            config["request_timeout"] = request_timeout
        # keep the currently-selected model across the reload — get_settings() would
        # otherwise reset it to the file default (deepseek-v4-flash) every time the user
        # changes provider or saves settings.
        keep_model = self.settings.model
        merge_file_config(config)
        self.settings = get_desktop_settings()
        if "model" not in config and keep_model:
            self.settings = self.settings.with_runtime(model=keep_model)
        self.mode = coerce_permission_tier(self.settings.default_mode)
        self.thinking = ThinkingMode.resolve(self.settings.default_thinking)
        if self.thinking.name not in THINKING_TIERS:
            self.thinking = ThinkingMode.resolve("fast")
        self.settings = self.settings.with_runtime(
            max_tokens=self.thinking.max_tokens,
            thinking_enabled=True,
            reasoning_effort=self.thinking.reasoning_effort,
        )
        return self.boot()

    def set_runtime(self, data: dict[str, Any]) -> dict[str, Any]:
        persisted: dict[str, Any] = {}
        mode = str(data.get("mode") or self.mode)
        if mode in MODES:
            resolved_mode = coerce_permission_tier(mode)
            if resolved_mode != self.mode:
                self.mode = resolved_mode
                persisted["default_mode"] = resolved_mode
        thinking_name = str(data.get("thinking") or self.thinking.name)
        if thinking_name in THINKING_TIERS:
            resolved_thinking = ThinkingMode.resolve(thinking_name)
            if resolved_thinking.name != self.thinking.name:
                self.thinking = resolved_thinking
                persisted["default_thinking"] = resolved_thinking.name
        model = str(data.get("model") or self.settings.model)
        previous_model = self.settings.model
        self.settings = self.settings.with_runtime(
            model=model,
            max_tokens=self.thinking.max_tokens,
            thinking_enabled=self.thinking.api_thinking,
            reasoning_effort=self.thinking.reasoning_effort,
        )
        if self.settings.model != previous_model:
            persisted["model"] = self.settings.model
        if persisted:
            merge_file_config(persisted)
        return self.boot()

    def models(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        data = data or {}
        settings_obj = self.settings
        if data:
            base = str(data.get("baseUrl") or settings_obj.base_url or "").strip().rstrip("/")
            key_value = str(data.get("apiKey") or "").strip() or settings_obj.api_key
            fmt = str(data.get("providerFormat") or settings_obj.provider_format)
            model = str(data.get("model") or settings_obj.model)
            settings_obj = replace_settings(settings_obj, base_url=base, api_key=key_value, provider_format=fmt)
            settings_obj = settings_obj.with_runtime(model=model)
        api_marker = str(hash(settings_obj.api_key or ""))
        key = "|".join([settings_obj.provider_format, settings_obj.base_url, settings_obj.model, api_marker])
        cached = self._models_cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return {"ok": True, "models": cached[1], "cached": True}
        try:
            models = DeepSeekClient(settings_obj, timeout=8.0).models()
            self._models_cache[key] = (time.monotonic(), models)
            return {"ok": True, "models": models}
        except Exception as exc:
            if cached and cached[1]:
                return {"ok": True, "models": cached[1], "cached": True, "stale": True, "error": str(exc)}
            return {"ok": False, "error": str(exc), "models": [settings_obj.model]}

    def test_connection(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Probe the endpoint with the dialog's (possibly unsaved) values, without
        persisting them. Unlike models() (a plain GET /models), this sends a real minimal
        chat request that exercises the thinking/reasoning parameter, so it verifies the
        endpoint actually completes AND that the reasoning param is accepted upstream."""
        data = data or {}
        base = str(data.get("baseUrl") or self.settings.base_url or "").strip().rstrip("/")
        key = str(data.get("apiKey") or "").strip() or self.settings.api_key
        fmt = str(data.get("providerFormat") or self.settings.provider_format)
        model = str(data.get("model") or self.settings.model)
        # carry the current thinking selection so the probe reflects real reasoning params
        probe = self.settings.with_runtime(
            model=model,
            max_tokens=1200,
            thinking_enabled=self.thinking.api_thinking,
            reasoning_effort=self.thinking.reasoning_effort,
        )
        probe = replace_settings(probe, base_url=base, api_key=key, provider_format=fmt)
        # show exactly which reasoning parameter this request will send upstream
        sample: dict[str, Any] = {"max_tokens": probe.max_tokens}
        apply_thinking_payload(sample, probe)
        reasoning_sent = {k: sample[k] for k in ("reasoning_effort", "reasoning", "thinking") if k in sample}
        if "generationConfig" in sample and sample["generationConfig"].get("thinkingConfig"):
            reasoning_sent["thinkingConfig"] = sample["generationConfig"]["thinkingConfig"]
        client = DeepSeekClient(probe, timeout=min(20.0, probe.request_timeout))
        try:
            reply = client.chat([Message("user", "连接测试：只回复 ok。")])
            return {
                "ok": True,
                "reply": (reply or "").strip()[:200],
                "model": model,
                "thinking": self.thinking.name,
                "reasoning": reasoning_sent,
                "resolved": client._base_url(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "reasoning": reasoning_sent, "resolved": client._base_url()}
        finally:
            client.close()

    def sessions(self) -> list[dict[str, Any]]:
        return SessionStore(self.settings.workspace).list()

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        title = " ".join(str(title).strip().split())[:80]
        if not title:
            return {"ok": False, "error": "empty title"}
        SessionStore(self.settings.workspace).update_metadata(session_id, title=title)
        return {"ok": True, "sessions": self.sessions()}

    def export_session(self, session_id: str = "") -> dict[str, Any]:
        """Export the visible conversation through the native Save dialog."""
        resolved_id = str(session_id or (self.session.session_id if self.session else "")).strip()
        if not resolved_id:
            return {"ok": False, "error": "当前还没有可导出的会话"}
        active_session_id = self._active_turn_session_id
        if not active_session_id and self._running and self.session is not None:
            active_session_id = self.session.session_id
        if self._running and resolved_id == active_session_id:
            return {"ok": False, "error": "当前会话正在生成，回复完成后再导出"}
        if self._window is None:
            return {"ok": False, "error": "window is not ready"}
        try:
            store = SessionStore(self.settings.workspace)
            session = store.load(resolved_id)
            metadata = store.metadata(resolved_id)
            first_user = next(
                (
                    message.content
                    for message in session.messages
                    if message.role == "user" and message.model_visible
                ),
                "",
            )
            from ..session import session_title_from_text

            title = str(metadata.get("title") or session_title_from_text(first_user))
            default_name = safe_markdown_filename(title)
            import webview

            selected = self._window.create_file_dialog(
                webview.FileDialog.SAVE,
                save_filename=default_name,
                file_types=("Markdown (*.md)",),
            ) or []
            if isinstance(selected, str):
                selected = [selected]
            if not selected:
                return {"ok": False, "cancelled": True}
            path = Path(str(selected[0])).expanduser()
            if not path.suffix:
                path = path.with_suffix(".md")
            if path.exists() and path.is_dir():
                return {"ok": False, "error": "导出目标是文件夹，请选择 Markdown 文件"}
            content = session_markdown(session, title=title)
            atomic_write_text(path, content)
            return {"ok": True, "path": str(path), "bytes": len(content.encode("utf-8"))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def pin_session(self, session_id: str, pinned: bool = True) -> dict[str, Any]:
        SessionStore(self.settings.workspace).update_metadata(session_id, pinned=bool(pinned))
        return {"ok": True, "sessions": self.sessions()}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        active_session_id = self._active_turn_session_id
        if not active_session_id and self._running and self.session is not None:
            active_session_id = self.session.session_id
        if self._running and session_id == active_session_id:
            return {"ok": False, "error": "当前会话正在生成，停止回复后再删除"}
        with self._pending_turn_lock:
            pending_session_id = str((self._pending_turn or {}).get("session_id") or "")
        if session_id == pending_session_id:
            return {"ok": False, "error": "当前会话有一条回复正在排队，停止回复后再删除"}
        SessionStore(self.settings.workspace).delete(session_id)
        self._context_by_session.pop(session_id, None)
        self._usage_by_session.pop(session_id, None)
        if self.session is not None and self.session.session_id == session_id:
            self._transition_session_hooks(session_id, None)
            self.session = None
        return {"ok": True, "sessions": self.sessions(), "context": self.context_status()}

    def resume(self, session_id: str, navigation_id: int | None = None) -> dict[str, Any]:
        loaded = SessionStore(self.settings.workspace).load(session_id)
        with self._session_navigation_lock:
            requested = self._coerce_navigation_id(navigation_id)
            if requested is not None and requested < self._session_navigation_id:
                return {"ok": False, "stale": True, "activated": False, "sessionId": loaded.session_id}
            self._session_navigation_id = requested if requested is not None else self._session_navigation_id + 1
            previous_id = self.session.session_id if self.session else None
            self._transition_session_hooks(previous_id, loaded.session_id)
            self.session = loaded
            self._restore_context_usage(session_id)
            self._restore_session_usage(session_id)
            return {
                "ok": True,
                "activated": True,
                "navigationId": self._session_navigation_id,
                "sessionId": loaded.session_id,
                "messages": serialize_messages(loaded.messages),
                "context": self.context_status(),
            }

    def new_session(self, navigation_id: int | None = None) -> dict[str, Any]:
        with self._session_navigation_lock:
            requested = self._coerce_navigation_id(navigation_id)
            if requested is not None and requested < self._session_navigation_id:
                return {"ok": False, "stale": True, "activated": False, "sessionId": self.session.session_id if self.session else None}
            self._session_navigation_id = requested if requested is not None else self._session_navigation_id + 1
            previous_id = self.session.session_id if self.session else None
            self._transition_session_hooks(previous_id, None)
            self.session = None
            return {
                "ok": True,
                "activated": True,
                "navigationId": self._session_navigation_id,
                "sessionId": None,
                "messages": [],
                "context": self.context_status(),
            }

    @staticmethod
    def _coerce_navigation_id(value: int | None) -> int | None:
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    def compact(self) -> dict[str, Any]:
        if self._running:
            return {"ok": False, "error": "回复生成期间不能压缩会话"}
        if self.session is None:
            return {"ok": False, "error": "no active session"}
        before = estimate_message_tokens(self.session.messages)
        client = DeepSeekClient(self.settings)
        self.session.messages = compact_context_messages(
            self.session.messages,
            self.settings.model,
            force=True,
            client=client,
            context_limit=self.settings.context_window_tokens,
            threshold_percent=self.settings.compact_threshold_percent,
        )
        self._record_session_usage(self.session.session_id, getattr(client, "usage", UsageStats()))
        self._context_by_session.pop(self.session.session_id, None)
        SessionStore(self.settings.workspace).update_metadata(self.session.session_id, context_usage=None)
        self.session.rewrite()
        after = estimate_message_tokens(self.session.messages)
        return {"ok": True, "before": before, "after": after, "messages": serialize_messages(self.session.messages), "context": self.context_status()}

    def context_status(self) -> dict[str, Any]:
        messages = self.session.messages if self.session else []
        info = context_window_info(self.settings.model)
        detected_limit = int(info["tokens"])
        limit = int(self.settings.context_window_tokens or detected_limit)
        threshold_percent = max(1.0, min(99.0, float(self.settings.compact_threshold_percent or 95.0)))
        threshold = int(limit * threshold_percent / 100)
        session_id = self.session.session_id if self.session else ""
        local_tokens = estimate_message_tokens(messages) if messages else 0
        snapshot = self._context_by_session.get(session_id) if session_id else None
        use_upstream = bool(snapshot and snapshot.get("model") == self.settings.model)
        baseline = int(snapshot.get("currentEstimate", local_tokens)) if use_upstream else local_tokens
        local_delta_raw = local_tokens - baseline if use_upstream else 0
        calibration = float(snapshot.get("calibration") or 1.0) if use_upstream else 1.0
        if not 0.2 <= calibration <= 5.0:
            calibration = 1.0
        local_delta = round(local_delta_raw * calibration) if use_upstream else 0
        context_tokens = max(0, int(snapshot["tokens"]) + local_delta) if use_upstream else local_tokens
        usage = snapshot.get("usage") if use_upstream else UsageStats()
        usage_quality = str(snapshot.get("quality") or "upstream") if use_upstream else "missing"
        request_tokens = int(snapshot.get("requestTokens") or getattr(usage, "input_tokens", 0)) if use_upstream else 0
        session_usage = self._usage_by_session.get(session_id, UsageStats()) if session_id else UsageStats()
        cache_hit = int(getattr(usage, "cached_input_tokens", 0))
        cache_miss = int(getattr(usage, "cache_miss_input_tokens", 0))
        if cache_miss <= 0 and cache_hit > 0 and request_tokens > cache_hit:
            cache_miss = request_tokens - cache_hit
        cache_total = cache_hit + cache_miss
        session_cache_hit = int(getattr(session_usage, "cached_input_tokens", 0))
        session_cache_miss = int(getattr(session_usage, "cache_miss_input_tokens", 0))
        if session_cache_miss <= 0 and session_cache_hit > 0 and session_usage.input_tokens > session_cache_hit:
            session_cache_miss = session_usage.input_tokens - session_cache_hit
        session_cache_total = session_cache_hit + session_cache_miss
        exact_upstream = bool(use_upstream and local_delta_raw == 0 and usage_quality == "upstream")
        known_context = use_upstream
        percent = round((context_tokens / limit * 100), 1) if known_context and limit else None
        threshold_percent_used = round((threshold / limit * 100), 1) if limit else threshold_percent
        return {
            "ok": True,
            "tokens": context_tokens if known_context else None,
            "contextTokens": context_tokens if known_context else None,
            "localVisibleTokens": local_tokens,
            "estimatedInputTokens": local_tokens,
            "estimatedOutputTokens": int(getattr(usage, "output_tokens", 0)),
            "inputTokens": request_tokens,
            "reportedInputTokens": int(getattr(usage, "input_tokens", 0)),
            "outputTokens": int(getattr(usage, "output_tokens", 0)),
            "reasoningTokens": int(getattr(usage, "reasoning_tokens", 0)),
            "cachedTokens": cache_hit,
            "cacheHitTokens": cache_hit,
            "cacheMissTokens": cache_miss,
            "cacheAvailable": cache_total > 0,
            "cachePercent": round(cache_hit / cache_total * 100, 2) if cache_total > 0 else None,
            "usageTotalTokens": int(getattr(usage, "total_tokens", 0)),
            "sessionInputTokens": int(getattr(session_usage, "input_tokens", 0)),
            "sessionOutputTokens": int(getattr(session_usage, "output_tokens", 0)),
            "sessionReasoningTokens": int(getattr(session_usage, "reasoning_tokens", 0)),
            "sessionCacheHitTokens": session_cache_hit,
            "sessionCacheMissTokens": session_cache_miss,
            "sessionCacheAvailable": session_cache_total > 0,
            "sessionCachePercent": round(session_cache_hit / session_cache_total * 100, 2) if session_cache_total > 0 else None,
            "sessionTotalTokens": int(getattr(session_usage, "total_tokens", 0)),
            "localDeltaTokens": local_delta,
            "calibrationFactor": round(calibration, 4),
            "limit": limit,
            "detectedLimit": detected_limit,
            "customLimit": bool(self.settings.context_window_tokens),
            "threshold": threshold,
            "thresholdPercent": threshold_percent_used,
            "percent": percent,
            "remainingTokens": max(0, threshold - context_tokens) if known_context else None,
            "source": "upstream" if exact_upstream else ("upstream-underreported" if usage_quality == "underreported" else ("upstream-stale" if use_upstream else ("custom" if self.settings.context_window_tokens else info.get("source", "fallback")))),
            "limitSource": "custom" if self.settings.context_window_tokens else info.get("source", "fallback"),
            "accurate": exact_upstream,
            "usageAvailable": use_upstream,
            "usageState": "current" if exact_upstream else ("underreported" if usage_quality == "underreported" else ("adjusted" if use_upstream else "missing")),
            "measure": "上游输入+输出实测" if exact_upstream else ("上游 usage 少报，按本地实际发送内容估算" if usage_quality == "underreported" else ("上次上游输入+输出 + 校准后的当前增量" if use_upstream else "上游未返回 usage，仅估算本地可见消息")),
            "model": self.settings.model,
            "sessionId": session_id or None,
            "autoCompact": True,
            "nearLimit": known_context and context_tokens >= int(threshold * 0.8),
            "needsCompact": known_context and context_tokens >= threshold,
        }

    def configure_context(self, data: dict[str, Any]) -> dict[str, Any]:
        limit_raw = data.get("contextWindowTokens")
        threshold_raw = data.get("compactThresholdPercent")
        limit = parse_int_setting(limit_raw, minimum=4_000, maximum=10_000_000)
        threshold = parse_float_setting(threshold_raw, minimum=1.0, maximum=99.0)
        current: dict[str, Any] = {}
        if limit is not None:
            current["context_window_tokens"] = limit
        elif "contextWindowTokens" in data and not str(limit_raw or "").strip():
            current["context_window_tokens"] = ""
        if threshold is not None:
            current["compact_threshold_percent"] = threshold
        elif "compactThresholdPercent" in data and not str(threshold_raw or "").strip():
            current["compact_threshold_percent"] = ""
        keep_model = self.settings.model
        merge_file_config(current)
        self.settings = get_desktop_settings().with_runtime(
            model=keep_model,
            max_tokens=self.thinking.max_tokens,
            thinking_enabled=self.thinking.api_thinking,
            reasoning_effort=self.thinking.reasoning_effort,
        )
        return {"ok": True, "context": self.context_status(), "boot": self.boot()}

    def save_upload(self, file: dict[str, Any]) -> dict[str, Any]:
        raw_name = str(file.get("name") or "upload.bin")
        name = safe_upload_name(raw_name)
        content = str(file.get("content") or "")
        media_type = ""
        if content.startswith("data:") and "," in content:
            media_type = content[5:].split(";", 1)[0]
        if "," in content:
            content = content.split(",", 1)[1]
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            return {"ok": False, "error": "附件内容不是有效的 Base64 数据"}
        if len(data) > MAX_BROWSER_UPLOAD_BYTES:
            return {"ok": False, "error": "浏览器兼容附件超过 32 MB，请使用本机文件选择器"}
        upload_dir = self.settings.workspace / ".deepseekfathom" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        path = unique_attachment_path(upload_dir, name)
        temp_path = upload_dir / f".{path.name}.part-{uuid4().hex}"
        try:
            temp_path.write_bytes(data)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        name = path.name
        result = {"ok": True, "name": name, "path": str(path), "size": len(data), "kind": "uploaded_file"}
        if "/" in raw_name.replace("\\", "/"):
            result["kind"] = "folder_file"
        if is_video_upload(name, media_type):
            frames = extract_video_frames(path)
            result["kind"] = "video"
            result["frames"] = frames
            result["frameCount"] = len(frames)
        return result

    def pick_files(self) -> dict[str, Any]:
        """Return native file paths without copying contents into app storage."""
        if self._window is None:
            return {"ok": False, "error": "window is not ready", "files": []}
        try:
            import webview

            paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True) or []
            if isinstance(paths, str):
                paths = [paths]
            return {"ok": True, "files": describe_local_paths(paths)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "files": []}

    def attach_local_paths(self, paths: list[str] | None) -> dict[str, Any]:
        return {"ok": True, "files": describe_local_paths(paths or [])}

    def download_attachment(self, data: dict[str, Any]) -> dict[str, Any]:
        """Stream a dragged web URL to disk with a hard cap to prevent OOM crashes."""
        import httpx

        url = str(data.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "error": "只支持 http/https 文件链接"}
        upload_dir = self.settings.workspace / ".deepseekfathom" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        path: Path | None = None
        temp_path: Path | None = None
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    name = network_attachment_name(url, response)
                    path = unique_attachment_path(upload_dir, name)
                    temp_path = upload_dir / f".{path.name}.part-{uuid4().hex}"
                    try:
                        declared = int(response.headers.get("content-length") or 0)
                    except (TypeError, ValueError):
                        declared = 0
                    if declared > MAX_NETWORK_ATTACHMENT_BYTES:
                        return {"ok": False, "error": "网络文件超过 100 MB，已停止下载"}
                    size = 0
                    too_large = False
                    with temp_path.open("wb") as stream:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_NETWORK_ATTACHMENT_BYTES:
                                too_large = True
                                break
                            stream.write(chunk)
                    if too_large:
                        return {"ok": False, "error": "网络文件超过 100 MB，已停止下载"}
                    os.replace(temp_path, path)
                    temp_path = None
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        if path is None or not path.is_file():
            return {"ok": False, "error": "网络附件没有生成有效文件"}
        return {
            "ok": True,
            "name": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "kind": "network_file",
            "sourceUrl": url,
        }

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        client_request_id = str(payload.get("clientRequestId") or "").strip()[:160]
        if client_request_id:
            request_key = f"send:{client_request_id}"
            with self._client_request_lock:
                previous = self._client_request_results.get(request_key)
                if previous is not None:
                    return {**previous, "duplicate": True}
                result = self._send_once(payload)
                self._client_request_results[request_key] = dict(result)
                while len(self._client_request_results) > 256:
                    self._client_request_results.pop(next(iter(self._client_request_results)))
                return result
        return self._send_once(payload)

    def record_slash_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a UI-only slash command without exposing it to the model."""

        client_request_id = str(payload.get("clientRequestId") or "").strip()[:160]
        request_key = f"slash:{client_request_id}" if client_request_id else ""
        if request_key:
            with self._client_request_lock:
                previous = self._client_request_results.get(request_key)
                if previous is not None:
                    return {**previous, "duplicate": True}
                result = self._record_slash_command_once(payload)
                self._client_request_results[request_key] = dict(result)
                while len(self._client_request_results) > 256:
                    self._client_request_results.pop(next(iter(self._client_request_results)))
                return result
        return self._record_slash_command_once(payload)

    def _record_slash_command_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = str(payload.get("command") or "").strip()
        if not command.startswith("/") or len(command) > 4096:
            return {"ok": False, "error": "invalid slash command"}
        with self._turn_state_lock:
            if self._running:
                return {"ok": False, "error": "turn already running"}
            if self.session is None:
                self.session = Session(self.settings.workspace)
            session = self.session
            session.append(Message(
                "user",
                command,
                ui_kind="command",
                display_content=command,
                model_visible=False,
            ))
            src_index = len(session.messages) - 1
        return {
            "ok": True,
            "sessionId": session.session_id,
            "srcIndex": src_index,
            "command": command,
        }

    def review_changes(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start an isolated, read-only review against one frozen change manifest."""

        data = payload if isinstance(payload, dict) else {}
        client_request_id = str(data.get("clientRequestId") or "").strip()[:160]
        request_key = f"review:{client_request_id}" if client_request_id else ""
        if request_key:
            with self._client_request_lock:
                previous = self._client_request_results.get(request_key)
                if previous is not None:
                    return {**previous, "duplicate": True}
                result = self._review_changes_once(data)
                self._client_request_results[request_key] = dict(result)
                while len(self._client_request_results) > 256:
                    self._client_request_results.pop(next(iter(self._client_request_results)))
                return result
        return self._review_changes_once(data)

    def _review_changes_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        native_command = resolve_native_command("review", self._extensions.home)
        if native_command is None or native_command.handler != "review":
            return {"ok": False, "error": "code review plugin is disabled"}
        command_text = str(payload.get("command") or "/review").strip()[:4096] or "/review"
        display_prompt = str(payload.get("displayPrompt") or command_text).strip()[:4096] or "/review"
        instructions = str(payload.get("instructions") or "").strip()[:20_000]

        with self._turn_state_lock:
            if self._running:
                return {"ok": False, "error": "turn already running"}
            if self._extension_mutating:
                return {"ok": False, "error": "extensions are updating"}
            source_session = self.session
            source_session_id = source_session.session_id if source_session is not None else None
            review_session = Session(self.settings.workspace)
            source_title = ""
            if source_session_id:
                source_title = str(
                    SessionStore(self.settings.workspace).metadata(source_session_id).get("title") or ""
                ).strip()
            SessionStore(self.settings.workspace).update_metadata(
                review_session.session_id,
                title=f"审查 · {source_title}" if source_title else "代码审查",
                review={
                    "state": "capturing",
                    "source_session_id": source_session_id,
                    "command": command_text,
                    "thinking": native_command.thinking,
                },
            )
            self._transition_session_hooks(source_session_id, review_session.session_id)
            self.session = review_session
            try:
                result = self._start_turn(
                    native_command.prompt,
                    display_prompt=display_prompt,
                    ui_kind="command",
                    review_request={
                        "source_session_id": source_session_id,
                        "command": command_text,
                        "instructions": instructions,
                        "base_prompt": native_command.prompt,
                        "thinking": native_command.thinking,
                    },
                )
            except Exception:
                self.session = source_session
                self._transition_session_hooks(review_session.session_id, source_session_id)
                raise
            if not result.get("ok"):
                self.session = source_session
                self._transition_session_hooks(review_session.session_id, source_session_id)
            return result

    def _prepare_review_context(
        self,
        session_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        target = self._review_service.capture()
        source_session_id = str(request.get("source_session_id") or "").strip() or None
        manifest = self._select_review_manifest(source_session_id, target)
        context = {
            "change_id": manifest.change_id,
            "scope": manifest.scope,
            "source_session_id": source_session_id,
            "target_hash": manifest.target_hash,
            "target_snapshot_id": target.snapshot_id,
            "thinking": str(request.get("thinking") or "deep"),
            "state": "ready",
        }
        SessionStore(self.settings.workspace).update_metadata(session_id, review=context)
        context["manifest"] = manifest
        context["prompt"] = self._review_prompt(
            manifest,
            base_prompt=str(request.get("base_prompt") or ""),
            instructions=str(request.get("instructions") or ""),
        )
        return context

    def _select_review_manifest(
        self,
        source_session_id: str | None,
        target: ChangeSnapshot,
    ) -> ChangeManifest:
        """Prefer the last turn's exact range when the workspace still matches it."""

        if source_session_id and target.supported and target.complete and target.content_hash:
            metadata = SessionStore(self.settings.workspace).metadata(source_session_id)
            records = metadata.get("review_turns") if isinstance(metadata.get("review_turns"), list) else []
            for raw in reversed(records):
                if not isinstance(raw, dict) or raw.get("after_hash") != target.content_hash:
                    continue
                change_id = str(raw.get("change_id") or "")
                if change_id:
                    try:
                        stored = self._review_service.store.load_manifest(change_id)
                    except (OSError, RuntimeError, ValueError):
                        stored = None
                    if (
                        stored is not None
                        and stored.target_hash == target.content_hash
                        and stored.repository == target.repository
                        and (stored.total_files or not stored.complete)
                    ):
                        return stored
                before_id = str(raw.get("before_snapshot_id") or "")
                after_id = str(raw.get("after_snapshot_id") or "")
                if not before_id or not after_id:
                    continue
                try:
                    before = self._review_service.store.load_snapshot(before_id)
                    after = self._review_service.store.load_snapshot(after_id)
                except (OSError, RuntimeError, ValueError):
                    continue
                if (
                    not before.supported
                    or not before.complete
                    or not after.supported
                    or not after.complete
                    or after.content_hash != target.content_hash
                    or before.repository != target.repository
                    or after.repository != target.repository
                ):
                    continue
                try:
                    candidate = self._review_service.changes_between(before, target)
                except (OSError, RuntimeError, ValueError):
                    continue
                if candidate.supported and candidate.complete and candidate.total_files:
                    return candidate
                break
        return self._review_service.changes_from_head(target)

    def _review_context_for_session(self, session_id: str | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        raw = SessionStore(self.settings.workspace).metadata(session_id).get("review")
        if not isinstance(raw, dict):
            return None
        if raw.get("state") not in {"ready", "completed", "error", "cancelled"}:
            raise RuntimeError("review session is incomplete; start a new /review")
        change_id = str(raw.get("change_id") or "")
        if not change_id:
            raise RuntimeError("review session has no frozen change manifest; start a new /review")
        try:
            manifest = self._review_service.store.load_manifest(change_id)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("frozen review artifact is no longer available; start a new /review") from exc
        context = dict(raw)
        context["manifest"] = manifest
        return context

    def _review_diff_contract(self, context: dict[str, Any]) -> ToolContract:
        manifest = context["manifest"]
        change_id = manifest.change_id
        expected_cursor: str | None = None
        context["diff_read_complete"] = (
            not manifest.supported or not manifest.complete or not manifest.files
        )

        def read_review_diff(arguments: dict[str, Any]) -> ToolResult:
            nonlocal expected_cursor
            cursor_value = arguments.get("cursor")
            try:
                cursor = None if cursor_value is None or cursor_value == "" else str(cursor_value).strip()
                if cursor != expected_cursor:
                    expected = "no cursor" if expected_cursor is None else expected_cursor
                    raise ValueError(f"review diff cursor is out of sequence; expected {expected}")
                limit = max(1024, min(64 * 1024, int(arguments.get("limit") or 64 * 1024)))
                page = self._review_service.store.read_diff(change_id, cursor=cursor, limit=limit)
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                return ToolResult(False, str(exc))
            next_cursor = page.get("nextCursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                return ToolResult(False, "review diff returned an invalid next cursor")
            expected_cursor = next_cursor
            if next_cursor is None:
                context["diff_read_complete"] = True
            return ToolResult(True, json.dumps(page, ensure_ascii=False))

        return ToolContract(
            name="read_review_diff",
            description=(
                f"Read the immutable review diff bound to changeId {change_id}. "
                f"Scope={manifest.scope}, files={manifest.total_files}. Start without a cursor, then pass "
                "nextCursor until it is null. This is the authoritative review input; never substitute live git_diff, "
                "call todo_write, or delegate the review."
            ),
            schema={
                "type": "object",
                "properties": {
                    "cursor": {"type": "string", "description": "Opaque nextCursor from the prior page."},
                    "limit": {"type": "integer", "minimum": 1024, "maximum": 65536},
                },
                "additionalProperties": False,
            },
            handler=read_review_diff,
            origin="native:code-review",
            read_only=True,
            trusted_read_only=True,
        )

    @staticmethod
    def _review_prompt(manifest: ChangeManifest, *, base_prompt: str, instructions: str) -> str:
        manifest_json = json.dumps(manifest.to_dict(), ensure_ascii=False, separators=(",", ":"))
        extra = f"\n\nAdditional user review instructions:\n{instructions}" if instructions else ""
        return (
            f"{base_prompt.strip()}\n\n"
            "Review only the immutable change manifest below. Call read_review_diff repeatedly, starting "
            "without a cursor and following nextCursor until it is null. Do not use live git_diff as the "
            "review scope. Do not call todo_write or delegate_agent. You may read workspace files only for "
            "supplemental context; if they disagree, "
            "the frozen diff is authoritative. If the manifest is unsupported or incomplete, report that "
            "limitation instead of claiming a complete review.\n\n"
            f"REVIEW_MANIFEST={manifest_json}{extra}"
        )

    def _capture_turn_snapshot(self) -> ChangeSnapshot | None:
        try:
            return self._review_service.capture()
        except (OSError, RuntimeError, ValueError):
            return None

    def _finish_turn_snapshot(
        self,
        session_id: str | None,
        turn_id: str,
        before: ChangeSnapshot | None,
        status: str,
    ) -> None:
        if not session_id or before is None:
            return
        after = self._capture_turn_snapshot()
        if after is None:
            return
        try:
            manifest = self._review_service.changes_between(before, after)
            store = SessionStore(self.settings.workspace)
            metadata = store.metadata(session_id)
            raw_records = metadata.get("review_turns") if isinstance(metadata.get("review_turns"), list) else []
            records = [item for item in raw_records if isinstance(item, dict) and item.get("turn_id") != turn_id]
            records.append({
                "turn_id": turn_id,
                "status": status,
                "before_snapshot_id": before.snapshot_id,
                "after_snapshot_id": after.snapshot_id,
                "before_hash": before.content_hash or None,
                "after_hash": after.content_hash or None,
                "change_id": manifest.change_id,
                "repository": after.repository or before.repository or None,
                "created_at": after.created_at,
            })
            store.update_metadata(session_id, review_turns=records[-64:])
        except (OSError, RuntimeError, ValueError):
            return

    def _set_review_state(
        self,
        session_id: str | None,
        state: str,
        *,
        stale_status: dict[str, Any] | None = None,
    ) -> None:
        if not session_id:
            return
        try:
            store = SessionStore(self.settings.workspace)
            review = store.metadata(session_id).get("review")
            if not isinstance(review, dict):
                return
            changes: dict[str, Any] = {**review, "state": state}
            if stale_status is not None:
                changes["stale"] = stale_status.get("stale")
                changes["stale_status"] = dict(stale_status)
            store.update_metadata(session_id, review=changes)
        except (OSError, RuntimeError, ValueError):
            return

    def _send_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        requested_display_prompt = str(payload.get("displayPrompt") or "").strip()
        display_prompt = requested_display_prompt or prompt
        ui_kind = str(payload.get("uiKind") or "").strip() or None
        native_command_name = str(payload.get("nativeCommand") or "").strip().lower().lstrip("/")[:128]
        if native_command_name:
            native_command = resolve_native_command(native_command_name, self._extensions.home)
            if native_command is None:
                return {"ok": False, "error": f"native command is disabled or unavailable: /{native_command_name}"}
            if native_command.handler != "agent":
                return {"ok": False, "error": f"native command requires its dedicated handler: /{native_command_name}"}
            user_instructions = prompt
            prompt = native_command.prompt
            if user_instructions:
                prompt += "\n\nAdditional user instructions:\n" + user_instructions
            display_prompt = requested_display_prompt or (
                f"/{native_command.name}" + (f" {user_instructions}" if user_instructions else "")
            )
            ui_kind = "command"
        goal = str(payload.get("goal") or "").strip() or None
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        images = [str(u) for u in (payload.get("images") or []) if isinstance(u, str) and u.startswith("data:")]
        non_image = [item for item in attachments if isinstance(item, dict)]
        attachment_note = format_attachment_prompt(non_image)
        if attachment_note:
            prompt += "\n\n" + attachment_note
        if images and not prompt:
            prompt = "请看这张图片。"
        if not prompt and not images:
            return {"ok": False, "error": "empty prompt"}
        with self._turn_state_lock:
            running = self._running
            cancelling = self._cancel_requested
        if running:
            if cancelling:
                return self._queue_turn_after_cancel(
                    prompt,
                    images=images,
                    goal=goal,
                    display_prompt=display_prompt,
                    ui_kind=ui_kind,
                    native_command_name=native_command_name or None,
                )
            return {"ok": False, "error": "turn already running"}
        return self._start_turn(
            prompt,
            images=images,
            goal=goal,
            display_prompt=display_prompt,
            ui_kind=ui_kind,
            native_command_name=native_command_name or None,
        )

    def _start_turn(
        self,
        prompt: str,
        images: list[str] | None = None,
        goal: str | None = None,
        restore_suffix: list[Message] | None = None,
        prepared_messages: list[Message] | None = None,
        display_prompt: str | None = None,
        ui_kind: str | None = None,
        review_request: dict[str, Any] | None = None,
        native_command_name: str | None = None,
    ) -> dict[str, Any]:
        with self._turn_state_lock:
            if self._running:
                return {"ok": False, "error": "turn already running"}
            if self._extension_mutating:
                return {"ok": False, "error": "extensions are updating"}
            self._cancel_requested = False
            self._running = True
            if self.session is None:
                self.session = Session(self.settings.workspace)
            session_id = self.session.session_id
            turn_id = uuid4().hex
            thread = threading.Thread(
                target=self._run_agent_turn,
                args=(
                    prompt,
                    images or [],
                    session_id,
                    turn_id,
                    goal,
                    restore_suffix,
                    prepared_messages,
                    display_prompt,
                    ui_kind,
                    review_request,
                    native_command_name,
                ),
                daemon=True,
            )
            try:
                thread.start()
            except Exception:
                self._running = False
                raise
        return {"ok": True, "sessionId": session_id, "turnId": turn_id}

    def _queue_turn_after_cancel(
        self,
        prompt: str,
        images: list[str] | None = None,
        goal: str | None = None,
        display_prompt: str | None = None,
        ui_kind: str | None = None,
        native_command_name: str | None = None,
    ) -> dict[str, Any]:
        with self._turn_state_lock:
            # The cancelled worker may finish between send() observing its state and
            # this method acquiring the lock. Start normally instead of stranding a
            # queued request that no worker remains to pick up.
            if not self._running:
                return self._start_turn(
                    prompt,
                    images=images,
                    goal=goal,
                    display_prompt=display_prompt,
                    ui_kind=ui_kind,
                    native_command_name=native_command_name,
                )
            if not self._cancel_requested:
                return {"ok": False, "error": "turn already running"}
            if self.session is None:
                self.session = Session(self.settings.workspace)
            session_id = self.session.session_id
            turn_id = uuid4().hex
            with self._pending_turn_lock:
                if self._pending_turn is not None:
                    return {"ok": False, "error": "已有一条回复正在等待当前取消完成"}
                self._pending_turn = {
                    "prompt": prompt,
                    "images": images or [],
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "goal": goal,
                    "display_prompt": display_prompt,
                    "ui_kind": ui_kind,
                    "native_command_name": native_command_name,
                }
        return {"ok": True, "queued": True, "sessionId": session_id, "turnId": turn_id}

    def _start_pending_turn_if_any(self) -> None:
        with self._turn_state_lock:
            with self._pending_turn_lock:
                pending = self._pending_turn
                self._pending_turn = None
            if not pending:
                self._running = False
                return
            self._cancel_requested = False
            self._running = True
        session_id = str(pending["session_id"])
        # Do not adopt the queued conversation here. The user may have switched again
        # while the cancelled worker was winding down; _run_agent_turn loads the queued
        # session by id and keeps its events scoped without changing the visible session.
        thread = threading.Thread(
            target=self._run_agent_turn,
            args=(
                str(pending["prompt"]),
                list(pending["images"]),
                session_id,
                str(pending["turn_id"]),
                pending.get("goal"),
                None,
                None,
                pending.get("display_prompt"),
                pending.get("ui_kind"),
                None,
                pending.get("native_command_name"),
            ),
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self._turn_state_lock:
                self._running = False
            raise

    @staticmethod
    def _is_real_user_message(message: Message) -> bool:
        return (
            message.role == "user"
            and message.model_visible
            and not message.content.startswith(("TOOL_RESULT", "SUBAGENT_RESULT", "USER_ANSWER"))
        )

    def _prepare_regenerated_turn(
        self, before_index: int | None = None
    ) -> tuple[str | None, list[str], list[Message], list[Message], Message | None]:
        """Prepare a replacement turn without modifying the persisted conversation.

        With before_index, target the user message at/nearest-before that transcript
        index (for retry/edit on any message); otherwise the last user message.
        Skips tool-result / subagent-result messages (which also carry role 'user').
        The clean prefix runs in an in-memory session. Only a successful replacement is
        atomically committed, so cancellation, provider errors, and process interruption
        leave the original JSONL untouched.
        """
        if self.session is None:
            return None, [], [], [], None
        messages = self.session.messages
        start = len(messages) - 1 if before_index is None else min(before_index, len(messages) - 1)
        for i in range(start, -1, -1):
            message = messages[i]
            if self._is_real_user_message(message):
                prompt = message.content
                suffix_start = len(messages)
                for j in range(i + 1, len(messages)):
                    if self._is_real_user_message(messages[j]):
                        suffix_start = j
                        break
                prefix = [clone_message(m) for m in messages[:i]]
                restore_suffix = [clone_message(m) for m in messages[suffix_start:]]
                return prompt, list(message.images), prefix, restore_suffix, clone_message(message)
        return None, [], [], [], None

    def _user_index_for(self, src_index: int | None) -> int | None:
        """Given a transcript index (any message), return the index to truncate at
        for a retry/edit: the user message at/just before src_index."""
        if self.session is None or src_index is None:
            return None
        return min(src_index, len(self.session.messages) - 1)

    def _restore_regenerated_suffix(self, session_id: str | None, suffix: list[Message] | None) -> None:
        if not session_id or not suffix:
            return
        restored = SessionStore(self.settings.workspace).load(session_id)
        restored.messages.extend(clone_message(m) for m in suffix)
        restored.rewrite()
        if self.session is not None and self.session.session_id == session_id:
            self.session = restored

    def _native_agent_command_from_display(self, value: str | None):
        match = re.match(r"^/([a-z0-9][a-z0-9-]*)(?:\s+([\s\S]*))?$", str(value or "").strip(), re.I)
        if match is None:
            return None, ""
        command = resolve_native_command(match.group(1), self._extensions.home)
        if command is None or command.handler != "agent":
            return None, ""
        return command, str(match.group(2) or "").strip()

    def retry(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Regenerate the answer for a user message (the one at srcIndex, or the last)."""
        if self._running:
            return {"ok": False, "error": "turn already running"}
        if self.session is None:
            return {"ok": False, "error": "no active session"}
        src = payload.get("srcIndex") if isinstance(payload, dict) else None
        prompt, images, prefix, restore_suffix, target = self._prepare_regenerated_turn(self._user_index_for(src))
        if prompt is None:
            return {"ok": False, "error": "no user message to retry"}
        native_command, _instructions = self._native_agent_command_from_display(
            target.display_content if target is not None and target.ui_kind == "command" else None
        )
        return self._start_turn(
            prompt,
            images=images,
            restore_suffix=restore_suffix,
            prepared_messages=prefix,
            display_prompt=target.display_content if target is not None else None,
            ui_kind=target.ui_kind if target is not None else None,
            native_command_name=native_command.name if native_command is not None else None,
        )

    def edit_resend(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Branch: replace a user message (at srcIndex, or the last) with edited text."""
        if self._running:
            return {"ok": False, "error": "turn already running"}
        if self.session is None:
            return {"ok": False, "error": "no active session"}
        text = str(payload.get("prompt") or "").strip()
        if not text:
            return {"ok": False, "error": "empty prompt"}
        old_prompt, images, prefix, restore_suffix, target = self._prepare_regenerated_turn(
            self._user_index_for(payload.get("srcIndex"))
        )
        if old_prompt is None:
            return {"ok": False, "error": "no user message to edit"}
        native_command, instructions = self._native_agent_command_from_display(
            text if target is not None and target.ui_kind == "command" else None
        )
        model_prompt = text
        if native_command is not None:
            model_prompt = native_command.prompt
            if instructions:
                model_prompt += "\n\nAdditional user instructions:\n" + instructions
        return self._start_turn(
            model_prompt,
            images=images,
            restore_suffix=restore_suffix,
            prepared_messages=prefix,
            display_prompt=text if target is not None and target.ui_kind else None,
            ui_kind=target.ui_kind if target is not None else None,
            native_command_name=native_command.name if native_command is not None else None,
        )

    def branch(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fork a new session from an assistant reply (Codex-style branch).

        History up to and including the assistant message (at srcIndex, or the latest)
        is copied into a new session; the original conversation is left untouched.
        """
        if self._running:
            return {"ok": False, "error": "turn already running"}
        if self.session is None:
            return {"ok": False, "error": "no active session"}
        messages = self.session.messages
        src = payload.get("srcIndex") if isinstance(payload, dict) else None
        if src is not None and 0 <= src < len(messages):
            idx = src
        else:
            idx = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "assistant"), None)
        if idx is None:
            return {"ok": False, "error": "no assistant message to branch from"}
        forked = Session(self.settings.workspace)
        forked.messages = [clone_message(m) for m in messages[: idx + 1]]
        forked.rewrite()
        store = SessionStore(self.settings.workspace)
        source_metadata = store.metadata(self.session.session_id)
        old_title = str(source_metadata.get("title") or "").strip()
        store.update_metadata(forked.session_id, title=(old_title + " · 分支") if old_title else "分支会话")
        copied_metadata: dict[str, Any] = {}
        review = source_metadata.get("review")
        if isinstance(review, dict):
            copied_metadata["review"] = dict(review)
        copied_turn_ids = {message.turn_id for message in forked.messages if message.turn_id}
        review_turns = source_metadata.get("review_turns")
        if isinstance(review_turns, list):
            copied_records = [
                dict(item)
                for item in review_turns
                if isinstance(item, dict) and item.get("turn_id") in copied_turn_ids
            ]
            if copied_records:
                copied_metadata["review_turns"] = copied_records[-64:]
        if copied_metadata:
            store.update_metadata(forked.session_id, **copied_metadata)
        self._transition_session_hooks(self.session.session_id, forked.session_id)
        self.session = forked
        return {
            "ok": True,
            "sessionId": forked.session_id,
            "messages": serialize_messages(forked.messages),
            "sessions": self.sessions(),
        }

    def cancel(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        requested_turn_id = str((data or {}).get("turnId") or "").strip()
        cancelled_pending: dict[str, Any] | None = None
        with self._pending_turn_lock:
            pending = self._pending_turn
            pending_turn_id = str((pending or {}).get("turn_id") or "")
            if pending is not None and (not requested_turn_id or requested_turn_id == pending_turn_id):
                cancelled_pending = pending
                self._pending_turn = None
        if cancelled_pending is not None:
            self._emit("turn:cancelled", {
                "message": "排队中的回复已取消",
                "sessionId": cancelled_pending.get("session_id"),
                "turnId": cancelled_pending.get("turn_id"),
            })
            # A turn id identifies exactly one request. Cancelling the queued request
            # must not accidentally target the older worker that is already winding down.
            if requested_turn_id:
                return {"ok": True, "running": self._running, "queuedCancelled": True}
        if not self._running:
            return {"ok": True, "running": False, "queuedCancelled": bool(cancelled_pending)}
        if requested_turn_id and self._active_turn_id and requested_turn_id != self._active_turn_id:
            return {"ok": True, "running": self._running, "ignored": True}
        self._cancel_requested = True
        cancelled_turn_id = self._active_turn_id
        if cancelled_turn_id:
            self._abandoned_turn_ids.add(cancelled_turn_id)
        # unblock any pending approval so the turn can wind down
        for pending in list(self._approvals.values()):
            pending["decision"] = False
            pending["event"].set()
        self._emit("turn:cancel", {
            "message": "正在取消当前回复；已发出的工具会等待当前调用返回。",
            "sessionId": self._active_turn_session_id,
            "turnId": self._active_turn_id,
        })
        active_client = self._active_client
        if active_client is not None:
            try:
                active_client.close()
            except Exception:
                pass
        # Keep the backend busy until the worker actually exits. A new send is queued
        # by send() while _cancel_requested is true, so two workers never write the same
        # session concurrently.
        return {"ok": True, "running": True, "cancelling": True}

    def _request_approval(self, name: str, arguments: dict[str, Any]) -> bool:
        """Blocking approval bridge: emit a request to the UI, wait for the user's
        decision. Runs on the agent worker thread; the UI resolves via resolve_approval.
        """
        if self._cancel_requested:
            return False
        request_id = uuid4().hex
        gate = threading.Event()
        self._approvals[request_id] = {"event": gate, "decision": False}
        self._emit("approval:request", {
            "id": request_id,
            "tool": name,
            "summary": approval_summary(name, arguments),
            "sessionId": self._active_turn_session_id,
            "turnId": self._active_turn_id,
        })
        # wait, but stay responsive to cancellation
        while not gate.wait(timeout=0.25):
            if self._cancel_requested:
                self._approvals.pop(request_id, None)
                return False
        pending = self._approvals.pop(request_id, None)
        return bool(pending and pending["decision"])

    def resolve_approval(self, data: dict[str, Any]) -> dict[str, Any]:
        request_id = str(data.get("id") or "")
        pending = self._approvals.get(request_id)
        if not pending:
            return {"ok": False, "error": "no pending approval"}
        pending["decision"] = bool(data.get("approved"))
        pending["event"].set()
        return {"ok": True}

    def _run_agent_turn(
        self,
        prompt: str,
        images: list[str] | None = None,
        turn_session_id: str | None = None,
        turn_id: str | None = None,
        goal: str | None = None,
        restore_suffix: list[Message] | None = None,
        prepared_messages: list[Message] | None = None,
        display_prompt: str | None = None,
        ui_kind: str | None = None,
        review_request: dict[str, Any] | None = None,
        native_command_name: str | None = None,
    ) -> None:
        with self._lock:
            turn_session_id = turn_session_id or (self.session.session_id if self.session else None)
            turn_id = turn_id or uuid4().hex
            with self._turn_state_lock:
                self._active_turn_session_id = turn_session_id
                self._active_turn_id = turn_id
            if prepared_messages is not None:
                try:
                    created_at = SessionStore(self.settings.workspace).load(str(turn_session_id)).created_at
                except (FileNotFoundError, OSError):
                    created_at = self.session.created_at if self.session and self.session.session_id == turn_session_id else ""
                turn_session = Session(
                    self.settings.workspace,
                    session_id=str(turn_session_id or uuid4()),
                    created_at=created_at or Session(self.settings.workspace).created_at,
                    messages=[clone_message(m) for m in prepared_messages],
                    persist=False,
                )
            else:
                try:
                    turn_session = SessionStore(self.settings.workspace).load(str(turn_session_id))
                except FileNotFoundError:
                    turn_session = Session(self.settings.workspace, session_id=str(turn_session_id or uuid4()))

            def persisted_transcript() -> list[dict[str, Any]]:
                try:
                    return serialize_messages(
                        SessionStore(self.settings.workspace).load(str(turn_session_id)).messages
                    )
                except (FileNotFoundError, OSError):
                    return []

            def emit_turn(event: str, payload: dict[str, Any] | None = None) -> None:
                if turn_id in self._abandoned_turn_ids and event != "turn:cancelled":
                    return
                scoped = dict(payload or {})
                scoped.setdefault("sessionId", turn_session_id)
                scoped.setdefault("turnId", turn_id)
                self._emit(event, scoped)

            def is_cancelled() -> bool:
                return turn_id in self._abandoned_turn_ids or (
                    self._cancel_requested and self._active_turn_id == turn_id
                )

            delta_parts: list[str] = []
            delta_chars = 0
            last_delta_emit = time.monotonic()
            review_context: dict[str, Any] | None = None
            review_terminal: dict[str, Any] | None = None
            review_session = review_request is not None or self._is_review_session(str(turn_session_id))
            turn_snapshot_before: ChangeSnapshot | None = None
            turn_snapshot_finished = False

            def flush_deltas() -> None:
                nonlocal delta_chars, last_delta_emit
                if not delta_parts:
                    return
                text = "".join(delta_parts)
                delta_parts.clear()
                delta_chars = 0
                last_delta_emit = time.monotonic()
                emit_turn("assistant:delta", {"text": text})

            def finish_lifecycle(status: str) -> dict[str, Any] | None:
                nonlocal review_terminal, turn_snapshot_finished
                if turn_snapshot_finished:
                    return review_terminal
                if review_session:
                    stale_status: dict[str, Any] | None = None
                    manifest = review_context.get("manifest") if review_context is not None else None
                    if isinstance(manifest, ChangeManifest):
                        try:
                            stale_status = self._review_service.stale_status(manifest)
                        except (OSError, RuntimeError, ValueError) as exc:
                            stale_status = {
                                "supported": False,
                                "complete": False,
                                "stale": None,
                                "referenceHash": manifest.target_hash or None,
                                "currentHash": None,
                                "reason": str(exc),
                            }
                        review_terminal = {
                            "changeId": manifest.change_id,
                            "scope": manifest.scope,
                            **stale_status,
                        }
                    self._set_review_state(str(turn_session_id), status, stale_status=stale_status)
                else:
                    self._finish_turn_snapshot(
                        str(turn_session_id) if turn_session_id else None,
                        turn_id,
                        turn_snapshot_before,
                        status,
                    )
                turn_snapshot_finished = True
                return review_terminal

            try:
                native_command = None
                if native_command_name:
                    native_command = resolve_native_command(native_command_name, self._extensions.home)
                    if native_command is None or native_command.handler != "agent":
                        raise RuntimeError(
                            f"native command is disabled or unavailable: /{native_command_name}"
                        )
                if review_request is not None:
                    review_context = self._prepare_review_context(str(turn_session_id), review_request)
                    prompt = str(review_context["prompt"])
                    display_prompt = display_prompt or str(review_request.get("command") or "/review")
                    ui_kind = "command"
                else:
                    review_context = self._review_context_for_session(str(turn_session_id))
                if review_context is None:
                    turn_snapshot_before = self._capture_turn_snapshot()

                try:
                    thinking_name = (
                        str(review_context.get("thinking") or "deep")
                        if review_context
                        else native_command.thinking if native_command is not None else self.thinking.name
                    )
                    active_thinking = ThinkingMode.resolve(
                        thinking_name
                    )
                except ValueError:
                    active_thinking = ThinkingMode.resolve("deep" if review_context else "fast")
                active_settings = self.settings.with_runtime(
                    max_tokens=active_thinking.max_tokens,
                    thinking_enabled=active_thinking.api_thinking,
                    reasoning_effort=active_thinking.reasoning_effort,
                )
                emit_turn("turn:start", {
                    "prompt": prompt,
                    "displayPrompt": display_prompt or prompt,
                    "uiKind": ui_kind,
                    "thinking": active_thinking.name,
                    "mode": "review" if review_context is not None else (
                        native_command.mode if native_command is not None else self.mode
                    ),
                })
                if is_cancelled():
                    raise RuntimeError("turn cancelled")

                def delta(text: str) -> None:
                    nonlocal delta_chars
                    if is_cancelled():
                        raise RuntimeError("turn cancelled")
                    if not text:
                        return
                    delta_parts.append(text)
                    delta_chars += len(text)
                    if delta_chars >= 4096 or time.monotonic() - last_delta_emit >= 0.033:
                        flush_deltas()

                def final(text: str) -> None:
                    nonlocal delta_chars
                    if is_cancelled():
                        raise RuntimeError("turn cancelled")
                    if (
                        text
                        and review_context is not None
                        and not review_context.get("diff_read_complete")
                    ):
                        raise RuntimeError(
                            "review stopped before the frozen diff was read completely"
                        )
                    if text:
                        flush_deltas()
                    else:
                        # An empty final retracts provisional prose before a tool call.
                        # Do not flash a buffered claim immediately before removing it.
                        delta_parts.clear()
                        delta_chars = 0
                    emit_turn("assistant:final", {"text": text})

                def event(text: str) -> None:
                    if is_cancelled():
                        raise RuntimeError("turn cancelled")
                    flush_deltas()
                    emit_turn("agent:event", parse_agent_event(text))

                context_tokens_hint: int | None = None
                if prepared_messages is None:
                    snapshot = self._context_by_session.get(str(turn_session_id))
                    if snapshot and snapshot.get("model") == self.settings.model:
                        local_estimate = estimate_message_tokens(turn_session.messages)
                        baseline = int(snapshot.get("currentEstimate", local_estimate))
                        calibration = float(snapshot.get("calibration") or 1.0)
                        if not 0.2 <= calibration <= 5.0:
                            calibration = 1.0
                        pending_prompt = estimate_message_tokens(
                            [Message("user", prompt, images=list(images or []))]
                        )
                        context_tokens_hint = max(
                            0,
                            int(snapshot.get("tokens") or 0)
                            + round((local_estimate - baseline) * calibration)
                            + pending_prompt,
                        )

                client = DeepSeekClient(active_settings)
                self._active_client = client
                result_session_id = turn_session_id
                try:
                    if review_context is not None:
                        skill_roots: tuple[Path, ...] = ()
                        instruction_files: tuple[Path, ...] = ()
                        hook_runner = None
                        force_session_start_hook = False
                        self._hook_session_start_pending.discard(str(turn_session_id))
                        extra_contracts = [self._review_diff_contract(review_context)]
                        runner_mode = "review"
                    else:
                        with self._extension_lock:
                            skill_roots = self._extensions.skill_roots
                            instruction_files = self._extensions.instruction_files
                            hook_runner = self._extensions.new_hook_runner()
                            force_session_start_hook = str(turn_session_id) in self._hook_session_start_pending
                            self._hook_session_start_pending.discard(str(turn_session_id))
                        extra_contracts = [*self._mcp_management_contracts(), *self._mcp_tool_contracts()]
                        runner_mode = native_command.mode if native_command is not None else self.mode
                    runner = FathomAgent(
                        active_settings,
                        mode=runner_mode,
                        thinking=active_thinking.name,
                        client=client,
                        approve=None if review_context is not None else self._request_approval,
                        context_tokens_hint=context_tokens_hint,
                        extra_tool_contracts=extra_contracts,
                        extra_skill_roots=skill_roots,
                        extra_instruction_files=instruction_files,
                        hook_runner=hook_runner,
                        force_session_start_hook=force_session_start_hook,
                    )
                    if review_context is not None:
                        review_tools = {"list_files", "read_file", "read_review_diff", "search_text"}
                        runner.tool_contracts = {
                            name: contract
                            for name, contract in runner.tool_contracts.items()
                            if name in review_tools
                        }
                        if hasattr(client, "runtime_tool_contracts"):
                            client.runtime_tool_contracts = dict(runner.tool_contracts)
                        manifest = review_context["manifest"]
                        page_rounds = sum(
                            max(1, (max(0, int(item.diff_bytes)) + 65_535) // 65_536)
                            for item in manifest.files
                        )
                        max_tool_rounds = min(72, max(4, page_rounds + 4))
                    else:
                        max_tool_rounds = None
                    result = runner.run(
                        prompt,
                        stream=True,
                        images=images or [],
                        on_delta=delta,
                        on_final=final,
                        on_event=event,
                        should_cancel=is_cancelled,
                        session=turn_session,
                        goal=None if review_context is not None else goal,
                        max_tool_rounds=max_tool_rounds,
                        require_todo=review_context is None,
                        display_prompt=display_prompt,
                        ui_kind=ui_kind,
                        turn_id=turn_id,
                    )
                    flush_deltas()
                    result_session_id = result.session_id
                finally:
                    self._last_usage = client.usage
                    self._record_session_usage(result_session_id, client.usage)
                    client.close()
                    if self._active_client is client:
                        self._active_client = None
                # Only adopt the finished turn's session if the user hasn't switched to a
                # different conversation meanwhile — otherwise the next send would land in
                # the OLD conversation (context bleeding across chats).
                current_id = self.session.session_id if self.session else None
                if prepared_messages is not None:
                    turn_session.messages.extend(
                        clone_message(m) for m in (restore_suffix or [])
                    )
                    turn_session.persist = True
                    turn_session.rewrite()
                else:
                    self._restore_regenerated_suffix(result.session_id, restore_suffix)
                finished_session = SessionStore(self.settings.workspace).load(result.session_id)
                self._record_context_usage(
                    result.session_id,
                    getattr(client, "last_usage", UsageStats()),
                    list(getattr(runner, "last_model_messages", []) or []),
                    finished_session.messages,
                )
                if current_id in (turn_session_id, result.session_id):
                    self.session = finished_session
                ensure_session_title(self.settings.workspace, finished_session)
                terminal_review = finish_lifecycle("completed")
                done_payload: dict[str, Any] = {"sessionId": result.session_id, "rounds": result.rounds}
                if terminal_review is not None:
                    done_payload["review"] = terminal_review
                emit_turn("turn:done", done_payload)
            except RuntimeError as exc:
                if str(exc) == "turn cancelled":
                    payload: dict[str, Any] = {"message": "当前回复已取消"}
                    if prepared_messages is not None:
                        payload["messages"] = persisted_transcript()
                    terminal_review = finish_lifecycle("cancelled")
                    if terminal_review is not None:
                        payload["review"] = terminal_review
                    emit_turn("turn:cancelled", payload)
                else:
                    payload = desktop_error_payload(exc)
                    if prepared_messages is not None:
                        payload["messages"] = persisted_transcript()
                    terminal_review = finish_lifecycle("error")
                    if terminal_review is not None:
                        payload["review"] = terminal_review
                    emit_turn("turn:error", payload)
            except Exception as exc:
                if is_cancelled():
                    payload = {"message": "当前回复已取消"}
                    if prepared_messages is not None:
                        payload["messages"] = persisted_transcript()
                    terminal_review = finish_lifecycle("cancelled")
                    if terminal_review is not None:
                        payload["review"] = terminal_review
                    emit_turn("turn:cancelled", payload)
                else:
                    payload = desktop_error_payload(exc)
                    if prepared_messages is not None:
                        payload["messages"] = persisted_transcript()
                    terminal_review = finish_lifecycle("error")
                    if terminal_review is not None:
                        payload["review"] = terminal_review
                    emit_turn("turn:error", payload)
            finally:
                finish_lifecycle("error")
                self._abandoned_turn_ids.discard(turn_id)
                active_finished = False
                with self._turn_state_lock:
                    if self._active_turn_id == turn_id:
                        active_finished = True
                        self._cancel_requested = False
                        self._active_turn_session_id = None
                        self._active_turn_id = None
                if active_finished:
                    if str(turn_session_id) in self._hook_session_end_pending:
                        self._hook_session_end_pending.discard(str(turn_session_id))
                        self._run_session_end_hook(str(turn_session_id))
                    # Keep _running true until this atomic handoff either claims the
                    # queued request or marks the backend idle. No second caller can
                    # slip into the gap and create another worker.
                    self._apply_pending_mcp_refresh()
                    self._start_pending_turn_if_any()

    def _record_session_usage(self, session_id: str | None, usage: UsageStats) -> None:
        if not session_id or not usage.source:
            return
        self._usage_total.merge(usage)
        bucket = self._usage_by_session.setdefault(session_id, UsageStats())
        bucket.merge(usage)
        SessionStore(self.settings.workspace).update_metadata(
            session_id,
            session_usage={
                "input_tokens": bucket.input_tokens,
                "output_tokens": bucket.output_tokens,
                "cached_input_tokens": bucket.cached_input_tokens,
                "cache_miss_input_tokens": bucket.cache_miss_input_tokens,
                "reasoning_tokens": bucket.reasoning_tokens,
                "total_tokens": bucket.total_tokens,
                "source": bucket.source,
            },
        )

    def _restore_session_usage(self, session_id: str) -> None:
        stored = SessionStore(self.settings.workspace).metadata(session_id).get("session_usage")
        if not isinstance(stored, dict):
            self._usage_by_session.pop(session_id, None)
            return
        try:
            usage = UsageStats(
                input_tokens=int(stored.get("input_tokens") or 0),
                output_tokens=int(stored.get("output_tokens") or 0),
                cached_input_tokens=int(stored.get("cached_input_tokens") or 0),
                total_tokens=int(stored.get("total_tokens") or 0),
                source=str(stored.get("source") or "upstream"),
                cache_miss_input_tokens=(
                    int(stored.get("cache_miss_input_tokens") or 0)
                    or (
                        max(0, int(stored.get("input_tokens") or 0) - int(stored.get("cached_input_tokens") or 0))
                        if int(stored.get("cached_input_tokens") or 0) > 0
                        else 0
                    )
                ),
                reasoning_tokens=int(stored.get("reasoning_tokens") or 0),
            )
        except (TypeError, ValueError):
            self._usage_by_session.pop(session_id, None)
            return
        if usage.input_tokens <= 0:
            self._usage_by_session.pop(session_id, None)
            return
        self._usage_by_session[session_id] = usage

    def _record_context_usage(
        self,
        session_id: str,
        usage: UsageStats,
        request_messages: list[Message],
        current_messages: list[Message],
    ) -> None:
        if not usage.source or usage.input_tokens <= 0:
            return
        request_estimate = estimate_message_tokens(request_messages)
        current_estimate = estimate_message_tokens(current_messages)
        underreported = bool(request_messages and usage.input_tokens < int(request_estimate * 0.8))
        request_tokens = max(usage.input_tokens, request_estimate) if underreported else usage.input_tokens
        # Match the provider's actual context snapshot: the request prompt plus the
        # completion it just produced. Local message estimates are used only for text
        # appended after that snapshot, never instead of exact completion usage.
        reconstructed_delta = max(0, request_tokens - usage.input_tokens)
        adjusted = max(0, request_tokens + usage.output_tokens, usage.total_tokens + reconstructed_delta)
        calibration = 1.0
        if not underreported and request_estimate > 0:
            candidate = request_tokens / request_estimate
            if 0.2 <= candidate <= 5.0:
                calibration = candidate
        snapshot = {
            "model": self.settings.model,
            "tokens": adjusted,
            "usage": usage,
            "currentEstimate": current_estimate,
            "requestTokens": request_tokens,
            "calibration": calibration,
            "quality": "underreported" if underreported else "upstream",
        }
        self._context_by_session[session_id] = snapshot
        SessionStore(self.settings.workspace).update_metadata(
            session_id,
            context_usage={
                "schema": 4,
                "model": self.settings.model,
                "tokens": adjusted,
                "current_estimate": current_estimate,
                "request_tokens": request_tokens,
                "calibration": calibration,
                "quality": snapshot["quality"],
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "cache_miss_input_tokens": usage.cache_miss_input_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "total_tokens": usage.total_tokens,
                "source": usage.source,
            },
        )

    def _restore_context_usage(self, session_id: str) -> None:
        stored = SessionStore(self.settings.workspace).metadata(session_id).get("context_usage")
        if not isinstance(stored, dict):
            self._context_by_session.pop(session_id, None)
            return
        # v1 snapshots treated any positive upstream number as complete context. That
        # is exactly how cache-heavy 140K requests became a fake 1.2K display. Do not
        # resurrect those snapshots. v2 is migrated by adding its exact completion
        # usage; v3 stores the full prompt+completion snapshot directly.
        schema = int(stored.get("schema") or 0)
        if schema not in (2, 3, 4):
            self._context_by_session.pop(session_id, None)
            return
        try:
            usage = UsageStats(
                input_tokens=int(stored.get("input_tokens") or 0),
                output_tokens=int(stored.get("output_tokens") or 0),
                cached_input_tokens=int(stored.get("cached_input_tokens") or 0),
                total_tokens=int(stored.get("total_tokens") or 0),
                source=str(stored.get("source") or "upstream"),
                cache_miss_input_tokens=(
                    int(stored.get("cache_miss_input_tokens") or 0)
                    or (
                        max(0, int(stored.get("input_tokens") or 0) - int(stored.get("cached_input_tokens") or 0))
                        if int(stored.get("cached_input_tokens") or 0) > 0
                        else 0
                    )
                ),
                reasoning_tokens=int(stored.get("reasoning_tokens") or 0),
            )
            tokens = int(stored.get("tokens") or 0)
            if schema == 2:
                tokens += usage.output_tokens
            current_estimate = int(stored.get("current_estimate") or 0)
            request_tokens = int(stored.get("request_tokens") or usage.input_tokens)
            calibration = float(stored.get("calibration") or 1.0)
            quality = str(stored.get("quality") or "upstream")
            model = str(stored.get("model") or "")
        except (TypeError, ValueError):
            self._context_by_session.pop(session_id, None)
            return
        if not model or tokens <= 0 or usage.input_tokens <= 0:
            self._context_by_session.pop(session_id, None)
            return
        self._context_by_session[session_id] = {
            "model": model,
            "tokens": tokens,
            "usage": usage,
            "currentEstimate": current_estimate,
            "requestTokens": request_tokens,
            "calibration": calibration if 0.2 <= calibration <= 5.0 else 1.0,
            "quality": quality,
        }

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._window is None:
            return
        data = json.dumps({"event": event, "payload": payload}, ensure_ascii=False)
        # U+2028/U+2029 are valid inside JSON strings but are line terminators in
        # JS source, which would break evaluate_js and silently drop the event.
        data = data.replace(" ", "\\u2028").replace(" ", "\\u2029")
        try:
            self._window.evaluate_js(f"window.DeepSeekDesktop.onNativeEvent({data});")
        except Exception:
            pass


def approval_summary(name: str, arguments: dict[str, Any]) -> str:
    text = summarize_arguments(arguments)
    return text[:500] if text else "（无参数）"


def desktop_error_payload(exc: BaseException) -> dict[str, str]:
    error = str(exc).strip() or exc.__class__.__name__
    summary = user_error_summary(error)
    return {"error": error, "summary": summary, "trace": traceback.format_exc(limit=8)}


def user_error_summary(error: str) -> str:
    text = " ".join((error or "").strip().split())
    if not text:
        return "运行失败，但没有返回具体错误。"
    if text.startswith("API error "):
        return "上游 API 返回错误：" + text.removeprefix("API error ").strip()
    if "上游返回的是网页而不是 API 响应" in text:
        return text
    if "timed out" in text.lower() or "timeout" in text.lower():
        return "上游 API 响应超时。可在设置中调整接口超时，或检查 Base URL 和网络。"
    if "NoneType" in text and "not subscriptable" in text:
        return "工具返回了空输出，旧版本处理空输出时崩溃。请更新到最新版本后重试。"
    return text[:500]


def parse_int_setting(value: Any, *, minimum: int, maximum: int) -> int | None:
    try:
        number = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if number < minimum or number > maximum:
        return None
    return number


def parse_float_setting(value: Any, *, minimum: float, maximum: float) -> float | None:
    try:
        number = float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    if number < minimum or number > maximum:
        return None
    return number


def mcp_result_to_tool_result(result: dict[str, Any]) -> ToolResult:
    """Convert bounded MCP content blocks into the agent's text/image result shape."""

    content = result.get("content", []) if isinstance(result, dict) else []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        content = []
    text_parts: list[str] = []
    images: list[str] = []
    image_bytes = 0
    allowed_images = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    for block in content[:100]:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
            continue
        if kind == "image" and len(images) < 4:
            mime = str(block.get("mimeType") or block.get("mime_type") or "").lower()
            data = block.get("data")
            if mime not in allowed_images or not isinstance(data, str):
                continue
            try:
                decoded_size = len(base64.b64decode(data, validate=True))
            except (binascii.Error, ValueError):
                continue
            if decoded_size > 8 * 1024 * 1024 or image_bytes + decoded_size > 20 * 1024 * 1024:
                continue
            image_bytes += decoded_size
            images.append(f"data:{mime};base64,{data}")
            continue
        if kind == "resource" and isinstance(block.get("resource"), dict):
            resource = block["resource"]
            if isinstance(resource.get("text"), str):
                text_parts.append(resource["text"])
                continue
        try:
            text_parts.append(json.dumps(block, ensure_ascii=False))
        except (TypeError, ValueError):
            continue
    output = "\n".join(part for part in text_parts if part).strip()
    if not output:
        output = "MCP 工具已完成。" if not result.get("isError") else "MCP 工具返回失败。"
    return ToolResult(not bool(result.get("isError")), output[:100_000], images=images)


def format_attachment_prompt(attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return ""
    private_files: list[str] = []
    path_files: list[str] = []
    for item in attachments:
        name = str(item.get("name") or "file")
        size = item.get("size")
        suffix = f" ({size} bytes)" if isinstance(size, int) else ""
        kind = str(item.get("kind") or "")
        path = str(item.get("path") or "")
        if kind in {"folder", "folder_file", "video", "local_file", "network_file", "uploaded_file"} and path:
            path_files.append(f"- {name}: {path}{suffix}")
        else:
            private_files.append(f"- {name}{suffix}")
    parts: list[str] = []
    if private_files:
        parts.append("附件文件（普通文件只记录名称和大小，不写入本地路径）：\n" + "\n".join(private_files))
    if path_files:
        parts.append("本机/网络附件路径：\n" + "\n".join(path_files))
        parts.append("需要读取附件时，请直接调用 read_file(path) 或 inspect_media(path)。")
    elif private_files:
        parts.append("普通文件不会把本地路径写入对话正文；如需读取内容，请让用户提供文件夹或明确路径。")
    return "\n".join(parts)


def safe_markdown_filename(title: str) -> str:
    cleaned = " ".join(str(title).strip().split())
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned).rstrip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if cleaned.split(".", 1)[0].upper() in reserved:
        cleaned = f"_{cleaned}"
    cleaned = cleaned[:80] or "DeepSeekFathom-会话"
    return cleaned if cleaned.lower().endswith(".md") else f"{cleaned}.md"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def markdown_code_block(content: str, language: str = "text") -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{content.rstrip()}\n{fence}"


def exported_tool_result(raw_output: str) -> tuple[str, str]:
    if not raw_output:
        return "无结果", "（没有执行结果）"
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return "完成", raw_output
    if not isinstance(payload, dict):
        return "完成", json.dumps(payload, ensure_ascii=False, indent=2)
    status = "成功" if payload.get("ok") is True else "失败" if payload.get("ok") is False else "完成"
    output = payload.get("output")
    if isinstance(output, str):
        return status, output or "（无输出）"
    return status, json.dumps(payload, ensure_ascii=False, indent=2)


def session_markdown(session: Session, *, title: str) -> str:
    """Render only the visible transcript; omit system prompts and raw tool protocol."""
    lines = [
        f"# {title.strip() or '未命名会话'}",
        "",
        f"- 会话 ID：`{session.session_id}`",
        f"- 创建时间：`{session.created_at}`",
        "",
        "---",
    ]
    for entry in serialize_messages(session.messages):
        role = entry.get("role")
        if role in {"user", "assistant"}:
            label = "用户" if role == "user" else "DeepSeekFathom"
            lines.extend(("", f"## {label}", ""))
            content = str(entry.get("content") or "").strip()
            if content:
                lines.append(content)
            src_index = entry.get("srcIndex")
            if isinstance(src_index, int) and 0 <= src_index < len(session.messages):
                image_count = len(session.messages[src_index].images)
                if image_count:
                    lines.extend(("", f"> 附带图片：{image_count} 张（图片二进制数据未写入导出文件）"))
            continue
        if role == "tool":
            name = str(entry.get("name") or "tool").replace("`", "")
            status, output = exported_tool_result(str(entry.get("output") or ""))
            lines.extend(("", f"### 工具 · `{name}` · {status}"))
            detail = str(entry.get("detail") or "").strip()
            if detail:
                lines.extend(("", "参数摘要：", "", markdown_code_block(detail)))
            lines.extend(("", "执行结果：", "", markdown_code_block(output)))
    lines.append("")
    return "\n".join(lines)


def serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Rebuild the desktop transcript from stored messages.

    An assistant message that is a tool call is emitted as a structured tool block
    (with the surrounding prose split out) and paired with the following TOOL_RESULT /
    SUBAGENT_RESULT so a resumed conversation shows tool cards instead of raw JSON.
    """
    from ..agent import is_tool_intro_only, parse_tool_calls, strip_tool_call_display, summarize_arguments

    visible: list[dict[str, Any]] = []
    pending_tools: list[dict[str, Any]] = []

    def finish_orphaned_tools() -> None:
        while pending_tools:
            pending_tool = pending_tools.pop(0)
            pending_tool["output"] = json.dumps(
                {"ok": False, "output": "没有执行结果，已按未执行处理。"},
                ensure_ascii=False,
            )
            pending_tool["orphaned"] = True

    def presentation(message: Message) -> dict[str, Any]:
        result: dict[str, Any] = {
            "modelVisible": message.model_visible,
        }
        if message.ui_kind is not None:
            result["uiKind"] = message.ui_kind
        if message.display_content is not None:
            result["displayContent"] = message.display_content
        if message.turn_id is not None:
            result["turnId"] = message.turn_id
        return result

    for idx, message in enumerate(messages):
        content = message.content
        if content.startswith(("TOOL_RESULT", "SUBAGENT_RESULT", "USER_ANSWER")):
            if pending_tools:
                _, _, body = content.partition("\n")
                pending_tools.pop(0)["output"] = body
            continue
        finish_orphaned_tools()
        if message.role not in {"user", "assistant"}:
            continue
        if message.role == "assistant":
            tool_calls = parse_tool_calls(content)
            if tool_calls:
                prose = strip_tool_call_display(content)
                if prose and not is_tool_intro_only(prose):
                    # pre-tool narration — not a standalone reply, carries no retry/branch
                    visible.append({
                        "role": "assistant",
                        "content": prose,
                        "srcIndex": idx,
                        "intermediate": True,
                        **presentation(message),
                    })
                for name, arguments in tool_calls:
                    pending_tool = {
                        "role": "tool",
                        "name": name,
                        "detail": summarize_arguments(arguments),
                        "output": "",
                        "srcIndex": idx,
                        **presentation(message),
                    }
                    pending_tools.append(pending_tool)
                    visible.append(pending_tool)
                continue
        visible.append({
            "role": message.role,
            "content": message.display_content if message.display_content is not None else content,
            "srcIndex": idx,
            **presentation(message),
        })
    finish_orphaned_tools()

    # Historical versions could persist an internal recovery answer immediately after
    # an already-complete reply. Preserve the JSONL on disk, but suppress that adjacent
    # duplicate during replay so reopening the conversation does not answer twice.
    deduplicated: list[dict[str, Any]] = []
    for entry in visible:
        if (
            entry.get("role") == "assistant"
            and deduplicated
            and deduplicated[-1].get("role") == "assistant"
        ):
            continue
        deduplicated.append(entry)
    visible = deduplicated

    # One real user turn owns one actionable assistant reply. Tool-separated narration
    # remains visible but only the final reply receives retry/branch controls.
    turn_assistants: list[dict[str, Any]] = []
    for entry in visible:
        if entry.get("role") == "user":
            for prior in turn_assistants[:-1]:
                prior["intermediate"] = True
            turn_assistants = []
        elif entry.get("role") == "assistant":
            turn_assistants.append(entry)
    for prior in turn_assistants[:-1]:
        prior["intermediate"] = True
    return visible


def estimate_cached_context_tokens(messages: list[Message]) -> int:
    if not messages:
        return 0
    cacheable: list[Message] = []
    for message in messages:
        if message.role == "system":
            cacheable.append(message)
            continue
        if message.content.startswith(("TOOL_RESULT", "SUBAGENT_RESULT")):
            continue
        if message.role in {"user", "assistant"} and len(message.content) >= 800:
            cacheable.append(message)
    # Recent messages are likely to stay stable across immediate retries/tool rounds.
    cacheable.extend(m for m in messages[-12:] if m not in cacheable and m.role in {"user", "assistant"})
    return min(estimate_message_tokens(cacheable), estimate_message_tokens(messages))


def ensure_session_title(workspace: Path, session: Session) -> None:
    store = SessionStore(workspace)
    meta = store.metadata(session.session_id)
    first_user = next(
        (
            message.content
            for message in session.messages
            if message.role == "user" and message.model_visible
        ),
        "",
    )
    from ..session import session_title_from_text

    changes: dict[str, Any] = {
        "created_at": session.created_at,
        "message_count": len(session.messages),
    }
    if not meta.get("title") and first_user:
        changes["title"] = session_title_from_text(first_user)
    store.update_metadata(session.session_id, **changes)


def parse_agent_event(text: str) -> dict[str, str]:
    if text.startswith("hook "):
        encoded = text.removeprefix("hook ").strip()
        try:
            payload = json.loads(base64.b64decode(encoded).decode("utf-8", "replace"))
        except (ValueError, json.JSONDecodeError):
            payload = {}
        event = str(payload.get("event") or "Hook")
        decision = str(payload.get("decision") or "pass")
        scope = str(payload.get("scope") or "")
        detail = str(payload.get("message") or "")
        duration = payload.get("durationMs")
        suffix = f"{scope} · {decision}" if scope else decision
        if isinstance(duration, int):
            suffix += f" · {duration} ms"
        if detail:
            suffix += f"\n{detail}"
        return {"kind": "hook", "name": event, "detail": suffix}
    if text.startswith("tool "):
        rest = text.removeprefix("tool ").strip()
        name, _, args = rest.partition(" ")
        return {"kind": "tool", "name": name, "detail": args}
    if text.startswith("done "):
        rest = text.removeprefix("done ").strip()
        name, _, b64 = rest.partition(" ")
        output = ""
        if b64:
            try:
                output = base64.b64decode(b64).decode("utf-8", "replace")
            except Exception:
                output = ""
        return {"kind": "done", "name": name, "detail": output}
    if text.startswith("todo "):
        b64 = text.removeprefix("todo ").strip()
        output = ""
        if b64:
            try:
                output = base64.b64decode(b64).decode("utf-8", "replace")
            except Exception:
                output = ""
        return {"kind": "todo", "name": "todo_write", "detail": output}
    if text.startswith("media "):
        rest = text.removeprefix("media ").strip()
        name, _, b64 = rest.partition(" ")
        output = ""
        if b64:
            try:
                output = base64.b64decode(b64).decode("utf-8", "replace")
            except Exception:
                output = ""
        return {"kind": "media", "name": name or "media", "detail": output}
    if text.startswith("thinking pass "):
        return {"kind": "thinking", "name": "internal", "detail": text}
    if text.startswith("thinkingnote "):
        rest = text.removeprefix("thinkingnote ").strip()
        label, _, b64 = rest.partition(" ")
        note = ""
        if b64:
            try:
                note = base64.b64decode(b64).decode("utf-8", "replace")
            except Exception:
                note = ""
        return {"kind": "thinking", "name": f"内部思考 {label}", "detail": note}
    if text.startswith("subanswer "):
        rest = text.removeprefix("subanswer ").strip()
        note = ""
        if rest:
            try:
                note = base64.b64decode(rest).decode("utf-8", "replace")
            except Exception:
                note = ""
        return {"kind": "subanswer", "name": "", "detail": note}
    if text.startswith("subevent "):
        rest = text.removeprefix("subevent ")
        sub_name, _, inner = rest.partition("␟")
        inner_event = parse_agent_event(inner)
        inner_event["sub"] = sub_name
        return inner_event
    if text.startswith("subagentdone "):
        rest = text.removeprefix("subagentdone ")
        name, _, tail = rest.partition("␟")
        summary = ""
        if tail:
            # tail is "rounds=N␟<base64 summary>"; take the last ␟-separated field
            b64 = tail.rpartition("␟")[2].strip()
            if b64:
                try:
                    summary = base64.b64decode(b64).decode("utf-8", "replace")
                except Exception:
                    summary = ""
        return {"kind": "subagentdone", "name": name.strip(), "detail": summary}
    if text == "toolpending":
        return {"kind": "toolpending", "name": "", "detail": ""}
    if text.startswith("subagent "):
        return {"kind": "subagent", "name": "subagent", "detail": text}
    if text.startswith("skill "):
        return {"kind": "skill", "name": "skill", "detail": text}
    if text.startswith("context compacted"):
        return {"kind": "compact", "name": "context", "detail": text}
    return {"kind": "event", "name": "agent", "detail": text}


def safe_upload_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    cleaned = "".join(ch if ch.isalnum() or ch in {".", "-", "_", " "} else "_" for ch in name).strip()
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip(". ")
    return cleaned[:120] or "upload.bin"


def unique_attachment_path(directory: Path, name: str) -> Path:
    candidate = directory / safe_upload_name(name)
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 10_000):
        numbered = directory / f"{stem} ({index}){suffix}"
        if not numbered.exists():
            return numbered
    return directory / f"{stem} ({uuid4().hex[:8]}){suffix}"


def network_attachment_name(url: str, response: Any) -> str:
    disposition = str(response.headers.get("content-disposition") or "")
    if disposition:
        message = EmailMessage()
        message["content-disposition"] = disposition
        filename = message.get_filename()
        if filename:
            return safe_upload_name(str(filename))

    candidates = [url]
    final_url = str(getattr(response, "url", "") or "")
    if final_url and final_url != url:
        candidates.append(final_url)
    for candidate in candidates:
        parsed = urlparse(candidate)
        filename = unquote(Path(parsed.path).name)
        if filename:
            return safe_upload_name(filename)

    media_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    extension = mimetypes.guess_extension(media_type) or ".bin"
    return safe_upload_name("network-download" + extension)


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg"}


def is_video_upload(name: str, media_type: str = "") -> bool:
    guessed = mimetypes.guess_type(name)[0] or ""
    return media_type.startswith("video/") or guessed.startswith("video/") or Path(name).suffix.lower() in VIDEO_EXTENSIONS


def describe_local_paths(paths: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(str(raw)).expanduser()
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                continue
            size = resolved.stat().st_size
        except OSError:
            continue
        kind = "video" if is_video_upload(resolved.name) else "local_file"
        files.append({"ok": True, "name": resolved.name, "path": str(resolved), "size": size, "kind": kind})
    return files


def native_drop_paths(event: dict[str, Any] | None) -> list[str]:
    """Extract WebView2 file paths populated by pywebview's native drop bridge."""
    if not isinstance(event, dict):
        return []
    transfer = event.get("dataTransfer")
    if not isinstance(transfer, dict):
        return []
    paths: list[str] = []
    for item in transfer.get("files") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("pywebviewFullPath") or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def bind_native_file_drop(window: Any, api: DesktopApi) -> None:
    """Use pywebview's WebView2 bridge so an Explorer drop keeps its real path."""
    def on_loaded() -> None:
        try:
            compose = window.dom.get_element(".composeCard")
            if compose is None:
                return

            def on_drop(event: dict[str, Any]) -> None:
                files = describe_local_paths(native_drop_paths(event))
                if files:
                    api._emit("native:drop", {"files": files})

            compose.events.drop += on_drop
        except Exception:
            # Browser content upload remains available when a backend has no paths.
            return

    window.events.loaded += on_loaded


def video_duration_seconds(path: Path, timeout: int = 8) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = run_hidden(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        duration = float(completed.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def extract_video_frames(path: Path, *, max_frames: int = 6, timeout: int = 20) -> list[str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    duration = video_duration_seconds(path) or 0
    if duration > 0:
        count = max(1, min(max_frames, int(duration) if duration >= 1 else 1))
        timestamps = [duration * (i + 1) / (count + 1) for i in range(count)]
    else:
        timestamps = [0]
    frames: list[str] = []
    frame_dir = path.parent / f".{path.stem}_frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    for index, timestamp in enumerate(timestamps, 1):
        out = frame_dir / f"frame_{index:02d}.jpg"
        try:
            completed = run_hidden(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(768,iw)':-2",
                    "-q:v",
                    "4",
                    str(out),
                ],
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0 or not out.exists():
            continue
        data = out.read_bytes()
        if data:
            frames.append("data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"))
    return frames


_DESKTOP_MUTEX_NAME = r"Local\DeepSeekFathom.Desktop"
_ERROR_ALREADY_EXISTS = 183


def focus_existing_desktop_window() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        find_window = user32.FindWindowW
        find_window.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        find_window.restype = ctypes.c_void_p
        show_window = user32.ShowWindow
        show_window.argtypes = [ctypes.c_void_p, ctypes.c_int]
        show_window.restype = ctypes.c_bool
        set_foreground = user32.SetForegroundWindow
        set_foreground.argtypes = [ctypes.c_void_p]
        set_foreground.restype = ctypes.c_bool
        hwnd = find_window(None, "DeepSeekFathom")
        if hwnd:
            show_window(hwnd, 9)  # SW_RESTORE
            set_foreground(hwnd)
    except (AttributeError, OSError, ValueError):
        return


def acquire_desktop_instance() -> tuple[bool, Any]:
    """Allow one desktop process per Windows login session and focus the first one."""
    if sys.platform != "win32":
        return True, None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = create_mutex(None, False, _DESKTOP_MUTEX_NAME)
        if not handle:
            return True, None
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            close_handle(handle)
            focus_existing_desktop_window()
            return False, None
        return True, (kernel32, handle)
    except (AttributeError, OSError, ValueError):
        # Do not make the app unusable on an unusual Windows runtime; atomic session
        # and config writes remain the second line of defence.
        return True, None


def release_desktop_instance(instance: Any) -> None:
    if not instance:
        return
    kernel32, handle = instance
    try:
        kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        pass


def main() -> None:
    acquired, instance = acquire_desktop_instance()
    if not acquired:
        return
    api: DesktopApi | None = None
    try:
        try:
            import webview
        except ImportError as exc:
            raise SystemExit(
                "桌面端需要 pywebview。安装：py -3 -m pip install --upgrade pywebview"
            ) from exc

        api = DesktopApi()
        width, height, min_size = desktop_window_geometry()
        window = webview.create_window(
            "DeepSeekFathom",
            str(ASSET_DIR / "index.html"),
            js_api=api,
            width=width,
            height=height,
            min_size=min_size,
            text_select=True,
        )
        api.bind_window(window)
        bind_native_file_drop(window, api)
        # Try common GUI backends in turn so a missing/broken default backend gives a clear,
        # actionable message instead of an opaque crash. Any failure skips to the next backend
        # (a raised-on-first-error loop showed up as "crashes twice then works").
        last_error: Exception | None = None
        for kwargs in ({}, {"gui": "edgechromium"}, {"gui": "qt"}, {"gui": "gtk"}):
            try:
                webview.start(debug=False, **kwargs)
                return
            except Exception as exc:  # backend unavailable/broken — try the next one
                last_error = exc
                continue
        raise SystemExit(
            "找不到可用的界面后端。Windows 请安装 Microsoft Edge WebView2 运行时；"
            "Linux 请安装 gtk（python3-gi / gir1.2-webkit2）或 Qt（pip install pyqt6 qtpy）。"
            + (f"\n最后一个错误：{last_error}" if last_error else "")
        )
    finally:
        if api is not None:
            api._shutdown_extensions()
        release_desktop_instance(instance)


if __name__ == "__main__":
    main()
