from __future__ import annotations

import subprocess
import sys
from typing import Any


def _hidden_process_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Hide child console windows when the GUI build launches a process."""
    if sys.platform != "win32":
        return kwargs

    hidden = dict(kwargs)
    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    create_new_console = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010))
    detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    flags = int(hidden.get("creationflags", 0) or 0)
    # Windows ignores CREATE_NO_WINDOW when either conflicting console flag is set.
    flags &= ~(create_new_console | detached_process)
    hidden["creationflags"] = flags | create_no_window

    startupinfo = hidden.get("startupinfo")
    if startupinfo is None:
        startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_factory is not None:
            startupinfo = startupinfo_factory()
    if startupinfo is not None:
        startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001))
        startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        hidden["startupinfo"] = startupinfo
    return hidden


def run_hidden(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(*args, **_hidden_process_kwargs(kwargs))


def popen_hidden(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
    return subprocess.Popen(*args, **_hidden_process_kwargs(kwargs))
