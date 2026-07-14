from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

from .config import DATA_DIRNAME, LEGACY_DATA_DIRNAME
from .messages import Message


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_METADATA_LOCK = threading.RLock()
_SESSION_LIST_CACHE_LOCK = threading.RLock()
_SESSION_INDEX_VERSION = 1


@dataclass(frozen=True)
class _FileSignature:
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int


@dataclass(frozen=True)
class _SessionListSummary:
    session_id: str
    created_at: str
    messages: int
    title: str


_SESSION_LIST_CACHE: dict[Path, tuple[_FileSignature, _SessionListSummary]] = {}
_METADATA_CACHE: dict[Path, tuple[_FileSignature, dict]] = {}


def validate_session_id(session_id: str) -> str:
    value = str(session_id or "")
    if not _SESSION_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError("invalid session id")
    return value


def _message_record(message: Message) -> dict:
    """Serialize a message for the session log. Images (data-URLs) are persisted so
    a reloaded conversation — or any follow-up turn, which reloads the session — can
    still send them to a vision model, matching how Codex keeps attachments."""
    record = {"role": message.role, "content": message.content}
    if message.name:
        record["name"] = message.name
    if message.images:
        record["images"] = list(message.images)
    if message.ui_kind is not None:
        record["ui_kind"] = message.ui_kind
    if message.display_content is not None:
        record["display_content"] = message.display_content
    if not message.model_visible:
        record["model_visible"] = False
    if message.turn_id is not None:
        record["turn_id"] = message.turn_id
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
    _session_list_title: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.session_id = validate_session_id(self.session_id)

    @property
    def path(self) -> Path:
        if self.storage_path is not None:
            return self.storage_path
        return self.workspace / DATA_DIRNAME / "sessions" / f"{self.session_id}.jsonl"

    def append(self, message: Message) -> None:
        self.messages.append(message)
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"session_id": self.session_id, "created_at": self.created_at, "message": _message_record(message)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        _publish_session_summary(self)

    def rewrite(self) -> None:
        """Persist current in-memory messages, replacing the append-only log.

        Used to truncate a conversation (retry / edit-and-branch): the JSONL file is
        rewritten from self.messages instead of appended to.
        """
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{uuid4().hex}")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                for message in self.messages:
                    event = {"session_id": self.session_id, "created_at": self.created_at, "message": _message_record(message)}
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        finally:
            tmp_path.unlink(missing_ok=True)
        self._session_list_title = None
        _publish_session_summary(self)


class SessionStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.sessions_dir = self.workspace / DATA_DIRNAME / "sessions"

    def _session_dirs(self) -> tuple[Path, ...]:
        home = Path.home()
        candidates = (
            self.sessions_dir,
            self.workspace / LEGACY_DATA_DIRNAME / "sessions",
            home / DATA_DIRNAME / "sessions",
            home / LEGACY_DATA_DIRNAME / "sessions",
        )
        directories: list[Path] = []
        seen: set[str] = set()
        for directory in candidates:
            key = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(directory))))
            if key in seen:
                continue
            seen.add(key)
            directories.append(directory)
        return tuple(directories)

    def _session_candidates(self, session_id: str) -> list[tuple[Path, os.stat_result, int]]:
        session_id = validate_session_id(session_id)
        candidates: list[tuple[Path, os.stat_result, int]] = []
        for priority, directory in enumerate(self._session_dirs()):
            path = directory / f"{session_id}.jsonl"
            try:
                stat = path.stat()
                if not path.is_file():
                    continue
            except OSError:
                continue
            candidates.append((path, stat, priority))
        return candidates

    @staticmethod
    def _choose_session_candidate(
        candidates: list[tuple[Path, os.stat_result, int]],
    ) -> tuple[Path, os.stat_result, int] | None:
        if not candidates:
            return None
        # A newer write represents the latest intentional copy. Equal timestamps
        # prefer append growth, then the current workspace over compatibility paths.
        return max(
            candidates,
            key=lambda item: (item[1].st_mtime_ns, item[1].st_size, -item[2]),
        )

    def list(self) -> list[dict]:
        rows: list[dict] = []
        by_session: dict[str, list[tuple[Path, os.stat_result, int]]] = {}
        seen_paths: set[str] = set()
        for priority, directory in enumerate(self._session_dirs()):
            try:
                paths = list(directory.glob("*.jsonl"))
            except OSError:
                continue
            for path in paths:
                path_key = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                try:
                    stat = path.stat()
                    session_id = validate_session_id(path.stem)
                    if not path.is_file():
                        continue
                except (OSError, ValueError):
                    continue
                by_session.setdefault(session_id, []).append((path, stat, priority))

        selected = [
            candidate
            for candidates in by_session.values()
            if (candidate := self._choose_session_candidate(candidates)) is not None
        ]
        selected.sort(key=lambda item: item[1].st_mtime_ns, reverse=True)
        for path, known_stat, _priority in selected:
            try:
                session_id = validate_session_id(path.stem)
                meta = self.metadata(session_id, session_path=path)
                summary, stat = _session_list_summary(path, known_stat, meta)
            except (OSError, ValueError):
                continue
            rows.append(
                {
                    "session_id": summary.session_id,
                    "created_at": summary.created_at,
                    "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "path": str(path),
                    "messages": summary.messages,
                    "title": meta.get("title") or summary.title,
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
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("created_at"):
                    session.created_at = event["created_at"]
                message = event.get("message") or {}
                if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
                    continue
                images = message.get("images")
                ui_kind = message.get("ui_kind")
                display_content = message.get("display_content")
                model_visible = message.get("model_visible", True)
                turn_id = message.get("turn_id")
                session.messages.append(
                    Message(
                        role=message["role"],
                        content=message.get("content", ""),
                        name=message.get("name"),
                        images=list(images) if isinstance(images, list) else [],
                        ui_kind=ui_kind if isinstance(ui_kind, str) else None,
                        display_content=display_content if isinstance(display_content, str) else None,
                        model_visible=model_visible if isinstance(model_visible, bool) else True,
                        turn_id=turn_id if isinstance(turn_id, str) else None,
                    )
                )
        return session

    def resolve_session_path(self, session_id: str) -> Path:
        session_id = validate_session_id(session_id)
        selected = self._choose_session_candidate(self._session_candidates(session_id))
        return selected[0] if selected is not None else self.sessions_dir / f"{session_id}.jsonl"

    def delete(self, session_id: str) -> None:
        session_id = validate_session_id(session_id)
        for directory in self._session_dirs():
            session_path = directory / f"{session_id}.jsonl"
            for path in (
                session_path,
                _session_index_path(session_path),
                session_path.with_suffix(".meta.json"),
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                _invalidate_path_cache(path)

    def metadata_path(self, session_id: str) -> Path:
        session_id = validate_session_id(session_id)
        return self.sessions_dir / f"{session_id}.meta.json"

    def metadata(self, session_id: str, *, session_path: Path | None = None) -> dict:
        session_id = validate_session_id(session_id)
        if session_path is None:
            session_path = self.resolve_session_path(session_id)
        metadata_paths = [
            (directory / f"{session_id}.meta.json", priority)
            for priority, directory in enumerate(self._session_dirs())
        ]
        adjacent = session_path.with_suffix(".meta.json")
        if all(adjacent != path for path, _priority in metadata_paths):
            metadata_paths.append((adjacent, len(metadata_paths)))

        candidates: list[tuple[Path, os.stat_result, int]] = []
        seen_paths: set[str] = set()
        for path, priority in metadata_paths:
            path_key = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            try:
                stat = path.stat()
                if not path.is_file():
                    continue
            except OSError:
                continue
            candidates.append((path, stat, priority))
        # Merge oldest to newest. For equal mtimes, lower-priority compatibility
        # directories go first so the current workspace wins the final update.
        candidates.sort(key=lambda item: (item[1].st_mtime_ns, -item[2]))
        merged: dict = {}
        for path, _stat, _priority in candidates:
            merged.update(self._read_metadata(path))
        return merged

    @staticmethod
    def _read_metadata(path: Path) -> dict:
        try:
            signature = _file_signature(path.stat())
        except OSError:
            _invalidate_path_cache(path)
            return {}
        cache_key = _cache_key(path)
        with _METADATA_LOCK:
            cached = _METADATA_CACHE.get(cache_key)
            if cached and cached[0] == signature:
                return deepcopy(cached[1])
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                _METADATA_CACHE.pop(cache_key, None)
                return {}
            if not isinstance(data, dict):
                _METADATA_CACHE.pop(cache_key, None)
                return {}
            _METADATA_CACHE[cache_key] = (signature, deepcopy(data))
            return deepcopy(data)

    def update_metadata(self, session_id: str, **changes) -> dict:
        with _METADATA_LOCK:
            path = self.metadata_path(session_id)
            # Persist only current-profile state. Compatibility metadata remains a
            # read-time layer so a later legacy update to another key stays visible.
            current_data = self._read_metadata(path)
            current_data.update(changes)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
            try:
                tmp_path.write_text(
                    json.dumps(current_data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp_path, path)
            finally:
                tmp_path.unlink(missing_ok=True)
            try:
                signature = _file_signature(path.stat())
            except OSError:
                _METADATA_CACHE.pop(_cache_key(path), None)
            else:
                _METADATA_CACHE[_cache_key(path)] = (signature, deepcopy(current_data))
            return self.metadata(session_id)


def _cache_key(path: Path) -> Path:
    return path.resolve(strict=False)


def _file_signature(stat: os.stat_result) -> _FileSignature:
    return _FileSignature(
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        ctime_ns=int(stat.st_ctime_ns),
        inode=int(getattr(stat, "st_ino", 0)),
    )


def _session_index_path(session_path: Path) -> Path:
    return session_path.with_suffix(".index.json")


def _invalidate_path_cache(path: Path) -> None:
    key = _cache_key(path)
    with _SESSION_LIST_CACHE_LOCK:
        _SESSION_LIST_CACHE.pop(key, None)
    with _METADATA_LOCK:
        _METADATA_CACHE.pop(key, None)


def _summary_from_session(session: Session) -> _SessionListSummary:
    title = session._session_list_title
    if title is None:
        first_user = next(
            (
                message.content
                for message in session.messages
                if message.role == "user" and message.model_visible
            ),
            None,
        )
        title = session_title_from_text(str(first_user or ""))
        if first_user is not None:
            session._session_list_title = title
    return _SessionListSummary(
        session_id=session.session_id,
        created_at=str(session.created_at),
        messages=len(session.messages),
        title=title,
    )


def _publish_session_summary(session: Session) -> None:
    path = session.path
    try:
        if path.stem != session.session_id:
            return
        stat = path.stat()
        summary = _summary_from_session(session)
    except (OSError, ValueError):
        return
    _publish_session_list_summary(path, stat, summary)


def _publish_session_list_summary(
    path: Path,
    stat: os.stat_result,
    summary: _SessionListSummary,
) -> bool:
    signature = _file_signature(stat)
    try:
        if _file_signature(path.stat()) != signature:
            return False
    except OSError:
        return False
    payload = {
        "version": _SESSION_INDEX_VERSION,
        "session_id": summary.session_id,
        "source": asdict(signature),
        "created_at": summary.created_at,
        "messages": summary.messages,
        "title": summary.title,
    }
    index_path = _session_index_path(path)
    tmp_path = index_path.with_name(f".{index_path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    with _SESSION_LIST_CACHE_LOCK:
        try:
            index_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp_path, index_path)
        except OSError:
            pass
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        _SESSION_LIST_CACHE[_cache_key(path)] = (signature, summary)
    return True


def _read_session_index(
    path: Path,
    signature: _FileSignature,
    session_id: str,
) -> _SessionListSummary | None:
    try:
        if _session_index_path(path).stat().st_size > 64 * 1024:
            return None
        data = json.loads(_session_index_path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != _SESSION_INDEX_VERSION:
        return None
    source = data.get("source")
    if not isinstance(source, dict) or source != asdict(signature):
        return None
    if data.get("session_id") != session_id:
        return None
    messages = data.get("messages")
    if isinstance(messages, bool) or not isinstance(messages, int) or messages < 0:
        return None
    created_at = data.get("created_at")
    title = data.get("title")
    if not isinstance(created_at, str) or not isinstance(title, str):
        return None
    return _SessionListSummary(session_id, created_at, messages, title)


def _scan_session_summary(path: Path, stat: os.stat_result) -> _SessionListSummary:
    session_id = validate_session_id(path.stem)
    created_at = ""
    messages = 0
    first_user = ""
    first_user_seen = False
    # This is the one-time upgrade path for logs created before list indexes existed.
    # Records are discarded immediately, so image data is never retained as Messages.
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("created_at"):
                created_at = str(event["created_at"])
            message = event.get("message") or {}
            if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
                continue
            messages += 1
            model_visible = message.get("model_visible", True)
            if not isinstance(model_visible, bool):
                model_visible = True
            if message["role"] == "user" and model_visible and not first_user_seen:
                content = message.get("content", "")
                first_user = content if isinstance(content, str) else str(content or "")
                first_user_seen = True
    if not created_at:
        created_at = datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat()
    return _SessionListSummary(
        session_id=session_id,
        created_at=created_at,
        messages=messages,
        title=session_title_from_text(first_user),
    )


def _summary_from_current_metadata(path: Path, stat: os.stat_result, metadata: dict) -> _SessionListSummary | None:
    messages = metadata.get("message_count")
    title = metadata.get("title")
    if isinstance(messages, bool) or not isinstance(messages, int) or messages < 0 or not isinstance(title, str) or not title:
        return None
    try:
        metadata_stat = path.with_suffix(".meta.json").stat()
    except OSError:
        return None
    if metadata_stat.st_mtime_ns < stat.st_mtime_ns:
        return None
    record_count = 0
    ended_with_newline = True
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            record_count += chunk.count(b"\n")
            ended_with_newline = chunk.endswith(b"\n")
    if not ended_with_newline:
        record_count += 1
    if record_count != messages:
        return None
    created_at = metadata.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        created_at = datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat()
    return _SessionListSummary(path.stem, created_at, messages, title)


def _session_list_summary(
    path: Path,
    known_stat: os.stat_result,
    metadata: dict,
) -> tuple[_SessionListSummary, os.stat_result]:
    session_id = validate_session_id(path.stem)
    stat = known_stat
    summary: _SessionListSummary | None = None
    for _attempt in range(2):
        signature = _file_signature(stat)
        cache_key = _cache_key(path)
        with _SESSION_LIST_CACHE_LOCK:
            cached = _SESSION_LIST_CACHE.get(cache_key)
            if cached and cached[0] == signature:
                return cached[1], stat
        indexed = _read_session_index(path, signature, session_id)
        if indexed is not None:
            with _SESSION_LIST_CACHE_LOCK:
                _SESSION_LIST_CACHE[cache_key] = (signature, indexed)
            return indexed, stat
        summary = _summary_from_current_metadata(path, stat, metadata)
        if summary is None:
            summary = _scan_session_summary(path, stat)
        current_stat = path.stat()
        if _file_signature(current_stat) == signature:
            _publish_session_list_summary(path, current_stat, summary)
            return summary, current_stat
        stat = current_stat
    # A session may be receiving streamed tool records. Return the stable portion
    # already observed and let the next sidebar refresh pick up the final record.
    assert summary is not None
    return summary, stat


def safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def session_title_from_text(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return "未命名会话"
    cleaned = cleaned.replace("```", "").replace("\n", " ")
    return cleaned[:36] + ("..." if len(cleaned) > 36 else "")
