from __future__ import annotations

from dataclasses import asdict, replace as replace_settings
import base64
import json
from pathlib import Path
import threading
import traceback
from typing import Any
from uuid import uuid4

from .. import __version__
from ..agent import TuLAgent, compact_context_messages, context_window_info, estimate_message_tokens, summarize_arguments
from ..config import Settings, get_settings, merge_file_config
from ..messages import Message
from ..policy import ThinkingMode
from ..provider import DeepSeekClient, apply_thinking_payload
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

    def bind_window(self, window: Any) -> None:
        self.window = window

    def boot(self) -> dict[str, Any]:
        return {
            "version": __version__,
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

    def models(self) -> dict[str, Any]:
        try:
            models = DeepSeekClient(self.settings).models()
            return {"ok": True, "models": models}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "models": [self.settings.model]}

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
        if self.session is not None and self.session.session_id == session_id:
            self.session = None
        return {"ok": True, "sessions": self.sessions()}

    def resume(self, session_id: str) -> dict[str, Any]:
        self.session = SessionStore(self.settings.workspace).load(session_id)
        return {"ok": True, "sessionId": self.session.session_id, "messages": serialize_messages(self.session.messages)}

    def new_session(self) -> dict[str, Any]:
        self.session = None
        return {"ok": True}

    def compact(self) -> dict[str, Any]:
        if self.session is None:
            return {"ok": False, "error": "no active session"}
        before = estimate_message_tokens(self.session.messages)
        self.session.messages = compact_context_messages(
            self.session.messages, self.settings.model, force=True, client=DeepSeekClient(self.settings)
        )
        after = estimate_message_tokens(self.session.messages)
        return {"ok": True, "before": before, "after": after, "messages": serialize_messages(self.session.messages), "context": self.context_status()}

    def context_status(self) -> dict[str, Any]:
        messages = self.session.messages if self.session else []
        tokens = estimate_message_tokens(messages) if messages else 0
        cached_tokens = estimate_cached_context_tokens(messages)
        info = context_window_info(self.settings.model)
        limit = int(info["tokens"])
        threshold = int(limit * 0.92)
        percent = round((tokens / limit * 100), 1) if limit else 0
        cache_percent = round((cached_tokens / tokens * 100), 1) if tokens else 0
        return {
            "ok": True,
            "tokens": tokens,
            "cachedTokens": cached_tokens,
            "cachePercent": cache_percent,
            "limit": limit,
            "threshold": threshold,
            "percent": percent,
            "source": info.get("source", "fallback"),
            "model": self.settings.model,
            "autoCompact": True,
            "nearLimit": tokens >= int(limit * 0.75),
            "needsCompact": tokens >= threshold,
        }

    def save_upload(self, file: dict[str, Any]) -> dict[str, Any]:
        name = safe_upload_name(str(file.get("name") or "upload.bin"))
        content = str(file.get("content") or "")
        if "," in content:
            content = content.split(",", 1)[1]
        data = base64.b64decode(content)
        upload_dir = self.settings.workspace / ".deepseek-tulagent" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        path = upload_dir / name
        path.write_bytes(data)
        return {"ok": True, "name": name, "path": str(path), "size": len(data)}

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._running:
            return {"ok": False, "error": "turn already running"}
        prompt = str(payload.get("prompt") or "").strip()
        goal = str(payload.get("goal") or "").strip() or None
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        images = [str(u) for u in (payload.get("images") or []) if isinstance(u, str) and u.startswith("data:")]
        non_image = [item for item in attachments if isinstance(item, dict)]
        if non_image:
            prompt += "\n\nAttached files:\n" + "\n".join(
                f"- {item.get('name')}: {item.get('path')} ({item.get('size')} bytes)" for item in non_image
            )
        if images and not prompt:
            prompt = "请看这张图片。"
        if not prompt and not images:
            return {"ok": False, "error": "empty prompt"}
        return self._start_turn(prompt, images=images, goal=goal)

    def _start_turn(self, prompt: str, images: list[str] | None = None, goal: str | None = None) -> dict[str, Any]:
        self._cancel_requested = False
        self._running = True
        if self.session is None:
            self.session = Session(self.settings.workspace)
        session_id = self.session.session_id
        turn_id = uuid4().hex
        thread = threading.Thread(target=self._run_agent_turn, args=(prompt, images or [], session_id, turn_id, goal), daemon=True)
        thread.start()
        return {"ok": True, "sessionId": session_id, "turnId": turn_id}

    def _truncate_to_last_user(self, before_index: int | None = None) -> str | None:
        """Drop a user turn and everything after it; return that user prompt.

        With before_index, target the user message at/nearest-before that transcript
        index (for retry/edit on any message); otherwise the last user message.
        Skips tool-result / subagent-result messages (which also carry role 'user').
        Rewrites the session log so a fresh turn appends cleanly.
        """
        if self.session is None:
            return None
        messages = self.session.messages
        start = len(messages) - 1 if before_index is None else min(before_index, len(messages) - 1)
        for i in range(start, -1, -1):
            message = messages[i]
            if message.role == "user" and not message.content.startswith(("TOOL_RESULT", "SUBAGENT_RESULT")):
                prompt = message.content
                self.session.messages = messages[:i]
                self.session.rewrite()
                return prompt
        return None

    def _user_index_for(self, src_index: int | None) -> int | None:
        """Given a transcript index (any message), return the index to truncate at
        for a retry/edit: the user message at/just before src_index."""
        if self.session is None or src_index is None:
            return None
        return min(src_index, len(self.session.messages) - 1)

    def retry(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Regenerate the answer for a user message (the one at srcIndex, or the last)."""
        if self._running:
            return {"ok": False, "error": "turn already running"}
        if self.session is None:
            return {"ok": False, "error": "no active session"}
        src = payload.get("srcIndex") if isinstance(payload, dict) else None
        prompt = self._truncate_to_last_user(self._user_index_for(src))
        if prompt is None:
            return {"ok": False, "error": "no user message to retry"}
        return self._start_turn(prompt)

    def edit_resend(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Branch: replace a user message (at srcIndex, or the last) with edited text."""
        if self._running:
            return {"ok": False, "error": "turn already running"}
        if self.session is None:
            return {"ok": False, "error": "no active session"}
        text = str(payload.get("prompt") or "").strip()
        if not text:
            return {"ok": False, "error": "empty prompt"}
        self._truncate_to_last_user(self._user_index_for(payload.get("srcIndex")))
        return self._start_turn(text)

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

    def cancel(self) -> dict[str, Any]:
        if not self._running:
            return {"ok": True, "running": False}
        self._cancel_requested = True
        # unblock any pending approval so the turn can wind down
        for pending in list(self._approvals.values()):
            pending["decision"] = False
            pending["event"].set()
        self._emit("turn:cancel", {
            "message": "正在取消当前回复；已发出的工具会等待当前调用返回。",
            "sessionId": self._active_turn_session_id,
            "turnId": self._active_turn_id,
        })
        return {"ok": True, "running": True}

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
    ) -> None:
        with self._lock:
            turn_session_id = turn_session_id or (self.session.session_id if self.session else None)
            turn_id = turn_id or uuid4().hex
            self._active_turn_session_id = turn_session_id
            self._active_turn_id = turn_id

            def emit_turn(event: str, payload: dict[str, Any] | None = None) -> None:
                scoped = dict(payload or {})
                scoped.setdefault("sessionId", turn_session_id)
                scoped.setdefault("turnId", turn_id)
                self._emit(event, scoped)

            try:
                emit_turn("turn:start", {"prompt": prompt, "thinking": self.thinking.name})

                def delta(text: str) -> None:
                    if self._cancel_requested:
                        raise RuntimeError("turn cancelled")
                    emit_turn("assistant:delta", {"text": text})

                def final(text: str) -> None:
                    if self._cancel_requested:
                        raise RuntimeError("turn cancelled")
                    emit_turn("assistant:final", {"text": text})

                def event(text: str) -> None:
                    if self._cancel_requested:
                        raise RuntimeError("turn cancelled")
                    emit_turn("agent:event", parse_agent_event(text))

                result = TuLAgent(
                    self.settings,
                    mode=self.mode,
                    thinking=self.thinking.name,
                    approve=(lambda _n, _a: True) if self.mode in {"root", "yolo"} else self._request_approval,
                ).run(prompt, stream=True, images=images or [], on_delta=delta, on_final=final, on_event=event, session=self.session, goal=goal)
                # Only adopt the finished turn's session if the user hasn't switched to a
                # different conversation meanwhile — otherwise the next send would land in
                # the OLD conversation (context bleeding across chats).
                current_id = self.session.session_id if self.session else None
                if current_id in (turn_session_id, result.session_id):
                    self.session = SessionStore(self.settings.workspace).load(result.session_id)
                ensure_session_title(self.settings.workspace, SessionStore(self.settings.workspace).load(result.session_id))
                emit_turn("turn:done", {"sessionId": result.session_id, "rounds": result.rounds})
            except RuntimeError as exc:
                if str(exc) == "turn cancelled":
                    emit_turn("turn:cancelled", {"message": "当前回复已取消"})
                else:
                    emit_turn("turn:error", {"error": str(exc), "trace": traceback.format_exc(limit=8)})
            except Exception as exc:
                emit_turn("turn:error", {"error": str(exc), "trace": traceback.format_exc(limit=8)})
            finally:
                self._running = False
                self._cancel_requested = False
                self._active_turn_session_id = None
                self._active_turn_id = None

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


def main() -> None:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "桌面端需要 pywebview。安装：py -3 -m pip install --upgrade \"deepseek-tulagent[desktop]\""
        ) from exc

    api = DesktopApi()
    window = webview.create_window(
        "Fathom",
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
