from __future__ import annotations

from pathlib import Path
import re
import tomllib
from concurrent.futures import ThreadPoolExecutor

import pytest

from deepseekfathom._core.agent import SYSTEM_PROMPT
import deepseekfathom._core.config as config_module
from deepseekfathom._core.config import DATA_DIRNAME, config_home, get_settings, migrate_legacy_data
from deepseekfathom._core.session import SessionStore


ROOT = Path(__file__).parents[1]


def test_public_distribution_and_docs_use_deepseekfathom_only() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "deepseekfathom"
    assert set(project["scripts"]) == {"deepseekfathom", "deepseekfathom-desktop"}
    assert all(str(value).startswith("deepseekfathom.") for value in project["scripts"].values())
    assert "DeepSeekFathom" in SYSTEM_PROMPT
    assert "TuL" not in SYSTEM_PROMPT
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"deepseek[-_ ]?tul|\bdstul\b", text, re.I)
        assert ".deepseekfathom" in text


def test_legacy_data_migration_only_fills_missing_files(tmp_path: Path) -> None:
    legacy = tmp_path / (".deepseek-" + "tulagent")
    target = tmp_path / DATA_DIRNAME
    (legacy / "sessions").mkdir(parents=True)
    (legacy / "sessions" / "old.jsonl").write_text("old session\n", encoding="utf-8")
    (legacy / "config.json").write_text('{"api_key":"legacy"}\n', encoding="utf-8")
    target.mkdir()
    (target / "config.json").write_text('{"api_key":"current"}\n', encoding="utf-8")

    migrate_legacy_data(legacy, target)

    assert (target / "sessions" / "old.jsonl").read_text(encoding="utf-8") == "old session\n"
    assert (target / "config.json").read_text(encoding="utf-8") == '{"api_key":"current"}\n'


def test_settings_migrate_workspace_data_to_new_directory(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    legacy_sessions = workspace / (".deepseek-" + "tulagent") / "sessions"
    legacy_sessions.mkdir(parents=True)
    (legacy_sessions / "kept.jsonl").write_text("conversation\n", encoding="utf-8")
    config = tmp_path / "config"
    monkeypatch.delenv("DSTUL_CONFIG_HOME", raising=False)
    monkeypatch.delenv("DSTUL_WORKSPACE", raising=False)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(config))
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(workspace))

    settings = get_settings()

    assert config_home() == config.resolve()
    assert settings.sessions_dir == workspace.resolve() / DATA_DIRNAME / "sessions"
    assert (settings.sessions_dir / "kept.jsonl").read_text(encoding="utf-8") == "conversation\n"
    assert SessionStore(settings.workspace).sessions_dir == settings.sessions_dir


def test_interrupted_migration_never_publishes_a_partial_file(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    target = tmp_path / "current"
    source.mkdir()
    payload = "complete conversation\n" * 100
    (source / "session.jsonl").write_text(payload, encoding="utf-8")
    real_copy = config_module.shutil.copy2

    def interrupted_copy(_source, destination):
        Path(destination).write_text("partial", encoding="utf-8")
        raise OSError("interrupted")

    monkeypatch.setattr(config_module.shutil, "copy2", interrupted_copy)
    with pytest.warns(RuntimeWarning, match="retry"):
        migrate_legacy_data(source, target)
    assert not (target / "session.jsonl").exists()

    monkeypatch.setattr(config_module.shutil, "copy2", real_copy)
    migrate_legacy_data(source, target)
    assert (target / "session.jsonl").read_text(encoding="utf-8") == payload


def test_concurrent_migration_never_overwrites_published_data(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    target = tmp_path / "current"
    source.mkdir()
    payload = b"session-data" * 10_000
    (source / "session.jsonl").write_bytes(payload)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _index: migrate_legacy_data(source, target), range(8)))

    assert (target / "session.jsonl").read_bytes() == payload
    assert not list(target.glob("*.migrate-*"))


def test_legacy_config_environment_is_migrated_to_new_home(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    legacy_config = tmp_path / "custom-config"
    legacy_config.mkdir()
    (legacy_config / "config.json").write_text('{"model":"legacy-model"}\n', encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(legacy_config))
    monkeypatch.delenv("DEEPSEEKFATHOM_CONFIG_HOME", raising=False)

    resolved = config_home()

    assert resolved == (home / DATA_DIRNAME).resolve()
    assert (resolved / "config.json").read_text(encoding="utf-8") == '{"model":"legacy-model"}\n'
