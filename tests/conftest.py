from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_user_home(monkeypatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep compatibility-path discovery away from the developer's real data."""

    home = tmp_path_factory.mktemp("isolated-user-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
