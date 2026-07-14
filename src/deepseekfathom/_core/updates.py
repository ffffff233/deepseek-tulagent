from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from typing import Any
import urllib.request

from .processes import popen_hidden, run_hidden


REPO = "ffffff233/DeepSeekFathom"
REPO_URL = f"https://github.com/{REPO}"
CURRENT_DISTRIBUTION = "deepseekfathom"
LEGACY_DISTRIBUTION = "deepseek-tulagent"
LEGACY_DISTRIBUTION_VERSION = "0.1.108"
CURRENT_CONSOLE_SCRIPTS = {
    "deepseekfathom": "deepseekfathom.cli:main",
    "deepseekfathom-desktop": "deepseekfathom.desktop:main",
}
LEGACY_CONSOLE_SCRIPTS = {
    "deepseek-tulagent": "deepseek_tulagent.cli:main",
    "deepseekTul": "deepseek_tulagent.cli:main",
    "deepseekTulDesktop": "deepseek_tulagent.desktop.app:main",
    "deepseekfathom": "deepseek_tulagent.cli:main",
    "deepseekfathom-desktop": "deepseek_tulagent.desktop.app:main",
    "dstul": "deepseek_tulagent.cli:main",
}
LEGACY_ALIAS_NAMES = frozenset(LEGACY_CONSOLE_SCRIPTS) - frozenset(CURRENT_CONSOLE_SCRIPTS)
_CLEANUP_HELPER_ENV = "DEEPSEEKFATHOM_LEGACY_CLEANUP_HELPER"


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str
    url: str


@dataclass(frozen=True)
class _LegacyDistributionPlan:
    environment_root: Path
    scripts_dir: Path
    legacy_package: Path
    legacy_metadata: Path
    legacy_aliases: tuple[Path, ...]
    current_launchers: tuple[tuple[str, str, Path], ...]


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


def _distribution(name: str) -> Any | None:
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None


def _metadata_name(distribution: Any) -> str:
    return str(distribution.metadata.get("Name") or "").strip()


def _console_scripts(distribution: Any) -> dict[str, str] | None:
    entries = [entry for entry in distribution.entry_points if entry.group == "console_scripts"]
    scripts = {entry.name: entry.value for entry in entries}
    if len(scripts) != len(entries):
        return None
    return scripts


def _launcher_filename(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _recorded_launcher_paths(distribution: Any, names: set[str]) -> dict[str, Path] | None:
    wanted = {_launcher_filename(name): name for name in names}
    if sys.platform == "win32":
        wanted = {filename.casefold(): name for filename, name in wanted.items()}
    found: dict[str, Path] = {}
    for item in distribution.files or ():
        filename = Path(str(item)).name
        name = wanted.get(filename.casefold() if sys.platform == "win32" else filename)
        if name is None:
            continue
        target = Path(distribution.locate_file(item)).resolve()
        if name in found and found[name] != target:
            return None
        found[name] = target
    return found if set(found) == names else None


def _recorded_metadata_path(distribution: Any) -> Path | None:
    candidates: set[Path] = set()
    for item in distribution.files or ():
        relative = Path(str(item))
        if relative.name != "METADATA" or not relative.parent.name.casefold().endswith(".dist-info"):
            continue
        candidates.add(Path(distribution.locate_file(relative.parent)).resolve())
    if len(candidates) != 1:
        return None
    return candidates.pop()


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _legacy_distribution_plan() -> _LegacyDistributionPlan | None:
    current = _distribution(CURRENT_DISTRIBUTION)
    legacy = _distribution(LEGACY_DISTRIBUTION)
    if current is None or legacy is None:
        return None
    if _metadata_name(current) != CURRENT_DISTRIBUTION:
        return None
    if _metadata_name(legacy) != LEGACY_DISTRIBUTION or str(legacy.version) != LEGACY_DISTRIBUTION_VERSION:
        return None
    if _console_scripts(current) != CURRENT_CONSOLE_SCRIPTS:
        return None
    if _console_scripts(legacy) != LEGACY_CONSOLE_SCRIPTS:
        return None

    current_root = Path(current.locate_file("")).resolve()
    legacy_root = Path(legacy.locate_file("")).resolve()
    if current_root != legacy_root:
        return None
    runtime_package = Path(__file__).resolve().parents[1]
    if Path(current.locate_file(CURRENT_DISTRIBUTION)).resolve() != runtime_package:
        return None

    current_launchers = _recorded_launcher_paths(current, set(CURRENT_CONSOLE_SCRIPTS))
    legacy_launchers = _recorded_launcher_paths(legacy, set(LEGACY_CONSOLE_SCRIPTS))
    legacy_metadata = _recorded_metadata_path(legacy)
    if current_launchers is None or legacy_launchers is None or legacy_metadata is None:
        return None
    scripts_dirs = {path.parent for path in (*current_launchers.values(), *legacy_launchers.values())}
    if len(scripts_dirs) != 1:
        return None
    scripts_dir = scripts_dirs.pop()
    interpreter_scripts = Path(sysconfig.get_path("scripts")).resolve()
    interpreter_paths = {Path(item).resolve() for item in sys.path if item}
    if scripts_dir != interpreter_scripts or current_root not in interpreter_paths:
        return None

    expected_metadata_name = "deepseek_tulagent-0.1.108.dist-info"
    legacy_package = (legacy_root / "deepseek_tulagent").resolve()
    actual_metadata_name = legacy_metadata.name.casefold() if sys.platform == "win32" else legacy_metadata.name
    if legacy_metadata.parent != legacy_root or actual_metadata_name != expected_metadata_name:
        return None
    if legacy_package.parent != legacy_root or scripts_dir == legacy_root:
        return None

    allowed_launchers = set(legacy_launchers.values())
    for item in legacy.files or ():
        target = Path(legacy.locate_file(item)).resolve()
        if _path_is_within(target, legacy_package) or _path_is_within(target, legacy_metadata):
            continue
        if target not in allowed_launchers:
            return None

    aliases = tuple(legacy_launchers[name] for name in sorted(LEGACY_ALIAS_NAMES, key=str.casefold))
    launchers = tuple(
        (name, target, current_launchers[name])
        for name, target in CURRENT_CONSOLE_SCRIPTS.items()
    )
    return _LegacyDistributionPlan(
        environment_root=current_root,
        scripts_dir=scripts_dir,
        legacy_package=legacy_package,
        legacy_metadata=legacy_metadata,
        legacy_aliases=aliases,
        current_launchers=launchers,
    )


def _launcher_matches(path: Path, target: str) -> bool:
    module, separator, function = target.partition(":")
    if not separator or not module or not function:
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) > 2_000_000:
        return False
    return (
        target.encode("utf-8") in data
        or f"from {module} import {function}".encode("utf-8") in data
    ) and b"deepseek_tulagent" not in data


def _generate_current_launchers(staging_dir: Path) -> None:
    from pip._vendor.distlib.scripts import ScriptMaker

    maker = ScriptMaker(None, str(staging_dir))
    maker.clobber = True
    maker.variants = {""}
    maker.set_mode = True
    maker.executable = sys.executable
    for name, target in CURRENT_CONSOLE_SCRIPTS.items():
        maker.make(f"{name} = {target}")


def _stage_current_launchers(plan: _LegacyDistributionPlan) -> tuple[Path, dict[Path, Path]] | None:
    try:
        staging_dir = Path(tempfile.mkdtemp(prefix=".deepseekfathom-migration-", dir=plan.scripts_dir))
    except OSError:
        return None

    try:
        try:
            _generate_current_launchers(staging_dir)
        except Exception:
            for name, target, installed_path in plan.current_launchers:
                if not _launcher_matches(installed_path, target):
                    raise
                shutil.copy2(installed_path, staging_dir / _launcher_filename(name))

        staged: dict[Path, Path] = {}
        for name, target, installed_path in plan.current_launchers:
            candidate = staging_dir / _launcher_filename(name)
            if not _launcher_matches(candidate, target):
                raise RuntimeError(f"could not stage {name}")
            staged[installed_path] = candidate
        return staging_dir, staged
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return None


def _remove_legacy_tree(path: Path, environment_root: Path) -> None:
    if path.parent != environment_root:
        raise RuntimeError("legacy distribution path escaped its Python environment")
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def _restore_current_launchers(plan: _LegacyDistributionPlan, staged: dict[Path, Path]) -> bool:
    restored = True
    targets = {path: target for _, target, path in plan.current_launchers}
    for installed_path, staged_path in staged.items():
        try:
            os.replace(staged_path, installed_path)
        except OSError:
            if not _launcher_matches(installed_path, targets[installed_path]):
                restored = False
    return restored and all(_launcher_matches(path, target) for _, target, path in plan.current_launchers)


def _perform_legacy_distribution_cleanup(plan: _LegacyDistributionPlan) -> bool:
    staged_result = _stage_current_launchers(plan)
    if staged_result is None:
        return False
    staging_dir, staged = staged_result
    cleaned = False
    launchers_ok = False
    try:
        try:
            for alias in plan.legacy_aliases:
                if alias.parent != plan.scripts_dir:
                    raise RuntimeError("legacy launcher path escaped the scripts directory")
                alias.unlink(missing_ok=True)
            _remove_legacy_tree(plan.legacy_package, plan.environment_root)
            _remove_legacy_tree(plan.legacy_metadata, plan.environment_root)
            cleaned = not plan.legacy_package.exists() and not plan.legacy_metadata.exists()
            cleaned = cleaned and not any(alias.exists() for alias in plan.legacy_aliases)
        except OSError:
            cleaned = False
        finally:
            launchers_ok = _restore_current_launchers(plan, staged)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    return cleaned and launchers_ok


def cleanup_legacy_distribution() -> bool:
    if bool(getattr(sys, "frozen", False)):
        return False
    try:
        plan = _legacy_distribution_plan()
        return bool(plan and _perform_legacy_distribution_cleanup(plan))
    except Exception:
        return False


def _wait_for_windows_process(process_id: int) -> None:
    if sys.platform != "win32" or process_id <= 0:
        return
    import ctypes

    synchronize = 0x00100000
    infinite = 0xFFFFFFFF
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(synchronize, False, process_id)
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, infinite)
    finally:
        kernel32.CloseHandle(handle)


def schedule_legacy_distribution_cleanup() -> bool:
    if (
        bool(getattr(sys, "frozen", False))
        or os.environ.get(_CLEANUP_HELPER_ENV) == "1"
    ):
        return False
    try:
        if _legacy_distribution_plan() is None:
            return False
        if sys.platform != "win32":
            return cleanup_legacy_distribution()
        executable = Path(sys.executable)
        pythonw = executable.with_name("pythonw.exe")
        helper_python = pythonw if pythonw.is_file() else executable
        env = os.environ.copy()
        env[_CLEANUP_HELPER_ENV] = "1"
        popen_hidden(
            [
                str(helper_python),
                "-m",
                "deepseekfathom._core.updates",
                "--cleanup-legacy-distribution",
                "--parent-pid",
                str(os.getpid()),
            ],
            cwd=executable.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True
    except Exception:
        return False


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
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path(__file__).resolve().parents[3]


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


def _helper_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cleanup-legacy-distribution", action="store_true")
    parser.add_argument("--parent-pid", type=int, default=0)
    args, unknown = parser.parse_known_args(argv)
    if unknown or not args.cleanup_legacy_distribution:
        return 2
    _wait_for_windows_process(args.parent_pid)
    cleanup_legacy_distribution()
    return 0


if __name__ == "__main__":
    raise SystemExit(_helper_main())
