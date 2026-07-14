from __future__ import annotations

import json
import os
from pathlib import Path
import time

import deepseekfathom._core.config as config_module
import pytest
from deepseekfathom._core.config import DATA_DIRNAME, LEGACY_DATA_DIRNAME
from deepseekfathom._core.messages import Message
from deepseekfathom._core.session import SessionStore


def _write_session(path: Path, session_id: str, *contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "session_id": session_id,
            "created_at": "2026-07-14T00:00:00+00:00",
            "message": {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": content,
            },
        }
        for index, content in enumerate(contents)
    ]
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


def _session_path(root: Path, data_dir: str, session_id: str) -> Path:
    return root / data_dir / "sessions" / f"{session_id}.jsonl"


def test_current_config_values_win_and_legacy_configs_fill_missing_keys(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    current_home = tmp_path / "current-config"
    explicit_legacy_home = tmp_path / "explicit-legacy"
    default_legacy_home = home / LEGACY_DATA_DIRNAME
    current_home.mkdir()
    explicit_legacy_home.mkdir()
    default_legacy_home.mkdir(parents=True)

    current_text = '{"model":"current-model","base_url":""}\n'
    (current_home / "config.json").write_text(current_text, encoding="utf-8")
    (explicit_legacy_home / "config.json").write_text(
        '{"model":"explicit-legacy","api_key":"explicit-key","provider_format":"openai"}\n',
        encoding="utf-8",
    )
    (default_legacy_home / "config.json").write_text(
        '{"model":"default-legacy","api_key":"default-key","max_tokens":4096}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(current_home))
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(explicit_legacy_home))

    loaded = config_module.load_file_config()

    assert loaded == {
        "model": "current-model",
        "base_url": "",
        "api_key": "explicit-key",
        "provider_format": "openai",
    }
    assert (current_home / "config.json").read_text(encoding="utf-8") == current_text


def test_explicit_current_config_home_does_not_inherit_default_home(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    current_home = tmp_path / "isolated-current"
    default_legacy_home = home / LEGACY_DATA_DIRNAME
    current_home.mkdir()
    default_legacy_home.mkdir(parents=True)
    current_text = '{"model":"isolated-model"}\n'
    (current_home / "config.json").write_text(current_text, encoding="utf-8")
    (default_legacy_home / "config.json").write_text(
        '{"api_key":"unrelated-key","model":"unrelated-model"}\n',
        encoding="utf-8",
    )
    (default_legacy_home / "sessions").mkdir()
    (default_legacy_home / "sessions" / "unrelated.jsonl").write_text(
        "unrelated\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(current_home))
    monkeypatch.delenv("DSTUL_CONFIG_HOME", raising=False)

    assert config_module.load_file_config() == {"model": "isolated-model"}
    assert (current_home / "config.json").read_text(encoding="utf-8") == current_text
    assert not (current_home / "sessions").exists()


def test_default_current_home_still_merges_default_legacy_config(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    current_home = home / DATA_DIRNAME
    legacy_home = home / LEGACY_DATA_DIRNAME
    current_home.mkdir(parents=True)
    legacy_home.mkdir()
    current_text = '{"model":"current-model"}\n'
    (current_home / "config.json").write_text(current_text, encoding="utf-8")
    (legacy_home / "config.json").write_text(
        '{"model":"legacy-model","api_key":"legacy-key"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.delenv("DEEPSEEKFATHOM_CONFIG_HOME", raising=False)
    monkeypatch.delenv("DSTUL_CONFIG_HOME", raising=False)

    assert config_module.load_file_config() == {
        "model": "current-model",
        "api_key": "legacy-key",
    }
    assert (current_home / "config.json").read_text(encoding="utf-8") == current_text


def test_invalid_legacy_config_does_not_change_current_config(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    current_home = tmp_path / "current-config"
    legacy_home = home / LEGACY_DATA_DIRNAME
    current_home.mkdir()
    legacy_home.mkdir(parents=True)
    current_text = '{"api_key":"keep-current"}\n'
    (current_home / "config.json").write_text(current_text, encoding="utf-8")
    (legacy_home / "config.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(current_home))
    monkeypatch.delenv("DSTUL_CONFIG_HOME", raising=False)

    assert config_module.load_file_config() == {"api_key": "keep-current"}
    assert (current_home / "config.json").read_text(encoding="utf-8") == current_text


def test_failed_directory_scan_leaves_current_data_untouched(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    target = tmp_path / "current"
    source.mkdir()
    target.mkdir()
    current = target / "session.jsonl"
    current.write_text("current user data\n", encoding="utf-8")

    def failed_scan(_path: Path, _pattern: str):
        raise OSError("unreadable legacy directory")

    monkeypatch.setattr(Path, "rglob", failed_scan)
    with pytest.warns(RuntimeWarning, match="retry"):
        config_module.migrate_legacy_data(source, target)

    assert current.read_text(encoding="utf-8") == "current user data\n"


def test_migration_skips_both_source_target_nesting_directions(tmp_path: Path) -> None:
    source_parent = tmp_path / "source-parent"
    nested_target = source_parent / "nested-target"
    source_parent.mkdir()
    (source_parent / "source.txt").write_text("source\n", encoding="utf-8")

    config_module.migrate_legacy_data(source_parent, nested_target)

    assert not nested_target.exists()

    target_parent = tmp_path / "target-parent"
    nested_source = target_parent / "nested-source"
    nested_source.mkdir(parents=True)
    (nested_source / "legacy.txt").write_text("legacy\n", encoding="utf-8")

    config_module.migrate_legacy_data(nested_source, target_parent)

    assert not (target_parent / "legacy.txt").exists()
    assert (nested_source / "legacy.txt").read_text(encoding="utf-8") == "legacy\n"


def test_migration_refuses_nested_target_reparse_component(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    target = tmp_path / "current"
    nested_source = source / "sessions"
    nested_target = target / "sessions"
    nested_source.mkdir(parents=True)
    nested_target.mkdir(parents=True)
    (nested_source / "conversation.jsonl").write_text("legacy data\n", encoding="utf-8")
    real_check = config_module._is_link_or_reparse

    monkeypatch.setattr(
        config_module,
        "_is_link_or_reparse",
        lambda path: Path(path) == nested_target or real_check(Path(path)),
    )

    with pytest.warns(RuntimeWarning, match="retry"):
        config_module.migrate_legacy_data(source, target)

    assert not (nested_target / "conversation.jsonl").exists()


def test_session_list_includes_all_current_and_legacy_locations(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    locations = (
        (workspace, DATA_DIRNAME, "workspace-current"),
        (workspace, LEGACY_DATA_DIRNAME, "workspace-legacy"),
        (home, DATA_DIRNAME, "home-current"),
        (home, LEGACY_DATA_DIRNAME, "home-legacy"),
    )
    for root, data_dir, session_id in locations:
        _write_session(_session_path(root, data_dir, session_id), session_id, session_id)

    rows = SessionStore(workspace).list()

    assert {row["session_id"] for row in rows} == {item[2] for item in locations}


def test_append_only_legacy_growth_wins_without_overwriting_migrated_copy(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    session_id = "append-growth"
    legacy_path = _session_path(workspace, LEGACY_DATA_DIRNAME, session_id)
    current_path = _session_path(workspace, DATA_DIRNAME, session_id)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    _write_session(legacy_path, session_id, "first")
    config_module.migrate_legacy_data(
        workspace / LEGACY_DATA_DIRNAME,
        workspace / DATA_DIRNAME,
    )
    original_current = current_path.read_bytes()
    _write_session(legacy_path, session_id, "first", "later answer")
    newer = max(current_path.stat().st_mtime_ns, legacy_path.stat().st_mtime_ns) + 10_000_000
    os.utime(legacy_path, ns=(newer, newer))

    store = SessionStore(workspace)
    row = store.list()[0]
    loaded = store.load(session_id)

    assert row["session_id"] == session_id
    assert row["path"] == str(legacy_path)
    assert row["messages"] == 2
    assert loaded.path == legacy_path
    assert [message.content for message in loaded.messages] == ["first", "later answer"]
    assert current_path.read_bytes() == original_current


def test_newer_intentional_current_copy_wins_and_list_matches_load(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    session_id = "intentional-current"
    legacy_path = _session_path(workspace, LEGACY_DATA_DIRNAME, session_id)
    current_path = _session_path(workspace, DATA_DIRNAME, session_id)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    _write_session(legacy_path, session_id, "old one", "old two", "old three")
    _write_session(current_path, session_id, "rewritten current")
    base = max(current_path.stat().st_mtime_ns, legacy_path.stat().st_mtime_ns) + 10_000_000
    os.utime(legacy_path, ns=(base, base))
    os.utime(current_path, ns=(base + 10_000_000, base + 10_000_000))

    store = SessionStore(workspace)
    rows = store.list()
    loaded = store.load(session_id)

    assert len(rows) == 1
    assert rows[0]["path"] == str(current_path)
    assert store.resolve_session_path(session_id) == current_path
    assert loaded.path == current_path
    assert [message.content for message in loaded.messages] == ["rewritten current"]

    loaded.append(Message("assistant", "continue here"))
    assert "continue here" in current_path.read_text(encoding="utf-8")
    assert "continue here" not in legacy_path.read_text(encoding="utf-8")


def test_equal_timestamp_duplicate_is_listed_once_and_keeps_append_growth(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    session_id = "equal-time-growth"
    current_path = _session_path(workspace, DATA_DIRNAME, session_id)
    legacy_path = _session_path(workspace, LEGACY_DATA_DIRNAME, session_id)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    _write_session(current_path, session_id, "first")
    _write_session(legacy_path, session_id, "first", "second")
    timestamp = 1_800_000_000_000_000_000
    os.utime(current_path, ns=(timestamp, timestamp))
    os.utime(legacy_path, ns=(timestamp, timestamp))

    store = SessionStore(workspace)
    rows = store.list()

    assert len(rows) == 1
    assert rows[0]["messages"] == 2
    assert rows[0]["path"] == str(legacy_path)
    assert store.resolve_session_path(session_id) == legacy_path


def test_legacy_session_metadata_is_kept_and_current_metadata_wins_per_key(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    session_id = "metadata-migration"
    legacy_path = _session_path(home, LEGACY_DATA_DIRNAME, session_id)
    current_metadata = _session_path(workspace, DATA_DIRNAME, session_id).with_suffix(".meta.json")
    legacy_metadata = legacy_path.with_suffix(".meta.json")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    _write_session(legacy_path, session_id, "legacy conversation")
    legacy_metadata.write_text(
        json.dumps({"title": "Newer legacy title", "pinned": True}),
        encoding="utf-8",
    )
    current_metadata.parent.mkdir(parents=True)
    current_metadata.write_text(
        json.dumps({"title": "Older current title", "current_only": "kept"}),
        encoding="utf-8",
    )
    now = time.time_ns()
    os.utime(current_metadata, ns=(now - 2_000_000_000, now - 2_000_000_000))
    os.utime(legacy_metadata, ns=(now - 1_000_000_000, now - 1_000_000_000))

    store = SessionStore(workspace)
    row = store.list()[0]

    assert row["title"] == "Newer legacy title"
    assert row["pinned"] is True
    assert store.metadata(session_id) == {
        "title": "Newer legacy title",
        "pinned": True,
        "current_only": "kept",
    }

    store.update_metadata(session_id, title="Latest current title")

    assert store.metadata(session_id) == {
        "title": "Latest current title",
        "pinned": True,
        "current_only": "kept",
    }
    assert store.list()[0]["title"] == "Latest current title"
    persisted_current = json.loads(current_metadata.read_text(encoding="utf-8"))
    assert persisted_current == {
        "title": "Latest current title",
        "current_only": "kept",
    }

    legacy_metadata.write_text(
        json.dumps({"pinned": False, "legacy_after_update": "visible"}),
        encoding="utf-8",
    )
    legacy_newer = current_metadata.stat().st_mtime_ns + 1_000_000_000
    os.utime(legacy_metadata, ns=(legacy_newer, legacy_newer))
    assert store.metadata(session_id) == {
        "title": "Latest current title",
        "pinned": False,
        "current_only": "kept",
        "legacy_after_update": "visible",
    }

    tied = time.time_ns()
    os.utime(current_metadata, ns=(tied, tied))
    os.utime(legacy_metadata, ns=(tied, tied))
    assert store.metadata(session_id)["title"] == "Latest current title"


def test_delete_removes_only_same_id_files_from_all_compatibility_dirs(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    session_id = "delete-every-copy"
    other_id = "keep-other-session"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    locations = (
        (workspace, DATA_DIRNAME),
        (workspace, LEGACY_DATA_DIRNAME),
        (home, DATA_DIRNAME),
        (home, LEGACY_DATA_DIRNAME),
    )
    deleted_paths: list[Path] = []
    kept_paths: list[Path] = []
    for root, data_dir in locations:
        path = _session_path(root, data_dir, session_id)
        other = _session_path(root, data_dir, other_id)
        _write_session(path, session_id, "delete me")
        _write_session(other, other_id, "keep me")
        for sidecar in (path.with_suffix(".index.json"), path.with_suffix(".meta.json")):
            sidecar.write_text("{}\n", encoding="utf-8")
        for sidecar in (other.with_suffix(".index.json"), other.with_suffix(".meta.json")):
            sidecar.write_text("{}\n", encoding="utf-8")
        deleted_paths.extend((path, path.with_suffix(".index.json"), path.with_suffix(".meta.json")))
        kept_paths.extend((other, other.with_suffix(".index.json"), other.with_suffix(".meta.json")))

    SessionStore(workspace).delete(session_id)

    assert all(not path.exists() for path in deleted_paths)
    assert all(path.exists() for path in kept_paths)
    assert SessionStore(workspace).resolve_session_path(other_id) in {
        _session_path(root, data_dir, other_id)
        for root, data_dir in locations
    }
