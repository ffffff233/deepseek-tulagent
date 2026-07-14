from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

import deepseekfathom._core.reviews as reviews_module
from deepseekfathom._core.reviews import (
    ChangeFile,
    ChangeLimits,
    ChangeManifest,
    ChangeSnapshotService,
    ReviewError,
    ReviewStore,
)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def git_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Review Test")
    git(repo, "config", "user.email", "review@example.test")
    (repo / "alpha.txt").write_text("alpha one\nalpha two\n", encoding="utf-8")
    (repo / "beta.txt").write_text("beta one\nbeta two\n", encoding="utf-8")
    (repo / "rename-me.txt").write_text("rename payload\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial")
    return repo


def new_service(
    tmp_path: Path,
    repo: Path,
    *,
    limits: ChangeLimits | None = None,
    page_bytes: int = 128,
) -> tuple[ChangeSnapshotService, ReviewStore]:
    store = ReviewStore(tmp_path / "review-store", max_page_bytes=page_bytes)
    service = ChangeSnapshotService(repo, store, limits=limits)
    return service, store


def all_diff_pages(store: ReviewStore, change_id: str, *, limit: int = 128) -> str:
    cursor = None
    chunks: list[str] = []
    seen: set[str | None] = set()
    while True:
        assert cursor not in seen
        seen.add(cursor)
        page = store.read_diff(change_id, cursor=cursor, limit=limit)
        chunks.append(page["text"])
        cursor = page["nextCursor"]
        if cursor is None:
            return "".join(chunks)


def git_object_inventory(repo: Path) -> dict[str, str]:
    common_dir = Path(git(repo, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = repo / common_dir
    objects = common_dir / "objects"
    return {
        path.relative_to(objects).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in objects.rglob("*")
        if path.is_file()
    }


def test_working_tree_manifest_includes_staged_unstaged_and_untracked_without_mutation(tmp_path: Path):
    repo = init_repo(tmp_path)
    git(repo, "switch", "-c", "feature/review")
    (repo / "alpha.txt").write_text("alpha staged\n", encoding="utf-8")
    git(repo, "add", "alpha.txt")
    (repo / "beta.txt").write_text("beta unstaged\n", encoding="utf-8")
    (repo / "new-file.txt").write_text("new untracked\nsecond line\n", encoding="utf-8")
    (repo / "ignored.tmp").write_text("must stay ignored\n", encoding="utf-8")

    index_path = Path(git(repo, "rev-parse", "--git-path", "index"))
    if not index_path.is_absolute():
        index_path = repo / index_path
    index_before = index_path.read_bytes()
    objects_before = git_object_inventory(repo)
    status_before = git_bytes(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    contents_before = {path.name: path.read_bytes() for path in repo.iterdir() if path.is_file()}
    service, store = new_service(tmp_path, repo, page_bytes=73)

    snapshot = service.capture()
    manifest = service.changes_from_head(snapshot)

    assert snapshot.supported is True and snapshot.complete is True
    assert snapshot.branch == "feature/review"
    assert snapshot.changed_files == 3
    assert manifest.supported is True and manifest.complete is True
    assert {item.path for item in manifest.files} == {"alpha.txt", "beta.txt", "new-file.txt"}
    assert "ignored.tmp" not in {item.path for item in manifest.files}
    assert any(item.status == "A" for item in manifest.files if item.path == "new-file.txt")
    combined = all_diff_pages(store, manifest.change_id, limit=73)
    assert "alpha staged" in combined
    assert "beta unstaged" in combined
    assert "new untracked" in combined
    assert "must stay ignored" not in combined
    assert store.manifest(manifest.change_id)["targetHash"] == snapshot.content_hash
    assert all("artifact" not in item for item in store.manifest(manifest.change_id)["files"])

    assert index_path.read_bytes() == index_before
    assert git_object_inventory(repo) == objects_before
    assert git_bytes(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all") == status_before
    assert {path.name: path.read_bytes() for path in repo.iterdir() if path.is_file()} == contents_before


def test_clean_repository_has_empty_complete_manifest(tmp_path: Path):
    repo = init_repo(tmp_path)
    service, store = new_service(tmp_path, repo)

    snapshot = service.capture()
    manifest = service.changes_from_head(snapshot)

    assert snapshot.complete is True
    assert snapshot.tree == snapshot.head_tree
    assert snapshot.changed_files == 0
    assert manifest.complete is True
    assert manifest.total_files == 0
    assert manifest.files == ()
    assert store.read_diff(manifest.change_id)["complete"] is True
    assert store.read_diff(manifest.change_id)["text"] == ""


def test_snapshot_range_detects_rename_and_preserves_old_path(tmp_path: Path):
    repo = init_repo(tmp_path)
    service, store = new_service(tmp_path, repo, page_bytes=64)
    before = service.capture()
    git(repo, "mv", "rename-me.txt", "renamed.txt")
    after = service.capture()

    manifest = service.changes_between(before, after)

    assert manifest.complete is True
    assert manifest.total_files == 1
    changed = manifest.files[0]
    assert changed.status.startswith("R")
    assert changed.old_path == "rename-me.txt"
    assert changed.path == "renamed.txt"
    page = store.read_file_diff(manifest.change_id, "renamed.txt", limit=64)
    text = page["text"]
    offset = page["nextOffset"]
    while offset is not None:
        page = store.read_file_diff(manifest.change_id, "renamed.txt", offset=offset, limit=64)
        text += page["text"]
        offset = page["nextOffset"]
    assert "rename from rename-me.txt" in text
    assert "rename to renamed.txt" in text


def test_snapshot_preserves_copy_source_when_status_reports_copy(tmp_path: Path):
    repo = init_repo(tmp_path)
    git(repo, "config", "status.renames", "copies")
    source = repo / "alpha.txt"
    copied = repo / "alpha-copy.txt"
    copied.write_bytes(source.read_bytes())
    source.write_text("changed source\n", encoding="utf-8")
    git(repo, "add", "-A")
    service, _store = new_service(tmp_path, repo)

    snapshot = service.capture()
    manifest = service.changes_from_head(snapshot)

    assert snapshot.complete is True
    assert manifest.complete is True
    assert {item.path for item in manifest.files} == {"alpha.txt", "alpha-copy.txt"}


def test_snapshot_excludes_application_owned_workspace_state(tmp_path: Path):
    repo = init_repo(tmp_path)
    app_state = repo / ".deepseekfathom" / "sessions"
    legacy_state = repo / (".deepseek-" + "tulagent") / "sessions"
    app_state.mkdir(parents=True)
    legacy_state.mkdir(parents=True)
    session_log = app_state / "session.jsonl"
    legacy_log = legacy_state / "legacy.jsonl"
    session_log.write_text('{"prompt":"internal"}\n', encoding="utf-8")
    legacy_log.write_text('{"prompt":"legacy internal"}\n', encoding="utf-8")
    (repo / "alpha.txt").write_text("user change\n", encoding="utf-8")
    service, _store = new_service(tmp_path, repo)

    snapshot = service.capture()
    manifest = service.changes_from_head(snapshot)
    session_log.write_text('{"prompt":"updated internal"}\n', encoding="utf-8")
    legacy_log.write_text('{"prompt":"updated legacy internal"}\n', encoding="utf-8")

    assert snapshot.complete is True
    assert snapshot.changed_files == 1
    assert {item.path for item in manifest.files} == {"alpha.txt"}
    assert service.stale_status(manifest)["stale"] is False


def test_snapshot_ignores_ambient_git_index_override(monkeypatch, tmp_path: Path):
    repo = init_repo(tmp_path)
    poisoned_index = tmp_path / "poisoned-index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(poisoned_index))
    (repo / "alpha.txt").write_text("one real change\n", encoding="utf-8")
    service, _store = new_service(tmp_path, repo)

    snapshot = service.capture()
    manifest = service.changes_from_head(snapshot)

    assert snapshot.complete is True
    assert {item.path for item in manifest.files} == {"alpha.txt"}
    assert poisoned_index.exists() is False


def test_binary_change_is_manifested_without_unbounded_binary_patch(tmp_path: Path):
    repo = init_repo(tmp_path)
    service, store = new_service(tmp_path, repo, page_bytes=80)
    before = service.capture()
    (repo / "asset.bin").write_bytes(b"\x00\x01\x02\xff" * 128)
    after = service.capture()

    manifest = service.changes_between(before, after)

    assert manifest.complete is True
    assert len(manifest.files) == 1
    item = manifest.files[0]
    assert item.path == "asset.bin"
    assert item.binary is True
    assert item.diff_bytes <= service.limits.max_file_diff_bytes
    diff = all_diff_pages(store, manifest.change_id, limit=80)
    assert "Binary files" in diff
    assert "GIT binary patch" not in diff


@pytest.mark.parametrize(
    ("limits", "files", "reason"),
    [
        (ChangeLimits(max_files=1), {"one.txt": b"1", "two.txt": b"2"}, "file count"),
        (ChangeLimits(max_file_bytes=4), {"large.txt": b"12345"}, "changed file exceeds"),
        (
            ChangeLimits(max_file_bytes=16, max_total_bytes=6),
            {"one.txt": b"1234", "two.txt": b"5678"},
            "total bytes",
        ),
        (ChangeLimits(max_binary_files=0), {"binary.bin": b"\x00data"}, "binary files"),
    ],
)
def test_capture_limits_fail_closed_as_incomplete(
    tmp_path: Path,
    limits: ChangeLimits,
    files: dict[str, bytes],
    reason: str,
):
    repo = init_repo(tmp_path)
    for name, content in files.items():
        (repo / name).write_bytes(content)
    service, store = new_service(tmp_path, repo, limits=limits)

    snapshot = service.capture()
    manifest = service.changes_from_head(snapshot)

    assert snapshot.supported is True
    assert snapshot.complete is False
    assert reason in snapshot.reason
    assert manifest.supported is True
    assert manifest.complete is False
    assert manifest.files == ()
    assert store.manifest(manifest.change_id)["complete"] is False


def test_stale_check_uses_working_tree_hash_and_leaves_no_persisted_probe(tmp_path: Path):
    repo = init_repo(tmp_path)
    service, store = new_service(tmp_path, repo)
    snapshot = service.capture()
    manifest = service.changes_from_head(snapshot)
    snapshots_before = {path.name for path in store.snapshots_dir.iterdir()}

    fresh = service.stale_status(manifest)
    assert fresh["stale"] is False
    assert fresh["currentHash"] == snapshot.content_hash
    assert {path.name for path in store.snapshots_dir.iterdir()} == snapshots_before

    (repo / "alpha.txt").write_text("changed after review input\n", encoding="utf-8")
    stale = service.stale_status(manifest)
    assert stale["stale"] is True
    assert stale["currentHash"] != snapshot.content_hash
    assert service.is_stale(manifest) is True
    assert {path.name for path in store.snapshots_dir.iterdir()} == snapshots_before


def test_file_diff_pagination_is_bounded_and_hashes_full_artifact(tmp_path: Path):
    repo = init_repo(tmp_path)
    service, store = new_service(tmp_path, repo, page_bytes=37)
    before = service.capture()
    (repo / "alpha.txt").write_text("\n".join(f"第 {index} 行" for index in range(80)) + "\n", encoding="utf-8")
    after = service.capture()
    manifest = service.changes_between(before, after)
    item = next(entry for entry in manifest.files if entry.path == "alpha.txt")

    offset = 0
    chunks: list[str] = []
    while True:
        page = store.read_file_diff(manifest.change_id, "alpha.txt", offset=offset, limit=37)
        assert len(page["text"].encode("utf-8")) <= 40
        chunks.append(page["text"])
        if page["complete"]:
            break
        assert page["nextOffset"] > offset
        offset = page["nextOffset"]
    payload = "".join(chunks).encode("utf-8")
    assert hashlib.sha256(payload).hexdigest() == item.diff_hash
    assert len(payload) == item.diff_bytes


def test_diff_reads_reject_same_length_artifact_tampering(tmp_path: Path):
    repo = init_repo(tmp_path)
    service, store = new_service(tmp_path, repo, page_bytes=37)
    before = service.capture()
    (repo / "alpha.txt").write_text("tamper target\n", encoding="utf-8")
    after = service.capture()
    manifest = service.changes_between(before, after)
    item = next(entry for entry in manifest.files if entry.path == "alpha.txt")
    artifact_path = store.change_dir(manifest.change_id) / item.artifact
    original = artifact_path.read_bytes()
    assert original
    artifact_path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    assert artifact_path.stat().st_size == item.diff_bytes

    with pytest.raises(ReviewError, match="hash does not match"):
        store.read_file_diff(manifest.change_id, "alpha.txt", limit=37)
    with pytest.raises(ReviewError, match="hash does not match"):
        store.read_diff(manifest.change_id, limit=37)


def test_diff_materialization_enforces_per_file_and_total_byte_limits(tmp_path: Path):
    repo = init_repo(tmp_path)
    limits = ChangeLimits(
        max_diff_bytes=90,
        max_file_diff_bytes=55,
        max_page_bytes=32,
    )
    service, store = new_service(tmp_path, repo, limits=limits, page_bytes=32)
    before = service.capture()
    (repo / "alpha.txt").write_text("alpha changed\n" * 20, encoding="utf-8")
    (repo / "beta.txt").write_text("beta changed\n" * 20, encoding="utf-8")
    after = service.capture()

    manifest = service.changes_between(before, after)

    assert manifest.complete is False
    assert manifest.truncated is True
    assert manifest.diff_bytes <= 90
    assert all(item.diff_bytes <= 55 for item in manifest.files)
    assert any(item.truncated for item in manifest.files)
    cursor = None
    while True:
        page = store.read_diff(manifest.change_id, cursor=cursor, limit=32)
        assert len(page["text"].encode("utf-8")) <= 35
        cursor = page["nextCursor"]
        if cursor is None:
            break


def test_review_store_inside_repository_fails_closed_before_creating_git_objects(tmp_path: Path):
    repo = init_repo(tmp_path)
    store = ReviewStore(repo / "review-data")
    service = ChangeSnapshotService(repo, store)
    index_before = (repo / ".git" / "index").read_bytes()
    status_before = git_bytes(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")

    snapshot = service.capture()

    assert snapshot.supported is True
    assert snapshot.complete is False
    assert "outside the Git working tree" in snapshot.reason
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert git_bytes(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all") == status_before
    assert not store.root.exists()


def test_snapshot_from_another_repository_cannot_escape_service_workspace(tmp_path: Path):
    first_root = tmp_path / "first"
    first_root.mkdir()
    first = init_repo(first_root)
    second_root = tmp_path / "second"
    second_root.mkdir()
    second = init_repo(second_root)
    service, _store = new_service(tmp_path, first)
    snapshot = service.capture()
    forged = replace(snapshot, repository=str(second))

    manifest = service.changes_from_head(forged)

    assert manifest.complete is False
    assert "active workspace repository" in manifest.reason


def test_review_store_rejects_diff_artifact_path_traversal(tmp_path: Path):
    store = ReviewStore(tmp_path / "review-store")
    item = ChangeFile(
        path="safe.txt",
        status="M",
        diff_bytes=4,
        diff_hash=hashlib.sha256(b"diff").hexdigest(),
        artifact="../escaped.diff",
    )
    manifest = ChangeManifest(
        change_id="safe-change",
        scope="working_tree",
        repository=str(tmp_path),
        created_at="2026-01-01T00:00:00+00:00",
        supported=True,
        complete=True,
        reason="",
        base_id="base",
        target_id="target",
        base_tree="a" * 40,
        target_tree="b" * 40,
        target_hash="c" * 64,
        change_hash="d" * 64,
        files=(item,),
        total_files=1,
        diff_bytes=4,
    )

    with pytest.raises(ReviewError, match="artifact"):
        store.save_change(manifest, {"../escaped.diff": b"diff"})

    assert not (store.root / "escaped.diff").exists()
    assert not (tmp_path / "escaped.diff").exists()


def test_snapshot_never_executes_repository_clean_filter_and_fails_closed(tmp_path: Path):
    repo = init_repo(tmp_path)
    marker = tmp_path / "filter-ran.txt"
    filter_script = tmp_path / "evil_filter.py"
    filter_script.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    filter_command = " ".join(shlex.quote(Path(value).as_posix()) for value in (sys.executable, filter_script, marker))
    git(repo, "config", "filter.evil.clean", filter_command)
    git(repo, "config", "filter.evil.required", "true")
    (repo / ".gitattributes").write_text("alpha.txt filter=evil\n", encoding="utf-8")
    (repo / "alpha.txt").write_text("changed without running filter\n", encoding="utf-8")
    git(repo, "hash-object", "--path=alpha.txt", "alpha.txt")
    assert marker.exists()
    marker.unlink()
    service, _store = new_service(tmp_path, repo)

    snapshot = service.capture()

    assert snapshot.complete is False
    assert "custom Git filter" in snapshot.reason
    assert marker.exists() is False


def test_snapshot_applies_builtin_crlf_normalization_to_changed_files(tmp_path: Path):
    repo = init_repo(tmp_path)
    git(repo, "config", "core.autocrlf", "true")
    path = repo / "alpha.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    git(repo, "add", "alpha.txt")
    git(repo, "commit", "-m", "normalize line endings")
    path.write_bytes(b"one\r\nTWO\r\nthree\r\n")
    service, store = new_service(tmp_path, repo)

    snapshot = service.capture()
    manifest = service.changes_from_head(snapshot)

    assert snapshot.complete is True
    assert manifest.complete is True
    item = next(entry for entry in manifest.files if entry.path == "alpha.txt")
    assert item.additions == 1
    assert item.deletions == 1
    diff = all_diff_pages(store, manifest.change_id)
    assert "-two\n+TWO\n" in diff
    assert "\r" not in diff


def test_snapshot_fails_closed_for_worktree_encoding(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / ".gitattributes").write_text(
        "alpha.txt working-tree-encoding=UTF-16LE\n",
        encoding="utf-8",
    )
    (repo / "alpha.txt").write_bytes("changed encoding\n".encode("utf-16-le"))
    service, _store = new_service(tmp_path, repo)

    snapshot = service.capture()

    assert snapshot.supported is True
    assert snapshot.complete is False
    assert "working-tree-encoding" in snapshot.reason


def test_dirty_submodule_fails_closed_instead_of_producing_empty_change(tmp_path: Path):
    sub_root = tmp_path / "sub-source"
    parent_root = tmp_path / "parent"
    sub_root.mkdir()
    parent_root.mkdir()
    sub_repo = init_repo(sub_root)
    repo = init_repo(parent_root)
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(sub_repo), "modules/sub")
    git(repo, "commit", "-am", "add submodule")
    service, _store = new_service(tmp_path, repo)
    before = service.capture()
    (repo / "modules" / "sub" / "alpha.txt").write_text("dirty inside submodule\n", encoding="utf-8")
    index_before = (repo / ".git" / "index").read_bytes()

    after = service.capture()
    manifest = service.changes_between(before, after)

    assert after.supported is True
    assert after.complete is False
    assert "submodule" in after.reason
    assert manifest.complete is False
    assert manifest.files == ()
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_non_git_workspace_is_explicitly_unsupported_and_incomplete(tmp_path: Path):
    workspace = tmp_path / "plain"
    workspace.mkdir()
    (workspace / "file.txt").write_text("plain\n", encoding="utf-8")
    service, store = new_service(tmp_path, workspace)

    snapshot = service.capture()
    manifest = service.changes_from_head(snapshot)

    assert snapshot.supported is False
    assert snapshot.complete is False
    assert "not a Git repository" in snapshot.reason
    assert manifest.supported is False
    assert manifest.complete is False
    assert manifest.files == ()
    assert store.manifest(manifest.change_id)["supported"] is False


def test_missing_git_executable_is_reported_as_unsupported(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def missing_git(*_args, **_kwargs):
        raise FileNotFoundError("git executable was not found")

    monkeypatch.setattr(reviews_module, "popen_hidden", missing_git)
    service, _store = new_service(tmp_path, workspace)

    snapshot = service.capture()

    assert snapshot.supported is False
    assert snapshot.complete is False
    assert "not a Git repository" in snapshot.reason
