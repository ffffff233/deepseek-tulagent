from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
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
import zipfile

from .policy import ApprovalPolicy


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str

    def to_message(self) -> str:
        return json.dumps({"ok": self.ok, "output": self.output}, ensure_ascii=False)


ToolHandler = Callable[[dict[str, Any]], ToolResult]


TOOL_DESCRIPTIONS = {
    "list_files": "read: list files in the workspace",
    "search_text": "read: search text in workspace files",
    "git_status": "read: show git short status",
    "read_file": "read: read UTF-8 text from a workspace file",
    "write_file": "gated write: create or overwrite a workspace file",
    "run_shell": "gated shell: run a shell command in the workspace",
    "apply_patch": "gated write: apply a unified diff with git apply",
    "download_url": "gated network+write: download URL into workspace",
    "clone_repo": "gated network+write: clone a Git/GitHub repository with mirror and archive fallbacks",
    "web_search": "network: query the configured local search engine and return result snippets",
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
        self._tools: dict[str, ToolHandler] = {
            "list_files": self.list_files,
            "search_text": self.search_text,
            "git_status": self.git_status,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "run_shell": self.run_shell,
            "apply_patch": self.apply_patch,
            "download_url": self.download_url,
            "clone_repo": self.clone_repo,
            "web_search": self.web_search,
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
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    def read_file(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.resolve_workspace_path(require_str(arguments, "path"))
        max_bytes = int(arguments.get("max_bytes", 20000))
        data = path.read_bytes()[:max_bytes]
        return ToolResult(True, data.decode("utf-8", errors="replace"))

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
                rel = str(path.relative_to(base))
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
                completed = subprocess.run(command, cwd=self.workspace, text=True, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return ToolResult(False, f"search timed out after {timeout}s")
            lines = completed.stdout.splitlines()[:max_matches]
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
        completed = subprocess.run(
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
        path = self.resolve_workspace_path(require_str(arguments, "path"))
        content = require_str(arguments, "content")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        return ToolResult(True, f"Wrote {self._display_path(path)}")

    def run_shell(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.allow_shell:
            raise ToolError("run_shell is disabled in this mode")
        command = require_str(arguments, "command")
        if is_background_command(command):
            return self.start_service({"name": arguments.get("name", "shell-bg"), "command": strip_background(command)})
        completed = subprocess.run(
            command,
            cwd=self.workspace,
            shell=True,
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
        completed = subprocess.run(
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
        return ToolResult(completed.returncode == 0, output.strip() or "patch applied")

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
        search_url = string_or_none(arguments.get("search_url") or arguments.get("engine_url") or arguments.get("base_url"))
        options = {
            "language": string_or_none(arguments.get("language")) or "zh-CN",
            "categories": string_or_none(arguments.get("categories")),
            "time_range": string_or_none(arguments.get("time_range") or arguments.get("timeRange")),
        }
        results, diagnostics = run_local_web_search(query, max_results=max_results, timeout=timeout, search_url=search_url, options=options)
        if not results:
            detail = "\n".join(diagnostics) if diagnostics else "no search sources attempted"
            return ToolResult(
                False,
                "local search engine unavailable or returned no results.\n"
                f"query: {query}\n"
                "Start a local SearXNG-compatible engine and set DSTUL_SEARCH_URL, "
                "or pass search_url, e.g. http://127.0.0.1:8080/search\n"
                f"{detail}",
            )
        fetch_pages = int(arguments.get("fetch_pages", arguments.get("fetchPages", 0)) or 0)
        if fetch_pages > 0:
            page_limit = int(arguments.get("page_chars", arguments.get("pageChars", 1200)) or 1200)
            results.extend(fetch_result_pages(results, timeout=timeout, fetch_pages=min(fetch_pages, 5), page_chars=max(200, min(page_limit, 4000))))
        return ToolResult(True, "\n\n".join(results))

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
        log = log_path.open("ab")
        process = subprocess.Popen(command, cwd=self.workspace, shell=True, stdout=log, stderr=subprocess.STDOUT)
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
        subprocess.run(["kill", str(pid)], capture_output=True, text=True)
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


def should_skip(path: Path) -> bool:
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "target", "dist", "build"}
    return any(part in ignored for part in path.parts)


def safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in name)[:80] or "service"


def process_alive(pid_path: Path) -> bool:
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0
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
        completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout)
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
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 DeepSeekTuL/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(max_bytes).decode("utf-8", errors="replace")


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


def fetch_result_pages(results: list[str], *, timeout: int, fetch_pages: int, page_chars: int) -> list[str]:
    pages: list[str] = []
    seen: set[str] = set()
    for result in results:
        url = result_url_key(result)
        if not url or url in seen:
            continue
        seen.add(url)
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
    try:
        html = fetch_text_url(url, timeout=timeout)
    except Exception as exc:
        return ToolResult(False, f"direct URL fetch failed for {url}: {exc}")
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = clean_html(title_match.group(1)) if title_match else url
    text = clean_page_text(html)
    return ToolResult(True, f"[URL]\n- {title}\n  {url}\n  {text[:1800]}")


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


def clean_html(text: str) -> str:
    text = re.sub(r"<.*?>", "", text, flags=re.DOTALL)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()
