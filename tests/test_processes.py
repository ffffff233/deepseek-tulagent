from __future__ import annotations

import subprocess
import sys

import pytest

from deepseekfathom._core import processes


class FakeStartupInfo:
    def __init__(self) -> None:
        self.dwFlags = 0
        self.wShowWindow = -1


def test_run_hidden_hides_windows_console_and_removes_conflicting_flags(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    monkeypatch.setattr(processes.sys, "platform", "win32")
    monkeypatch.setattr(processes.subprocess, "STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr(processes.subprocess, "STARTF_USESHOWWINDOW", 0x01, raising=False)
    monkeypatch.setattr(processes.subprocess, "SW_HIDE", 0, raising=False)
    monkeypatch.setattr(processes.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(processes.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(processes.subprocess, "DETACHED_PROCESS", 0x08, raising=False)
    monkeypatch.setattr(processes.subprocess, "run", fake_run)

    result = processes.run_hidden(["cmd.exe", "/c", "echo ok"], creationflags=0x418, text=True)

    assert result.returncode == 0
    kwargs = captured["kwargs"]
    assert kwargs["creationflags"] == 0x08000400
    assert kwargs["startupinfo"].dwFlags & 0x01
    assert kwargs["startupinfo"].wShowWindow == 0
    assert kwargs["text"] is True


def test_run_hidden_leaves_non_windows_process_options_unchanged(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(processes.sys, "platform", "linux")
    monkeypatch.setattr(processes.subprocess, "run", fake_run)

    processes.run_hidden(["sh", "-c", "true"], text=True)

    assert captured == {"text": True}


def test_popen_hidden_uses_the_same_windows_options(monkeypatch):
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 42

    def fake_popen(*_args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(processes.sys, "platform", "win32")
    monkeypatch.setattr(processes.subprocess, "STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr(processes.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(processes.subprocess, "Popen", fake_popen)

    process = processes.popen_hidden(["cmd.exe", "/c", "server"])

    assert process.pid == 42
    assert captured["creationflags"] & 0x08000000
    assert isinstance(captured["startupinfo"], FakeStartupInfo)


@pytest.mark.skipif(sys.platform != "win32", reason="requires the Windows console API")
def test_run_hidden_child_has_no_console_window():
    completed = processes.run_hidden(
        [
            sys.executable,
            "-c",
            "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == "0"
