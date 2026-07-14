from pathlib import Path
import json
import re

from deepseekfathom._core.agent import AgentResult, FathomAgent, compact_context_messages
from deepseekfathom._core.config import Settings
from deepseekfathom._core.messages import Message
from deepseekfathom._core.session import Session
from deepseekfathom._core.tools import ToolRegistry, _file_change_ui


ASSET_ROOT = Path(__file__).parents[1] / "src" / "deepseekfathom" / "_core" / "desktop" / "assets"


def test_large_replacement_diff_keeps_both_sides_and_full_counts() -> None:
    old = "\n".join(f"old line {index} " + "x" * 24 for index in range(900))
    new = "\n".join(f"new line {index} " + "y" * 24 for index in range(900))

    ui = _file_change_ui("large.txt", old, new, existed=True)

    assert ui["truncated"] is True
    assert ui["additions"] == 900
    assert ui["deletions"] == 900
    assert ui["omitted_lines"] > 0
    assert "old line 0" in ui["diff"]
    assert "new line 899" in ui["diff"]
    assert "diff lines omitted" in ui["diff"]


def test_failed_patch_does_not_claim_a_file_change(tmp_path: Path) -> None:
    result = ToolRegistry(tmp_path, allow_write=True).apply_patch(
        {"patch": "this is not a unified diff"}
    )

    assert result.ok is False
    assert result.ui is None


def test_desktop_batches_small_stream_deltas(monkeypatch, tmp_path: Path) -> None:
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    session = Session(tmp_path, session_id="delta-batch")
    session.append(Message("user", "start"))
    api.session = session

    class Window:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def evaluate_js(self, script: str) -> None:
            match = re.search(r"onNativeEvent\((.*)\);$", script)
            assert match
            self.events.append(json.loads(match.group(1)))

    class FakeAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            self.last_model_messages: list[Message] = []

        def run(self, _prompt: str, **kwargs) -> AgentResult:
            for _ in range(1000):
                kwargs["on_delta"]("x")
            kwargs["on_final"]("x" * 1000)
            kwargs["session"].append(Message("assistant", "x" * 1000))
            return AgentResult(kwargs["session"].session_id, "x" * 1000, 1)

    window = Window()
    api.bind_window(window)
    monkeypatch.setattr(desktop, "FathomAgent", FakeAgent)

    api._run_agent_turn("continue", [], "delta-batch", "turn-batch")

    deltas = [event for event in window.events if event["event"] == "assistant:delta"]
    assert "".join(event["payload"]["text"] for event in deltas) == "x" * 1000
    assert len(deltas) <= 2
    names = [event["event"] for event in window.events]
    assert names.index("assistant:delta") < names.index("assistant:final") < names.index("turn:done")


def test_session_rows_keep_scrollable_height() -> None:
    css = (ASSET_ROOT / "style.css").read_text(encoding="utf-8")
    session_rule = css.split(".sessionItem {", 1)[1].split("}", 1)[0]

    assert "flex: 0 0 auto" in session_rule
    assert "min-height: 42px" in session_rule


def test_frontend_session_navigation_unlocks_and_reports_failures() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert "打开会话失败：" in js
    assert "新建会话失败：" in js
    assert js.count("state.resuming = false;") >= 2
    assert "if (requestId === state.resumeRequestId)" in js
    assert "replayMessage(entry, result.sessionId)" in js


def test_frontend_session_scrollbar_maps_exact_endpoints_and_keeps_anchor() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert "function setSessionScrollFromThumbTop" in js
    assert "clamped >= travel ? maxScroll" in js
    assert "moveEvent.clientY - railRect.top - grabOffset" in js
    assert "wasAtBottom" in js
    assert "anchorOffset" in js


def test_frontend_stream_batching_preserves_each_bubble_at_tool_boundaries() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert "pendingStreamBubbles: new Set()" in js
    assert "state.pendingStreamBubbles.add(bubble)" in js
    assert "const targets = Array.from(state.pendingStreamBubbles)" in js
    assert "flushStreamingBubble(narrationBubble)" in js


def test_frontend_diff_contract_uses_native_tools_and_honest_line_numbers() -> None:
    js = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "style.css").read_text(encoding="utf-8")

    assert 'protocol: "native-openai-with-text-fallback"' in js
    assert 'protocol: "json-in-text"' not in js
    omitted_branch = js.split('/^\\.\\.\\. \\d+ diff lines omitted \\.\\.\\.$/.test(line)', 1)[1]
    omitted_branch = omitted_branch.split("} else if", 1)[0]
    assert "oldLine = null" in omitted_branch
    assert "newLine = null" in omitted_branch
    assert '.diffLine.add code' in css and '.diffLine.del code' in css
    assert "overflow: auto" in css.split(".fileDiff {", 1)[1].split("}", 1)[0]


def test_upstream_context_snapshot_can_trigger_compaction() -> None:
    messages = [Message("system", "system")]
    messages.extend(
        Message("user" if index % 2 == 0 else "assistant", f"message {index} " + "x" * 1000)
        for index in range(30)
    )

    unchanged = compact_context_messages(
        messages,
        "test-model",
        context_limit=10_000,
        threshold_percent=90,
    )
    compacted = compact_context_messages(
        messages,
        "test-model",
        context_limit=10_000,
        threshold_percent=90,
        observed_tokens=9_500,
    )

    assert unchanged is messages
    assert compacted is not messages
    assert len(compacted) < len(messages)


def test_compaction_retries_below_ten_messages_and_keeps_skill_tool_pair() -> None:
    skill_call = Message(
        "assistant",
        '{"tool":"read_skill","arguments":{"name":"repo-debug"}}',
    )
    skill_result = Message(
        "user",
        'TOOL_RESULT name=read_skill\n{"ok":true,"output":"<skill-pin name=repo-debug>rules</skill-pin>"}',
    )
    messages = [
        Message("system", "system"),
        skill_call,
        skill_result,
        *[
            Message("user" if index % 2 == 0 else "assistant", f"recent {index} " + "z" * 4000)
            for index in range(7)
        ],
    ]

    compacted = compact_context_messages(
        messages,
        "test-model",
        force=True,
        context_limit=5_000,
    )

    assert compacted is not messages
    call_index = next(index for index, message in enumerate(compacted) if message is skill_call)
    assert compacted[call_index + 1] is skill_result


def test_agent_uses_desktop_context_hint_for_automatic_compaction(tmp_path: Path) -> None:
    class Client:
        def __init__(self) -> None:
            self.replies = ["handoff", "final"]
            self.calls = 0

        def chat(self, _messages):
            self.calls += 1
            return self.replies.pop(0)

    config = Settings(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="test-model",
        workspace=tmp_path,
        max_tool_rounds=4,
        max_tokens=2048,
        request_timeout=30,
        default_mode="root",
        default_thinking="fast",
        context_window_tokens=10_000,
        compact_threshold_percent=90,
    )
    session = Session(tmp_path, session_id="hinted", persist=False)
    session.messages = [Message("system", "system")]
    session.messages.extend(
        Message("user" if index % 2 == 0 else "assistant", f"message {index} " + "x" * 1000)
        for index in range(30)
    )
    client = Client()

    result = FathomAgent(
        config,
        mode="root",
        client=client,
        context_tokens_hint=9_500,
    ).run("continue", session=session, require_todo=False)

    assert result.answer == "final"
    assert client.calls == 2
    assert any("handoff" in message.content for message in session.messages if message.role == "system")
