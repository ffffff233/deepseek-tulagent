from __future__ import annotations

from email.message import Message
from pathlib import Path
import os
import subprocess
import sys
import venv
import zipfile

import pytest

from deepseekfathom._core import updates


PROJECT_ROOT = Path(__file__).parents[1]


def _wheel(
    output: Path,
    distribution_name: str,
    version: str,
    package_files: dict[str, str],
    console_scripts: dict[str, str],
) -> Path:
    normalized = distribution_name.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    members = dict(package_files)
    members[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.1\n"
        f"Name: {distribution_name}\n"
        f"Version: {version}\n"
    )
    members[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: DeepSeekFathom migration test\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )
    members[f"{dist_info}/entry_points.txt"] = "[console_scripts]\n" + "".join(
        f"{name} = {target}\n" for name, target in console_scripts.items()
    )
    record_path = f"{dist_info}/RECORD"
    members[record_path] = "".join(f"{name},,\n" for name in (*members, record_path))

    wheel_path = output / f"{normalized}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return wheel_path


def _run(command: list[str | Path], **kwargs) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(part) for part in command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        **kwargs,
    )


def test_legacy_distribution_cleanup_preserves_new_launchers_in_temp_venv(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    legacy_wheel = _wheel(
        wheels,
        updates.LEGACY_DISTRIBUTION,
        updates.LEGACY_DISTRIBUTION_VERSION,
        {
            "deepseek_tulagent/__init__.py": "",
            "deepseek_tulagent/cli.py": "def main():\n    print('legacy-cli')\n",
            "deepseek_tulagent/desktop/__init__.py": "def main():\n    print('legacy-desktop')\n",
            "deepseek_tulagent/desktop/app.py": "def main():\n    print('legacy-desktop')\n",
        },
        updates.LEGACY_CONSOLE_SCRIPTS,
    )
    current_wheel = _wheel(
        wheels,
        updates.CURRENT_DISTRIBUTION,
        "9.8.7",
        {
            "deepseekfathom/__init__.py": "",
            "deepseekfathom/cli.py": "def main():\n    print('current-cli')\n",
            "deepseekfathom/desktop.py": "def main():\n    print('current-desktop')\n",
            "deepseekfathom/_core/__init__.py": "",
            "deepseekfathom/_core/processes.py": (
                PROJECT_ROOT / "src" / "deepseekfathom" / "_core" / "processes.py"
            ).read_text(encoding="utf-8"),
            "deepseekfathom/_core/updates.py": (
                PROJECT_ROOT / "src" / "deepseekfathom" / "_core" / "updates.py"
            ).read_text(encoding="utf-8"),
        },
        updates.CURRENT_CONSOLE_SCRIPTS,
    )

    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=True).create(environment)
    scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    _run([python, "-m", "pip", "install", "--no-index", "--no-deps", legacy_wheel])
    _run([python, "-m", "pip", "install", "--no-index", "--no-deps", current_wheel])

    cli_launcher = scripts / updates._launcher_filename("deepseekfathom")
    desktop_launcher = scripts / updates._launcher_filename("deepseekfathom-desktop")
    assert _run([cli_launcher]).stdout.strip() == "current-cli"
    assert _run([desktop_launcher]).stdout.strip() == "current-desktop"
    _run(
        [
            python,
            "-m",
            "deepseekfathom._core.updates",
            "--cleanup-legacy-distribution",
            "--parent-pid",
            "0",
        ],
        cwd=environment,
    )

    assert _run([cli_launcher]).stdout.strip() == "current-cli"
    assert _run([desktop_launcher]).stdout.strip() == "current-desktop"
    for alias in updates.LEGACY_ALIAS_NAMES:
        assert not (scripts / updates._launcher_filename(alias)).exists()
    site_packages = Path(
        _run([python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).stdout.strip()
    )
    assert not (site_packages / "deepseek_tulagent").exists()
    installed = _run(
        [
            python,
            "-c",
            "from importlib import metadata; print(metadata.version('deepseekfathom')); "
            "print('legacy-present' if any(d.metadata.get('Name') == 'deepseek-tulagent' "
            "for d in metadata.distributions()) else 'legacy-absent')",
        ]
    ).stdout.splitlines()
    assert installed == ["9.8.7", "legacy-absent"]


@pytest.mark.parametrize(
    ("name", "version"),
    [("deepseek-tulagent-extra", "0.1.108"), ("deepseek-tulagent", "0.1.107")],
)
def test_migration_rejects_non_exact_legacy_distribution(monkeypatch, name: str, version: str) -> None:
    class FakeDistribution:
        def __init__(self, distribution_name: str, distribution_version: str):
            self.metadata = Message()
            self.metadata["Name"] = distribution_name
            self.version = distribution_version

    current = FakeDistribution(updates.CURRENT_DISTRIBUTION, "1.0")
    legacy = FakeDistribution(name, version)
    monkeypatch.setattr(
        updates,
        "_distribution",
        lambda requested: current if requested == updates.CURRENT_DISTRIBUTION else legacy,
    )

    assert updates._legacy_distribution_plan() is None


def test_cleanup_helper_waits_for_launcher_before_migrating(monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(updates, "_wait_for_windows_process", lambda process_id: events.append(("wait", process_id)))
    monkeypatch.setattr(updates, "cleanup_legacy_distribution", lambda: events.append("cleanup") or True)

    assert updates._helper_main(["--cleanup-legacy-distribution", "--parent-pid", "321"]) == 0
    assert events == [("wait", 321), "cleanup"]


def test_cli_startup_schedules_legacy_cleanup(monkeypatch, capsys) -> None:
    import deepseekfathom._core.cli as cli

    scheduled: list[bool] = []
    monkeypatch.setattr(cli, "schedule_legacy_distribution_cleanup", lambda: scheduled.append(True) or True)
    monkeypatch.setattr(cli, "install_terminal_safety", lambda: None)
    monkeypatch.setattr(cli, "get_settings", lambda: object())

    assert cli.main(["version"]) == 0
    assert scheduled == [True]
    assert capsys.readouterr().out.strip()


def test_posix_startup_runs_validated_cleanup_without_spawning_helper(monkeypatch) -> None:
    cleaned: list[bool] = []
    monkeypatch.setattr(updates.sys, "platform", "linux")
    monkeypatch.delattr(updates.sys, "frozen", raising=False)
    monkeypatch.delenv(updates._CLEANUP_HELPER_ENV, raising=False)
    monkeypatch.setattr(updates, "_legacy_distribution_plan", lambda: object())
    monkeypatch.setattr(updates, "cleanup_legacy_distribution", lambda: cleaned.append(True) or True)
    monkeypatch.setattr(
        updates,
        "popen_hidden",
        lambda *args, **kwargs: pytest.fail("POSIX cleanup must not spawn the Windows helper"),
    )

    assert updates.schedule_legacy_distribution_cleanup() is True
    assert cleaned == [True]
    assert updates._launcher_filename("deepseekfathom") == "deepseekfathom"


def test_frozen_process_never_schedules_distribution_cleanup(monkeypatch) -> None:
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        updates,
        "_legacy_distribution_plan",
        lambda: pytest.fail("frozen builds must not inspect pip distributions"),
    )

    assert updates.schedule_legacy_distribution_cleanup() is False
    assert updates.cleanup_legacy_distribution() is False
