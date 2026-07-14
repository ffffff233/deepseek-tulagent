from __future__ import annotations

from pathlib import Path
import subprocess

from deepseekfathom._core.policy import ApprovalPolicy
from deepseekfathom._core.tools import ToolRegistry


def git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True, text=True)


def initialized_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(tmp_path, "add", "app.py")
    git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def registry(workspace: Path) -> ToolRegistry:
    return ToolRegistry(workspace, policy=ApprovalPolicy.from_mode("plan"))


def test_git_diff_reads_working_and_staged_changes(tmp_path: Path) -> None:
    workspace = initialized_repo(tmp_path)
    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")

    working = registry(workspace).git_diff({"scope": "working"})
    assert working.ok is True
    assert "-value = 1" in working.output
    assert "+value = 2" in working.output

    git(workspace, "add", "app.py")
    staged = registry(workspace).git_diff({"scope": "staged"})
    assert staged.ok is True
    assert "+value = 2" in staged.output


def test_git_diff_can_review_latest_commit_and_reject_bad_scope(tmp_path: Path) -> None:
    workspace = initialized_repo(tmp_path)
    latest = registry(workspace).git_diff({"scope": "last_commit", "context": 1})
    assert latest.ok is True
    assert "initial" in latest.output
    assert "+value = 1" in latest.output

    try:
        registry(workspace).git_diff({"scope": "everything"})
    except Exception as exc:
        assert "scope must be" in str(exc)
    else:
        raise AssertionError("invalid git_diff scope was accepted")


def test_git_diff_is_bounded_and_path_scoped(tmp_path: Path) -> None:
    workspace = initialized_repo(tmp_path)
    (workspace / "other.txt").write_text("unchanged\n", encoding="utf-8")
    git(workspace, "add", "other.txt")
    git(workspace, "commit", "-m", "other")
    (workspace / "app.py").write_text("\n".join(f"line {index}" for index in range(3000)), encoding="utf-8")

    result = registry(workspace).git_diff({"path": "app.py", "max_chars": 2_000})

    assert result.ok is True
    assert "diff truncated" in result.output
    assert "other.txt" not in result.output
