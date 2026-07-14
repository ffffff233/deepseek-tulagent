from __future__ import annotations

import json

from deepseekfathom._core.messages import Message, clone_message
from deepseekfathom._core.session import Session, SessionStore


def test_message_keeps_first_four_positional_arguments_and_clone_is_independent() -> None:
    original = Message(
        "user",
        "model content",
        "author",
        ["data:image/png;base64,abc"],
        "command",
        "/mcp",
        False,
        "turn-1",
    )

    cloned = clone_message(original, content="updated", turn_id="turn-2")
    cloned.images.append("data:image/png;base64,def")

    assert original.role == "user"
    assert original.name == "author"
    assert original.images == ["data:image/png;base64,abc"]
    assert original.ui_kind == "command"
    assert original.display_content == "/mcp"
    assert original.model_visible is False
    assert original.turn_id == "turn-1"
    assert cloned.content == "updated"
    assert cloned.turn_id == "turn-2"
    assert cloned.images == ["data:image/png;base64,abc", "data:image/png;base64,def"]


def test_session_append_load_and_rewrite_roundtrip_message_metadata(tmp_path) -> None:
    session = Session(tmp_path, session_id="metadata-roundtrip")
    session.append(
        Message(
            "user",
            "internal command",
            images=["data:image/png;base64,abc"],
            ui_kind="command",
            display_content="/mcp",
            model_visible=False,
            turn_id="turn-1",
        )
    )

    loaded = SessionStore(tmp_path).load(session.session_id)
    assert loaded.messages == session.messages

    loaded.messages[0] = clone_message(
        loaded.messages[0],
        display_content="/review",
        turn_id="turn-2",
    )
    loaded.rewrite()

    rewritten = SessionStore(tmp_path).load(session.session_id)
    assert rewritten.messages == loaded.messages
    record = json.loads(rewritten.path.read_text(encoding="utf-8").splitlines()[0])["message"]
    assert record == {
        "role": "user",
        "content": "internal command",
        "images": ["data:image/png;base64,abc"],
        "ui_kind": "command",
        "display_content": "/review",
        "model_visible": False,
        "turn_id": "turn-2",
    }


def test_legacy_jsonl_loads_with_message_metadata_defaults(tmp_path) -> None:
    sessions = tmp_path / ".deepseekfathom" / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "legacy-metadata.jsonl"
    path.write_text(
        json.dumps(
            {
                "session_id": "legacy-metadata",
                "created_at": "2026-07-14T00:00:00+00:00",
                "message": {
                    "role": "user",
                    "content": "legacy content",
                    "name": "legacy-name",
                    "images": ["data:image/png;base64,legacy"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    message = SessionStore(tmp_path).load("legacy-metadata").messages[0]
    assert message == Message(
        "user",
        "legacy content",
        "legacy-name",
        ["data:image/png;base64,legacy"],
    )
    assert message.ui_kind is None
    assert message.display_content is None
    assert message.model_visible is True
    assert message.turn_id is None


def test_session_titles_skip_model_hidden_user_messages_for_live_and_scanned_logs(tmp_path) -> None:
    live = Session(tmp_path, session_id="live-hidden-title")
    live.append(Message("user", "/mcp", ui_kind="command", model_visible=False))
    live.append(Message("user", "真正的问题"))
    assert SessionStore(tmp_path).list()[0]["title"] == "真正的问题"

    sessions = tmp_path / ".deepseekfathom" / "sessions"
    scanned_path = sessions / "scanned-hidden-title.jsonl"
    events = [
        {
            "session_id": "scanned-hidden-title",
            "created_at": "2026-07-14T00:00:00+00:00",
            "message": {"role": "user", "content": "/review", "model_visible": False},
        },
        {
            "session_id": "scanned-hidden-title",
            "created_at": "2026-07-14T00:00:00+00:00",
            "message": {"role": "user", "content": "扫描后的标题"},
        },
    ]
    scanned_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )

    rows = {row["session_id"]: row for row in SessionStore(tmp_path).list()}
    assert rows["scanned-hidden-title"]["title"] == "扫描后的标题"
