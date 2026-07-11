from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import threading
from uuid import uuid4


_CONFIG_LOCK = threading.RLock()

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
        return self.workspace / ".deepseek-tulagent" / "sessions"

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
    workspace = Path(os.getenv("DSTUL_WORKSPACE", os.getcwd())).expanduser().resolve()
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
        max_tool_rounds=int(os.getenv("DSTUL_MAX_TOOL_ROUNDS") or file_config.get("max_tool_rounds") or "256"),
        max_tokens=int(os.getenv("DSTUL_MAX_TOKENS") or file_config.get("max_tokens") or "8192"),
        request_timeout=float(os.getenv("DSTUL_REQUEST_TIMEOUT") or file_config.get("request_timeout") or "60"),
        default_mode=str(file_config.get("default_mode") or "root"),
        default_thinking=str(file_config.get("default_thinking") or "fast"),
        provider_format=provider_format,
        context_window_tokens=int(file_config["context_window_tokens"]) if file_config.get("context_window_tokens") else None,
        compact_threshold_percent=float(file_config.get("compact_threshold_percent") or 95.0),
    )


def resolve_model(model: str) -> str:
    return MODEL_ALIASES.get(model.lower(), model)


def config_home() -> Path:
    return Path(os.getenv("DSTUL_CONFIG_HOME", "~/.deepseek-tulagent")).expanduser().resolve()


def config_path() -> Path:
    return config_home() / "config.json"


def load_file_config() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
