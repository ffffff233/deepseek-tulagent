from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile
import threading
from typing import Any, Iterable
from uuid import uuid4

from .config import config_home


NATIVE_MANIFEST = "deepseekfathom-plugin.json"
CODEX_MANIFEST = ".codex-plugin/plugin.json"
CLAUDE_MANIFEST = ".claude-plugin/plugin.json"
REASONIX_MANIFEST = "reasonix-plugin.json"
MANIFEST_PATHS = (NATIVE_MANIFEST, CODEX_MANIFEST, CLAUDE_MANIFEST, REASONIX_MANIFEST)
STATE_FILENAME = "plugin-packages.json"
PLUGINS_DIRNAME = "plugins"
MAX_MANIFEST_BYTES = 1_000_000
VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STATE_LOCK = threading.RLock()


class PluginError(ValueError):
    pass


class PluginStateError(PluginError):
    pass


@dataclass(frozen=True)
class PluginHook:
    event: str
    command: str = ""
    match: str = "*"
    description: str = ""
    timeout_ms: int | None = None
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    context_file: Path | None = None


@dataclass(frozen=True)
class PluginMCPServer:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    startup_timeout_ms: int = 5_000
    call_timeout_ms: int = 300_000
    tool_timeout_ms: dict[str, int] = field(default_factory=dict)
    trusted_read_only_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class PluginManifest:
    name: str
    root: Path
    manifest_path: Path
    manifest_kind: str
    version: str = ""
    description: str = ""
    skills: tuple[Path, ...] = ()
    instructions: tuple[Path, ...] = ()
    hooks: tuple[PluginHook, ...] = ()
    mcp_servers: tuple[PluginMCPServer, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstalledPlugin:
    name: str
    root: str
    source: str = ""
    version: str = ""
    description: str = ""
    manifest_kind: str = ""
    enabled: bool = False
    linked: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "root": self.root,
            "source": self.source,
            "version": self.version,
            "description": self.description,
            "manifestKind": self.manifest_kind,
            "enabled": self.enabled,
            "linked": self.linked,
        }


@dataclass(frozen=True)
class PluginState:
    version: int = 1
    plugins: tuple[InstalledPlugin, ...] = ()


@dataclass(frozen=True)
class PluginPackage:
    installed: InstalledPlugin
    manifest: PluginManifest | None
    error: str = ""
    scope: str = "user"


def extension_home(home: Path | None = None) -> Path:
    return (Path(home).expanduser() if home is not None else config_home()).resolve()


def state_path(home: Path | None = None) -> Path:
    return extension_home(home) / STATE_FILENAME


def plugins_dir(home: Path | None = None) -> Path:
    return extension_home(home) / PLUGINS_DIRNAME


def install_root(name: str, home: Path | None = None) -> Path:
    validate_plugin_name(name)
    return plugins_dir(home) / name


def validate_plugin_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not VALID_NAME.fullmatch(cleaned):
        raise PluginError(f"invalid plugin name: {name!r}")
    return cleaned


def parse_plugin(root: Path) -> PluginManifest:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise PluginError(f"plugin root is not a directory: {root}")
    selected = next((root / relative for relative in MANIFEST_PATHS if (root / relative).is_file()), None)
    if selected is None:
        names = ", ".join(MANIFEST_PATHS)
        raise PluginError(f"plugin has no supported manifest ({names}): {root}")
    raw = _read_manifest(selected)
    name = validate_plugin_name(raw.get("name", ""))
    kind = {
        NATIVE_MANIFEST: "deepseekfathom",
        CODEX_MANIFEST: "codex",
        CLAUDE_MANIFEST: "claude",
        REASONIX_MANIFEST: "reasonix",
    }[selected.relative_to(root).as_posix()]

    skill_values = _path_values(raw.get("skills"), "skills")
    if not skill_values and kind in {"codex", "claude"}:
        skill_values = _conventional_skill_roots(root)
    skills = tuple(_safe_member(root, value, "skill path") for value in skill_values)

    instruction_values = _path_values(raw.get("instructions"), "instructions")
    for filename in ("AGENTS.md", "CLAUDE.md", "REASONIX.md"):
        if (root / filename).is_file() and filename not in instruction_values:
            instruction_values.append(filename)
    instructions = tuple(_safe_member(root, value, "instruction path") for value in instruction_values)

    hooks, hook_warnings = _parse_hooks(root, raw.get("hooks"))
    if kind == "codex":
        compat = root / "hooks" / "session-start-codex"
        if compat.is_file():
            hooks.append(PluginHook("SessionStart", command=str(compat.resolve()), cwd=root))
    if kind == "claude":
        claude_hooks, claude_warnings = _parse_claude_settings_hooks(root)
        hooks.extend(claude_hooks)
        hook_warnings.extend(claude_warnings)
    mcp_servers = _parse_mcp_servers(root, raw.get("mcpServers"))

    return PluginManifest(
        name=name,
        root=root,
        manifest_path=selected.resolve(),
        manifest_kind=kind,
        version=_optional_text(raw.get("version")),
        description=_optional_text(raw.get("description")),
        skills=skills,
        instructions=instructions,
        hooks=tuple(hooks),
        mcp_servers=tuple(mcp_servers),
        warnings=tuple(hook_warnings),
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            body = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise PluginError(f"cannot read plugin manifest {path}: {exc}") from exc
    if len(body) > MAX_MANIFEST_BYTES:
        raise PluginError(f"plugin manifest exceeds {MAX_MANIFEST_BYTES} bytes: {path}")
    try:
        parsed = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginError(f"invalid plugin manifest {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PluginError(f"plugin manifest must be a JSON object: {path}")
    return parsed


def _path_values(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise PluginError(f"plugin {field_name} must be a path or path array")
    out: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("path")
        if not isinstance(item, str) or not item.strip():
            raise PluginError(f"plugin {field_name} contains an invalid path")
        cleaned = item.strip()
        if cleaned not in out:
            out.append(cleaned)
    return out


def _conventional_skill_roots(root: Path) -> list[str]:
    roots: list[str] = []
    for relative in ("skills", ".claude/skills"):
        path = root / relative
        try:
            if any((entry / "SKILL.md").is_file() for entry in path.iterdir() if entry.is_dir()):
                roots.append(relative)
        except OSError:
            continue
    return roots


def _safe_member(root: Path, raw: str, label: str) -> Path:
    value = str(raw or "").strip()
    if not value or "\x00" in value:
        raise PluginError(f"{label} is empty or invalid")
    portable = PureWindowsPath(value)
    candidate_path = Path(value)
    if candidate_path.is_absolute() or portable.is_absolute() or portable.drive:
        raise PluginError(f"{label} must be relative to the plugin root: {value!r}")
    normalized = value.replace("\\", "/")
    if ".." in Path(normalized).parts:
        raise PluginError(f"{label} escapes the plugin root: {value!r}")
    candidate = (root / Path(normalized)).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise PluginError(f"{label} escapes the plugin root: {value!r}") from exc
    return candidate


def _parse_hooks(root: Path, value: Any) -> tuple[list[PluginHook], list[str]]:
    if value is None:
        return [], []
    if not isinstance(value, dict):
        raise PluginError("plugin hooks must be an object keyed by event")
    hooks: list[PluginHook] = []
    warnings: list[str] = []
    for event, entries in value.items():
        if not isinstance(event, str) or not event.strip():
            raise PluginError("plugin hook event is required")
        if not isinstance(entries, list):
            raise PluginError(f"plugin hooks.{event} must be an array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise PluginError(f"plugin hook {event} must be an object")
            command = _optional_text(entry.get("command"))
            context_raw = _optional_text(entry.get("contextFile"))
            if not command and not context_raw:
                warnings.append(f"hook {event} skipped: command or contextFile is required")
                continue
            cwd_raw = _optional_text(entry.get("cwd"))
            cwd = _safe_member(root, cwd_raw, "hook cwd") if cwd_raw else root
            context_file = _safe_member(root, context_raw, "hook contextFile") if context_raw else None
            if command and not bool(entry.get("shellCommand")) and _looks_like_plugin_path(command, root):
                command = str(_safe_member(root, command, "hook command"))
            timeout = _positive_int(entry.get("timeout"), default=None)
            hooks.append(PluginHook(
                event=event.strip(),
                command=command,
                match=_optional_text(entry.get("match")) or _optional_text(entry.get("matcher")) or "*",
                description=_optional_text(entry.get("description")),
                timeout_ms=timeout,
                cwd=cwd,
                env=_string_map(entry.get("env"), f"hook {event} env"),
                context_file=context_file,
            ))
    return hooks, warnings


def _parse_mcp_servers(root: Path, value: Any) -> list[PluginMCPServer]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise PluginError("plugin mcpServers must be an object")
    servers: list[PluginMCPServer] = []
    for raw_name in sorted(value):
        name = validate_plugin_name(raw_name)
        entry = value[raw_name]
        if not isinstance(entry, dict):
            raise PluginError(f"plugin MCP server {name!r} must be an object")
        command = _optional_text(entry.get("command"))
        if command and _looks_like_plugin_path(command, root):
            command = str(_safe_member(root, command, f"MCP server {name} command"))
        transport = (_optional_text(entry.get("type")) or ("http" if entry.get("url") else "stdio")).lower()
        if transport not in {"stdio", "http", "streamable-http", "streamable_http"}:
            raise PluginError(f"unsupported MCP transport for {name!r}: {transport!r}")
        args = entry.get("args") or []
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise PluginError(f"MCP server {name!r} args must be a string array")
        servers.append(PluginMCPServer(
            name=name,
            transport=transport,
            command=command,
            args=tuple(args),
            env=_string_map(entry.get("env"), f"MCP server {name} env"),
            url=_optional_text(entry.get("url")),
            headers=_string_map(entry.get("headers"), f"MCP server {name} headers"),
            enabled=bool(entry.get("enabled", entry.get("auto_start", True))),
            startup_timeout_ms=_positive_int(entry.get("startup_timeout_ms"), 5_000) or 5_000,
            call_timeout_ms=_positive_int(entry.get("call_timeout_ms"), 300_000) or 300_000,
            tool_timeout_ms=_positive_int_map(entry.get("tool_timeout_ms"), f"MCP server {name} tool_timeout_ms"),
            trusted_read_only_tools=tuple(_string_list(entry.get("trusted_read_only_tools"), f"MCP server {name} trusted_read_only_tools")),
        ))
    return servers


def _parse_claude_settings_hooks(root: Path) -> tuple[list[PluginHook], list[str]]:
    path = root / ".claude" / "settings.json"
    if not path.is_file():
        return [], []
    try:
        raw = _read_manifest(path)
    except PluginError as exc:
        return [], [f".claude/settings.json: {exc}"]
    mapping = raw.get("hooks")
    if not isinstance(mapping, dict):
        if mapping is None:
            return [], []
        return [], [".claude/settings.json: hooks must be an object"]
    hooks: list[PluginHook] = []
    warnings: list[str] = []
    for event, blocks in mapping.items():
        if not isinstance(event, str) or not isinstance(blocks, list):
            warnings.append(f".claude/settings.json: invalid hook block for {event}")
            continue
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("hooks"), list):
                warnings.append(f".claude/settings.json: invalid hook list for {event}")
                continue
            matcher = _optional_text(block.get("matcher")) or _optional_text(block.get("match")) or "*"
            for item in block["hooks"]:
                if not isinstance(item, dict):
                    warnings.append(f".claude/settings.json: invalid hook item for {event}")
                    continue
                hook_type = _optional_text(item.get("type")) or "command"
                if hook_type != "command":
                    warnings.append(f".claude/settings.json: skipped unsupported hook type {hook_type!r} for {event}")
                    continue
                command = _optional_text(item.get("command"))
                if not command:
                    warnings.append(f".claude/settings.json: skipped empty command for {event}")
                    continue
                timeout_seconds = _positive_int(item.get("timeout"), None)
                hooks.append(PluginHook(
                    event=event.strip(),
                    command=command,
                    match=matcher,
                    description=_optional_text(item.get("description")) or "Claude-compatible plugin hook",
                    timeout_ms=timeout_seconds * 1000 if timeout_seconds else None,
                    cwd=root,
                    env=_string_map(item.get("env"), f"Claude hook {event} env"),
                ))
    return hooks, warnings


def _looks_like_plugin_path(command: str, root: Path) -> bool:
    # A bare executable path is package-relative. Commands containing shell
    # whitespace remain command strings (for example ``node hooks/start.js``);
    # resolving that whole string as a filename would corrupt it.
    if any(character.isspace() for character in command):
        return False
    return "/" in command or "\\" in command or (root / command).exists()


def _optional_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_map(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PluginError(f"{label} must be an object")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, str):
            raise PluginError(f"{label} must contain string keys and values")
        out[key] = item
    return out


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PluginError(f"{label} must be a string array")
    return [item for item in value if item]


def _positive_int(value: Any, default: int | None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        raise PluginError("timeout must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PluginError("timeout must be a positive integer") from exc
    if parsed <= 0:
        raise PluginError("timeout must be a positive integer")
    return parsed


def _positive_int_map(value: Any, label: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PluginError(f"{label} must be an object")
    out: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise PluginError(f"{label} contains an invalid tool name")
        out[key] = _positive_int(item, None) or 0
    return out


def load_plugin_state(home: Path | None = None) -> PluginState:
    path = state_path(home)
    if not path.exists():
        return PluginState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginStateError(f"invalid plugin state {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("plugins", []), list):
        raise PluginStateError(f"invalid plugin state shape: {path}")
    plugins: list[InstalledPlugin] = []
    for item in raw.get("plugins", []):
        if not isinstance(item, dict):
            raise PluginStateError(f"plugin state contains a non-object entry: {path}")
        try:
            name = validate_plugin_name(item.get("name", ""))
        except PluginError as exc:
            raise PluginStateError(f"plugin state contains an invalid name in {path}: {exc}") from exc
        root = _optional_text(item.get("root"))
        if not root:
            raise PluginStateError(f"plugin {name!r} has no root in {path}")
        plugins.append(InstalledPlugin(
            name=name,
            root=root,
            source=_optional_text(item.get("source")),
            version=_optional_text(item.get("version")),
            description=_optional_text(item.get("description")),
            manifest_kind=_optional_text(item.get("manifestKind")),
            enabled=bool(item.get("enabled", False)),
            linked=bool(item.get("linked", Path(root).is_absolute())),
        ))
    plugins.sort(key=lambda item: item.name.casefold())
    version = raw.get("version", 1)
    return PluginState(int(version) if isinstance(version, int) and version > 0 else 1, tuple(plugins))


def save_plugin_state(state: PluginState, home: Path | None = None) -> Path:
    path = state_path(home)
    payload = {
        "version": max(1, int(state.version)),
        "plugins": [item.to_json() for item in sorted(state.plugins, key=lambda value: value.name.casefold())],
    }
    with _STATE_LOCK:
        _atomic_write_json(path, payload)
    return path


def upsert_plugin_state(plugin: InstalledPlugin, home: Path | None = None) -> Path:
    validate_plugin_name(plugin.name)
    with _STATE_LOCK:
        state = load_plugin_state(home)
        plugins = [item for item in state.plugins if item.name != plugin.name]
        plugins.append(plugin)
        return save_plugin_state(PluginState(state.version, tuple(plugins)), home)


def set_plugin_enabled(name: str, enabled: bool, home: Path | None = None) -> Path:
    name = validate_plugin_name(name)
    with _STATE_LOCK:
        state = load_plugin_state(home)
        found = False
        plugins: list[InstalledPlugin] = []
        for item in state.plugins:
            if item.name == name:
                found = True
                item = replace(item, enabled=bool(enabled))
            plugins.append(item)
        if not found:
            raise PluginStateError(f"plugin is not installed: {name}")
        return save_plugin_state(PluginState(state.version, tuple(plugins)), home)


def remove_plugin_state(name: str, home: Path | None = None) -> InstalledPlugin | None:
    name = validate_plugin_name(name)
    with _STATE_LOCK:
        state = load_plugin_state(home)
        removed = next((item for item in state.plugins if item.name == name), None)
        if removed is None:
            return None
        save_plugin_state(PluginState(state.version, tuple(item for item in state.plugins if item.name != name)), home)
        return removed


def discover_installed_plugins(home: Path | None = None, *, include_disabled: bool = True) -> list[PluginPackage]:
    base = extension_home(home)
    state = load_plugin_state(base)
    packages: list[PluginPackage] = []
    for installed in state.plugins:
        if not include_disabled and not installed.enabled:
            continue
        root = Path(installed.root).expanduser()
        if not root.is_absolute():
            root = base / root
        try:
            manifest = parse_plugin(root)
            error = ""
        except (OSError, PluginError) as exc:
            manifest = None
            error = str(exc)
        packages.append(PluginPackage(installed, manifest, error, "user"))
    return packages


def discover_project_plugins(workspace: Path) -> list[PluginPackage]:
    root = Path(workspace).resolve() / ".deepseekfathom" / PLUGINS_DIRNAME
    try:
        entries = sorted((entry for entry in root.iterdir() if entry.is_dir()), key=lambda path: path.name.casefold())
    except OSError:
        return []
    packages: list[PluginPackage] = []
    for entry in entries:
        try:
            manifest = parse_plugin(entry)
            installed = InstalledPlugin(
                name=manifest.name,
                root=str(entry),
                source="project-discovery",
                version=manifest.version,
                description=manifest.description,
                manifest_kind=manifest.manifest_kind,
                enabled=False,
                linked=True,
            )
            packages.append(PluginPackage(installed, manifest, scope="project"))
        except (OSError, PluginError) as exc:
            name = entry.name if VALID_NAME.fullmatch(entry.name) else "invalid"
            installed = InstalledPlugin(name=name, root=str(entry), source="project-discovery", linked=True)
            packages.append(PluginPackage(installed, None, str(exc), "project"))
    return packages


def install_local_plugin(
    source: Path,
    home: Path | None = None,
    *,
    name: str | None = None,
    replace: bool = False,
    link: bool = False,
    enabled: bool = True,
) -> InstalledPlugin:
    base = extension_home(home)
    source_root = Path(source).expanduser().resolve()
    manifest = parse_plugin(source_root)
    install_name = validate_plugin_name(name or manifest.name)
    target = install_root(install_name, base)
    if (target.exists() or target.is_symlink()) and not replace:
        raise PluginError(f"plugin already exists: {install_name}")
    _validate_tree_symlinks(source_root)
    target.parent.mkdir(parents=True, exist_ok=True)

    previous = next((item for item in load_plugin_state(base).plugins if item.name == install_name), None)
    backup: Path | None = None
    staging: Path | None = None
    installed_new_tree = False
    try:
        if link:
            staging = Path(tempfile.mkdtemp(prefix=f".{install_name}.link-", dir=target.parent))
            staging.rmdir()
            os.symlink(source_root, staging, target_is_directory=True)
        else:
            staging = Path(tempfile.mkdtemp(prefix=f".{install_name}.staging-", dir=target.parent))
            shutil.rmtree(staging)
            shutil.copytree(source_root, staging, symlinks=False)
            staged_manifest = parse_plugin(staging)
            if _capability_signature(staged_manifest) != _capability_signature(manifest):
                raise PluginError("installed plugin copy does not match the approved capability set")
        if target.exists() or target.is_symlink():
            backup = target.with_name(f".{target.name}.old-{uuid4().hex}")
            target.rename(backup)
        staging.rename(target)
        staging = None
        installed_new_tree = True
        installed = InstalledPlugin(
            name=install_name,
            root=str(source_root) if link else target.relative_to(base).as_posix(),
            source=str(source_root),
            version=manifest.version,
            description=manifest.description,
            manifest_kind=manifest.manifest_kind,
            enabled=bool(enabled),
            linked=link,
        )
        upsert_plugin_state(installed, base)
        if backup is not None:
            _remove_path(backup)
        return installed
    except Exception:
        if installed_new_tree and (target.exists() or target.is_symlink()):
            _remove_path(target)
        if backup is not None and (backup.exists() or backup.is_symlink()):
            backup.rename(target)
        if previous is not None:
            try:
                upsert_plugin_state(previous, base)
            except PluginError:
                pass
        raise
    finally:
        if staging is not None:
            _remove_path(staging)


def uninstall_plugin(name: str, home: Path | None = None) -> InstalledPlugin | None:
    base = extension_home(home)
    installed = remove_plugin_state(name, base)
    if installed is None or installed.linked:
        return installed
    root = (base / installed.root).resolve()
    allowed = plugins_dir(base).resolve()
    try:
        root.relative_to(allowed)
    except ValueError:
        return installed
    _remove_path(root)
    return installed


def _validate_tree_symlinks(root: Path) -> None:
    resolved_root = root.resolve()
    try:
        entries = root.rglob("*")
        for entry in entries:
            if not entry.is_symlink():
                continue
            resolved = entry.resolve(strict=False)
            try:
                resolved.relative_to(resolved_root)
            except ValueError as exc:
                raise PluginError(f"plugin symlink escapes the plugin root: {entry}") from exc
    except OSError as exc:
        raise PluginError(f"cannot inspect plugin tree: {exc}") from exc


def _capability_signature(manifest: PluginManifest) -> tuple[int, int, int, tuple[str, ...]]:
    return (
        len(manifest.skills),
        len(manifest.instructions),
        len(manifest.hooks),
        tuple(server.name for server in manifest.mcp_servers),
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
