from __future__ import annotations

from dataclasses import asdict, replace as replace_settings
import base64
import json
import mimetypes
from pathlib import Path
import shutil
import subprocess
import threading
import time
import traceback
from typing import Any
from uuid import uuid4

from . import DESKTOP_VERSION
from ..agent import TuLAgent, compact_context_messages, context_window_info, estimate_message_tokens, summarize_arguments
from ..config import Settings, get_settings, merge_file_config
from ..messages import Message
from ..policy import ThinkingMode
from ..provider import DeepSeekClient, UsageStats, apply_thinking_payload
from ..session import Session, SessionStore
from ..skills import SkillStore


ASSET_DIR = Path(__file__).resolve().parent / "assets"
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
THINKING_TIERS = ["instant", "fast", "balanced", "deep", "ultra"]
THINKING_LABELS = {
    "instant": "Minimal",
    "fast": "Low",
    "balanced": "Medium",
    "deep": "High",
    "ultra": "Extra High",
}


class DesktopApi:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.mode = coerce_permission_tier(self.settings.default_mode)
        self.thinking = ThinkingMode.resolve(self.settings.default_thinking)
        self.session: Session | None = None
        self.window: Any = None
        self._lock = threading.Lock()
        self._running = False
        self._cancel_requested = False
        self._approvals: dict[str, dict[str, Any]] = {}
        self._active_turn_session_id: str | None = None
        self._active_turn_id: str | None = None
        self._pending_turn: dict[str, Any] | None = None
        self._abandoned_turn_ids: set[str] = set()
        self._models_cache: dict[str, tuple[float, list[str]]] = {}
        self._last_usage = UsageStats()
        self._usage_total = UsageStats()
        self._usage_by_session: dict[str, UsageStats] = {}
        self._context_by_session: dict[str, dict[str, Any]] = {}

    def bind_window(self, window: Any) -> None:
        self.window = window

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

    def configure(self, data: dict[str, Any]) -> dict[str, Any]:
        config: dict[str, Any] = {}
        for source, target in {
            "apiKey": "api_key",
            "baseUrl": "base_url",
            "model": "model",
            "providerFormat": "provider_format",
            "defaultMode": "default_mode",
            "defaultThinking": "default_thinking",
        }.items():
            value = data.get(source)
            if isinstance(value, str) and value.strip():
                config[target] = value.strip().rstrip("/") if source == "baseUrl" else value.strip()
        # keep the currently-selected model across the reload — get_settings() would
        # otherwise reset it to the file default (deepseek-v4-flash) every time the user
        # changes provider or saves settings.
        keep_model = self.settings.model
        merge_file_config(config)
        self.settings = get_settings()
        if "model" not in config and keep_model:
            self.settings = self.settings.with_runtime(model=keep_model)
        self.mode = coerce_permission_tier(self.settings.default_mode)
        self.thinking = ThinkingMode.resolve(self.settings.default_thinking)
        return self.boot()

    def set_runtime(self, data: dict[str, Any]) -> dict[str, Any]:
        mode = str(data.get("mode") or self.mode)
        if mode in MODES:
            self.mode = coerce_permission_tier(mode)
        thinking_name = str(data.get("thinking") or self.thinking.name)
        if thinking_name in ThinkingMode.names():
            self.thinking = ThinkingMode.resolve(thinking_name)
        model = str(data.get("model") or self.settings.model)
        self.settings = self.settings.with_runtime(
            model=model,
            max_tokens=self.thinking.max_tokens,
            thinking_enabled=self.thinking.api_thinking,
            reasoning_effort=self.thinking.reasoning_effort,
        )
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
        client = DeepSeekClient(probe)
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

    def sessions(self) -> list[dict[str, Any]]:
        return SessionStore(self.settings.workspace).list()

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        title = " ".join(str(title).strip().split())[:80]
        if not title:
            return {"ok": False, "error": "empty title"}
        SessionStore(self.settings.workspace).update_metadata(session_id, title=title)
        return {"ok": True, "sessions": self.sessions()}

    def pin_session(self, session_id: str, pinned: bool = True) -> dict[str, Any]:
        SessionStore(self.settings.workspace).update_metadata(session_id, pinned=bool(pinned))
        return {"ok": True, "sessions": self.sessions()}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        SessionStore(self.settings.workspace).delete(session_id)
        self._context_by_session.pop(session_id, None)
        self._usage_by_session.pop(session_id, None)
        if self.session is not None and self.session.session_id == session_id:
            self.session = None
        return {"ok": True, "sessions": self.sessions(), "context": self.context_status()}

    def resume(self, session_id: str) -> dict[str, Any]:
        self.session = SessionStore(self.settings.workspace).load(session_id)
        self._restore_context_usage(session_id)
        return {
            "ok": True,
            "sessionId": self.session.session_id,
            "messages": serialize_messages(self.session.messages),
            "context": self.context_status(),
        }

    def new_session(self) -> dict[str, Any]:
        self.session = None
        return {"ok": True, "sessionId": None, "messages": [], "context": self.context_status()}

    def compact(self) -> dict[str, Any]:
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
        local_delta = local_tokens - baseline if use_upstream else 0
        context_tokens = max(0, int(snapshot["tokens"]) + local_delta) if use_upstream else local_tokens
        usage = snapshot.get("usage") if use_upstream else UsageStats()
        exact_upstream = bool(use_upstream and local_delta == 0)
        known_context = use_upstream
        percent = round((context_tokens / limit * 100), 1) if known_context and limit else None
        threshold_percent_used = round((threshold / limit * 100), 1) if limit else threshold_percent
        return {
            "ok": True,
            "tokens": context_tokens if known_context else None,
            "contextTokens": context_tokens if known_context else None,
            "localVisibleTokens": local_tokens,
            "estimatedInputTokens": local_tokens,
            "estimatedOutputTokens": max(0, context_tokens - int(getattr(usage, "input_tokens", 0))),
            "inputTokens": int(getattr(usage, "input_tokens", 0)),
            "outputTokens": int(getattr(usage, "output_tokens", 0)),
            "cachedTokens": int(getattr(usage, "cached_input_tokens", 0)),
            "cachePercent": round(int(getattr(usage, "cached_input_tokens", 0)) / max(1, int(getattr(usage, "input_tokens", 0))) * 100, 1),
            "usageTotalTokens": int(getattr(usage, "total_tokens", 0)),
            "limit": limit,
            "detectedLimit": detected_limit,
            "customLimit": bool(self.settings.context_window_tokens),
            "threshold": threshold,
            "thresholdPercent": threshold_percent_used,
            "percent": percent,
            "remainingTokens": max(0, threshold - context_tokens) if known_context else None,
            "source": "upstream" if exact_upstream else ("upstream-stale" if use_upstream else ("custom" if self.settings.context_window_tokens else info.get("source", "fallback"))),
            "limitSource": "custom" if self.settings.context_window_tokens else info.get("source", "fallback"),
            "accurate": exact_upstream,
            "usageAvailable": use_upstream,
            "usageState": "current" if exact_upstream else ("adjusted" if use_upstream else "missing"),
            "measure": "上游输入实测" if exact_upstream else ("上次上游输入 + 当前会话增量" if use_upstream else "上游未返回 usage，仅估算本地可见消息"),
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
        if threshold is not None:
            current["compact_threshold_percent"] = threshold
        merge_file_config(current)
        self.settings = get_settings().with_runtime(
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
        data = base64.b64decode(content)
        upload_dir = self.settings.workspace / ".deepseek-tulagent" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        path = upload_dir / name
        path.write_bytes(data)
        result = {"ok": True, "name": name, "path": str(path), "size": len(data)}
        if "/" in raw_name.replace("\\", "/"):
            result["kind"] = "folder_file"
        if is_video_upload(name, media_type):
            frames = extract_video_frames(path)
            result["kind"] = "video"
            result["frames"] = frames
            result["frameCount"] = len(frames)
        return result

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
    ) -> dict[str, Any]:
        self._cancel_requested = False
        self._running = True
        if self.session is None:
            self.session = Session(self.settings.workspace)
        session_id = self.session.session_id
        turn_id = uuid4().hex
        thread = threading.Thread(
            target=self._run_agent_turn,
            args=(prompt, images or [], session_id, turn_id, goal, restore_suffix),
            daemon=True,
        )
        thread.start()
        return {"ok": True, "sessionId": session_id, "turnId": turn_id}

    def _queue_turn_after_cancel(self, prompt: str, images: list[str] | None = None, goal: str | None = None) -> dict[str, Any]:
        if self.session is None:
            self.session = Session(self.settings.workspace)
        session_id = self.session.session_id
        turn_id = uuid4().hex
        self._pending_turn = {
            "prompt": prompt,
            "images": images or [],
            "session_id": session_id,
            "turn_id": turn_id,
            "goal": goal,
        }
        return {"ok": True, "queued": True, "sessionId": session_id, "turnId": turn_id}

    def _start_pending_turn_if_any(self) -> None:
        pending = self._pending_turn
        self._pending_turn = None
        if not pending:
            return
        session_id = str(pending["session_id"])
        if self.session is None or self.session.session_id != session_id:
            try:
                self.session = SessionStore(self.settings.workspace).load(session_id)
            except FileNotFoundError:
                self.session = Session(self.settings.workspace, session_id=session_id)
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

    def _prepare_regenerated_turn(self, before_index: int | None = None) -> tuple[str, list[Message]] | tuple[None, list[Message]]:
        """Drop the target turn from model context; preserve later turns for UI/session.

        With before_index, target the user message at/nearest-before that transcript
        index (for retry/edit on any message); otherwise the last user message.
        Skips tool-result / subagent-result messages (which also carry role 'user').
        Rewrites the session log to the clean prefix before running, then the caller
        passes the preserved suffix to _start_turn so it can be restored after the new
        answer is persisted.
        """
        if self.session is None:
            return None, []
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
                restore_suffix = [Message(m.role, m.content, m.name, list(m.images)) for m in messages[suffix_start:]]
                self.session.messages = messages[:i]
                self.session.rewrite()
                return prompt, restore_suffix
        return None, []

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
        prompt, restore_suffix = self._prepare_regenerated_turn(self._user_index_for(src))
        if prompt is None:
            return {"ok": False, "error": "no user message to retry"}
        return self._start_turn(prompt, restore_suffix=restore_suffix)

    def edit_resend(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Branch: replace a user message (at srcIndex, or the last) with edited text."""
        if self._running:
            return {"ok": False, "error": "turn already running"}
        if self.session is None:
            return {"ok": False, "error": "no active session"}
        text = str(payload.get("prompt") or "").strip()
        if not text:
            return {"ok": False, "error": "empty prompt"}
        _old_prompt, restore_suffix = self._prepare_regenerated_turn(self._user_index_for(payload.get("srcIndex")))
        return self._start_turn(text, restore_suffix=restore_suffix)

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
        forked.messages = [Message(m.role, m.content, m.name) for m in messages[: idx + 1]]
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
        if not self._running:
            return {"ok": True, "running": False}
        requested_turn_id = str((data or {}).get("turnId") or "").strip()
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
        self._running = False
        self._cancel_requested = False
        self._active_turn_session_id = None
        self._active_turn_id = None
        return {"ok": True, "running": False}

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
    ) -> None:
        with self._lock:
            turn_session_id = turn_session_id or (self.session.session_id if self.session else None)
            turn_id = turn_id or uuid4().hex
            self._active_turn_session_id = turn_session_id
            self._active_turn_id = turn_id
            try:
                turn_session = SessionStore(self.settings.workspace).load(str(turn_session_id))
            except FileNotFoundError:
                turn_session = Session(self.settings.workspace, session_id=str(turn_session_id or uuid4()))

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

            try:
                emit_turn("turn:start", {"prompt": prompt, "thinking": self.thinking.name})

                def delta(text: str) -> None:
                    if is_cancelled():
                        raise RuntimeError("turn cancelled")
                    emit_turn("assistant:delta", {"text": text})

                def final(text: str) -> None:
                    if is_cancelled():
                        raise RuntimeError("turn cancelled")
                    emit_turn("assistant:final", {"text": text})

                def event(text: str) -> None:
                    if is_cancelled():
                        raise RuntimeError("turn cancelled")
                    emit_turn("agent:event", parse_agent_event(text))

                client = DeepSeekClient(self.settings)
                result_session_id = turn_session_id
                try:
                    runner = TuLAgent(
                        self.settings,
                        mode=self.mode,
                        thinking=self.thinking.name,
                        client=client,
                        approve=(lambda _n, _a: True) if self.mode in {"root", "yolo"} else self._request_approval,
                    )
                    result = runner.run(prompt, stream=True, images=images or [], on_delta=delta, on_final=final, on_event=event, should_cancel=is_cancelled, session=turn_session, goal=goal)
                    result_session_id = result.session_id
                finally:
                    self._last_usage = client.usage
                    self._record_session_usage(result_session_id, client.usage)
                # Only adopt the finished turn's session if the user hasn't switched to a
                # different conversation meanwhile — otherwise the next send would land in
                # the OLD conversation (context bleeding across chats).
                current_id = self.session.session_id if self.session else None
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
                self._restore_regenerated_suffix(turn_session_id, restore_suffix)
                if str(exc) == "turn cancelled":
                    emit_turn("turn:cancelled", {"message": "当前回复已取消"})
                else:
                    emit_turn("turn:error", desktop_error_payload(exc))
            except Exception as exc:
                self._restore_regenerated_suffix(turn_session_id, restore_suffix)
                emit_turn("turn:error", desktop_error_payload(exc))
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
        adjusted = max(0, usage.input_tokens + current_estimate - request_estimate) if request_messages else usage.input_tokens
        snapshot = {
            "model": self.settings.model,
            "tokens": adjusted,
            "usage": usage,
            "currentEstimate": current_estimate,
        }
        self._context_by_session[session_id] = snapshot
        SessionStore(self.settings.workspace).update_metadata(
            session_id,
            context_usage={
                "model": self.settings.model,
                "tokens": adjusted,
                "current_estimate": current_estimate,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "total_tokens": usage.total_tokens,
                "source": usage.source,
            },
        )

    def _restore_context_usage(self, session_id: str) -> None:
        stored = SessionStore(self.settings.workspace).metadata(session_id).get("context_usage")
        if not isinstance(stored, dict):
            self._context_by_session.pop(session_id, None)
            return
        try:
            usage = UsageStats(
                input_tokens=int(stored.get("input_tokens") or 0),
                output_tokens=int(stored.get("output_tokens") or 0),
                cached_input_tokens=int(stored.get("cached_input_tokens") or 0),
                total_tokens=int(stored.get("total_tokens") or 0),
                source=str(stored.get("source") or "upstream"),
            )
            tokens = int(stored.get("tokens") or 0)
            current_estimate = int(stored.get("current_estimate") or 0)
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
        }

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.window is None:
            return
        data = json.dumps({"event": event, "payload": payload}, ensure_ascii=False)
        # U+2028/U+2029 are valid inside JSON strings but are line terminators in
        # JS source, which would break evaluate_js and silently drop the event.
        data = data.replace(" ", "\\u2028").replace(" ", "\\u2029")
        try:
            self.window.evaluate_js(f"window.DeepSeekDesktop.onNativeEvent({data});")
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
        if kind in {"folder", "folder_file", "video"} and path:
            path_files.append(f"- {name}: {path}{suffix}")
        else:
            private_files.append(f"- {name}{suffix}")
    parts: list[str] = []
    if private_files:
        parts.append("附件文件（普通文件只记录名称和大小，不写入本地路径）：\n" + "\n".join(private_files))
    if path_files:
        parts.append("文件夹/媒体附件（只有这些附件会直接提供本地路径）：\n" + "\n".join(path_files))
        parts.append("如果需要查看文件夹/媒体内容，请调用 inspect_media(path) 或 read_file(path)。")
    elif private_files:
        parts.append("普通文件不会把本地路径写入对话正文；如需读取内容，请让用户提供文件夹或明确路径。")
    return "\n".join(parts)


def serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Rebuild the desktop transcript from stored messages.

    An assistant message that is a tool call is emitted as a structured tool block
    (with the surrounding prose split out) and paired with the following TOOL_RESULT /
    SUBAGENT_RESULT so a resumed conversation shows tool cards instead of raw JSON.
    """
    from ..agent import is_tool_intro_only, parse_tool_call, strip_tool_call_display, summarize_arguments

    visible: list[dict[str, Any]] = []
    pending_tool: dict[str, Any] | None = None
    for idx, message in enumerate(messages):
        content = message.content
        if content.startswith(("TOOL_RESULT", "SUBAGENT_RESULT", "USER_ANSWER")):
            if pending_tool is not None:
                _, _, body = content.partition("\n")
                pending_tool["output"] = body
                pending_tool = None
            continue
        if message.role not in {"user", "assistant"}:
            continue
        if message.role == "assistant":
            tool_call = parse_tool_call(content)
            if tool_call:
                name, arguments = tool_call
                prose = strip_tool_call_display(content)
                if prose and not is_tool_intro_only(prose):
                    # pre-tool narration — not a standalone reply, carries no retry/branch
                    visible.append({"role": "assistant", "content": prose, "srcIndex": idx, "intermediate": True})
                pending_tool = {
                    "role": "tool",
                    "name": name,
                    "detail": summarize_arguments(arguments),
                    "output": "",
                    "srcIndex": idx,
                }
                visible.append(pending_tool)
                continue
        pending_tool = None
        visible.append({"role": message.role, "content": content, "srcIndex": idx})
    return visible[-320:]


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
    if meta.get("title"):
        return
    first_user = next((message.content for message in session.messages if message.role == "user"), "")
    from ..session import session_title_from_text

    store.update_metadata(session.session_id, title=session_title_from_text(first_user))


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


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg"}


def is_video_upload(name: str, media_type: str = "") -> bool:
    guessed = mimetypes.guess_type(name)[0] or ""
    return media_type.startswith("video/") or guessed.startswith("video/") or Path(name).suffix.lower() in VIDEO_EXTENSIONS


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


def main() -> None:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "桌面端需要 pywebview。安装：py -3 -m pip install --upgrade pywebview"
        ) from exc

    api = DesktopApi()
    window = webview.create_window(
        "DeepSeekFathom",
        str(ASSET_DIR / "index.html"),
        js_api=api,
        width=1180,
        height=780,
        min_size=(920, 620),
        text_select=True,
    )
    api.bind_window(window)
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


if __name__ == "__main__":
    main()
