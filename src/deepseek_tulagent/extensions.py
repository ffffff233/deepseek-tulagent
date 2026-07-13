from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Iterable
from urllib.parse import urlsplit
from uuid import uuid4

from .config import _CONFIG_LOCK
from .hooks import HookInspection, inspect_hooks, is_project_trusted
from .plugins import (
    PluginMCPServer,
    PluginPackage,
    PluginStateError,
    VALID_NAME,
    discover_installed_plugins,
    discover_project_plugins,
    extension_home,
)


PROJECT_MCP_FILENAME = ".mcp.json"
MAX_MCP_CONFIG_BYTES = 1_000_000
MAX_USER_MCP_ENTRY_BYTES = 128 * 1024
MAX_USER_MCP_SERVERS = 128
MAX_MCP_COLLECTION_ITEMS = 256
MAX_MCP_TEXT_CHARS = 16_384
MAX_MCP_PATH_CHARS = 4_096
MAX_MCP_URL_CHARS = 4_096
MAX_MCP_TIMEOUT_MS = 86_400_000
_DANGEROUS_OBJECT_KEYS = frozenset({"__proto__", "prototype", "constructor"})
_RESERVED_MCP_HEADERS = frozenset({
    "accept",
    "connection",
    "content-length",
    "content-type",
    "host",
    "mcp-protocol-version",
    "mcp-session-id",
    "transfer-encoding",
})
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_USER_MCP_FIELDS = frozenset({
    "name",
    "transport",
    "type",
    "command",
    "args",
    "env",
    "cwd",
    "url",
    "headers",
    "enabled",
    "auto_start",
    "startup_timeout_ms",
    "startupTimeoutMs",
    "call_timeout_ms",
    "callTimeoutMs",
    "tool_timeout_ms",
    "toolTimeoutMs",
    "trusted_read_only_tools",
    "trustedReadOnlyTools",
})


class UserMCPConfigError(ValueError):
    """A safe-to-display validation error for user-managed MCP configuration."""


@dataclass(frozen=True)
class ExtensionIssue:
    severity: str
    code: str
    subsystem: str
    name: str
    source: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "subsystem": self.subsystem,
            "name": self.name,
            "source": self.source,
            "message": self.message,
        }


@dataclass(frozen=True)
class MCPServerSpec:
    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    trusted: bool = True
    source_scope: str = "global"
    source_path: Path | None = None
    plugin: str = ""
    startup_timeout_ms: int = 5_000
    call_timeout_ms: int = 300_000
    tool_timeout_ms: dict[str, int] = field(default_factory=dict)
    trusted_read_only_tools: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.enabled and self.trusted

    def to_public_dict(self, workspace: Path, home: Path) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": _command_display(self.command),
            "argsCount": len(self.args),
            "envKeys": sorted(self.env),
            "cwd": _display_path(self.cwd, workspace, home),
            "urlHost": _url_host(self.url),
            "headerKeys": sorted(self.headers),
            "enabled": self.enabled,
            "trusted": self.trusted,
            "active": self.active,
            "sourceScope": self.source_scope,
            "source": _display_path(self.source_path, workspace, home),
            "plugin": self.plugin or None,
            "startupTimeoutMs": self.startup_timeout_ms,
            "callTimeoutMs": self.call_timeout_ms,
            "toolTimeouts": sorted(self.tool_timeout_ms),
            "trustedReadOnlyTools": list(self.trusted_read_only_tools),
        }

    def to_runtime_config(self, workspace: Path) -> Any:
        """Build a runtime config without starting the server."""

        if not self.active:
            raise ValueError(f"MCP server is disabled or untrusted: {self.name}")
        from .mcp import MCPServerConfig

        return MCPServerConfig(
            name=self.name,
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd or Path(workspace).resolve(),
            startup_timeout=self.startup_timeout_ms / 1000.0,
            call_timeout=self.call_timeout_ms / 1000.0,
            tool_timeouts={name: timeout / 1000.0 for name, timeout in self.tool_timeout_ms.items()},
            transport=self.transport,
            url=self.url,
            headers=self.headers if self.transport != "stdio" else {},
        )


@dataclass(frozen=True)
class ExtensionReport:
    workspace: Path
    home: Path
    plugins: tuple[PluginPackage, ...]
    mcp_servers: tuple[MCPServerSpec, ...]
    hooks: HookInspection
    issues: tuple[ExtensionIssue, ...]
    static: bool = True

    @property
    def skill_roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        for package in self.plugins:
            if package.installed.enabled and package.manifest is not None:
                roots.extend(package.manifest.skills)
        return tuple(dict.fromkeys(roots))

    @property
    def instruction_files(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for package in self.plugins:
            if package.installed.enabled and package.manifest is not None:
                paths.extend(package.manifest.instructions)
        return tuple(dict.fromkeys(paths))

    def to_dict(self) -> dict[str, Any]:
        plugin_entries: list[dict[str, Any]] = []
        for package in self.plugins:
            manifest = package.manifest
            plugin_entries.append({
                "name": package.installed.name,
                "version": package.installed.version or (manifest.version if manifest else ""),
                "enabled": package.installed.enabled,
                "scope": package.scope,
                "source": _source_display(package.installed.source, self.workspace, self.home),
                "root": _display_path(Path(package.installed.root), self.workspace, self.home),
                "manifestKind": manifest.manifest_kind if manifest else package.installed.manifest_kind,
                "skills": len(manifest.skills) if manifest else 0,
                "instructions": len(manifest.instructions) if manifest else 0,
                "hooks": len(manifest.hooks) if manifest else 0,
                "mcpServers": len(manifest.mcp_servers) if manifest else 0,
                "error": _sanitize_public_text(package.error, self.workspace, self.home) if package.error else None,
                "warnings": [_sanitize_public_text(item, self.workspace, self.home) for item in manifest.warnings] if manifest else [],
            })
        hook_entries = [{
            "id": hook.hook_id,
            "event": hook.event,
            "scope": hook.scope,
            "source": _display_path(hook.source, self.workspace, self.home),
            "match": hook.match,
            "timeoutMs": hook.timeout_ms,
            "enabled": hook.enabled,
            "plugin": hook.plugin or None,
            "command": _command_display(hook.command),
            "contextFile": _display_path(hook.context_file, self.workspace, self.home) if hook.context_file else None,
            "envKeys": sorted(hook.env),
        } for hook in self.hooks.hooks]
        issues = list(self.issues)
        issues.extend(ExtensionIssue(
            issue.severity,
            issue.code,
            "hooks",
            issue.event or "hooks",
            issue.source,
            issue.message,
        ) for issue in self.hooks.issues)
        issues.sort(key=lambda item: ({"error": 0, "warning": 1, "info": 2}.get(item.severity, 3), item.code, item.name))
        return {
            "schemaVersion": 1,
            "static": True,
            "root": "<workspace>",
            "summary": {
                "plugins": len(self.plugins),
                "enabledPlugins": sum(package.installed.enabled and package.manifest is not None for package in self.plugins),
                "mcpServers": len(self.mcp_servers),
                "activeMcpServers": sum(server.active for server in self.mcp_servers),
                "hooks": len(self.hooks.hooks),
                "activeHooks": len(self.hooks.active),
                "errors": sum(issue.severity == "error" for issue in issues),
                "warnings": sum(issue.severity == "warning" for issue in issues),
            },
            "plugins": {"supported": True, "entries": plugin_entries},
            "mcp": {
                "supported": True,
                "live": False,
                "projectDefined": (self.workspace / PROJECT_MCP_FILENAME).is_file(),
                "projectTrusted": is_project_trusted(self.workspace, self.home, "mcp"),
                "entries": [server.to_public_dict(self.workspace, self.home) for server in self.mcp_servers],
            },
            "hooks": {
                "supported": True,
                "projectDefined": self.hooks.project_defined,
                "projectTrusted": self.hooks.project_trusted,
                "entries": hook_entries,
            },
            "skillRoots": [_display_path(path, self.workspace, self.home) for path in self.skill_roots],
            "instructionFiles": [_display_path(path, self.workspace, self.home) for path in self.instruction_files],
            "issues": [_public_issue(issue, self.workspace, self.home) for issue in issues],
        }


class ExtensionRuntime:
    """Workspace extension catalog with explicit, side-effect-free refresh.

    Construction and diagnostics only read configuration. ``build_mcp_host``
    creates a host object but still does not connect it; callers must make the
    separately authorized ``connect`` or ``connect_all`` call.
    """

    def __init__(self, workspace: Path, home: Path | None = None) -> None:
        from .hooks import HookRunner

        self.workspace = Path(workspace).expanduser().resolve()
        self.home = extension_home(home)
        self._lock = threading.RLock()
        self._report = inspect_extensions(self.workspace, self.home)
        self._hook_runner = HookRunner(self._report.hooks.active, self.workspace)

    @property
    def report(self) -> ExtensionReport:
        with self._lock:
            return self._report

    @property
    def skill_roots(self) -> tuple[Path, ...]:
        return self.report.skill_roots

    @property
    def instruction_files(self) -> tuple[Path, ...]:
        return self.report.instruction_files

    @property
    def hook_runner(self) -> Any:
        with self._lock:
            return self._hook_runner

    @property
    def mcp_specs(self) -> tuple[MCPServerSpec, ...]:
        return tuple(server for server in self.report.mcp_servers if server.active)

    def refresh(self) -> ExtensionReport:
        from .hooks import HookRunner

        report = inspect_extensions(self.workspace, self.home)
        runner = HookRunner(report.hooks.active, self.workspace)
        with self._lock:
            self._report = report
            self._hook_runner = runner
        return report

    def diagnostics(self) -> dict[str, Any]:
        return self.report.to_dict()

    def active_mcp_configs(self) -> list[Any]:
        return [
            server.to_runtime_config(self.workspace)
            for server in self.report.mcp_servers
            if server.active
        ]

    def new_hook_runner(self, *, notifier: Any = None) -> Any:
        from .hooks import HookRunner

        return HookRunner(self.report.hooks.active, self.workspace, notifier=notifier)


def inspect_extensions(workspace: Path, home: Path | None = None) -> ExtensionReport:
    workspace = Path(workspace).expanduser().resolve()
    home = extension_home(home)
    issues: list[ExtensionIssue] = []
    try:
        installed = discover_installed_plugins(home)
    except PluginStateError as exc:
        installed = []
        issues.append(ExtensionIssue(
            "error",
            "plugin.invalid_state",
            "plugins",
            "plugin-packages",
            _display_path(home / "plugin-packages.json", workspace, home),
            str(exc),
        ))
    project_packages = discover_project_plugins(workspace)
    plugins = installed + project_packages
    for package in plugins:
        if package.error:
            issues.append(ExtensionIssue(
                "error" if package.installed.enabled else "warning",
                "plugin.invalid_manifest",
                "plugins",
                package.installed.name,
                _display_path(Path(package.installed.root), workspace, home),
                package.error,
            ))
        if package.scope == "project":
            issues.append(ExtensionIssue(
                "info",
                "plugin.project_discovered",
                "plugins",
                package.installed.name,
                _display_path(Path(package.installed.root), workspace, home),
                "Project plugin was discovered statically and remains disabled until explicitly installed or trusted.",
            ))

    plugin_hooks: list[tuple[str, Any]] = []
    for package in installed:
        if not package.installed.enabled or package.manifest is None:
            continue
        plugin_hooks.extend((package.installed.name, hook) for hook in package.manifest.hooks)
    hook_inspection = inspect_hooks(workspace, home, plugin_hooks=plugin_hooks)

    merged: dict[str, MCPServerSpec] = {}
    global_path = home / "config.json"
    global_entries, global_issues = _read_mcp_config(global_path, workspace, home, scope="global", trusted=True)
    issues.extend(global_issues)
    _merge_mcp(merged, global_entries, issues, workspace, home)

    project_path = workspace / PROJECT_MCP_FILENAME
    project_trusted = is_project_trusted(workspace, home, "mcp")
    project_entries, project_issues = _read_mcp_config(project_path, workspace, home, scope="project", trusted=project_trusted)
    issues.extend(project_issues)
    if project_entries and not project_trusted:
        issues.append(ExtensionIssue(
            "warning",
            "mcp.untrusted_project",
            "mcp",
            "project-mcp",
            _display_path(project_path, workspace, home),
            "Project MCP servers are present but will not start until MCP access is explicitly trusted for this workspace.",
        ))
    _merge_mcp(merged, project_entries, issues, workspace, home)

    plugin_entries: list[MCPServerSpec] = []
    for package in installed:
        if not package.installed.enabled or package.manifest is None:
            continue
        for server in package.manifest.mcp_servers:
            plugin_entries.append(_from_plugin_mcp(server, package, package.manifest.manifest_path))
    _merge_mcp(merged, plugin_entries, issues, workspace, home)

    issues.sort(key=lambda item: ({"error": 0, "warning": 1, "info": 2}.get(item.severity, 3), item.code, item.name))
    return ExtensionReport(
        workspace=workspace,
        home=home,
        plugins=tuple(plugins),
        mcp_servers=tuple(merged[name] for name in sorted(merged, key=str.casefold)),
        hooks=hook_inspection,
        issues=tuple(issues),
    )


def user_mcp_config_path(home: Path | None = None) -> Path:
    """Return the only file that user MCP CRUD is allowed to modify."""

    return extension_home(home) / "config.json"


def get_user_mcp_server(name: str, home: Path | None = None) -> dict[str, Any]:
    """Read one user-scoped MCP server, including values needed by the local editor."""

    clean_name = _validate_user_mcp_name(name)
    path = user_mcp_config_path(home)
    with _CONFIG_LOCK:
        data = _read_user_mcp_config(path)
        servers = _user_mcp_mapping(data)
        if clean_name not in servers:
            raise UserMCPConfigError(f'找不到用户 MCP 服务 "{clean_name}"')
        raw = servers[clean_name]
        if not isinstance(raw, dict):
            raise UserMCPConfigError("用户 MCP 服务配置必须是对象")
        payload = dict(raw)
        payload["name"] = clean_name
        normalized_name, normalized = _normalize_user_mcp_server(payload)
        return _editable_user_mcp_server(normalized_name, normalized)


def save_user_mcp_server(
    server: Any,
    home: Path | None = None,
    *,
    original_name: str | None = None,
) -> dict[str, Any]:
    """Atomically create/update/rename one user MCP server and preserve all other config."""

    name, normalized = _normalize_user_mcp_server(server)
    old_name = _validate_user_mcp_name(original_name) if original_name not in (None, "") else None
    path = user_mcp_config_path(home)
    with _CONFIG_LOCK:
        data = _read_user_mcp_config(path)
        servers = _user_mcp_mapping(data)
        if old_name is not None and old_name not in servers:
            raise UserMCPConfigError(f'找不到要重命名的用户 MCP 服务 "{old_name}"')

        replaced_name = old_name if old_name is not None else (name if name in servers else None)
        folded = name.casefold()
        collision = next(
            (
                existing
                for existing in servers
                if existing != replaced_name and existing.casefold() == folded
            ),
            None,
        )
        if collision is not None:
            raise UserMCPConfigError(f'用户 MCP 服务名称 "{name}" 已被占用')
        if replaced_name is None and len(servers) >= MAX_USER_MCP_SERVERS:
            raise UserMCPConfigError(f"用户 MCP 服务数量不能超过 {MAX_USER_MCP_SERVERS}")

        updated_servers = dict(servers)
        if replaced_name is not None and replaced_name != name:
            updated_servers.pop(replaced_name, None)
        updated_servers[name] = normalized
        updated = dict(data)
        updated["mcpServers"] = updated_servers
        _atomic_write_user_mcp_config(path, updated)
    return {
        "name": name,
        "transport": normalized["type"],
        "headerKeys": sorted(normalized.get("headers") or {}),
    }


def delete_user_mcp_server(name: str, home: Path | None = None) -> str:
    """Atomically delete one user-scoped MCP server without touching project/plugins."""

    clean_name = _validate_user_mcp_name(name)
    path = user_mcp_config_path(home)
    with _CONFIG_LOCK:
        data = _read_user_mcp_config(path)
        servers = _user_mcp_mapping(data)
        if clean_name not in servers:
            raise UserMCPConfigError(f'找不到用户 MCP 服务 "{clean_name}"')
        updated_servers = dict(servers)
        updated_servers.pop(clean_name)
        updated = dict(data)
        updated["mcpServers"] = updated_servers
        _atomic_write_user_mcp_config(path, updated)
    return clean_name


def _normalize_user_mcp_server(server: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(server, dict):
        raise UserMCPConfigError("用户 MCP 服务配置必须是对象")
    _reject_dangerous_object_keys(server)
    unknown = sorted(str(key) for key in server if key not in _USER_MCP_FIELDS)
    if unknown:
        raise UserMCPConfigError(f"用户 MCP 服务包含不支持的字段：{', '.join(unknown[:5])}")
    try:
        entry_bytes = len(json.dumps(server, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise UserMCPConfigError("用户 MCP 服务配置无法序列化") from exc
    if entry_bytes > MAX_USER_MCP_ENTRY_BYTES:
        raise UserMCPConfigError(f"单个用户 MCP 服务配置不能超过 {MAX_USER_MCP_ENTRY_BYTES} 字节")

    name = _validate_user_mcp_name(server.get("name"))
    transport_value = _alias_value(server, "transport", "type", "stdio")
    if not isinstance(transport_value, str):
        raise UserMCPConfigError("MCP transport 必须是字符串")
    transport = transport_value.strip().lower().replace("_", "-")
    if transport in {"http", "streamable-http"}:
        transport = "http"
    elif transport != "stdio":
        raise UserMCPConfigError("MCP transport 只支持 stdio 或 http")

    enabled_value = _alias_value(server, "enabled", "auto_start", True)
    if not isinstance(enabled_value, bool):
        raise UserMCPConfigError("MCP enabled 必须是布尔值")
    normalized: dict[str, Any] = {"type": transport, "enabled": enabled_value}

    if transport == "stdio":
        command = _clean_mcp_text(server.get("command"), "command", required=True, limit=MAX_MCP_PATH_CHARS)
        if _has_parent_traversal(command):
            raise UserMCPConfigError("MCP command 不能使用上级目录跳转")
        args = _clean_mcp_string_list(server.get("args"), "args")
        env = _clean_mcp_string_map(server.get("env"), "env", key_kind="env")
        cwd = _clean_mcp_cwd(server.get("cwd"))
        if server.get("url") not in (None, ""):
            raise UserMCPConfigError("stdio MCP 服务不能设置 url")
        headers = _clean_mcp_string_map(server.get("headers"), "headers", key_kind="header")
        normalized["command"] = command
        if args:
            normalized["args"] = args
        if env:
            normalized["env"] = env
        if cwd:
            normalized["cwd"] = cwd
        if headers:
            normalized["headers"] = headers
    else:
        url = _clean_http_mcp_url(server.get("url"))
        headers = _clean_mcp_string_map(server.get("headers"), "headers", key_kind="header")
        if server.get("command") not in (None, ""):
            raise UserMCPConfigError("HTTP MCP 服务不能设置 command")
        if server.get("args") not in (None, [], ()):
            raise UserMCPConfigError("HTTP MCP 服务不能设置 args")
        if server.get("env") not in (None, {}):
            raise UserMCPConfigError("HTTP MCP 服务不能设置 env")
        if server.get("cwd") not in (None, ""):
            raise UserMCPConfigError("HTTP MCP 服务不能设置 cwd")
        normalized["url"] = url
        if headers:
            normalized["headers"] = headers

    for canonical, camel in (
        ("startup_timeout_ms", "startupTimeoutMs"),
        ("call_timeout_ms", "callTimeoutMs"),
    ):
        timeout = _alias_value(server, canonical, camel, None)
        if timeout is not None:
            normalized[canonical] = _clean_mcp_timeout(timeout, canonical)

    raw_tool_timeouts = _alias_value(server, "tool_timeout_ms", "toolTimeoutMs", None)
    if raw_tool_timeouts is not None:
        normalized["tool_timeout_ms"] = _clean_mcp_timeout_map(raw_tool_timeouts)

    raw_read_only = _alias_value(server, "trusted_read_only_tools", "trustedReadOnlyTools", None)
    if raw_read_only is not None:
        normalized["trusted_read_only_tools"] = _clean_mcp_string_list(
            raw_read_only,
            "trusted_read_only_tools",
            max_items=MAX_MCP_COLLECTION_ITEMS,
        )

    return name, normalized


def _editable_user_mcp_server(name: str, normalized: dict[str, Any]) -> dict[str, Any]:
    transport = str(normalized.get("type") or "stdio")
    return {
        "name": name,
        "transport": transport,
        "command": str(normalized.get("command") or ""),
        "args": list(normalized.get("args") or []),
        "env": dict(normalized.get("env") or {}),
        "cwd": str(normalized.get("cwd") or ""),
        "url": str(normalized.get("url") or ""),
        # Header values are returned only by this explicit local edit endpoint.
        # Diagnostics and mutation responses continue to expose names only.
        "headers": dict(normalized.get("headers") or {}),
        "enabled": bool(normalized.get("enabled", True)),
        "startup_timeout_ms": normalized.get("startup_timeout_ms"),
        "call_timeout_ms": normalized.get("call_timeout_ms"),
        "tool_timeout_ms": dict(normalized.get("tool_timeout_ms") or {}),
        "trusted_read_only_tools": list(normalized.get("trusted_read_only_tools") or []),
    }


def _read_user_mcp_config(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise UserMCPConfigError("用户配置文件不能是符号链接")
    if not path.exists():
        return {}
    if not path.is_file():
        raise UserMCPConfigError("用户配置路径必须是普通文件")
    try:
        with path.open("rb") as handle:
            body = handle.read(MAX_MCP_CONFIG_BYTES + 1)
    except OSError as exc:
        raise UserMCPConfigError("无法读取用户配置文件") from exc
    if len(body) > MAX_MCP_CONFIG_BYTES:
        raise UserMCPConfigError(f"用户配置文件不能超过 {MAX_MCP_CONFIG_BYTES} 字节")
    try:
        parsed = json.loads(body.decode("utf-8-sig"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise UserMCPConfigError("用户配置文件不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise UserMCPConfigError("用户配置文件必须是对象")
    return parsed


def _user_mcp_mapping(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("mcpServers", {})
    if not isinstance(value, dict):
        raise UserMCPConfigError("用户配置中的 mcpServers 必须是对象")
    if len(value) > MAX_USER_MCP_SERVERS:
        raise UserMCPConfigError(f"用户 MCP 服务数量不能超过 {MAX_USER_MCP_SERVERS}")
    seen: set[str] = set()
    for name in value:
        clean = _validate_user_mcp_name(name)
        folded = clean.casefold()
        if folded in seen:
            raise UserMCPConfigError("用户 MCP 服务名称必须唯一（不区分大小写）")
        seen.add(folded)
    return value


def _atomic_write_user_mcp_config(path: Path, data: dict[str, Any]) -> None:
    if path.is_symlink():
        raise UserMCPConfigError("用户配置文件不能是符号链接")
    try:
        body = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise UserMCPConfigError("用户配置文件无法序列化") from exc
    if len(body) > MAX_MCP_CONFIG_BYTES:
        raise UserMCPConfigError(f"用户配置文件不能超过 {MAX_MCP_CONFIG_BYTES} 字节")
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        raise UserMCPConfigError("无法原子保存用户 MCP 配置") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _validate_user_mcp_name(value: Any) -> str:
    if not isinstance(value, str):
        raise UserMCPConfigError("用户 MCP 服务名称必须是字符串")
    name = value.strip()
    if not VALID_NAME.fullmatch(name) or name.casefold() in _DANGEROUS_OBJECT_KEYS:
        raise UserMCPConfigError("用户 MCP 服务名称格式无效")
    return name


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise UserMCPConfigError("用户配置文件包含重复的对象键")
        value[key] = item
    return value


def _reject_dangerous_object_keys(value: Any) -> None:
    pending: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 16 or visited > 4_096:
            raise UserMCPConfigError("用户 MCP 服务配置嵌套或项目数量过多")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise UserMCPConfigError("用户 MCP 服务配置的对象键必须是字符串")
                if key.casefold() in _DANGEROUS_OBJECT_KEYS:
                    raise UserMCPConfigError("用户 MCP 服务配置包含危险对象键")
                pending.append((child, depth + 1))
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)


def _alias_value(data: dict[str, Any], first: str, second: str, default: Any) -> Any:
    first_present = first in data
    second_present = second in data
    if first_present and second_present and data[first] != data[second]:
        raise UserMCPConfigError(f"MCP 字段 {first} 与 {second} 不能冲突")
    if first_present:
        return data[first]
    if second_present:
        return data[second]
    return default


def _clean_mcp_text(value: Any, label: str, *, required: bool = False, limit: int = MAX_MCP_TEXT_CHARS) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise UserMCPConfigError(f"MCP {label} 必须是字符串")
    text = value.strip() if required else value
    if required and not text:
        raise UserMCPConfigError(f"MCP {label} 不能为空")
    if len(text) > limit:
        raise UserMCPConfigError(f"MCP {label} 过长")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise UserMCPConfigError(f"MCP {label} 不能包含控制字符")
    return text


def _clean_mcp_string_list(
    value: Any,
    label: str,
    *,
    max_items: int = MAX_MCP_COLLECTION_ITEMS,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise UserMCPConfigError(f"MCP {label} 必须是字符串数组")
    if len(value) > max_items:
        raise UserMCPConfigError(f"MCP {label} 项目过多")
    return [_clean_mcp_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _clean_mcp_string_map(value: Any, label: str, *, key_kind: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UserMCPConfigError(f"MCP {label} 必须是对象")
    if len(value) > MAX_MCP_COLLECTION_ITEMS:
        raise UserMCPConfigError(f"MCP {label} 项目过多")
    output: dict[str, str] = {}
    seen: set[str] = set()
    for key, item in value.items():
        if not isinstance(key, str) or key.casefold() in _DANGEROUS_OBJECT_KEYS:
            raise UserMCPConfigError(f"MCP {label} 包含无效键名")
        if key_kind == "env" and not _ENV_NAME.fullmatch(key):
            raise UserMCPConfigError("MCP env 包含无效环境变量名")
        if key_kind == "header" and not _HEADER_NAME.fullmatch(key):
            raise UserMCPConfigError("MCP headers 包含无效请求头名称")
        if key_kind == "header" and key.casefold() in _RESERVED_MCP_HEADERS:
            raise UserMCPConfigError("MCP headers 不能覆盖协议保留请求头")
        folded = key.casefold()
        if folded in seen:
            raise UserMCPConfigError(f"MCP {label} 键名不能重复（不区分大小写）")
        seen.add(folded)
        text = _clean_mcp_text(item, f"{label}.{key}")
        if key_kind == "header" and any(char in text for char in ("\r", "\n")):
            raise UserMCPConfigError("MCP headers 不能包含换行符")
        output[key] = text
    return output


def _clean_mcp_cwd(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = _clean_mcp_text(value, "cwd", limit=MAX_MCP_PATH_CHARS)
    if _has_parent_traversal(text):
        raise UserMCPConfigError("MCP cwd 不能使用上级目录跳转")
    return text


def _has_parent_traversal(value: str) -> bool:
    try:
        return ".." in Path(value).parts
    except (OSError, ValueError):
        raise UserMCPConfigError("MCP 本地路径格式无效")


def _clean_http_mcp_url(value: Any) -> str:
    text = _clean_mcp_text(value, "url", required=True, limit=MAX_MCP_URL_CHARS)
    if any(char.isspace() for char in text):
        raise UserMCPConfigError("HTTP MCP URL 不能包含空白字符")
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UserMCPConfigError("HTTP MCP URL 格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise UserMCPConfigError("HTTP MCP URL 必须使用 http(s) 并包含主机名")
    if parsed.username is not None or parsed.password is not None:
        raise UserMCPConfigError("HTTP MCP URL 不能内嵌凭据，请使用 headers")
    if parsed.fragment:
        raise UserMCPConfigError("HTTP MCP URL 不能包含 fragment")
    _ = port
    return text


def _clean_mcp_timeout(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise UserMCPConfigError(f"MCP {label} 必须是正整数")
    try:
        timeout = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise UserMCPConfigError(f"MCP {label} 必须是正整数") from exc
    if timeout <= 0 or timeout > MAX_MCP_TIMEOUT_MS:
        raise UserMCPConfigError(f"MCP {label} 超出允许范围")
    return timeout


def _clean_mcp_timeout_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise UserMCPConfigError("MCP tool_timeout_ms 必须是对象")
    if len(value) > MAX_MCP_COLLECTION_ITEMS:
        raise UserMCPConfigError("MCP tool_timeout_ms 项目过多")
    output: dict[str, int] = {}
    for key, item in value.items():
        tool = _clean_mcp_text(key, "tool_timeout_ms key", required=True, limit=256)
        if tool.casefold() in _DANGEROUS_OBJECT_KEYS:
            raise UserMCPConfigError("MCP tool_timeout_ms 包含危险对象键")
        output[tool] = _clean_mcp_timeout(item, f"tool_timeout_ms.{tool}")
    return output


def _read_mcp_config(
    path: Path,
    workspace: Path,
    home: Path,
    *,
    scope: str,
    trusted: bool,
) -> tuple[list[MCPServerSpec], list[ExtensionIssue]]:
    if not path.exists():
        return [], []
    source = _display_path(path, workspace, home)
    try:
        with path.open("rb") as handle:
            body = handle.read(MAX_MCP_CONFIG_BYTES + 1)
        if len(body) > MAX_MCP_CONFIG_BYTES:
            raise ValueError(f"MCP configuration exceeds {MAX_MCP_CONFIG_BYTES} bytes")
        parsed = json.loads(body.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return [], [ExtensionIssue("error", "mcp.malformed_config", "mcp", path.name, source, str(exc))]
    if not isinstance(parsed, dict):
        return [], [ExtensionIssue("error", "mcp.malformed_config", "mcp", path.name, source, "MCP configuration must be an object.")]
    mapping = parsed.get("mcpServers", {})
    if not isinstance(mapping, dict):
        return [], [ExtensionIssue("error", "mcp.malformed_config", "mcp", path.name, source, "mcpServers must be an object.")]
    entries: list[MCPServerSpec] = []
    issues: list[ExtensionIssue] = []
    for name in sorted(mapping, key=str.casefold):
        raw = mapping[name]
        try:
            entries.append(_parse_mcp_entry(name, raw, path, scope, trusted))
        except ValueError as exc:
            issues.append(ExtensionIssue("error", "mcp.invalid_server", "mcp", str(name), source, str(exc)))
    return entries, issues


def _parse_mcp_entry(
    name: Any,
    raw: Any,
    source_path: Path,
    scope: str,
    trusted: bool,
) -> MCPServerSpec:
    if not isinstance(name, str) or not VALID_NAME.fullmatch(name) or name.casefold() in _DANGEROUS_OBJECT_KEYS:
        raise ValueError(f"invalid MCP server name: {name!r}")
    if not isinstance(raw, dict):
        raise ValueError("MCP server must be an object")
    _reject_dangerous_object_keys(raw)
    command = raw.get("command", "")
    url = raw.get("url", "")
    if not isinstance(command, str) or not isinstance(url, str):
        raise ValueError("command and url must be strings")
    transport = raw.get("type") or ("http" if url else "stdio")
    if not isinstance(transport, str):
        raise ValueError("transport type must be a string")
    transport = transport.strip().lower()
    if transport not in {"stdio", "http", "streamable-http", "streamable_http"}:
        raise ValueError(f"unsupported MCP transport: {transport!r}")
    args = _clean_mcp_string_list(raw.get("args"), "args")
    env = _clean_mcp_string_map(raw.get("env"), "env", key_kind="env")
    headers = _clean_mcp_string_map(raw.get("headers"), "headers", key_kind="header")
    cwd_value = _clean_mcp_cwd(raw.get("cwd"))
    if transport == "stdio":
        command = _clean_mcp_text(command, "command", required=True, limit=MAX_MCP_PATH_CHARS)
        if _has_parent_traversal(command):
            raise ValueError("MCP command cannot traverse a parent directory")
        if url.strip():
            raise ValueError("stdio MCP server cannot set url")
    else:
        url = _clean_http_mcp_url(url)
        if command.strip():
            raise ValueError("HTTP MCP server cannot set command")
        if args or env or cwd_value:
            raise ValueError("HTTP MCP server cannot set local command options")
    tool_timeouts = _positive_int_map(raw.get("tool_timeout_ms", raw.get("tool_timeout_seconds")), "tool timeout")
    if "tool_timeout_ms" not in raw and "tool_timeout_seconds" in raw:
        tool_timeouts = {key: value * 1000 for key, value in tool_timeouts.items()}
    read_only = _clean_mcp_string_list(raw.get("trusted_read_only_tools"), "trusted_read_only_tools")
    enabled = raw.get("enabled", raw.get("auto_start", True))
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    startup_timeout = _positive_int(raw.get("startup_timeout_ms"), 5_000)
    call_timeout = _positive_int(raw.get("call_timeout_ms"), 0)
    if not call_timeout:
        call_timeout = _positive_int(raw.get("call_timeout_seconds"), 300) * 1000
    return MCPServerSpec(
        name=name,
        transport=transport,
        command=command.strip(),
        args=tuple(args),
        env=env,
        cwd=_mcp_cwd(cwd_value, source_path),
        url=url.strip(),
        headers=headers,
        enabled=enabled,
        trusted=trusted,
        source_scope=scope,
        source_path=source_path.resolve(),
        startup_timeout_ms=startup_timeout,
        call_timeout_ms=call_timeout,
        tool_timeout_ms=tool_timeouts,
        trusted_read_only_tools=tuple(read_only),
    )


def _from_plugin_mcp(server: PluginMCPServer, package: PluginPackage, source: Path) -> MCPServerSpec:
    return MCPServerSpec(
        name=server.name,
        transport=server.transport,
        command=server.command,
        args=server.args,
        env=dict(server.env),
        cwd=package.manifest.root if package.manifest is not None else None,
        url=server.url,
        headers=dict(server.headers),
        enabled=server.enabled and package.installed.enabled,
        trusted=True,
        source_scope="plugin",
        source_path=source,
        plugin=package.installed.name,
        startup_timeout_ms=server.startup_timeout_ms,
        call_timeout_ms=server.call_timeout_ms,
        tool_timeout_ms=dict(server.tool_timeout_ms),
        trusted_read_only_tools=server.trusted_read_only_tools,
    )


def _merge_mcp(
    merged: dict[str, MCPServerSpec],
    entries: Iterable[MCPServerSpec],
    issues: list[ExtensionIssue],
    workspace: Path,
    home: Path,
) -> None:
    for entry in entries:
        winner = merged.get(entry.name)
        if winner is None:
            merged[entry.name] = entry
            continue
        issues.append(ExtensionIssue(
            "warning",
            "mcp.shadowed_server",
            "mcp",
            entry.name,
            _display_path(entry.source_path, workspace, home),
            f"MCP server is shadowed by the higher-priority {winner.source_scope} definition.",
        ))


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("timeout must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError("timeout must be a positive integer")
    return parsed


def _positive_int_map(value: Any, label: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return {str(key): _positive_int(item, 0) for key, item in value.items() if isinstance(key, str) and key}


def _mcp_cwd(value: Any, source_path: Path) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("cwd must be a string")
    candidate = Path(value).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (source_path.parent / candidate).resolve()


def _url_host(value: str) -> str:
    if not value:
        return ""
    try:
        return urlsplit(value).netloc
    except ValueError:
        return "<invalid>"


def _command_display(command: str) -> str:
    if not command:
        return ""
    token = command.strip().split(maxsplit=1)[0].strip('"\'')
    return Path(token).name or token


def _source_display(source: str, workspace: Path, home: Path) -> str:
    value = str(source or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(value)
            return f"{parsed.scheme}://{parsed.netloc}"
        except ValueError:
            return "<invalid-url>"
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return _display_path(candidate, workspace, home)
    return value


def _sanitize_public_text(text: str, workspace: Path, home: Path) -> str:
    from .mcp import redact_sensitive

    value = redact_sensitive(str(text or ""))
    for raw, replacement in (
        (str(workspace), "<workspace>"),
        (str(home), "<config>"),
        (str(Path.home()), "~"),
    ):
        value = value.replace(raw, replacement).replace(raw.replace("\\", "/"), replacement)
    value = re.sub(r"(?i)\b[A-Z]:[\\/][^\r\n,;]+", "<external-path>", value)
    return " ".join(value.split())[:2_000]


def _public_issue(issue: ExtensionIssue, workspace: Path, home: Path) -> dict[str, Any]:
    value = issue.to_dict()
    source = str(issue.source or "")
    candidate = Path(source).expanduser()
    if candidate.is_absolute():
        value["source"] = _display_path(candidate, workspace, home)
    value["message"] = _sanitize_public_text(issue.message, workspace, home)
    return value


def _display_path(path: Path | None, workspace: Path, home: Path) -> str:
    if path is None:
        return ""
    path = Path(path).expanduser()
    if not path.is_absolute():
        return path.as_posix()
    resolved = path.resolve(strict=False)
    try:
        return "<workspace>/" + resolved.relative_to(workspace).as_posix()
    except ValueError:
        pass
    try:
        return "<config>/" + resolved.relative_to(home).as_posix()
    except ValueError:
        return "<external>/" + resolved.name
