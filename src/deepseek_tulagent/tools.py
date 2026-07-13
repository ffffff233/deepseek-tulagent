from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import base64
import difflib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any
from html import unescape
import re
import urllib.parse
import urllib.request
import urllib.robotparser
from uuid import uuid4
import zipfile

from .policy import ApprovalPolicy
from .processes import popen_hidden, run_hidden
from .skills import SkillStore, render_skill


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str
    images: list[str] | None = None
    ui: dict[str, Any] | None = None

    def to_message(self) -> str:
        payload: dict[str, Any] = {"ok": self.ok, "output": self.output}
        if self.ui:
            payload["ui"] = self.ui
        return json.dumps(payload, ensure_ascii=False)


def _bounded_diff(text: str, max_chars: int = 18_000) -> tuple[str, bool, int]:
    """Keep both ends of a large diff without cutting through a line.

    Unified diffs commonly place a large deletion block before its replacement. A
    prefix-only truncation therefore hides the additions and makes the UI report a
    misleading one-sided change. Keeping a balanced head and tail preserves both
    sides while the structured counters still describe the complete patch.
    """
    if len(text) <= max_chars:
        return text, False, 0
    lines = text.splitlines()
    budget = max(256, max_chars - 160)
    head_budget = budget // 2
    tail_budget = budget - head_budget
    head: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if head and used + cost > head_budget:
            break
        head.append(line)
        used += cost
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head):]):
        cost = len(line) + 1
        if tail and used + cost > tail_budget:
            break
        tail.append(line)
        used += cost
    tail.reverse()
    omitted_lines = max(0, len(lines) - len(head) - len(tail))
    marker = f"... {omitted_lines} diff lines omitted ..."
    return "\n".join((*head, marker, *tail)), True, omitted_lines


def _diff_stats(text: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _file_change_ui(path: str, old: str, new: str, *, existed: bool) -> dict[str, Any]:
    from_name = f"a/{path}" if existed else "/dev/null"
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=from_name,
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    bounded, truncated, omitted_lines = _bounded_diff(diff)
    additions, deletions = _diff_stats(diff)
    return {
        "kind": "file_change",
        "operation": "modified" if existed else "created",
        "path": path,
        "paths": [path],
        "diff": bounded,
        "additions": additions,
        "deletions": deletions,
        "truncated": truncated,
        "omitted_lines": omitted_lines,
    }


def _patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].split("\t", 1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith("b/"):
            raw = raw[2:]
        if raw and raw not in paths:
            paths.append(raw)
    return paths


ToolHandler = Callable[[dict[str, Any]], ToolResult]


TOOL_DESCRIPTIONS = {
    "list_files": "read: list files in the workspace",
    "search_text": "read: search text in workspace files",
    "git_status": "read: show git short status",
    "read_file": "read: read UTF-8 text from a workspace file",
    "list_skills": "read: search discovered skill names and descriptions without loading their bodies",
    "read_skill": "read: load a discovered skill body into the current context on demand",
    "write_file": "gated write: create or overwrite a workspace file",
    "run_shell": "gated shell: run a shell command in the workspace",
    "apply_patch": "gated write: apply a unified diff with git apply",
    "download_url": "gated network+write: download URL into workspace",
    "clone_repo": "gated network+write: clone a Git/GitHub repository with mirror and archive fallbacks",
    "web_search": "network: search the web via Baidu/Bing/DuckDuckGo and return result snippets",
    "todo_write": "session: create or update the visible task list with pending/in_progress/completed/cancelled items",
    "inspect_media": "read+vision: inspect an image/video path by attaching screenshots or sampled frames to the next model turn",
    "start_service": "gated shell: start background service with pid/log tracking",
    "stop_service": "gated shell: stop a tracked background service",
    "service_status": "read: inspect a tracked background service",
}


class ToolRegistry:
    def __init__(
        self,
        workspace: Path,
        allow_write: bool | None = None,
        allow_shell: bool | None = None,
        policy: ApprovalPolicy | None = None,
        skill_store: SkillStore | None = None,
    ):
        self.workspace = workspace.resolve()
        self.policy = policy or ApprovalPolicy(
            "custom",
            True,
            bool(allow_write),
            bool(allow_shell),
            False,
            True,
        )
        self.allow_write = self.policy.allow_write
        self.allow_shell = self.policy.allow_shell
        # Full-access tiers (完全访问 / root, yolo) lift the workspace confinement so file
        # tools can reach anywhere — matching the shell, which was never path-confined, and
        # Codex's full-access mode. Restricted tiers keep files inside the workspace.
        self.unconfined = self.policy.name in {"root", "yolo"}
        self.skill_store = skill_store or SkillStore(self.workspace)
        self._tools: dict[str, ToolHandler] = {
            "list_files": self.list_files,
            "search_text": self.search_text,
            "git_status": self.git_status,
            "read_file": self.read_file,
            "list_skills": self.list_skills,
            "read_skill": self.read_skill,
            "write_file": self.write_file,
            "run_shell": self.run_shell,
            "apply_patch": self.apply_patch,
            "download_url": self.download_url,
            "clone_repo": self.clone_repo,
            "web_search": self.web_search,
            "todo_write": self.todo_write,
            "inspect_media": self.inspect_media,
            "start_service": self.start_service,
            "stop_service": self.stop_service,
            "service_status": self.service_status,
        }

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> dict[str, str]:
        return {name: TOOL_DESCRIPTIONS.get(name, "tool") for name in self.names}

    def run(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in self._tools:
            raise ToolError(f"Unknown tool: {name}")
        return self._tools[name](arguments)

    def resolve_workspace_path(self, raw_path: str) -> Path:
        raw_path = normalize_user_path(raw_path)
        candidate = Path(raw_path).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (self.workspace / raw_path).resolve()
        if self.unconfined:
            return path  # 完全访问 / root: reach anywhere, like the shell already can
        try:
            path.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolError(f"Path escapes workspace: {raw_path}") from exc
        return path

    def _display_path(self, path: Path) -> str:
        """Workspace-relative path for display, or the absolute path when outside it
        (full-access mode) so we never crash on relative_to()."""
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)

    def read_file(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.resolve_workspace_path(require_str(arguments, "path"))
        max_bytes = int(arguments.get("max_bytes", 20000))
        data = path.read_bytes()[:max_bytes]
        return ToolResult(True, data.decode("utf-8", errors="replace"))

    def read_skill(self, arguments: dict[str, Any]) -> ToolResult:
        name = require_str(arguments, "name")
        extra = arguments.get("arguments", "")
        if not isinstance(extra, str):
            raise ToolError("read_skill arguments must be a string")
        store = self.skill_store
        skill = store.get(name)
        if skill is None:
            available = ", ".join(item.name for item in store.list()) or "none"
            raise ToolError(f"Unknown skill: {name}. Available skills: {available}")
        return ToolResult(True, render_skill(skill, extra))

    def list_skills(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query", ""))
        limit = int(arguments.get("limit", 100))
        skills = self.skill_store.search(query, limit)
        payload = [
            {"name": skill.name, "description": skill.description, "scope": skill.scope}
            for skill in skills
        ]
        return ToolResult(True, json.dumps({"skills": payload, "count": len(payload)}, ensure_ascii=False))

    def list_files(self, arguments: dict[str, Any]) -> ToolResult:
        root = self.resolve_workspace_path(str(arguments.get("path", ".")))
        max_entries = int(arguments.get("max_entries", 300))
        # inside the workspace, show workspace-relative paths; when listing an outside
        # directory (full-access mode), show paths relative to that directory
        try:
            root.relative_to(self.workspace)
            base = self.workspace
        except ValueError:
            base = root if root.is_dir() else root.parent
        if root.is_file():
            return ToolResult(True, self._display_path(root))
        entries: list[str] = []
        for path in sorted(root.rglob("*")):
            if len(entries) >= max_entries:
                entries.append("...")
                break
            if should_skip(path):
                continue
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                rel = str(path)
            entries.append(rel + ("/" if path.is_dir() else ""))
        return ToolResult(True, "\n".join(entries))

    def search_text(self, arguments: dict[str, Any]) -> ToolResult:
        query = require_str(arguments, "query")
        path = self.resolve_workspace_path(str(arguments.get("path", ".")))
        max_matches = int(arguments.get("max_matches", 100))
        timeout = int(arguments.get("timeout", 5))
        rg = find_rg()
        if rg:
            command = [
                rg,
                "--line-number",
                "--color=never",
                "--hidden",
                "--glob", "!.git",
                "--glob", "!node_modules",
                "--glob", "!__pycache__",
                "--glob", "!.venv",
                "--glob", "!target",
                "--glob", "!dist",
                "--glob", "!build",
                "--max-filesize", str(arguments.get("max_filesize", "1M")),
                "--max-count", "1",
                "--fixed-strings",
                query,
                str(path),
            ]
            try:
                completed = run_hidden(command, cwd=self.workspace, text=True, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return ToolResult(False, f"search timed out after {timeout}s")
            lines = completed.stdout.splitlines()[:max_matches]
            workspace_prefix = str(self.workspace) + os.sep
            normalized_lines: list[str] = []
            for line in lines:
                if line.startswith(workspace_prefix):
                    line = line[len(workspace_prefix):]
                    path_part, separator, detail = line.partition(":")
                    line = path_part.replace("\\", "/") + separator + detail
                normalized_lines.append(line)
            lines = normalized_lines
            if len(completed.stdout.splitlines()) > max_matches:
                lines.append("...")
            if completed.returncode not in {0, 1}:
                return ToolResult(False, completed.stderr.strip() or "search failed")
            return ToolResult(True, "\n".join(lines))
        matches: list[str] = []
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for file_path in files:
            if len(matches) >= max_matches:
                matches.append("...")
                break
            if should_skip(file_path):
                continue
            try:
                for line_no, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if query in line:
                        rel = self._display_path(file_path)
                        matches.append(f"{rel}:{line_no}: {line[:240]}")
                        if len(matches) >= max_matches:
                            break
            except OSError:
                continue
        return ToolResult(True, "\n".join(matches))

    def git_status(self, arguments: dict[str, Any]) -> ToolResult:
        completed = run_hidden(
            ["git", "status", "--short"],
            cwd=self.workspace,
            text=True,
            capture_output=True,
            timeout=int(arguments.get("timeout", 30)),
        )
        output = completed.stdout
        if completed.stderr:
            output += "\n[stderr]\n" + completed.stderr
        return ToolResult(completed.returncode == 0, output.strip() or "clean")

    def write_file(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.allow_write:
            raise ToolError("write_file is disabled in this mode")
        raw_path = require_str(arguments, "path")
        if raw_path.strip() in {"...", "…"}:
            raise ToolError("write_file path is a placeholder, not a file path")
        path = self.resolve_workspace_path(raw_path)
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ToolError("Missing string argument: content")
        if path.is_dir():
            raise ToolError(f"write_file target is a directory, not a file: {self._display_path(path)}")
        existed = path.is_file()
        old_content = path.read_text(encoding="utf-8", errors="replace") if existed else ""
        display_path = self._display_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex[:8]}")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return ToolResult(
            True,
            f"Wrote {display_path}",
            ui=_file_change_ui(display_path, old_content, content, existed=existed),
        )

    def run_shell(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.allow_shell:
            raise ToolError("run_shell is disabled in this mode")
        command = require_str(arguments, "command")
        if is_background_command(command):
            return self.start_service({"name": arguments.get("name", "shell-bg"), "command": strip_background(command)})
        invocation, use_shell = shell_invocation(command)
        completed = run_hidden(
            invocation,
            cwd=self.workspace,
            shell=use_shell,
            text=True,
            capture_output=True,
            timeout=int(arguments.get("timeout", 60)),
        )
        output = completed.stdout or ""
        stderr = completed.stderr or ""
        if stderr:
            output += "\n[stderr]\n" + stderr
        return ToolResult(completed.returncode == 0, (output.strip() or "clean")[-30000:])

    def apply_patch(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.allow_write:
            raise ToolError("apply_patch is disabled in this mode")
        patch = require_str(arguments, "patch")
        completed = run_hidden(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=self.workspace,
            input=patch,
            text=True,
            capture_output=True,
            timeout=int(arguments.get("timeout", 60)),
        )
        output = completed.stdout
        if completed.stderr:
            output += "\n[stderr]\n" + completed.stderr
        paths = _patch_paths(patch)
        display_path = paths[0] if len(paths) == 1 else f"{len(paths)} 个文件"
        succeeded = completed.returncode == 0
        bounded, truncated, omitted_lines = _bounded_diff(patch)
        additions, deletions = _diff_stats(patch)
        return ToolResult(
            succeeded,
            output.strip() or ("patch applied" if succeeded else "patch was not applied"),
            ui={
                "kind": "file_change",
                "operation": "modified",
                "path": display_path,
                "paths": paths,
                "diff": bounded,
                "additions": additions,
                "deletions": deletions,
                "truncated": truncated,
                "omitted_lines": omitted_lines,
            } if succeeded else None,
        )

    def download_url(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.policy.allow_network:
            raise ToolError("download_url is disabled in this mode")
        url = require_str(arguments, "url")
        raw_dest = require_str(arguments, "path")
        path = self.resolve_workspace_path(raw_dest)
        max_bytes = int(arguments.get("max_bytes", 20_000_000))
        with urllib.request.urlopen(url, timeout=int(arguments.get("timeout", 60))) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ToolError(f"download exceeded max_bytes={max_bytes}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ToolResult(True, f"Downloaded {len(data)} bytes to {self._display_path(path)}")

    def clone_repo(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.policy.allow_network or not self.allow_write:
            raise ToolError("clone_repo is disabled in this mode")
        repo = str(arguments.get("repo") or arguments.get("url") or arguments.get("repo/url") or arguments.get("repository") or "").strip()
        if not repo:
            raise ToolError("Missing string argument: repo")
        dest = self.resolve_workspace_path(require_str(arguments, "path"))
        branch = str(arguments.get("branch") or "").strip()
        timeout = int(arguments.get("timeout", 120))
        if dest.exists() and any(dest.iterdir() if dest.is_dir() else [dest]):
            return ToolResult(False, f"target exists and is not empty: {self._display_path(dest)}")
        dest.parent.mkdir(parents=True, exist_ok=True)

        attempts: list[str] = []
        for clone_url in clone_url_candidates(repo):
            command = ["git", "clone", "--depth", "1"]
            if branch:
                command += ["--branch", branch]
            command += [clone_url, str(dest)]
            ok, summary = run_clone_attempt(command, self.workspace, timeout)
            attempts.append(f"git clone {redact_url(clone_url)} -> {summary}")
            if ok:
                return ToolResult(True, "Repository cloned.\n" + "\n".join(attempts))
            cleanup_empty_path(dest)

        github_repo = parse_github_repo(repo)
        if github_repo:
            owner, name = github_repo
            refs = [branch] if branch else ["main", "master"]
            with tempfile.TemporaryDirectory(prefix="dstul-clone-") as tmp:
                tmp_path = Path(tmp)
                for ref in refs:
                    for archive_url in github_archive_candidates(owner, name, ref):
                        archive_path = tmp_path / archive_filename(archive_url)
                        ok, summary = download_archive(archive_url, archive_path, timeout)
                        attempts.append(f"archive {redact_url(archive_url)} -> {summary}")
                        if not ok:
                            continue
                        ok, summary = extract_repo_archive(archive_path, dest)
                        attempts.append(f"extract {archive_path.name} -> {summary}")
                        if ok:
                            return ToolResult(True, "Repository downloaded from archive fallback.\n" + "\n".join(attempts))
                        cleanup_empty_path(dest)

        hint = (
            "All clone methods failed. If the network requires a local proxy, set HTTPS_PROXY/HTTP_PROXY "
            "or configure git proxy, then retry clone_repo."
        )
        return ToolResult(False, "\n".join(attempts + [hint]))

    def web_search(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.policy.allow_network:
            raise ToolError("web_search is disabled in this mode")
        query = require_str(arguments, "query")
        max_results = int(arguments.get("max_results", 5))
        timeout = int(arguments.get("timeout", 10))
        if looks_like_url(query):
            return fetch_direct_url(query, timeout=timeout)
        options = {
            "language": string_or_none(arguments.get("language")) or "zh-CN",
            "engines": string_or_none(arguments.get("engines") or arguments.get("engine") or arguments.get("search_engine")),
        }
        results, diagnostics = run_web_search(query, max_results=max_results, timeout=timeout, options=options)
        if not results:
            detail = "\n".join(diagnostics) if diagnostics else "no search sources attempted"
            return ToolResult(
                False,
                "web search returned no parseable results.\n"
                f"query: {query}\n"
                f"{detail}",
            )
        fetch_pages = int(arguments.get("fetch_pages", arguments.get("fetchPages", 0)) or 0)
        if fetch_pages > 0:
            page_limit = int(arguments.get("page_chars", arguments.get("pageChars", 800)) or 800)
            results.extend(fetch_result_pages(results, timeout=timeout, fetch_pages=min(fetch_pages, 5), page_chars=max(200, min(page_limit, 1600))))
        return ToolResult(True, "\n\n".join(results))

    def todo_write(self, arguments: dict[str, Any]) -> ToolResult:
        raw_items = arguments.get("todos") or arguments.get("items") or []
        if not isinstance(raw_items, list):
            raise ToolError("todo_write requires a todos array")
        todos: list[dict[str, str]] = []
        seen_in_progress = False
        allowed = {"pending", "in_progress", "completed", "cancelled"}
        for index, item in enumerate(raw_items):
            if isinstance(item, str):
                content = item.strip()
                status = "pending"
            elif isinstance(item, dict):
                content = str(item.get("content") or item.get("text") or item.get("title") or "").strip()
                status = str(item.get("status") or "pending").strip()
            else:
                continue
            if not content:
                continue
            if status not in allowed:
                status = "pending"
            if status == "in_progress":
                if seen_in_progress:
                    status = "pending"
                else:
                    seen_in_progress = True
            todos.append({"id": f"todo-{index + 1}", "content": content[:500], "status": status})
        return ToolResult(True, json.dumps({"todos": todos}, ensure_ascii=False))

    def inspect_media(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.resolve_workspace_path(require_str(arguments, "path"))
        max_frames = max(1, min(int(arguments.get("max_frames", arguments.get("maxFrames", 6)) or 6), 12))
        if not path.exists() or not path.is_file():
            return ToolResult(False, f"media file not found: {self._display_path(path)}")
        media = mimetypes.guess_type(path.name)[0] or ""
        images: list[str] = []
        kind = "file"
        if media.startswith("image/") or path.suffix.lower() in IMAGE_EXTENSIONS:
            images = [image_data_url(path, media or "image/png")]
            kind = "image"
        elif media.startswith("video/") or path.suffix.lower() in VIDEO_EXTENSIONS:
            images = extract_video_frame_data_urls(path, max_frames=max_frames)
            kind = "video"
        else:
            return ToolResult(False, f"unsupported media type for inspect_media: {path.name}")
        if not images:
            return ToolResult(False, f"could not extract visual frames from {self._display_path(path)}")
        output = (
            f"inspected {kind}: {self._display_path(path)}\n"
            f"attached {len(images)} visual frame(s) for the next model turn.\n"
            "Use the attached image(s) to answer the user's media request."
        )
        return ToolResult(True, output, images=images)

    def start_service(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.allow_shell:
            raise ToolError("start_service is disabled in this mode")
        name = require_str(arguments, "name")
        command = require_str(arguments, "command")
        services_dir = self.workspace / ".deepseek-tulagent" / "services"
        services_dir.mkdir(parents=True, exist_ok=True)
        log_path = services_dir / f"{safe_name(name)}.log"
        pid_path = services_dir / f"{safe_name(name)}.pid"
        if pid_path.exists() and process_alive(pid_path):
            return ToolResult(True, f"Service {name} already running with pid {pid_path.read_text().strip()}")
        invocation, use_shell = shell_invocation(command)
        with log_path.open("ab") as log:
            process = popen_hidden(
                invocation,
                cwd=self.workspace,
                shell=use_shell,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        pid_path.write_text(str(process.pid), encoding="utf-8")
        return ToolResult(True, f"Started {name} pid={process.pid} log={self._display_path(log_path)}")

    def stop_service(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.allow_shell:
            raise ToolError("stop_service is disabled in this mode")
        name = require_str(arguments, "name")
        pid_path = self.workspace / ".deepseek-tulagent" / "services" / f"{safe_name(name)}.pid"
        if not pid_path.exists():
            return ToolResult(True, f"Service {name} is not recorded")
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        if os.name == "nt":
            run_hidden(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        else:
            run_hidden(["kill", str(pid)], capture_output=True, text=True)
        pid_path.unlink(missing_ok=True)
        return ToolResult(True, f"Stopped {name} pid={pid}")

    def service_status(self, arguments: dict[str, Any]) -> ToolResult:
        name = require_str(arguments, "name")
        pid_path = self.workspace / ".deepseek-tulagent" / "services" / f"{safe_name(name)}.pid"
        if not pid_path.exists():
            return ToolResult(True, f"{name}: stopped")
        alive = process_alive(pid_path)
        return ToolResult(True, f"{name}: {'running' if alive else 'dead'} pid={pid_path.read_text().strip()}")


def require_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError(f"Missing string argument: {key}")
    return value


def normalize_user_path(raw_path: str) -> str:
    path = raw_path.strip()
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", path):
        path = re.sub(r"^[A-Za-z]:[\\/]+", "", path)
    path = path.replace("\\", "/")
    return path


POWERSHELL_COMMAND_RE = re.compile(
    r"(?i)(?:^|[;|]\s*)(?:Get|Set|New|Remove|Test|Write|Select|Where|ForEach|Start|Stop)-[A-Za-z]"
)
POSIX_COMMAND_RE = re.compile(
    r"(?i)^\s*(?:printf|ls|pwd|cat|cp|mv|rm|touch|grep|sed|awk|find|head|tail|chmod|export|which)(?:\s|$)"
)


def shell_invocation(command: str) -> tuple[str | list[str], bool]:
    """Choose a shell that matches the model's command dialect.

    Models frequently emit POSIX shell even on Windows. Git Bash is preferred for
    those commands; PowerShell remains available for native cmdlets, and ordinary
    Windows commands keep the system shell behavior used by earlier releases.
    """
    if os.name != "nt":
        return command, True
    if looks_like_powershell(command):
        return powershell_invocation(command), False
    if POSIX_COMMAND_RE.search(command) or any(marker in command for marker in ("$(", "${", "/dev/null", "#!/bin/")):
        bash = find_git_bash()
        if bash:
            return [bash, "-lc", command], False
        return powershell_invocation(command, posix_compat=True), False
    return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", command], False


def looks_like_powershell(command: str) -> bool:
    return bool(
        POWERSHELL_COMMAND_RE.search(command)
        or re.search(r"(?i)\$(?:env:|PSVersionTable|ErrorActionPreference|_)\b", command)
    )


def find_git_bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "bin" / "bash.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def powershell_invocation(command: str, *, posix_compat: bool = False) -> list[str]:
    executable = shutil.which("powershell.exe") or "powershell.exe"
    if posix_compat:
        command = (
            "function printf { param([string]$Format, "
            "[Parameter(ValueFromRemainingArguments=$true)][object[]]$Values) "
            "if ($Values.Count) { [Console]::Write($Format, $Values) } "
            "else { [Console]::Write($Format) } }; "
            + command
        )
    return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]


def should_skip(path: Path) -> bool:
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "target", "dist", "build"}
    return any(part in ignored for part in path.parts)


def safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in name)[:80] or "service"


def process_alive(pid_path: Path) -> bool:
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        if os.name == "nt":
            completed = run_hidden(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
            )
            return completed.returncode == 0 and f'"{pid}"' in completed.stdout
        return run_hidden(["kill", "-0", str(pid)], capture_output=True).returncode == 0
    except (OSError, ValueError):
        return False


def find_rg() -> str | None:
    found = shutil.which("rg")
    if found:
        return found
    candidates = [
        "/usr/bin/rg",
        "/usr/local/bin/rg",
        "/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/codex-path/rg",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def is_background_command(command: str) -> bool:
    stripped = command.strip()
    return stripped.endswith("&") or "&>/dev/null &" in stripped or ">/dev/null 2>&1 &" in stripped


def strip_background(command: str) -> str:
    stripped = command.strip()
    for suffix in ("&>/dev/null &", ">/dev/null 2>&1 &", "&"):
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)].strip()
    return stripped


def clone_url_candidates(repo: str) -> list[str]:
    normalized = normalize_git_url(repo)
    candidates = [normalized]
    github_repo = parse_github_repo(normalized)
    if github_repo:
        owner, name = github_repo
        https_url = f"https://github.com/{owner}/{name}.git"
        candidates = [
            https_url,
            f"https://ghproxy.net/{https_url}",
            f"https://mirror.ghproxy.com/{https_url}",
            f"https://hub.gitmirror.com/{https_url}",
            f"https://gitclone.com/github.com/{owner}/{name}.git",
            f"https://githubfast.com/{owner}/{name}.git",
        ]
        if normalized not in candidates:
            candidates.insert(0, normalized)
    return dedupe(candidates)


def normalize_git_url(repo: str) -> str:
    repo = repo.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return f"https://github.com/{repo}.git"
    if repo.startswith("git@github.com:"):
        slug = repo.removeprefix("git@github.com:").removesuffix(".git")
        return f"https://github.com/{slug}.git"
    github_repo = parse_github_repo_without_normalize(repo)
    if github_repo and not repo.rstrip("/").endswith(".git"):
        return repo.rstrip("/") + ".git"
    return repo


def parse_github_repo(repo: str) -> tuple[str, str] | None:
    repo = normalize_git_url(repo)
    return parse_github_repo_without_normalize(repo)


def parse_github_repo_without_normalize(repo: str) -> tuple[str, str] | None:
    if repo.startswith("git@github.com:"):
        repo = "https://github.com/" + repo.removeprefix("git@github.com:")
    parsed = urllib.parse.urlparse(repo)
    path = parsed.path.strip("/")
    if "github.com" not in parsed.netloc and not path.startswith("github.com/"):
        return None
    if path.startswith("github.com/"):
        path = path.removeprefix("github.com/")
    parts = path.removesuffix(".git").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def github_archive_candidates(owner: str, repo: str, ref: str) -> list[str]:
    quoted_ref = urllib.parse.quote(ref, safe="")
    direct = f"https://github.com/{owner}/{repo}/archive/refs/heads/{quoted_ref}.zip"
    codeload = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{quoted_ref}"
    return [
        direct,
        codeload,
        f"https://ghproxy.net/{direct}",
        f"https://mirror.ghproxy.com/{direct}",
        f"https://hub.gitmirror.com/{direct}",
    ]


def run_clone_attempt(command: list[str], cwd: Path, timeout: int) -> tuple[bool, str]:
    try:
        completed = run_hidden(command, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    output = (completed.stderr or completed.stdout or "").strip().splitlines()
    summary = output[-1][:300] if output else f"exit {completed.returncode}"
    return completed.returncode == 0, summary


def download_archive(url: str, dest: Path, timeout: int) -> tuple[bool, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "DeepSeekTuL/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            dest.write_bytes(response.read())
    except Exception as exc:  # urllib raises several network-specific subclasses.
        return False, str(exc)[:300]
    return True, f"downloaded {dest.stat().st_size} bytes"


def extract_repo_archive(archive_path: Path, dest: Path) -> tuple[bool, str]:
    try:
        with tempfile.TemporaryDirectory(prefix="dstul-extract-") as tmp:
            tmp_path = Path(tmp)
            if zipfile.is_zipfile(archive_path):
                with zipfile.ZipFile(archive_path) as archive:
                    safe_extract_zip(archive, tmp_path)
            elif tarfile.is_tarfile(archive_path):
                with tarfile.open(archive_path) as archive:
                    safe_extract_tar(archive, tmp_path)
            else:
                return False, "unsupported archive format"
            roots = [path for path in tmp_path.iterdir()]
            source = roots[0] if len(roots) == 1 and roots[0].is_dir() else tmp_path
            dest.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                shutil.move(str(item), str(dest / item.name))
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return False, str(exc)[:300]
    return True, f"extracted to {dest.name}"


def safe_extract_zip(archive: zipfile.ZipFile, dest: Path) -> None:
    for member in archive.infolist():
        target = (dest / member.filename).resolve()
        target.relative_to(dest.resolve())
    archive.extractall(dest)


def safe_extract_tar(archive: tarfile.TarFile, dest: Path) -> None:
    for member in archive.getmembers():
        target = (dest / member.name).resolve()
        target.relative_to(dest.resolve())
    archive.extractall(dest)


def archive_filename(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name or "repo.zip"
    return name if "." in name else f"{name}.zip"


def cleanup_empty_path(path: Path) -> None:
    if path.is_dir() and not any(path.iterdir()):
        path.rmdir()


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        return urllib.parse.urlunparse(parsed._replace(netloc=netloc))
    return url


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def looks_like_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def fetch_text_url(url: str, *, timeout: int, max_bytes: int = 1_500_000) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 DeepSeekTuL/0.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(max_bytes).decode("utf-8", errors="replace")


def can_fetch_url(url: str, *, timeout: int) -> tuple[bool, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "unsupported URL"
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        robots_text = fetch_text_url(robots_url, timeout=timeout, max_bytes=200_000)
    except Exception as exc:
        return True, f"robots unavailable: {exc}"
    parser.parse(robots_text.splitlines())
    allowed = parser.can_fetch("DeepSeekTuL", url)
    return allowed, "robots allowed" if allowed else "blocked by robots.txt"


def local_search_url_candidates(search_url: str | None = None) -> list[str]:
    configured = string_or_none(search_url) or string_or_none(os.getenv("DSTUL_SEARCH_URL"))
    if configured:
        return [configured.rstrip("/")]
    return [
        "http://127.0.0.1:8080/search",
        "http://127.0.0.1:8888/search",
        "http://127.0.0.1:4000/search",
        "http://localhost:8080/search",
        "http://localhost:8888/search",
        "http://localhost:4000/search",
        "http://127.0.0.1:8090/yacysearch.json",
        "http://localhost:8090/yacysearch.json",
    ]


def local_search_request_url(base_url: str, query: str, options: dict[str, str | None] | None = None, max_results: int = 5) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme:
        base_url = "http://" + base_url
    parsed = urllib.parse.urlparse(base_url)
    path = parsed.path.rstrip("/")
    is_yacy = path.endswith("/yacysearch.json")
    if not is_yacy and not path.endswith("/search"):
        base_url = base_url.rstrip("/") + "/search"
    options = options or {}
    if is_yacy:
        params = {"query": query, "resource": "local", "maximumRecords": str(max_results)}
    else:
        params = {"q": query, "format": "json", "language": options.get("language") or "zh-CN", "safesearch": "0"}
        if options.get("categories"):
            params["categories"] = str(options["categories"])
        if options.get("time_range"):
            params["time_range"] = str(options["time_range"])
    return base_url + ("&" if "?" in base_url else "?") + urllib.parse.urlencode(params)


def run_local_web_search(
    query: str,
    *,
    max_results: int,
    timeout: int,
    search_url: str | None = None,
    options: dict[str, str | None] | None = None,
) -> tuple[list[str], list[str]]:
    diagnostics: list[str] = []
    seen: set[str] = set()
    combined: list[str] = []
    for base_url in local_search_url_candidates(search_url):
        url = local_search_request_url(base_url, query, options=options, max_results=max_results)
        try:
            body = fetch_text_url(url, timeout=timeout)
        except Exception as exc:
            diagnostics.append(f"{base_url}: request failed: {exc}")
            continue
        parsed = parse_local_search_results(body, max_results)
        if not parsed:
            diagnostics.append(f"{base_url}: no SearXNG JSON results parsed")
            continue
        for result in parsed:
            key = result_url_key(result)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            combined.append(f"[Local Search]\n{result}")
            if len(combined) >= max_results:
                return combined, diagnostics
    return combined, diagnostics


def parse_local_search_results(body: str, max_results: int) -> list[str]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    items = data.get("results") if isinstance(data, dict) else None
    if not isinstance(items, list):
        channels = data.get("channels") if isinstance(data, dict) else None
        if isinstance(channels, list) and channels and isinstance(channels[0], dict):
            items = channels[0].get("items")
    if not isinstance(items, list):
        return []
    results: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = clean_html(str(item.get("title") or item.get("pretty_url") or item.get("url") or "")).strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        snippet = clean_html(str(item.get("content") or item.get("snippet") or item.get("description") or "")).strip()
        if not title and not url:
            continue
        results.append(f"- {title or url}\n  {url}\n  {snippet}".strip())
        if len(results) >= max_results:
            break
    return results


def web_search_engine_order(options: dict[str, str | None] | None = None) -> list[str]:
    raw = string_or_none((options or {}).get("engines")) or string_or_none(os.getenv("DSTUL_SEARCH_ENGINES"))
    requested = raw.split(",") if raw else ["baidu", "bing", "duckduckgo"]
    aliases = {
        "bd": "baidu",
        "baidu": "baidu",
        "百度": "baidu",
        "bing": "bing",
        "必应": "bing",
        "ddg": "duckduckgo",
        "duck": "duckduckgo",
        "duckduckgo": "duckduckgo",
    }
    engines: list[str] = []
    for item in requested:
        normalized = aliases.get(item.strip().lower())
        if normalized and normalized not in engines:
            engines.append(normalized)
    return engines


def third_party_search_request_url(engine: str, query: str, options: dict[str, str | None] | None, max_results: int) -> str:
    language = string_or_none((options or {}).get("language")) or "zh-CN"
    if engine == "baidu":
        params = {"wd": query, "rn": str(max_results), "ie": "utf-8"}
        return "https://www.baidu.com/s?" + urllib.parse.urlencode(params)
    if engine == "bing":
        params = {"q": query, "count": str(max_results), "mkt": language, "setlang": language.split("-")[0]}
        return "https://www.bing.com/search?" + urllib.parse.urlencode(params)
    if engine == "duckduckgo":
        kl = "cn-zh" if language.lower().startswith("zh") else "us-en"
        params = {"q": query, "kl": kl}
        return "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode(params)
    raise ValueError(f"unsupported search engine: {engine}")


def run_web_search(
    query: str,
    *,
    max_results: int,
    timeout: int,
    options: dict[str, str | None] | None = None,
) -> tuple[list[str], list[str]]:
    diagnostics: list[str] = []
    seen: set[str] = set()
    combined: list[str] = []
    parsers = {
        "baidu": parse_baidu_results,
        "bing": parse_bing_results,
        "duckduckgo": parse_duckduckgo_results,
    }
    labels = {
        "baidu": "Baidu",
        "bing": "Bing",
        "duckduckgo": "DuckDuckGo",
    }
    engines = web_search_engine_order(options)
    if not engines:
        return [], ["no supported search engines requested; use baidu,bing,duckduckgo"]
    for engine in engines:
        url = third_party_search_request_url(engine, query, options, max_results)
        try:
            body = fetch_text_url(url, timeout=timeout)
        except Exception as exc:
            diagnostics.append(f"{engine}: request failed: {exc}")
            continue
        parsed = parsers[engine](body, max_results)
        if not parsed:
            diagnostics.append(f"{engine}: no parseable results")
            continue
        added = 0
        for result in parsed:
            key = result_url_key(result)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            combined.append(f"[{labels[engine]}]\n{result}")
            added += 1
            if len([item for item in combined if search_result_has_snippet(item)]) >= max_results:
                return prefer_snippet_results(combined, max_results), diagnostics
        if added == 0:
            diagnostics.append(f"{engine}: only duplicate results")
    return prefer_snippet_results(combined, max_results), diagnostics


def search_result_has_snippet(result: str) -> bool:
    for line in result.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("[") or stripped.startswith("- ") or stripped.startswith(("http://", "https://")):
            continue
        return True
    return False


def prefer_snippet_results(results: list[str], max_results: int) -> list[str]:
    with_snippets = [result for result in results if search_result_has_snippet(result)]
    without_snippets = [result for result in results if not search_result_has_snippet(result)]
    return (with_snippets + without_snippets)[:max_results]


def fetch_result_pages(results: list[str], *, timeout: int, fetch_pages: int, page_chars: int) -> list[str]:
    pages: list[str] = []
    seen: set[str] = set()
    for result in results:
        url = result_url_key(result)
        if not url or url in seen:
            continue
        seen.add(url)
        allowed, reason = can_fetch_url(url, timeout=timeout)
        if not allowed:
            pages.append(f"[Fetched Page]\n- skipped by robots.txt\n  {url}\n  {reason}")
            continue
        try:
            html = fetch_text_url(url, timeout=timeout, max_bytes=800_000)
        except Exception as exc:
            pages.append(f"[Fetched Page]\n- fetch failed\n  {url}\n  {exc}")
            continue
        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = clean_html(title_match.group(1)) if title_match else url
        text = clean_page_text(html)
        pages.append(f"[Fetched Page]\n- {title}\n  {url}\n  {text[:page_chars]}")
        if len(pages) >= fetch_pages:
            break
    return pages


def result_url_key(result: str) -> str:
    for line in result.splitlines():
        stripped = line.strip()
        if stripped.startswith(("http://", "https://")):
            return stripped
    return result


def fetch_direct_url(url: str, *, timeout: int) -> ToolResult:
    allowed, reason = can_fetch_url(url, timeout=timeout)
    if not allowed:
        return ToolResult(False, f"direct URL fetch skipped by robots.txt for {url}: {reason}")
    try:
        html = fetch_text_url(url, timeout=timeout)
    except Exception as exc:
        return ToolResult(False, f"direct URL fetch failed for {url}: {exc}")
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = clean_html(title_match.group(1)) if title_match else url
    text = clean_page_text(html)
    return ToolResult(True, f"[URL]\n- {title}\n  {url}\n  {text[:1800]}")


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg"}


def image_data_url(path: Path, media: str = "image/png") -> str:
    return f"data:{media};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def video_duration_seconds(path: Path, timeout: int = 8) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = run_hidden(
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


def extract_video_frame_data_urls(path: Path, *, max_frames: int = 6, timeout: int = 20) -> list[str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    duration = video_duration_seconds(path) or 0
    if duration > 0:
        count = max(1, min(max_frames, int(duration) if duration >= 1 else 1))
        timestamps = [duration * (i + 1) / (count + 1) for i in range(count)]
    else:
        timestamps = [0]
    frame_dir = path.parent / f".{path.stem}_inspect_frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frames: list[str] = []
    for index, timestamp in enumerate(timestamps, 1):
        out = frame_dir / f"frame_{index:02d}.jpg"
        try:
            completed = run_hidden(
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
        if completed.returncode == 0 and out.exists() and out.stat().st_size:
            frames.append(image_data_url(out, "image/jpeg"))
    return frames


def clean_page_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", html)
    return clean_html(html)


def parse_duckduckgo_results(html: str, max_results: int) -> list[str]:
    results: list[str] = []
    blocks = re.findall(r'<div class="result__body">(.*?)</div>\s*</div>', html, flags=re.DOTALL)
    if not blocks:
        blocks = re.findall(r'<a rel="nofollow" class="result__a".*?</a>.*?(?=<a rel="nofollow" class="result__a"|$)', html, flags=re.DOTALL)
    for block in blocks:
        title_match = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.DOTALL)
        if not title_match:
            continue
        href = normalize_duckduckgo_url(unescape(title_match.group(1)))
        title = clean_html(title_match.group(2))
        snippet_match = re.search(r'class="result__snippet"[^>]*>(.*?)</a>|class="result__snippet"[^>]*>(.*?)</div>', block, flags=re.DOTALL)
        snippet = clean_html(next((group for group in (snippet_match.groups() if snippet_match else []) if group), ""))
        results.append(f"- {title}\n  {href}\n  {snippet}".strip())
        if len(results) >= max_results:
            break
    return results


def parse_baidu_results(html: str, max_results: int) -> list[str]:
    results: list[str] = []
    h3_matches = list(re.finditer(r"(?is)<h3[^>]*>\s*<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>\s*</h3>", html))
    for index, match in enumerate(h3_matches):
        href = normalize_baidu_url(unescape(match.group(1)))
        title = clean_html(match.group(2))
        next_start = h3_matches[index + 1].start() if index + 1 < len(h3_matches) else min(len(html), match.end() + 3000)
        block = html[match.end() : next_start]
        snippet = ""
        for pattern in (
            r"(?is)<span[^>]*class=[\"'][^\"']*content-right_[^\"']*[\"'][^>]*>(.*?)</span>",
            r"(?is)<div[^>]*class=[\"'][^\"']*c-abstract[^\"']*[\"'][^>]*>(.*?)</div>",
            r"(?is)<span[^>]*class=[\"'][^\"']*c-abstract[^\"']*[\"'][^>]*>(.*?)</span>",
            r"(?is)<div[^>]*class=[\"'][^\"']*c-span-last[^\"']*[\"'][^>]*>(.*?)</div>",
            r"(?is)<span[^>]*class=[\"'][^\"']*c-color-text[^\"']*[\"'][^>]*>(.*?)</span>",
        ):
            snippet_match = re.search(pattern, block)
            if snippet_match:
                snippet = clean_html(snippet_match.group(1))
                break
        if not title and not href:
            continue
        results.append(f"- {title or href}\n  {href}\n  {snippet}".strip())
        if len(results) >= max_results:
            break
    return results


def parse_bing_results(html: str, max_results: int) -> list[str]:
    results: list[str] = []
    blocks = re.findall(r'<li class="b_algo".*?</li>', html, flags=re.DOTALL)
    for block in blocks:
        title_match = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>', block, flags=re.DOTALL)
        if not title_match:
            continue
        href = normalize_bing_url(unescape(title_match.group(1)))
        title = clean_html(title_match.group(2))
        snippet_match = re.search(r'<div class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', block, flags=re.DOTALL)
        snippet = clean_html(snippet_match.group(1)) if snippet_match else ""
        results.append(f"- {title}\n  {href}\n  {snippet}".strip())
        if len(results) >= max_results:
            break
    return results


def normalize_bing_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        query = urllib.parse.parse_qs(parsed.query)
        encoded = query.get("u", [""])[0]
        if encoded.startswith("a1"):
            try:
                import base64

                padded = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
                decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
                if decoded.startswith(("http://", "https://")):
                    return decoded
            except (ValueError, OSError):
                pass
    return url


def normalize_duckduckgo_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        query = urllib.parse.parse_qs(parsed.query)
        target = query.get("uddg", [""])[0]
        if target.startswith(("http://", "https://")):
            return target
    return url


def normalize_baidu_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.baidu.com" + url
    return url


def clean_html(text: str) -> str:
    text = re.sub(r"<.*?>", "", text, flags=re.DOTALL)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()
