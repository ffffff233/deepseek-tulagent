from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from .messages import Message


def _message_record(message: Message) -> dict:
    """Serialize a message for the session log. Images (data-URLs) are persisted so
    a reloaded conversation — or any follow-up turn, which reloads the session — can
    still send them to a vision model, matching how Codex keeps attachments."""
    record = {"role": message.role, "content": message.content}
    if message.name:
        record["name"] = message.name
    if message.images:
        record["images"] = list(message.images)
    return record


@dataclass
class Session:
    workspace: Path
    session_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    messages: list[Message] = field(default_factory=list)
    storage_path: Path | None = None
    # In-memory-only session (subagents): kept out of the on-disk sessions/ directory so
    # a delegated subagent never shows up as its own conversation in the sidebar.
    persist: bool = True

    @property
    def path(self) -> Path:
        if self.storage_path is not None:
            return self.storage_path
        return self.workspace / ".deepseek-tulagent" / "sessions" / f"{self.session_id}.jsonl"

    def append(self, message: Message) -> None:
        self.messages.append(message)
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"session_id": self.session_id, "created_at": self.created_at, "message": _message_record(message)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def rewrite(self) -> None:
        """Persist current in-memory messages, replacing the append-only log.

        Used to truncate a conversation (retry / edit-and-branch): the JSONL file is
        rewritten from self.messages instead of appended to.
        """
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for message in self.messages:
                event = {"session_id": self.session_id, "created_at": self.created_at, "message": _message_record(message)}
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)


class SessionStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.sessions_dir = self.workspace / ".deepseek-tulagent" / "sessions"

    def list(self) -> list[dict]:
        if not self.sessions_dir.exists():
            return []
        rows: list[dict] = []
        for path in sorted(self.sessions_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
            loaded = self.load(path.stem)
            meta = self.metadata(loaded.session_id)
            first_user = next((message.content for message in loaded.messages if message.role == "user"), "")
            rows.append(
                {
                    "session_id": loaded.session_id,
                    "created_at": loaded.created_at,
                    "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                    "path": str(path),
                    "messages": len(loaded.messages),
                    "title": meta.get("title") or session_title_from_text(first_user),
                    "pinned": bool(meta.get("pinned")),
                }
            )
        return sorted(rows, key=lambda row: (bool(row["pinned"]), row["updated_at"]), reverse=True)

    def load(self, session_id: str) -> Session:
        path = self.resolve_session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"session not found: {session_id}")
        session = Session(self.workspace, session_id=session_id, storage_path=path)
        session.messages.clear()
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("created_at"):
                    session.created_at = event["created_at"]
                message = event.get("message") or {}
                images = message.get("images")
                session.messages.append(
                    Message(
                        role=message["role"],
                        content=message.get("content", ""),
                        name=message.get("name"),
                        images=list(images) if isinstance(images, list) else [],
                    )
                )
        return session

    def resolve_session_path(self, session_id: str) -> Path:
        candidates = [
            self.sessions_dir / f"{session_id}.jsonl",
            Path.home() / ".deepseek-tulagent" / "sessions" / f"{session_id}.jsonl",
        ]
        for path in candidates:
            if path.exists():
                return path
        return candidates[0]

    def delete(self, session_id: str) -> None:
        for path in (self.resolve_session_path(session_id), self.metadata_path(session_id)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def metadata_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.meta.json"

    def metadata(self, session_id: str) -> dict:
        path = self.metadata_path(session_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def update_metadata(self, session_id: str, **changes) -> dict:
        data = self.metadata(session_id)
        data.update(changes)
        path = self.metadata_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
        return data


def session_title_from_text(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return "未命名会话"
    cleaned = cleaned.replace("```", "").replace("\n", " ")
    return cleaned[:36] + ("..." if len(cleaned) > 36 else "")
