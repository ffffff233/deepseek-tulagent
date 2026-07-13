from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

from .processes import run_hidden


REPO = "ffffff233/DeepSeekFathom"
REPO_URL = f"https://github.com/{REPO}"


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str
    url: str


def normalize_version(version: str) -> str:
    return version.strip().removeprefix("v")


def version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in normalize_version(version).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits or "0"))
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    return version_tuple(latest) > version_tuple(current)


def fetch_latest_tag(timeout: float = 2.0) -> str | None:
    url = f"https://api.github.com/repos/{REPO}/tags"
    request = urllib.request.Request(url, headers={"User-Agent": "DeepSeekFathom"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read(200_000).decode("utf-8", errors="replace"))
    if not isinstance(data, list):
        return None
    versions = [item.get("name") for item in data if isinstance(item, dict) and isinstance(item.get("name"), str)]
    versions = [name for name in versions if name.startswith("v")]
    if not versions:
        return None
    return sorted(versions, key=version_tuple, reverse=True)[0]


def check_for_update(current: str, timeout: float = 2.0) -> UpdateInfo | None:
    latest = fetch_latest_tag(timeout=timeout)
    if latest and is_newer(latest, current):
        return UpdateInfo(current=current, latest=normalize_version(latest), url=f"{REPO_URL}/releases/tag/v{normalize_version(latest)}")
    return None


def source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def update_to(version: str, timeout: int = 180) -> tuple[bool, str]:
    target = "v" + normalize_version(version)
    root = source_root()
    if (root / ".git").exists():
        status = run_hidden(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True, timeout=30)
        if status.returncode != 0:
            return False, status.stderr.strip() or "could not inspect git status"
        if status.stdout.strip():
            return (
                False,
                "local source changes detected; update stopped to avoid overwriting user edits. "
                "Commit, stash, or back up your changes, then run deepseekfathom update again. "
                "User config and skills are never touched by the updater.",
            )
        commands = [
            ["git", "fetch", "--tags", "origin"],
            ["git", "checkout", target],
        ]
        output: list[str] = []
        for command in commands:
            completed = run_hidden(command, cwd=root, text=True, capture_output=True, timeout=timeout)
            output.append(completed.stdout + completed.stderr)
            if completed.returncode != 0:
                pip_ok, pip_output = pip_install_archive(target, timeout=timeout)
                detail = "".join(output).strip()
                if pip_ok:
                    return True, (
                        f"git update failed, but tarball fallback succeeded.\n{pip_output}\n"
                        "If git failed because of proxy syntax, configure HTTP_PROXY/HTTPS_PROXY or fix git config http.proxy."
                    )
                return False, (
                    f"{detail}\n\n"
                    f"tarball fallback also failed:\n{pip_output}\n\n"
                    "Proxy tips: use HTTP_PROXY/HTTPS_PROXY for pip/tarball fallback, or configure git with "
                    "`git config --global http.proxy http://127.0.0.1:PORT`."
                ).strip()
        return True, f"updated source tree to {target}. Restart deepseekfathom."

    return pip_install_archive(target, timeout=timeout)


def pip_install_archive(target: str, timeout: int = 180) -> tuple[bool, str]:
    archive_url = f"{REPO_URL}/archive/refs/tags/{target}.tar.gz"
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        archive_url,
    ]
    env = os.environ.copy()
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    completed = run_hidden(command, text=True, capture_output=True, timeout=timeout, env=env)
    output = completed.stdout + completed.stderr
    return completed.returncode == 0, (output.strip() or f"updated to {target}. Restart deepseekfathom.")
