from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
from typing import Any, Iterable
from uuid import uuid4
import zlib

from .config import DATA_DIRNAME, LEGACY_DATA_DIRNAME, config_home
from .processes import popen_hidden


SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChangeLimits:
    max_files: int = 512
    max_total_bytes: int = 64 * 1024 * 1024
    max_file_bytes: int = 16 * 1024 * 1024
    max_binary_files: int = 64
    max_path_bytes: int = 4096
    max_diff_bytes: int = 4 * 1024 * 1024
    max_file_diff_bytes: int = 512 * 1024
    max_page_bytes: int = 64 * 1024
    command_timeout: float = 30.0

    def __post_init__(self) -> None:
        positive = (
            self.max_files,
            self.max_total_bytes,
            self.max_file_bytes,
            self.max_path_bytes,
            self.max_diff_bytes,
            self.max_file_diff_bytes,
            self.max_page_bytes,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("review limits must be positive")
        if self.max_binary_files < 0 or self.command_timeout <= 0:
            raise ValueError("review limits must be non-negative and timeout must be positive")


@dataclass(frozen=True)
class ChangeSnapshot:
    snapshot_id: str
    workspace: str
    repository: str
    created_at: str
    supported: bool
    complete: bool
    reason: str = ""
    branch: str = ""
    head: str = ""
    head_tree: str = ""
    tree: str = ""
    content_hash: str = ""
    changed_files: int = 0
    changed_bytes: int = 0
    binary_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "snapshotId": self.snapshot_id,
            "workspace": self.workspace,
            "repository": self.repository,
            "createdAt": self.created_at,
            "supported": self.supported,
            "complete": self.complete,
            "reason": self.reason or None,
            "branch": self.branch or None,
            "head": self.head or None,
            "headTree": self.head_tree or None,
            "tree": self.tree or None,
            "hash": self.content_hash or None,
            "changedFiles": self.changed_files,
            "changedBytes": self.changed_bytes,
            "binaryFiles": self.binary_files,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeSnapshot:
        return cls(
            snapshot_id=str(data.get("snapshotId") or ""),
            workspace=str(data.get("workspace") or ""),
            repository=str(data.get("repository") or ""),
            created_at=str(data.get("createdAt") or ""),
            supported=bool(data.get("supported")),
            complete=bool(data.get("complete")),
            reason=str(data.get("reason") or ""),
            branch=str(data.get("branch") or ""),
            head=str(data.get("head") or ""),
            head_tree=str(data.get("headTree") or ""),
            tree=str(data.get("tree") or ""),
            content_hash=str(data.get("hash") or ""),
            changed_files=int(data.get("changedFiles") or 0),
            changed_bytes=int(data.get("changedBytes") or 0),
            binary_files=int(data.get("binaryFiles") or 0),
        )


@dataclass(frozen=True)
class ChangeFile:
    path: str
    status: str
    old_path: str = ""
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    diff_bytes: int = 0
    truncated: bool = False
    diff_hash: str = ""
    artifact: str = field(default="", repr=False)

    def to_dict(self, *, internal: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": self.path,
            "oldPath": self.old_path or None,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
            "diffBytes": self.diff_bytes,
            "truncated": self.truncated,
            "diffHash": self.diff_hash,
        }
        if internal:
            result["artifact"] = self.artifact
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeFile:
        return cls(
            path=str(data.get("path") or ""),
            old_path=str(data.get("oldPath") or ""),
            status=str(data.get("status") or ""),
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
            binary=bool(data.get("binary")),
            diff_bytes=int(data.get("diffBytes") or 0),
            truncated=bool(data.get("truncated")),
            diff_hash=str(data.get("diffHash") or ""),
            artifact=str(data.get("artifact") or ""),
        )


@dataclass(frozen=True)
class ChangeManifest:
    change_id: str
    scope: str
    repository: str
    created_at: str
    supported: bool
    complete: bool
    reason: str
    base_id: str
    target_id: str
    base_tree: str
    target_tree: str
    target_hash: str
    change_hash: str
    files: tuple[ChangeFile, ...] = ()
    total_files: int = 0
    omitted_files: int = 0
    diff_bytes: int = 0
    truncated: bool = False

    def to_dict(self, *, internal: bool = False) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "changeId": self.change_id,
            "scope": self.scope,
            "repository": self.repository,
            "createdAt": self.created_at,
            "supported": self.supported,
            "complete": self.complete,
            "reason": self.reason or None,
            "baseId": self.base_id or None,
            "targetId": self.target_id or None,
            "baseTree": self.base_tree or None,
            "targetTree": self.target_tree or None,
            "targetHash": self.target_hash or None,
            "changeHash": self.change_hash or None,
            "files": [item.to_dict(internal=internal) for item in self.files],
            "totalFiles": self.total_files,
            "includedFiles": len(self.files),
            "omittedFiles": self.omitted_files,
            "diffBytes": self.diff_bytes,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeManifest:
        raw_files = data.get("files") if isinstance(data.get("files"), list) else []
        return cls(
            change_id=str(data.get("changeId") or ""),
            scope=str(data.get("scope") or ""),
            repository=str(data.get("repository") or ""),
            created_at=str(data.get("createdAt") or ""),
            supported=bool(data.get("supported")),
            complete=bool(data.get("complete")),
            reason=str(data.get("reason") or ""),
            base_id=str(data.get("baseId") or ""),
            target_id=str(data.get("targetId") or ""),
            base_tree=str(data.get("baseTree") or ""),
            target_tree=str(data.get("targetTree") or ""),
            target_hash=str(data.get("targetHash") or ""),
            change_hash=str(data.get("changeHash") or ""),
            files=tuple(ChangeFile.from_dict(item) for item in raw_files if isinstance(item, dict)),
            total_files=int(data.get("totalFiles") or 0),
            omitted_files=int(data.get("omittedFiles") or 0),
            diff_bytes=int(data.get("diffBytes") or 0),
            truncated=bool(data.get("truncated")),
        )


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    output: bytes
    truncated: bool
    timed_out: bool


@dataclass(frozen=True)
class _Repository:
    root: Path
    common_dir: Path
    objects_dir: Path
    head: str
    head_tree: str
    branch: str
    object_format: str
    filter_names: tuple[str, ...]


@dataclass(frozen=True)
class _StatusEntry:
    status: str
    path: str
    old_path: str = ""


class ReviewStore:
    def __init__(
        self,
        root: Path | None = None,
        *,
        max_records: int = 64,
        max_page_bytes: int = 64 * 1024,
    ) -> None:
        if max_records <= 0 or max_page_bytes <= 0:
            raise ValueError("review store limits must be positive")
        self.root = (Path(root) if root is not None else config_home() / "reviews").expanduser().resolve()
        self.max_records = max_records
        self.max_page_bytes = max_page_bytes
        self._lock = threading.RLock()

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def changes_dir(self) -> Path:
        return self.root / "changes"

    def snapshot_dir(self, snapshot_id: str) -> Path:
        return self.snapshots_dir / _safe_id(snapshot_id)

    def change_dir(self, change_id: str) -> Path:
        return self.changes_dir / _safe_id(change_id)

    def save_snapshot(self, snapshot: ChangeSnapshot) -> None:
        with self._lock:
            target = self.snapshot_dir(snapshot.snapshot_id)
            self._prepare_directory(target)
            _atomic_write_json(target / "snapshot.json", snapshot.to_dict())
            self._prune(self.snapshots_dir)

    def load_snapshot(self, snapshot_id: str) -> ChangeSnapshot:
        path = self.snapshot_dir(snapshot_id) / "snapshot.json"
        data = _read_json(path)
        snapshot = ChangeSnapshot.from_dict(data)
        if snapshot.snapshot_id != snapshot_id:
            raise ReviewError("snapshot id does not match its storage record")
        return snapshot

    def save_change(self, manifest: ChangeManifest, diffs: dict[str, bytes]) -> None:
        with self._lock:
            target = self.change_dir(manifest.change_id)
            self._prepare_directory(target)
            for item in manifest.files:
                if not item.artifact:
                    continue
                payload = diffs.get(item.artifact, b"")
                _atomic_write_bytes(self._artifact_path(manifest.change_id, item.artifact), payload)
            _atomic_write_json(target / "manifest.json", manifest.to_dict(internal=True))
            self._prune(self.changes_dir)

    def load_manifest(self, change_id: str) -> ChangeManifest:
        path = self.change_dir(change_id) / "manifest.json"
        manifest = ChangeManifest.from_dict(_read_json(path))
        if manifest.change_id != change_id:
            raise ReviewError("change id does not match its storage record")
        return manifest

    def manifest(self, change_id: str) -> dict[str, Any]:
        return self.load_manifest(change_id).to_dict()

    def read_file_diff(
        self,
        change_id: str,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if offset < 0:
            raise ValueError("diff offset cannot be negative")
        manifest = self.load_manifest(change_id)
        item = next((entry for entry in manifest.files if entry.path == path), None)
        if item is None:
            raise KeyError(path)
        page_limit = min(max(1, int(limit or self.max_page_bytes)), self.max_page_bytes)
        artifact_path = self._artifact_path(change_id, item.artifact)
        try:
            artifact_size = artifact_path.stat().st_size
        except FileNotFoundError:
            artifact_size = 0
        if artifact_size != item.diff_bytes:
            raise ReviewError(f"diff artifact size does not match manifest: {item.path}")
        text, consumed = _read_verified_text_page(
            artifact_path,
            offset,
            page_limit,
            artifact_size,
            item.diff_hash,
            item.path,
        )
        next_offset = offset + consumed
        complete = next_offset >= artifact_size
        return {
            "changeId": change_id,
            "path": item.path,
            "oldPath": item.old_path or None,
            "status": item.status,
            "binary": item.binary,
            "truncated": item.truncated,
            "offset": offset,
            "nextOffset": None if complete else next_offset,
            "complete": complete,
            "text": text,
            "diffHash": item.diff_hash,
        }

    def read_diff(
        self,
        change_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        manifest = self.load_manifest(change_id)
        index, offset = _parse_cursor(cursor)
        if index >= len(manifest.files):
            return {
                "changeId": change_id,
                "cursor": cursor,
                "nextCursor": None,
                "complete": True,
                "file": None,
                "text": "",
            }
        item = manifest.files[index]
        page = self.read_file_diff(change_id, item.path, offset=offset, limit=limit)
        if page["complete"]:
            next_cursor = None if index + 1 >= len(manifest.files) else f"{index + 1}:0"
        else:
            next_cursor = f"{index}:{page['nextOffset']}"
        return {
            "changeId": change_id,
            "cursor": cursor,
            "nextCursor": next_cursor,
            "complete": next_cursor is None,
            "file": item.to_dict(),
            "text": page["text"],
            "offset": page["offset"],
            "nextOffset": page["nextOffset"],
        }

    def remove_snapshot(self, snapshot_id: str) -> None:
        with self._lock:
            _remove_tree(self.snapshot_dir(snapshot_id))

    def _artifact_path(self, change_id: str, artifact: str) -> Path:
        if (
            not artifact
            or Path(artifact).name != artifact
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.diff", artifact)
        ):
            raise ReviewError("invalid diff artifact name")
        return self.change_dir(change_id) / artifact

    def _prune(self, directory: Path) -> None:
        try:
            entries = [entry for entry in directory.iterdir() if entry.is_dir()]
        except FileNotFoundError:
            return
        entries.sort(key=lambda entry: entry.stat().st_mtime_ns, reverse=True)
        for stale in entries[self.max_records:]:
            _remove_tree(stale)

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


class ChangeSnapshotService:
    def __init__(
        self,
        workspace: Path,
        store: ReviewStore | None = None,
        *,
        limits: ChangeLimits | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.limits = limits or ChangeLimits()
        self.store = store or ReviewStore(max_page_bytes=self.limits.max_page_bytes)
        self._lock = threading.RLock()

    def capture(self) -> ChangeSnapshot:
        with self._lock:
            return self._capture(persist=True)

    def changes_from_head(self, target: ChangeSnapshot | None = None) -> ChangeManifest:
        with self._lock:
            snapshot = target or self._capture(persist=True)
            if not snapshot.supported or not snapshot.complete:
                return self._incomplete_manifest("working_tree", None, snapshot)
            active_repository, _ = self._discover_repository()
            if (
                active_repository is None
                or Path(snapshot.repository).resolve() != active_repository.root
            ):
                return self._incomplete_manifest(
                    "working_tree",
                    None,
                    snapshot,
                    reason="snapshot does not belong to the active workspace repository",
                )
            return self._materialize(
                scope="working_tree",
                repository=Path(snapshot.repository),
                base_id=f"HEAD:{snapshot.head}",
                target_id=snapshot.snapshot_id,
                base_tree=snapshot.head_tree,
                target_tree=snapshot.tree,
                target_hash=snapshot.content_hash,
                object_snapshot_ids=(snapshot.snapshot_id,),
            )

    def changes_between(
        self,
        before: ChangeSnapshot,
        after: ChangeSnapshot | None = None,
    ) -> ChangeManifest:
        with self._lock:
            target = after or self._capture(persist=True)
            active_repository, _ = self._discover_repository()
            if active_repository is None or any(
                Path(snapshot.repository or ".").resolve() != active_repository.root
                for snapshot in (before, target)
            ):
                return self._incomplete_manifest(
                    "snapshot_range",
                    before,
                    target,
                    reason="snapshot does not belong to the active workspace repository",
                )
            if Path(before.repository or ".").resolve() != Path(target.repository or ".").resolve():
                return self._incomplete_manifest(
                    "snapshot_range",
                    before,
                    target,
                    reason="snapshots belong to different repositories",
                )
            if not before.supported or not before.complete or not target.supported or not target.complete:
                return self._incomplete_manifest("snapshot_range", before, target)
            return self._materialize(
                scope="snapshot_range",
                repository=Path(target.repository),
                base_id=before.snapshot_id,
                target_id=target.snapshot_id,
                base_tree=before.tree,
                target_tree=target.tree,
                target_hash=target.content_hash,
                object_snapshot_ids=(before.snapshot_id, target.snapshot_id),
            )

    def stale_status(self, reference: ChangeSnapshot | ChangeManifest) -> dict[str, Any]:
        expected = reference.content_hash if isinstance(reference, ChangeSnapshot) else reference.target_hash
        current = self._capture(persist=False)
        stale: bool | None
        if not current.supported or not current.complete or not expected:
            stale = None
        else:
            stale = current.content_hash != expected
        return {
            "supported": current.supported,
            "complete": current.complete,
            "stale": stale,
            "referenceHash": expected or None,
            "currentHash": current.content_hash or None,
            "reason": current.reason or None,
        }

    def is_stale(self, reference: ChangeSnapshot | ChangeManifest) -> bool:
        return self.stale_status(reference)["stale"] is not False

    def _capture(self, *, persist: bool) -> ChangeSnapshot:
        snapshot_id = uuid4().hex
        created_at = _utc_now()
        repository, discovery_error = self._discover_repository()
        if repository is None:
            snapshot = ChangeSnapshot(
                snapshot_id=snapshot_id,
                workspace=str(self.workspace),
                repository="",
                created_at=created_at,
                supported=False,
                complete=False,
                reason=discovery_error or "not a Git repository; filesystem snapshots are unsupported",
            )
            if persist:
                self.store.save_snapshot(snapshot)
            return snapshot

        try:
            self.store.root.relative_to(repository.root)
        except ValueError:
            pass
        else:
            snapshot = self._limited_snapshot(
                snapshot_id,
                created_at,
                repository,
                "review store must be outside the Git working tree",
            )
            return snapshot

        status_entries, status_error = self._status_entries(repository)
        if status_error:
            snapshot = self._limited_snapshot(snapshot_id, created_at, repository, status_error)
            if persist:
                self.store.save_snapshot(snapshot)
            return snapshot
        attribute_error = self._unsupported_attribute_error(repository, status_entries)
        if attribute_error:
            snapshot = self._limited_snapshot(
                snapshot_id,
                created_at,
                repository,
                attribute_error,
                changed_files=len(status_entries),
            )
            if persist:
                self.store.save_snapshot(snapshot)
            return snapshot
        limit_error, changed_bytes, binary_files = self._check_capture_limits(repository.root, status_entries)
        if limit_error:
            snapshot = self._limited_snapshot(
                snapshot_id,
                created_at,
                repository,
                limit_error,
                changed_files=len(status_entries),
                changed_bytes=changed_bytes,
                binary_files=binary_files,
            )
            if persist:
                self.store.save_snapshot(snapshot)
            return snapshot

        snapshot_dir = self.store.snapshot_dir(snapshot_id)
        objects_dir = snapshot_dir / "objects"
        first_index = snapshot_dir / "index-first"
        second_index = snapshot_dir / "index-second"
        tree = repository.head_tree
        try:
            if status_entries:
                objects_dir.mkdir(parents=True, exist_ok=True)
                first_tree = self._build_snapshot_tree(repository, status_entries, objects_dir, first_index)
                second_entries, second_error = self._status_entries(repository)
                if second_error:
                    raise ReviewError(second_error)
                second_attribute_error = self._unsupported_attribute_error(repository, second_entries)
                if second_attribute_error:
                    raise ReviewError(second_attribute_error)
                second_limit_error, second_bytes, second_binaries = self._check_capture_limits(
                    repository.root,
                    second_entries,
                )
                if second_limit_error:
                    raise ReviewError(second_limit_error)
                second_tree = self._build_snapshot_tree(repository, second_entries, objects_dir, second_index)
                current_repository = self._repository_for_path(repository.root)
                if (
                    current_repository is None
                    or current_repository.head != repository.head
                    or current_repository.branch != repository.branch
                    or current_repository.filter_names != repository.filter_names
                    or second_entries != status_entries
                    or second_tree != first_tree
                    or second_bytes != changed_bytes
                    or second_binaries != binary_files
                ):
                    raise ReviewError("HEAD or working tree changed while the snapshot was being captured")
                tree = second_tree
            else:
                second_entries, second_error = self._status_entries(repository)
                current_repository = self._repository_for_path(repository.root)
                if (
                    second_error
                    or second_entries
                    or current_repository is None
                    or current_repository.head != repository.head
                    or current_repository.branch != repository.branch
                    or current_repository.filter_names != repository.filter_names
                ):
                    raise ReviewError(
                        second_error or "HEAD or working tree changed while the snapshot was being captured"
                    )
            content_hash = _snapshot_hash(repository.root, repository.head, tree)
            snapshot = ChangeSnapshot(
                snapshot_id=snapshot_id,
                workspace=str(self.workspace),
                repository=str(repository.root),
                created_at=created_at,
                supported=True,
                complete=True,
                branch=repository.branch,
                head=repository.head,
                head_tree=repository.head_tree,
                tree=tree,
                content_hash=content_hash,
                changed_files=len(status_entries),
                changed_bytes=changed_bytes,
                binary_files=binary_files,
            )
            if persist:
                self.store.save_snapshot(snapshot)
            return snapshot
        except (OSError, ReviewError) as exc:
            _remove_tree(objects_dir)
            snapshot = self._limited_snapshot(
                snapshot_id,
                created_at,
                repository,
                f"Git snapshot failed: {exc}",
                changed_files=len(status_entries),
                changed_bytes=changed_bytes,
                binary_files=binary_files,
            )
            if persist:
                self.store.save_snapshot(snapshot)
            return snapshot
        finally:
            for index_path in (first_index, second_index):
                index_path.unlink(missing_ok=True)
                index_path.with_name(index_path.name + ".lock").unlink(missing_ok=True)
            if not persist:
                _remove_tree(snapshot_dir)

    def _build_snapshot_tree(
        self,
        repository: _Repository,
        entries: list[_StatusEntry],
        objects_dir: Path,
        index_path: Path,
    ) -> str:
        env = self._snapshot_env(repository, objects_dir, index_path)
        self._git_checked(repository.root, ["read-tree", "HEAD"], env=env, output_limit=64 * 1024)
        total_blob_bytes = 0
        for entry in entries:
            if entry.old_path and "R" in entry.status:
                self._git_checked(
                    repository.root,
                    ["update-index", "--force-remove", "--", entry.old_path],
                    env=env,
                    output_limit=64 * 1024,
                )
            worktree_path = _safe_repository_path(repository.root, entry.path)
            try:
                file_stat = worktree_path.lstat()
            except FileNotFoundError:
                self._git_checked(
                    repository.root,
                    ["update-index", "--force-remove", "--", entry.path],
                    env=env,
                    output_limit=64 * 1024,
                )
                continue
            mode, payload = self._worktree_blob(repository, entry.path, worktree_path, file_stat)
            total_blob_bytes += len(payload)
            if total_blob_bytes > self.limits.max_total_bytes:
                raise ReviewError(
                    f"changed files exceed {self.limits.max_total_bytes} total bytes while being read"
                )
            if mode == "120000":
                object_id = _write_loose_blob(objects_dir, payload, repository.object_format)
            else:
                object_id = self._hash_worktree_blob(repository, entry.path, payload, env, index_path)
            self._git_checked(
                repository.root,
                ["update-index", "--add", "--cacheinfo", f"{mode},{object_id},{entry.path}"],
                env=env,
                output_limit=64 * 1024,
            )
        tree = self._git_text(
            repository.root,
            ["write-tree"],
            env=env,
            output_limit=256,
        ).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", tree):
            raise ReviewError("git write-tree returned an invalid object id")
        return tree

    def _worktree_blob(
        self,
        repository: _Repository,
        relative: str,
        path: Path,
        file_stat: os.stat_result,
    ) -> tuple[str, bytes]:
        if stat.S_ISLNK(file_stat.st_mode):
            return "120000", os.fsencode(os.readlink(path))
        if not stat.S_ISREG(file_stat.st_mode):
            raise ReviewError(f"unsupported changed file type: {relative}")
        with path.open("rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise ReviewError(f"changed file type changed while being read: {relative}")
            if (
                getattr(file_stat, "st_ino", 0)
                and getattr(opened_stat, "st_ino", 0)
                and (
                    file_stat.st_dev != opened_stat.st_dev
                    or file_stat.st_ino != opened_stat.st_ino
                )
            ):
                raise ReviewError(f"changed file was replaced while being read: {relative}")
            if (
                int(opened_stat.st_size) != int(file_stat.st_size)
                or int(opened_stat.st_mtime_ns) != int(file_stat.st_mtime_ns)
            ):
                raise ReviewError(f"changed file changed while being read: {relative}")
            if opened_stat.st_size > self.limits.max_file_bytes:
                raise ReviewError(
                    f"changed file exceeds {self.limits.max_file_bytes} bytes while being read: {relative}"
                )
            payload = handle.read(self.limits.max_file_bytes + 1)
            final_stat = os.fstat(handle.fileno())
        if len(payload) > self.limits.max_file_bytes:
            raise ReviewError(
                f"changed file exceeds {self.limits.max_file_bytes} bytes while being read: {relative}"
            )
        if (
            int(final_stat.st_size) != int(opened_stat.st_size)
            or int(final_stat.st_mtime_ns) != int(opened_stat.st_mtime_ns)
        ):
            raise ReviewError(f"changed file changed while being read: {relative}")
        existing_mode = self._tracked_mode(repository, relative)
        if os.name == "nt" and existing_mode in {"100644", "100755"}:
            mode = existing_mode
        else:
            mode = "100755" if file_stat.st_mode & stat.S_IXUSR else "100644"
        return mode, payload

    def _hash_worktree_blob(
        self,
        repository: _Repository,
        relative: str,
        payload: bytes,
        env: dict[str, str],
        index_path: Path,
    ) -> str:
        input_path = index_path.with_name(f".blob-input-{uuid4().hex}")
        try:
            _atomic_write_bytes(input_path, payload)
            object_id = self._git_text(
                repository.root,
                ["hash-object", "-w", f"--path={relative}", str(input_path)],
                env=env,
                output_limit=256,
                filter_names=repository.filter_names,
            ).strip()
        finally:
            input_path.unlink(missing_ok=True)
        expected_length = 40 if repository.object_format == "sha1" else 64
        if not re.fullmatch(rf"[0-9a-fA-F]{{{expected_length}}}", object_id):
            raise ReviewError("git hash-object returned an invalid object id")
        return object_id

    def _tracked_mode(self, repository: _Repository, relative: str) -> str:
        result = self._run_git(
            repository.root,
            ["ls-files", "-s", "--", _literal_pathspec(relative)],
            env=_base_git_env(),
            output_limit=16 * 1024,
        )
        if result.returncode != 0 or result.timed_out or result.truncated:
            return ""
        for line in result.output.decode("utf-8", "replace").splitlines():
            fields = line.split(maxsplit=3)
            if len(fields) >= 3 and fields[2] == "0" and fields[0] in {"100644", "100755"}:
                return fields[0]
        return ""

    def _materialize(
        self,
        *,
        scope: str,
        repository: Path,
        base_id: str,
        target_id: str,
        base_tree: str,
        target_tree: str,
        target_hash: str,
        object_snapshot_ids: tuple[str, ...],
    ) -> ChangeManifest:
        change_id = uuid4().hex
        repo = self._repository_for_path(repository)
        if repo is None:
            target = self.store.load_snapshot(target_id) if _ID_RE.fullmatch(target_id) else None
            return self._incomplete_manifest(scope, None, target, reason="repository is no longer available")
        env = self._comparison_env(repo, object_snapshot_ids)
        list_limit = max(64 * 1024, self.limits.max_files * min(self.limits.max_path_bytes + 32, 8192))
        listing = self._run_git(
            repo.root,
            ["diff", "--no-ext-diff", "--no-textconv", "--find-renames=50%", "--name-status", "-z", base_tree, target_tree, "--"],
            env=env,
            output_limit=list_limit,
        )
        if listing.timed_out:
            return self._save_failed_manifest(
                change_id, scope, repo.root, base_id, target_id, base_tree, target_tree, target_hash,
                "Git change listing timed out",
            )
        if listing.returncode != 0 and not listing.truncated:
            detail = listing.output.decode("utf-8", "replace").strip()
            return self._save_failed_manifest(
                change_id, scope, repo.root, base_id, target_id, base_tree, target_tree, target_hash,
                f"Git change listing failed: {detail or listing.returncode}",
            )
        if listing.truncated:
            return self._save_failed_manifest(
                change_id, scope, repo.root, base_id, target_id, base_tree, target_tree, target_hash,
                "changed file manifest exceeded its bounded output limit",
                truncated=True,
            )

        entries = _parse_name_status(listing.output)
        total_files = len(entries)
        included = entries[: self.limits.max_files]
        omitted_files = max(0, total_files - len(included))
        diffs: dict[str, bytes] = {}
        files: list[ChangeFile] = []
        remaining = self.limits.max_diff_bytes
        any_truncated = omitted_files > 0
        reasons: list[str] = []
        if omitted_files:
            reasons.append(f"{omitted_files} changed files omitted by file limit")
        binary_count = 0

        for index, entry in enumerate(included):
            artifact = f"{index:04d}.diff"
            file_limit = min(self.limits.max_file_diff_bytes, remaining)
            if file_limit <= 0:
                payload = b""
                truncated = True
            else:
                raw_pathspecs = [entry.old_path, entry.path] if entry.old_path else [entry.path]
                pathspecs = [_literal_pathspec(path) for path in raw_pathspecs]
                result = self._run_git(
                    repo.root,
                    [
                        "diff", "--no-ext-diff", "--no-textconv", "--find-renames=50%",
                        "--src-prefix=a/", "--dst-prefix=b/", "--unified=3",
                        base_tree, target_tree, "--", *pathspecs,
                    ],
                    env=env,
                    output_limit=max(1, file_limit),
                )
                if result.timed_out:
                    payload = _bounded_with_marker(result.output, b"\n... diff timed out ...\n", file_limit)
                    truncated = True
                elif result.returncode != 0 and not result.truncated:
                    detail = result.output.decode("utf-8", "replace").strip()
                    error_payload = f"... diff unavailable: {detail or result.returncode} ...\n".encode("utf-8")
                    payload = error_payload[:file_limit]
                    truncated = True
                else:
                    payload = result.output
                    truncated = result.truncated
                    if truncated:
                        payload = _bounded_with_marker(payload, b"\n... diff truncated ...\n", file_limit)
            binary = _is_binary_diff(payload)
            if binary:
                binary_count += 1
                if binary_count > self.limits.max_binary_files:
                    payload = b"... binary diff omitted by binary file limit ...\n"
                    truncated = True
            payload = payload[: max(0, remaining)]
            remaining -= len(payload)
            additions, deletions = _diff_stats(payload)
            diff_hash = hashlib.sha256(payload).hexdigest()
            files.append(ChangeFile(
                path=entry.path,
                old_path=entry.old_path,
                status=entry.status,
                additions=additions,
                deletions=deletions,
                binary=binary,
                diff_bytes=len(payload),
                truncated=truncated,
                diff_hash=diff_hash,
                artifact=artifact,
            ))
            diffs[artifact] = payload
            any_truncated = any_truncated or truncated

        if binary_count > self.limits.max_binary_files:
            reasons.append("binary file limit exceeded")
        if any(item.truncated for item in files):
            reasons.append("one or more file diffs were truncated by review limits")
        if remaining <= 0 and any(entry.diff_bytes == 0 or entry.truncated for entry in files):
            reasons.append("total diff byte limit reached")
        digest = hashlib.sha256()
        digest.update(base_tree.encode("ascii", "ignore"))
        digest.update(b"\0")
        digest.update(target_tree.encode("ascii", "ignore"))
        for item in files:
            digest.update(b"\0")
            digest.update(item.status.encode("ascii", "ignore"))
            digest.update(b"\0")
            digest.update(item.old_path.encode("utf-8", "surrogatepass"))
            digest.update(b"\0")
            digest.update(item.path.encode("utf-8", "surrogatepass"))
            digest.update(bytes.fromhex(item.diff_hash))
        complete = not any_truncated
        manifest = ChangeManifest(
            change_id=change_id,
            scope=scope,
            repository=str(repo.root),
            created_at=_utc_now(),
            supported=True,
            complete=complete,
            reason="; ".join(dict.fromkeys(reasons)),
            base_id=base_id,
            target_id=target_id,
            base_tree=base_tree,
            target_tree=target_tree,
            target_hash=target_hash,
            change_hash=digest.hexdigest(),
            files=tuple(files),
            total_files=total_files,
            omitted_files=omitted_files,
            diff_bytes=sum(item.diff_bytes for item in files),
            truncated=any_truncated,
        )
        self.store.save_change(manifest, diffs)
        return manifest

    def _discover_repository(self) -> tuple[_Repository | None, str]:
        base_env = _base_git_env()
        root_result = self._run_git(
            self.workspace,
            ["rev-parse", "--show-toplevel"],
            env=base_env,
            output_limit=16 * 1024,
        )
        if root_result.returncode != 0 or root_result.timed_out or root_result.truncated:
            return None, "not a Git repository; filesystem snapshots are unsupported"
        root_text = root_result.output.decode("utf-8", "replace").strip()
        if not root_text:
            return None, "Git did not report a repository root"
        root = Path(root_text).resolve()
        repository = self._repository_for_path(root)
        if repository is None:
            return None, "Git repository has no committed HEAD; snapshot is incomplete"
        return repository, ""

    def _repository_for_path(self, root: Path) -> _Repository | None:
        env = _base_git_env()
        try:
            head = self._git_text(root, ["rev-parse", "--verify", "HEAD"], env=env, output_limit=256).strip()
            head_tree = self._git_text(root, ["rev-parse", "HEAD^{tree}"], env=env, output_limit=256).strip()
            common_raw = self._git_text(root, ["rev-parse", "--git-common-dir"], env=env, output_limit=16 * 1024).strip()
            branch = self._git_text(root, ["branch", "--show-current"], env=env, output_limit=16 * 1024).strip()
            object_format = self._git_text(root, ["rev-parse", "--show-object-format"], env=env, output_limit=64).strip()
        except ReviewError:
            return None
        if object_format not in {"sha1", "sha256"}:
            return None
        filter_result = self._run_git(
            root,
            ["config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
            env=env,
            output_limit=64 * 1024,
        )
        if filter_result.timed_out or filter_result.truncated or filter_result.returncode not in {0, 1}:
            return None
        filter_names: set[str] = set()
        for raw_key in filter_result.output.decode("utf-8", "replace").splitlines():
            match = re.fullmatch(r"filter\.([A-Za-z0-9_.-]+)\.(?:clean|smudge|process|required)", raw_key, re.IGNORECASE)
            if match is None:
                return None
            filter_names.add(match.group(1))
        common_dir = Path(common_raw)
        if not common_dir.is_absolute():
            common_dir = (root / common_dir).resolve()
        return _Repository(
            root.resolve(),
            common_dir,
            common_dir / "objects",
            head,
            head_tree,
            branch,
            object_format,
            tuple(sorted(filter_names, key=str.casefold)),
        )

    def _status_entries(self, repository: _Repository) -> tuple[list[_StatusEntry], str]:
        limit = max(64 * 1024, self.limits.max_files * min(self.limits.max_path_bytes + 32, 8192))
        pathspecs = ["."]
        for dirname in (DATA_DIRNAME, LEGACY_DATA_DIRNAME):
            try:
                owned_relative = (self.workspace / dirname).relative_to(repository.root)
            except ValueError:
                continue
            pathspecs.append(f":(top,exclude,literal){owned_relative.as_posix()}")
        result = self._run_git(
            repository.root,
            [
                "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no",
                "--", *pathspecs,
            ],
            env=_base_git_env(),
            output_limit=limit,
            filter_names=repository.filter_names,
        )
        if result.timed_out:
            return [], "Git status timed out"
        if result.truncated:
            return [], "Git status exceeded its bounded output limit"
        if result.returncode != 0:
            detail = result.output.decode("utf-8", "replace").strip()
            return [], f"Git status failed: {detail or result.returncode}"
        try:
            return _parse_porcelain_status(result.output), ""
        except ValueError as exc:
            return [], f"Git status was malformed: {exc}"

    def _unsupported_attribute_error(
        self,
        repository: _Repository,
        entries: list[_StatusEntry],
    ) -> str:
        if not entries:
            return ""
        configured_filters = set(repository.filter_names)
        paths: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if entry.path in seen:
                continue
            path = _safe_repository_path(repository.root, entry.path)
            try:
                file_stat = path.lstat()
            except FileNotFoundError:
                continue
            if not (stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode)):
                continue
            seen.add(entry.path)
            paths.append(entry.path)

        batches: list[list[str]] = []
        batch: list[str] = []
        batch_bytes = 0
        for path in paths:
            path_bytes = len(path.encode("utf-8", "surrogatepass")) + 1
            if batch and batch_bytes + path_bytes > 12 * 1024:
                batches.append(batch)
                batch = []
                batch_bytes = 0
            batch.append(path)
            batch_bytes += path_bytes
        if batch:
            batches.append(batch)

        for path_batch in batches:
            output_limit = max(
                4096,
                sum(2 * len(path.encode("utf-8", "surrogatepass")) + 128 for path in path_batch),
            )
            result = self._run_git(
                repository.root,
                ["check-attr", "-z", "filter", "working-tree-encoding", "--", *path_batch],
                env=_base_git_env(),
                output_limit=output_limit,
                filter_names=repository.filter_names,
            )
            if result.timed_out:
                return "Git attribute inspection timed out"
            if result.truncated:
                return "Git attribute inspection exceeded its bounded output limit"
            if result.returncode != 0:
                detail = result.output.decode("utf-8", "replace").strip()
                return f"Git attribute inspection failed: {detail or result.returncode}"
            tokens = result.output.split(b"\0")
            if tokens and not tokens[-1]:
                tokens.pop()
            if len(tokens) % 3:
                return "Git attribute inspection returned malformed output"
            for index in range(0, len(tokens), 3):
                path = os.fsdecode(tokens[index])
                attribute = tokens[index + 1].decode("utf-8", "replace")
                value = tokens[index + 2].decode("utf-8", "replace")
                if attribute == "filter" and value in configured_filters:
                    return f"custom Git filter is unsupported for changed file: {path}"
                if attribute == "working-tree-encoding" and value not in {"unspecified", "unset"}:
                    return f"Git working-tree-encoding is unsupported for changed file: {path}"
        return ""

    def _check_capture_limits(
        self,
        root: Path,
        entries: list[_StatusEntry],
    ) -> tuple[str, int, int]:
        if len(entries) > self.limits.max_files:
            return f"changed file count exceeds limit {self.limits.max_files}", 0, 0
        total_bytes = 0
        binary_files = 0
        for entry in entries:
            encoded = entry.path.encode("utf-8", "surrogatepass")
            if len(encoded) > self.limits.max_path_bytes:
                return f"changed path exceeds {self.limits.max_path_bytes} bytes", total_bytes, binary_files
            path = _safe_repository_path(root, entry.path)
            try:
                file_stat = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(file_stat.st_mode):
                return f"changed Git submodule or directory is unsupported: {entry.path}", total_bytes, binary_files
            if not (stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode)):
                return f"changed special file is unsupported: {entry.path}", total_bytes, binary_files
            size = int(file_stat.st_size)
            if size > self.limits.max_file_bytes:
                return f"changed file exceeds {self.limits.max_file_bytes} bytes: {entry.path}", total_bytes, binary_files
            total_bytes += size
            if total_bytes > self.limits.max_total_bytes:
                return f"changed files exceed {self.limits.max_total_bytes} total bytes", total_bytes, binary_files
            if path.is_file() and not path.is_symlink() and _looks_binary(path):
                binary_files += 1
                if binary_files > self.limits.max_binary_files:
                    return f"changed binary files exceed limit {self.limits.max_binary_files}", total_bytes, binary_files
        return "", total_bytes, binary_files

    def _limited_snapshot(
        self,
        snapshot_id: str,
        created_at: str,
        repository: _Repository,
        reason: str,
        *,
        changed_files: int = 0,
        changed_bytes: int = 0,
        binary_files: int = 0,
    ) -> ChangeSnapshot:
        return ChangeSnapshot(
            snapshot_id=snapshot_id,
            workspace=str(self.workspace),
            repository=str(repository.root),
            created_at=created_at,
            supported=True,
            complete=False,
            reason=reason,
            branch=repository.branch,
            head=repository.head,
            head_tree=repository.head_tree,
            changed_files=changed_files,
            changed_bytes=changed_bytes,
            binary_files=binary_files,
        )

    def _incomplete_manifest(
        self,
        scope: str,
        before: ChangeSnapshot | None,
        target: ChangeSnapshot,
        *,
        reason: str = "",
    ) -> ChangeManifest:
        message = reason or target.reason or (before.reason if before else "") or "snapshot is incomplete"
        manifest = ChangeManifest(
            change_id=uuid4().hex,
            scope=scope,
            repository=target.repository or (before.repository if before else ""),
            created_at=_utc_now(),
            supported=bool(target.supported and (before.supported if before else True)),
            complete=False,
            reason=message,
            base_id=before.snapshot_id if before else (f"HEAD:{target.head}" if target.head else ""),
            target_id=target.snapshot_id,
            base_tree=before.tree if before else target.head_tree,
            target_tree=target.tree,
            target_hash=target.content_hash,
            change_hash="",
        )
        self.store.save_change(manifest, {})
        return manifest

    def _save_failed_manifest(
        self,
        change_id: str,
        scope: str,
        repository: Path,
        base_id: str,
        target_id: str,
        base_tree: str,
        target_tree: str,
        target_hash: str,
        reason: str,
        *,
        truncated: bool = False,
    ) -> ChangeManifest:
        manifest = ChangeManifest(
            change_id=change_id,
            scope=scope,
            repository=str(repository),
            created_at=_utc_now(),
            supported=True,
            complete=False,
            reason=reason,
            base_id=base_id,
            target_id=target_id,
            base_tree=base_tree,
            target_tree=target_tree,
            target_hash=target_hash,
            change_hash="",
            truncated=truncated,
        )
        self.store.save_change(manifest, {})
        return manifest

    def _snapshot_env(
        self,
        repository: _Repository,
        objects_dir: Path,
        index_path: Path,
    ) -> dict[str, str]:
        env = _base_git_env()
        env["GIT_INDEX_FILE"] = str(index_path)
        env["GIT_OBJECT_DIRECTORY"] = str(objects_dir)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(repository.objects_dir)
        return env

    def _comparison_env(
        self,
        repository: _Repository,
        snapshot_ids: Iterable[str],
    ) -> dict[str, str]:
        scratch = self.store.root / "scratch-objects"
        scratch.mkdir(parents=True, exist_ok=True)
        alternates = [repository.objects_dir]
        for snapshot_id in snapshot_ids:
            objects = self.store.snapshot_dir(snapshot_id) / "objects"
            if objects.is_dir():
                alternates.append(objects)
        env = _base_git_env()
        env["GIT_OBJECT_DIRECTORY"] = str(scratch)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.pathsep.join(str(path) for path in dict.fromkeys(alternates))
        return env

    def _git_text(
        self,
        cwd: Path,
        args: list[str],
        *,
        env: dict[str, str],
        output_limit: int,
        filter_names: tuple[str, ...] = (),
    ) -> str:
        result = self._run_git(
            cwd,
            args,
            env=env,
            output_limit=output_limit,
            filter_names=filter_names,
        )
        if result.timed_out:
            raise ReviewError(f"git {args[0]} timed out")
        if result.truncated:
            raise ReviewError(f"git {args[0]} output exceeded its limit")
        if result.returncode != 0:
            detail = result.output.decode("utf-8", "replace").strip()
            raise ReviewError(detail or f"git {args[0]} failed with {result.returncode}")
        return result.output.decode("utf-8", "replace")

    def _git_checked(
        self,
        cwd: Path,
        args: list[str],
        *,
        env: dict[str, str],
        output_limit: int,
    ) -> None:
        self._git_text(cwd, args, env=env, output_limit=output_limit)

    def _run_git(
        self,
        cwd: Path,
        args: list[str],
        *,
        env: dict[str, str],
        output_limit: int,
        filter_names: tuple[str, ...] = (),
    ) -> _CommandResult:
        filter_overrides: list[str] = []
        for name in filter_names:
            filter_overrides.extend([
                "-c", f"filter.{name}.clean=",
                "-c", f"filter.{name}.smudge=",
                "-c", f"filter.{name}.process=",
                "-c", f"filter.{name}.required=false",
            ])
        command = [
            "git",
            "-c", "core.quotepath=false",
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            *filter_overrides,
            *args,
        ]
        try:
            process = popen_hidden(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            return _CommandResult(127, str(exc).encode("utf-8", "replace")[:output_limit], False, False)
        timed_out = threading.Event()

        def expire() -> None:
            if process.poll() is not None:
                return
            try:
                process.kill()
                timed_out.set()
            except OSError:
                pass

        timer = threading.Timer(self.limits.command_timeout, expire)
        timer.daemon = True
        timer.start()
        output = bytearray()
        truncated = False
        try:
            stream = process.stdout
            if stream is None:
                raise ReviewError("git process did not expose stdout")
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                remaining = output_limit - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    truncated = True
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break
            returncode = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
        return _CommandResult(returncode, bytes(output), truncated, timed_out.is_set())


def _base_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
        "GIT_QUARANTINE_PATH",
        "GIT_SHALLOW_FILE",
    ):
        env.pop(name, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env


def _parse_porcelain_status(payload: bytes) -> list[_StatusEntry]:
    tokens = payload.split(b"\0")
    entries: list[_StatusEntry] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4 or token[2:3] != b" ":
            raise ValueError("invalid porcelain record")
        status = token[:2].decode("ascii", "replace")
        path = os.fsdecode(token[3:])
        old_path = ""
        if "R" in status or "C" in status:
            if index >= len(tokens) or not tokens[index]:
                raise ValueError("rename record has no source path")
            old_path = os.fsdecode(tokens[index])
            index += 1
        entries.append(_StatusEntry(status.strip() or status, path, old_path))
    return entries


def _parse_name_status(payload: bytes) -> list[_StatusEntry]:
    tokens = payload.split(b"\0")
    entries: list[_StatusEntry] = []
    index = 0
    while index < len(tokens):
        raw_status = tokens[index]
        index += 1
        if not raw_status:
            continue
        if index >= len(tokens):
            raise ReviewError("Git name-status output ended unexpectedly")
        status = raw_status.decode("ascii", "replace")
        first = os.fsdecode(tokens[index])
        index += 1
        if status.startswith(("R", "C")):
            if index >= len(tokens):
                raise ReviewError("Git rename output ended unexpectedly")
            second = os.fsdecode(tokens[index])
            index += 1
            entries.append(_StatusEntry(status, second, first))
        else:
            entries.append(_StatusEntry(status, first))
    return entries


def _literal_pathspec(path: str) -> str:
    return f":(top,literal){path}"


def _safe_repository_path(root: Path, relative: str) -> Path:
    if not relative or "\0" in relative:
        raise ReviewError("Git reported an invalid path")
    candidate = Path(os.path.abspath(root / Path(relative)))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReviewError("Git reported a path outside the repository") from exc
    return candidate


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(8192)
    except OSError:
        return False


def _is_binary_diff(payload: bytes) -> bool:
    lowered = payload.lower()
    return b"binary files " in lowered or b"git binary patch" in lowered or b"binary file" in lowered


def _diff_stats(payload: bytes) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in payload.splitlines():
        if line.startswith((b"+++", b"---")):
            continue
        if line.startswith(b"+"):
            additions += 1
        elif line.startswith(b"-"):
            deletions += 1
    return additions, deletions


def _snapshot_hash(repository: Path, head: str, tree: str) -> str:
    digest = hashlib.sha256()
    digest.update(str(repository.resolve()).encode("utf-8", "surrogatepass"))
    digest.update(b"\0")
    digest.update(head.encode("ascii", "ignore"))
    digest.update(b"\0")
    digest.update(tree.encode("ascii", "ignore"))
    return digest.hexdigest()


def _write_loose_blob(objects_dir: Path, payload: bytes, object_format: str) -> str:
    try:
        digest = hashlib.new(object_format)
    except ValueError as exc:
        raise ReviewError(f"unsupported Git object format: {object_format}") from exc
    raw = f"blob {len(payload)}\0".encode("ascii") + payload
    digest.update(raw)
    object_id = digest.hexdigest()
    target = objects_dir / object_id[:2] / object_id[2:]
    if not target.exists():
        _atomic_write_bytes(target, zlib.compress(raw))
    return object_id


def _read_verified_text_page(
    path: Path,
    offset: int,
    limit: int,
    size: int,
    expected_hash: str,
    display_path: str,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    page_end = min(size, offset + limit + 4)
    page = bytearray()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                chunk_start = total
                chunk_end = chunk_start + len(chunk)
                digest.update(chunk)
                if chunk_end > offset and chunk_start < page_end:
                    start = max(0, offset - chunk_start)
                    end = min(len(chunk), page_end - chunk_start)
                    page.extend(chunk[start:end])
                total = chunk_end
    except OSError as exc:
        raise ReviewError(f"cannot read diff artifact: {display_path}") from exc
    if total != size:
        raise ReviewError(f"diff artifact size does not match manifest: {display_path}")
    normalized_hash = str(expected_hash or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash) or digest.hexdigest() != normalized_hash:
        raise ReviewError(f"diff artifact hash does not match manifest: {display_path}")
    if offset >= size:
        return "", 0
    chunk = bytes(page)
    cut = min(limit, len(chunk))
    while cut > 0:
        try:
            return chunk[:cut].decode("utf-8"), cut
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.end == cut and offset + cut < size:
                if exc.start == 0 and cut < len(chunk):
                    cut += 1
                    continue
                cut = exc.start
                continue
            return chunk[:cut].decode("utf-8", "replace"), cut
    if chunk:
        consumed = min(len(chunk), max(limit, 1) + 3)
        return chunk[:consumed].decode("utf-8", "replace"), consumed
    return "", 0


def _bounded_with_marker(payload: bytes, marker: bytes, limit: int) -> bytes:
    if limit <= 0:
        return b""
    if len(marker) >= limit:
        return marker[:limit]
    return payload[: limit - len(marker)] + marker


def _parse_cursor(cursor: str | None) -> tuple[int, int]:
    if not cursor:
        return 0, 0
    match = re.fullmatch(r"(\d+):(\d+)", str(cursor))
    if match is None:
        raise ValueError("invalid diff cursor")
    return int(match.group(1)), int(match.group(2))


def _safe_id(value: str) -> str:
    text = str(value or "")
    if not _ID_RE.fullmatch(text) or text in {".", ".."}:
        raise ValueError("invalid review record id")
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReviewError(f"review record not found: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"invalid review record: {path.name}") from exc
    if not isinstance(data, dict):
        raise ReviewError(f"invalid review record shape: {path.name}")
    return data


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _atomic_write_bytes(path, body)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def make_writable_then_retry(function: Any, failed_path: str, _error: Any) -> None:
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            function(failed_path)
        except OSError:
            pass

    shutil.rmtree(path, onerror=make_writable_then_retry)
