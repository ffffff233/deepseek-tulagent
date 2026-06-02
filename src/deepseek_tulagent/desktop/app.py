from __future__ import annotations

from dataclasses import asdict
import base64
import json
from pathlib import Path
import threading
import traceback
from typing import Any

from .. import __version__
from ..agent import TuLAgent
from ..config import Settings, get_settings, save_file_config
from ..messages import Message
from ..policy import ThinkingMode
from ..provider import DeepSeekClient
from ..session import Session, SessionStore
from ..skills import SkillStore


ASSET_DIR = Path(__file__).resolve().parent / "assets"
MODES = ["plan", "review", "agent", "trusted", "yolo", "root"]


class DesktopApi:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.mode = self.settings.default_mode
        self.thinking = ThinkingMode.resolve(self.settings.default_thinking)
        self.session: Session | None = None
        self.window: Any = None
        self._lock = threading.Lock()

    def bind_window(self, window: Any) -> None:
        self.window = window

    def boot(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "workspace": str(self.settings.workspace),
            "baseUrl": self.settings.base_url,
            "model": self.settings.model,
            "mode": self.mode,
            "thinking": self.thinking.name,
            "modes": list(MODES),
            "thinkingModes": ThinkingMode.names(),
            "skills": [asdict(skill) | {"path": str(skill.path)} for skill in SkillStore(self.settings.workspace).list()],
            "sessionId": self.session.session_id if self.session else None,
            "apiKeySet": bool(self.settings.api_key),
            "compatFormats": ["DeepSeek", "OpenAI-compatible"],
        }

    def configure(self, data: dict[str, Any]) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if isinstance(data.get("apiKey"), str) and data["apiKey"]:
            config["api_key"] = data["apiKey"]
        if isinstance(data.get("baseUrl"), str) and data["baseUrl"]:
            config["base_url"] = data["baseUrl"].rstrip("/")
        if isinstance(data.get("model"), str) and data["model"]:
            config["model"] = data["model"]
        if isinstance(data.get("defaultMode"), str) and data["defaultMode"]:
            config["default_mode"] = data["defaultMode"]
        if isinstance(data.get("defaultThinking"), str) and data["defaultThinking"]:
            config["default_thinking"] = data["defaultThinking"]
        save_file_config(config)
        self.settings = get_settings()
        self.mode = self.settings.default_mode
        self.thinking = ThinkingMode.resolve(self.settings.default_thinking)
        return self.boot()

    def set_runtime(self, data: dict[str, Any]) -> dict[str, Any]:
        mode = str(data.get("mode") or self.mode)
        if mode in MODES:
            self.mode = mode
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

    def sessions(self) -> list[dict[str, Any]]:
        return SessionStore(self.settings.workspace).list()

    def resume(self, session_id: str) -> dict[str, Any]:
        self.session = SessionStore(self.settings.workspace).load(session_id)
        return {"ok": True, "sessionId": self.session.session_id, "messages": serialize_messages(self.session.messages)}

    def new_session(self) -> dict[str, Any]:
        self.session = None
        return {"ok": True}

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
        prompt = str(payload.get("prompt") or "").strip()
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        if attachments:
            prompt += "\n\nAttached files:\n" + "\n".join(
                f"- {item.get('name')}: {item.get('path')} ({item.get('size')} bytes)" for item in attachments if isinstance(item, dict)
            )
        if not prompt:
            return {"ok": False, "error": "empty prompt"}
        thread = threading.Thread(target=self._run_agent_turn, args=(prompt,), daemon=True)
        thread.start()
        return {"ok": True}

    def _run_agent_turn(self, prompt: str) -> None:
        with self._lock:
            try:
                self._emit("turn:start", {"prompt": prompt, "thinking": self.thinking.name})

                def delta(text: str) -> None:
                    self._emit("assistant:delta", {"text": text})

                def event(text: str) -> None:
                    self._emit("agent:event", parse_agent_event(text))

                result = TuLAgent(
                    self.settings,
                    mode=self.mode,
                    thinking=self.thinking.name,
                    approve=(lambda _name, _args: True) if self.mode in {"root", "yolo"} else None,
                ).run(prompt, stream=True, on_delta=delta, on_event=event, session=self.session)
                self.session = SessionStore(self.settings.workspace).load(result.session_id)
                self._emit("turn:done", {"sessionId": result.session_id, "rounds": result.rounds})
            except Exception as exc:
                self._emit("turn:error", {"error": str(exc), "trace": traceback.format_exc(limit=8)})

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.window is None:
            return
        data = json.dumps({"event": event, "payload": payload}, ensure_ascii=False)
        self.window.evaluate_js(f"window.DeepSeekDesktop.onNativeEvent({data});")


def serialize_messages(messages: list[Message]) -> list[dict[str, str]]:
    visible: list[dict[str, str]] = []
    for message in messages:
        if message.role not in {"user", "assistant"}:
            continue
        content = message.content
        if content.startswith("TOOL_RESULT") or content.startswith("SUBAGENT_RESULT"):
            continue
        visible.append({"role": message.role, "content": content})
    return visible[-40:]


def parse_agent_event(text: str) -> dict[str, str]:
    if text.startswith("tool "):
        rest = text.removeprefix("tool ").strip()
        name, _, args = rest.partition(" ")
        return {"kind": "tool", "name": name, "detail": args}
    if text.startswith("done "):
        return {"kind": "done", "name": text.removeprefix("done ").strip(), "detail": ""}
    if text.startswith("thinking pass "):
        return {"kind": "thinking", "name": "internal", "detail": text}
    if text.startswith("subagent "):
        return {"kind": "subagent", "name": "subagent", "detail": text}
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
        raise SystemExit("Desktop mode requires pywebview. Install with: python -m pip install pywebview") from exc

    api = DesktopApi()
    window = webview.create_window(
        "DeepSeek TuLAgent",
        str(ASSET_DIR / "index.html"),
        js_api=api,
        width=1180,
        height=780,
        min_size=(920, 620),
        text_select=True,
    )
    api.bind_window(window)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
