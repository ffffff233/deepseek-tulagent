from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
import stat as stat_module
import threading
import warnings
from uuid import uuid4


_CONFIG_LOCK = threading.RLock()
DATA_DIRNAME = ".deepseekfathom"
LEGACY_DATA_DIRNAME = ".deepseek-tulagent"

MODEL_ALIASES = {
    "v4-pro": "deepseek-v4-pro",
    "v4-flash": "deepseek-v4-flash",
    "pro": "deepseek-v4-pro",
    "flash": "deepseek-v4-flash",
}


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str
    model: str
    workspace: Path
    max_tool_rounds: int
    max_tokens: int
    request_timeout: float
    default_mode: str
    default_thinking: str
    provider_format: str = "deepseek"
    thinking_enabled: bool = True
    reasoning_effort: str | None = None
    context_window_tokens: int | None = None
    compact_threshold_percent: float = 95.0

    @property
    def sessions_dir(self) -> Path:
        return self.workspace / DATA_DIRNAME / "sessions"

    def with_runtime(
        self,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> "Settings":
        return replace(
            self,
            model=resolve_model(model) if model else self.model,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            thinking_enabled=self.thinking_enabled if thinking_enabled is None else thinking_enabled,
            reasoning_effort=reasoning_effort,
        )


def get_settings() -> Settings:
    file_config = load_file_config()
    workspace = Path(environment_value("DEEPSEEKFATHOM_WORKSPACE", "DSTUL_WORKSPACE", os.getcwd())).expanduser().resolve()
    migrate_workspace_data(workspace)
    # GUI-writable fields: the saved config file wins over the environment. The desktop
    # settings dialog writes to the config file, so an env var left over from launch must
    # not silently shadow what the user just saved ("保存后不生效 / 无法发送").
    model = string_or_none(file_config.get("model")) or os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-flash"
    api_key = string_or_none(file_config.get("api_key")) or os.getenv("DEEPSEEK_API_KEY")
    if "base_url" in file_config and isinstance(file_config.get("base_url"), str):
        # An explicitly saved empty value means "use the selected provider's default".
        # Preserve it instead of falling back to a stale launch-time environment value.
        base_url = str(file_config["base_url"]).strip()
    else:
        base_url = os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    provider_format = string_or_none(file_config.get("provider_format")) or os.getenv("DEEPSEEK_PROVIDER_FORMAT") or "deepseek"
    return Settings(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=resolve_model(model),
        workspace=workspace,
        max_tool_rounds=int(environment_value("DEEPSEEKFATHOM_MAX_TOOL_ROUNDS", "DSTUL_MAX_TOOL_ROUNDS") or file_config.get("max_tool_rounds") or "256"),
        max_tokens=int(environment_value("DEEPSEEKFATHOM_MAX_TOKENS", "DSTUL_MAX_TOKENS") or file_config.get("max_tokens") or "8192"),
        request_timeout=float(environment_value("DEEPSEEKFATHOM_REQUEST_TIMEOUT", "DSTUL_REQUEST_TIMEOUT") or file_config.get("request_timeout") or "60"),
        default_mode=str(file_config.get("default_mode") or "root"),
        default_thinking=str(file_config.get("default_thinking") or "fast"),
        provider_format=provider_format,
        context_window_tokens=int(file_config["context_window_tokens"]) if file_config.get("context_window_tokens") else None,
        compact_threshold_percent=float(file_config.get("compact_threshold_percent") or 95.0),
    )


def resolve_model(model: str) -> str:
    return MODEL_ALIASES.get(model.lower(), model)


def config_home() -> Path:
    configured = os.getenv("DEEPSEEKFATHOM_CONFIG_HOME")
    target = (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / DATA_DIRNAME).resolve()
    )
    for source in _legacy_data_homes():
        migrate_legacy_data(source, target)
    return target


def _legacy_data_homes() -> tuple[Path, ...]:
    """Return legacy data homes in precedence order, without duplicate paths."""

    candidates: list[Path] = []
    configured = os.getenv("DSTUL_CONFIG_HOME")
    if configured:
        candidates.append(Path(configured).expanduser())
    # An explicit current config home is an isolated profile. Import only an
    # explicitly paired legacy home; never leak defaults from the real user home.
    if not os.getenv("DEEPSEEKFATHOM_CONFIG_HOME"):
        candidates.append(Path.home() / LEGACY_DATA_DIRNAME)

    paths: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        key = os.path.normcase(os.fspath(path))
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return tuple(paths)


def environment_value(name: str, legacy_name: str | None = None, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is not None:
        return value
    if legacy_name:
        value = os.getenv(legacy_name)
        if value is not None:
            return value
    return default


def migrate_workspace_data(workspace: Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    target = root / DATA_DIRNAME
    migrate_legacy_data(root / LEGACY_DATA_DIRNAME, target)
    return target


def migrate_legacy_data(source: Path, target: Path) -> None:
    """Copy legacy data once-by-content without replacing user-owned targets."""

    source_path = Path(source).expanduser()
    target_path = Path(target).expanduser()
    try:
        if _is_link_or_reparse(source_path) or _is_link_or_reparse(target_path):
            return
        source = source_path.resolve()
        target = target_path.resolve()
    except (OSError, RuntimeError):
        _warn_migration_failure()
        return
    if (
        source == target
        or source in target.parents
        or target in source.parents
        or not source.is_dir()
    ):
        return
    failures = 0
    with _CONFIG_LOCK:
        try:
            for item in source.rglob("*"):
                try:
                    if _is_link_or_reparse(item):
                        continue
                    destination = target / item.relative_to(source)
                    if item.is_dir():
                        if not _prepare_migration_parent(target, destination.parent):
                            failures += 1
                            continue
                        if _is_link_or_reparse(destination):
                            failures += 1
                            continue
                        destination.mkdir(parents=True, exist_ok=True)
                    elif item.is_file() and not destination.exists():
                        if not _prepare_migration_parent(target, destination.parent):
                            failures += 1
                            continue
                        temp = destination.with_name(
                            f".{destination.name}.migrate-{os.getpid()}-{uuid4().hex}"
                        )
                        try:
                            shutil.copy2(item, temp)
                            if not _migration_parent_is_safe(target, destination.parent):
                                failures += 1
                                continue
                            # Linking a complete temporary file publishes it atomically and
                            # fails instead of replacing a destination created by another
                            # process during concurrent first startup.
                            os.link(temp, destination)
                        except FileExistsError:
                            pass
                        finally:
                            temp.unlink(missing_ok=True)
                except OSError:
                    failures += 1
        except OSError:
            failures += 1
    if failures:
        _warn_migration_failure()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    reparse_flag = int(getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _migration_parent_is_safe(target: Path, parent: Path) -> bool:
    try:
        relative = parent.relative_to(target)
    except ValueError:
        return False
    current = target
    if _is_link_or_reparse(current):
        return False
    for part in relative.parts:
        current /= part
        if _is_link_or_reparse(current):
            return False
    try:
        resolved_parent = parent.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved_parent == target or target in resolved_parent.parents


def _prepare_migration_parent(target: Path, parent: Path) -> bool:
    if not _migration_parent_is_safe(target, parent):
        return False
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return _migration_parent_is_safe(target, parent)


def _warn_migration_failure() -> None:
    warnings.warn(
        "DeepSeekFathom could not migrate some legacy data files; it will retry on the next startup.",
        RuntimeWarning,
        stacklevel=3,
    )


def config_path() -> Path:
    return config_home() / "config.json"


def load_file_config() -> dict:
    path = config_path()
    # A current config is user-owned. Read legacy configs only as lower-priority
    # defaults so migration can recover missing keys without rewriting or replacing
    # a current file that another process may be editing.
    merged: dict = {}
    for legacy_home in reversed(_legacy_data_homes()):
        merged.update(_read_config_object(legacy_home / "config.json"))
    merged.update(_read_config_object(path))
    return merged


def _read_config_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_file_config(data: dict) -> Path:
    with _CONFIG_LOCK:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.chmod(0o600)
            tmp.replace(path)
            path.chmod(0o600)
            return path
        finally:
            tmp.unlink(missing_ok=True)


def merge_file_config(data: dict) -> Path:
    with _CONFIG_LOCK:
        merged = load_file_config()
        merged.update({key: value for key, value in data.items() if value is not None})
        return save_file_config(merged)


def string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
