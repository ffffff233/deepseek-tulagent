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
from ..agent import TuLAgent, compact_context_messages, context_window_info, estimate_message_tokens, summarize_arguments
from ..capabilities import collect_capability_report
from ..config import Settings, get_settings, merge_file_config
from ..messages import Message
from ..policy import ThinkingMode
from ..provider import DeepSeekClient, UsageStats, apply_thinking_payload
from ..session import Session, SessionStore
from ..skills import SkillStore


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
THINKING_TIERS = ["fast", "balanced", "deep", "ultra"]
THINKING_LABELS = {
    "fast": "Low",
    "balanced": "Medium",
    "deep": "High",
    "ultra": "XHigh",
}


def _copy_missing_user_data(source: Path, target: Path) -> None:
    """Migrate legacy install-local data without replacing anything user-owned."""
    if not source.is_dir() or source.resolve() == target.resolve():
        return
    for item in source.rglob("*"):
        try:
            relative = item.relative_to(source)
            destination = target / relative
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
        except OSError:
            continue


def get_desktop_settings() -> Settings:
    settings = get_settings()
    if not getattr(sys, "frozen", False) or os.getenv("DSTUL_WORKSPACE"):
        return settings
    user_workspace = Path.home().resolve()
    _copy_missing_user_data(settings.workspace / ".deepseek-tulagent", user_workspace / ".deepseek-tulagent")
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
        self._session_navigation_lock = threading.Lock()
        self._session_navigation_id = 0
        self._running = False
        self._cancel_requested = False
        self._approvals: dict[str, dict[str, Any]] = {}
        self._active_turn_session_id: str | None = None
        self._active_turn_id: str | None = None
        self._pending_turn: dict[str, Any] | None = None
        self._pending_turn_lock = threading.Lock()
        self._abandoned_turn_ids: set[str] = set()
        self._active_client: DeepSeekClient | None = None
        self._models_cache: dict[str, tuple[float, list[str]]] = {}
        self._last_usage = UsageStats()
        self._usage_total = UsageStats()
        self._usage_by_session: dict[str, UsageStats] = {}
        self._context_by_session: dict[str, dict[str, Any]] = {}

    def bind_window(self, window: Any) -> None:
        self._window = window

    def boot(self) -> dict[str, Any]:
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
            "skills": [asdict(skill) | {"path": str(skill.path)} for skill in SkillStore(self.settings.workspace).list()],
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
        }

    def capability_diagnostics(self) -> dict[str, Any]:
        return collect_capability_report(self.settings.workspace, mode=self.mode)

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
        if thinking_name in ThinkingMode.names():
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
            first_user = next((message.content for message in session.messages if message.role == "user"), "")
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
            self.session = None
        return {"ok": True, "sessions": self.sessions(), "context": self.context_status()}

    def resume(self, session_id: str, navigation_id: int | None = None) -> dict[str, Any]:
        loaded = SessionStore(self.settings.workspace).load(session_id)
        with self._session_navigation_lock:
            requested = self._coerce_navigation_id(navigation_id)
            if requested is not None and requested < self._session_navigation_id:
                return {"ok": False, "stale": True, "activated": False, "sessionId": loaded.session_id}
            self._session_navigation_id = requested if requested is not None else self._session_navigation_id + 1
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
        upload_dir = self.settings.workspace / ".deepseek-tulagent" / "uploads"
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
        upload_dir = self.settings.workspace / ".deepseek-tulagent" / "uploads"
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
        prompt = str(payload.get("prompt") or "").strip()
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
        if self._running:
            if self._cancel_requested:
                return self._queue_turn_after_cancel(prompt, images=images, goal=goal)
            return {"ok": False, "error": "turn already running"}
        return self._start_turn(prompt, images=images, goal=goal)

    def _start_turn(
        self,
        prompt: str,
        images: list[str] | None = None,
        goal: str | None = None,
        restore_suffix: list[Message] | None = None,
        prepared_messages: list[Message] | None = None,
    ) -> dict[str, Any]:
        self._cancel_requested = False
        self._running = True
        if self.session is None:
            self.session = Session(self.settings.workspace)
        session_id = self.session.session_id
        turn_id = uuid4().hex
        thread = threading.Thread(
            target=self._run_agent_turn,
            args=(prompt, images or [], session_id, turn_id, goal, restore_suffix, prepared_messages),
            daemon=True,
        )
        thread.start()
        return {"ok": True, "sessionId": session_id, "turnId": turn_id}

    def _queue_turn_after_cancel(self, prompt: str, images: list[str] | None = None, goal: str | None = None) -> dict[str, Any]:
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
            }
        return {"ok": True, "queued": True, "sessionId": session_id, "turnId": turn_id}

    def _start_pending_turn_if_any(self) -> None:
        with self._pending_turn_lock:
            pending = self._pending_turn
            self._pending_turn = None
        if not pending:
            return
        session_id = str(pending["session_id"])
        # Do not adopt the queued conversation here. The user may have switched again
        # while the cancelled worker was winding down; _run_agent_turn loads the queued
        # session by id and keeps its events scoped without changing the visible session.
        self._cancel_requested = False
        self._running = True
        thread = threading.Thread(
            target=self._run_agent_turn,
            args=(
                str(pending["prompt"]),
                list(pending["images"]),
                session_id,
                str(pending["turn_id"]),
                pending.get("goal"),
            ),
            daemon=True,
        )
        thread.start()

    @staticmethod
    def _is_real_user_message(message: Message) -> bool:
        return message.role == "user" and not message.content.startswith(("TOOL_RESULT", "SUBAGENT_RESULT", "USER_ANSWER"))

    def _prepare_regenerated_turn(
        self, before_index: int | None = None
    ) -> tuple[str | None, list[str], list[Message], list[Message]]:
        """Prepare a replacement turn without modifying the persisted conversation.

        With before_index, target the user message at/nearest-before that transcript
        index (for retry/edit on any message); otherwise the last user message.
        Skips tool-result / subagent-result messages (which also carry role 'user').
        The clean prefix runs in an in-memory session. Only a successful replacement is
        atomically committed, so cancellation, provider errors, and process interruption
        leave the original JSONL untouched.
        """
        if self.session is None:
            return None, [], [], []
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
                prefix = [Message(m.role, m.content, m.name, list(m.images)) for m in messages[:i]]
                restore_suffix = [Message(m.role, m.content, m.name, list(m.images)) for m in messages[suffix_start:]]
                return prompt, list(message.images), prefix, restore_suffix
        return None, [], [], []

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
        restored.messages.extend(Message(m.role, m.content, m.name, list(m.images)) for m in suffix)
        restored.rewrite()
        if self.session is not None and self.session.session_id == session_id:
            self.session = restored

    def retry(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Regenerate the answer for a user message (the one at srcIndex, or the last)."""
        if self._running:
            return {"ok": False, "error": "turn already running"}
        if self.session is None:
            return {"ok": False, "error": "no active session"}
        src = payload.get("srcIndex") if isinstance(payload, dict) else None
        prompt, images, prefix, restore_suffix = self._prepare_regenerated_turn(self._user_index_for(src))
        if prompt is None:
            return {"ok": False, "error": "no user message to retry"}
        return self._start_turn(
            prompt,
            images=images,
            restore_suffix=restore_suffix,
            prepared_messages=prefix,
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
        old_prompt, images, prefix, restore_suffix = self._prepare_regenerated_turn(
            self._user_index_for(payload.get("srcIndex"))
        )
        if old_prompt is None:
            return {"ok": False, "error": "no user message to edit"}
        return self._start_turn(
            text,
            images=images,
            restore_suffix=restore_suffix,
            prepared_messages=prefix,
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
        forked.messages = [Message(m.role, m.content, m.name, list(m.images)) for m in messages[: idx + 1]]
        forked.rewrite()
        store = SessionStore(self.settings.workspace)
        old_title = str(store.metadata(self.session.session_id).get("title") or "").strip()
        store.update_metadata(forked.session_id, title=(old_title + " · 分支") if old_title else "分支会话")
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
    ) -> None:
        with self._lock:
            turn_session_id = turn_session_id or (self.session.session_id if self.session else None)
            turn_id = turn_id or uuid4().hex
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
                    messages=[Message(m.role, m.content, m.name, list(m.images)) for m in prepared_messages],
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

            def flush_deltas() -> None:
                nonlocal delta_chars, last_delta_emit
                if not delta_parts:
                    return
                text = "".join(delta_parts)
                delta_parts.clear()
                delta_chars = 0
                last_delta_emit = time.monotonic()
                emit_turn("assistant:delta", {"text": text})

            try:
                emit_turn("turn:start", {"prompt": prompt, "thinking": self.thinking.name})

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

                client = DeepSeekClient(self.settings)
                self._active_client = client
                result_session_id = turn_session_id
                try:
                    runner = TuLAgent(
                        self.settings,
                        mode=self.mode,
                        thinking=self.thinking.name,
                        client=client,
                        approve=(lambda _n, _a: True) if self.mode in {"root", "yolo"} else self._request_approval,
                        context_tokens_hint=context_tokens_hint,
                    )
                    result = runner.run(prompt, stream=True, images=images or [], on_delta=delta, on_final=final, on_event=event, should_cancel=is_cancelled, session=turn_session, goal=goal)
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
                        Message(m.role, m.content, m.name, list(m.images)) for m in (restore_suffix or [])
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
                emit_turn("turn:done", {"sessionId": result.session_id, "rounds": result.rounds})
            except RuntimeError as exc:
                if str(exc) == "turn cancelled":
                    payload: dict[str, Any] = {"message": "当前回复已取消"}
                    if prepared_messages is not None:
                        payload["messages"] = persisted_transcript()
                    emit_turn("turn:cancelled", payload)
                else:
                    payload = desktop_error_payload(exc)
                    if prepared_messages is not None:
                        payload["messages"] = persisted_transcript()
                    emit_turn("turn:error", payload)
            except Exception as exc:
                if is_cancelled():
                    payload = {"message": "当前回复已取消"}
                    if prepared_messages is not None:
                        payload["messages"] = persisted_transcript()
                    emit_turn("turn:cancelled", payload)
                else:
                    payload = desktop_error_payload(exc)
                    if prepared_messages is not None:
                        payload["messages"] = persisted_transcript()
                    emit_turn("turn:error", payload)
            finally:
                self._abandoned_turn_ids.discard(turn_id)
                if self._active_turn_id == turn_id:
                    self._running = False
                    self._cancel_requested = False
                    self._active_turn_session_id = None
                    self._active_turn_id = None
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
                    visible.append({"role": "assistant", "content": prose, "srcIndex": idx, "intermediate": True})
                for name, arguments in tool_calls:
                    pending_tool = {
                        "role": "tool",
                        "name": name,
                        "detail": summarize_arguments(arguments),
                        "output": "",
                        "srcIndex": idx,
                    }
                    pending_tools.append(pending_tool)
                    visible.append(pending_tool)
                continue
        visible.append({"role": message.role, "content": content, "srcIndex": idx})
    finish_orphaned_tools()
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
    first_user = next((message.content for message in session.messages if message.role == "user"), "")
    from ..session import session_title_from_text

    changes: dict[str, Any] = {
        "created_at": session.created_at,
        "message_count": len(session.messages),
    }
    if not meta.get("title") and first_user:
        changes["title"] = session_title_from_text(first_user)
    store.update_metadata(session.session_id, **changes)


def parse_agent_event(text: str) -> dict[str, str]:
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
        completed = subprocess.run(
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
            completed = subprocess.run(
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
        release_desktop_instance(instance)


if __name__ == "__main__":
    main()
