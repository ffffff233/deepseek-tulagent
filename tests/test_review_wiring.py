from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import threading
import time

import deepseekfathom._core.desktop.app as desktop
from deepseekfathom._core.agent import AgentResult, FathomAgent
from deepseekfathom._core.messages import Message
from deepseekfathom._core.native_plugins import resolve_native_command, set_native_plugin_enabled
from deepseekfathom._core.policy import ThinkingMode
from deepseekfathom._core.provider import UsageStats
from deepseekfathom._core.session import Session, SessionStore
from deepseekfathom._core.tool_contracts import ToolContract


def git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def make_repository(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git(workspace, "init")
    git(workspace, "config", "user.email", "review@example.test")
    git(workspace, "config", "user.name", "Review Test")
    (workspace / ".gitignore").write_text(".deepseekfathom/\n", encoding="utf-8")
    (workspace / "tracked.txt").write_text("before\n", encoding="utf-8")
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", "baseline")
    return workspace


def wait_idle(api: desktop.DesktopApi) -> None:
    deadline = time.time() + 10
    while api._running and time.time() < deadline:
        time.sleep(0.01)
    assert api._running is False


class FakeClient:
    def __init__(self, *_args, **_kwargs):
        self.usage = UsageStats()
        self.last_usage = UsageStats()
        self.runtime_tool_contracts: dict[str, ToolContract] = {}

    def close(self) -> None:
        return None


def fake_agent_class(state: dict):
    class FakeAgent:
        def __init__(self, settings, *, mode, thinking="fast", extra_tool_contracts=(), hook_runner=None, approve=None, **_kwargs):
            self.settings = settings
            self.mode = mode
            self.thinking = thinking
            self.last_model_messages: list[Message] = []
            state.setdefault("constructors", []).append({
                "mode": mode,
                "thinking": thinking,
                "hook_runner": hook_runner,
                "approve": approve,
            })
            contracts = {contract.name: contract for contract in extra_tool_contracts}
            for name, read_only in {
                "list_files": True,
                "read_file": True,
                "search_text": True,
                "git_diff": True,
                "run_shell": False,
                "write_file": False,
            }.items():
                contracts[name] = ToolContract(
                    name=name,
                    description=name,
                    schema={"type": "object", "properties": {}},
                    origin="builtin",
                    read_only=read_only,
                    trusted_read_only=read_only,
                )
            self.tool_contracts = contracts

        def run(self, prompt, **kwargs):
            turn_id = kwargs.get("turn_id")
            session = kwargs["session"]
            state.setdefault("runs", []).append({
                "mode": self.mode,
                "thinking": self.thinking,
                "prompt": prompt,
                "tools": set(self.tool_contracts),
                "display_prompt": kwargs.get("display_prompt"),
                "ui_kind": kwargs.get("ui_kind"),
                "turn_id": turn_id,
                "require_todo": kwargs.get("require_todo"),
                "max_tool_rounds": kwargs.get("max_tool_rounds"),
            })
            session.append(Message(
                "user",
                prompt,
                display_content=kwargs.get("display_prompt"),
                ui_kind=kwargs.get("ui_kind"),
                turn_id=turn_id,
            ))
            if self.mode == "review":
                if not state.get("skip_review_diff"):
                    contract = self.tool_contracts["read_review_diff"]
                    cursor = None
                    pages: list[dict] = []
                    while True:
                        arguments = {"cursor": cursor} if cursor else {}
                        result = contract.handler(arguments)
                        assert result is not None and result.ok is True
                        page = json.loads(result.output)
                        pages.append(page)
                        cursor = page.get("nextCursor")
                        if not cursor:
                            break
                    state.setdefault("review_pages", []).append(pages)
                if state.get("mutate_during_review"):
                    (self.settings.workspace / "tracked.txt").write_text(
                        "changed during review\n",
                        encoding="utf-8",
                    )
                answer = "review complete"
            else:
                (self.settings.workspace / "tracked.txt").write_text("after\n", encoding="utf-8")
                answer = "changed"
            if kwargs.get("on_final"):
                kwargs["on_final"](answer)
            session.append(Message("assistant", answer, turn_id=turn_id))
            return AgentResult(session.session_id, answer, 1)

    return FakeAgent


def new_api(monkeypatch, tmp_path: Path, workspace: Path, state: dict) -> desktop.DesktopApi:
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(workspace))
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setattr(desktop, "DeepSeekClient", FakeClient)
    monkeypatch.setattr(desktop, "FathomAgent", fake_agent_class(state))
    return desktop.DesktopApi()


def test_native_agent_command_uses_backend_prompt_and_per_turn_policy(monkeypatch, tmp_path: Path) -> None:
    workspace = make_repository(tmp_path)
    state: dict = {}
    api = new_api(monkeypatch, tmp_path, workspace, state)
    api.session = Session(workspace, session_id="native-command")
    api.mode = "root"
    api.thinking = ThinkingMode.resolve("max")
    command = resolve_native_command("commit", api._extensions.home)
    assert command is not None

    started = api.send({
        "prompt": "Use Conventional Commits.",
        "displayPrompt": "/commit Use Conventional Commits.",
        "uiKind": "command",
        "nativeCommand": "commit",
        "clientRequestId": "native-commit-1",
    })
    assert started["ok"] is True
    wait_idle(api)

    run = state["runs"][-1]
    assert run["mode"] == command.mode == "plan"
    assert run["thinking"] == command.thinking == "fast"
    assert run["prompt"].startswith(command.prompt)
    assert run["prompt"].endswith("Additional user instructions:\nUse Conventional Commits.")
    assert run["display_prompt"] == "/commit Use Conventional Commits."
    assert run["ui_kind"] == "command"
    assert api.mode == "root"
    assert api.thinking.name == "max"
    persisted = SessionStore(workspace).load(started["sessionId"]).messages
    assert persisted[0].display_content == "/commit Use Conventional Commits."

    retried = api.retry({})
    assert retried["ok"] is True
    wait_idle(api)
    assert state["runs"][-1]["mode"] == "plan"
    assert state["runs"][-1]["thinking"] == "fast"

    edited = api.edit_resend({"prompt": "/commit Focus on the CLI."})
    assert edited["ok"] is True
    wait_idle(api)
    edited_run = state["runs"][-1]
    assert edited_run["mode"] == "plan"
    assert edited_run["thinking"] == "fast"
    assert edited_run["prompt"].endswith("Additional user instructions:\nFocus on the CLI.")

    set_native_plugin_enabled("commit-assistant", False, api._extensions.home)
    rejected = api.send({
        "prompt": "ignore",
        "displayPrompt": "/commit ignore",
        "nativeCommand": "commit",
        "clientRequestId": "native-commit-disabled",
    })
    assert rejected["ok"] is False
    assert "disabled or unavailable" in rejected["error"]


def test_review_uses_last_turn_snapshot_and_an_isolated_frozen_tool(monkeypatch, tmp_path: Path) -> None:
    workspace = make_repository(tmp_path)
    state: dict = {}
    api = new_api(monkeypatch, tmp_path, workspace, state)
    api.session = Session(workspace, session_id="source-session")

    sent = api.send({"prompt": "change the tracked file", "clientRequestId": "normal-1"})
    assert sent["ok"] is True
    wait_idle(api)
    source_id = api.session.session_id
    source_messages = SessionStore(workspace).load(source_id).messages
    turn_records = SessionStore(workspace).metadata(source_id)["review_turns"]
    assert turn_records[-1]["status"] == "completed"
    assert turn_records[-1]["before_hash"] != turn_records[-1]["after_hash"]
    assert turn_records[-1]["change_id"]
    api._review_service.store.remove_snapshot(turn_records[-1]["before_snapshot_id"])
    api._review_service.store.remove_snapshot(turn_records[-1]["after_snapshot_id"])

    started = api.review_changes({
        "command": "/review focus on regressions",
        "displayPrompt": "/review focus on regressions",
        "instructions": "Focus on regressions.",
        "clientRequestId": "review-1",
    })
    assert started["ok"] is True
    assert started["sessionId"] != source_id
    wait_idle(api)

    review_run = state["runs"][-1]
    assert review_run["mode"] == "review"
    assert review_run["tools"] == {"list_files", "read_file", "read_review_diff", "search_text"}
    assert review_run["require_todo"] is False
    assert review_run["max_tool_rounds"] == 5
    assert review_run["display_prompt"] == "/review focus on regressions"
    assert "snapshot_range" in review_run["prompt"]
    diff_text = "\n".join(page.get("text", "") for page in state["review_pages"][-1])
    assert "tracked.txt" in diff_text
    assert "+after" in diff_text
    assert SessionStore(workspace).load(source_id).messages == source_messages

    review_meta = SessionStore(workspace).metadata(started["sessionId"])["review"]
    assert review_meta["state"] == "completed"
    assert review_meta["source_session_id"] == source_id
    assert review_meta["scope"] == "snapshot_range"
    assert review_meta["stale"] is False
    persisted = SessionStore(workspace).load(started["sessionId"]).messages
    assert persisted[0].display_content == "/review focus on regressions"
    assert persisted[0].ui_kind == "command"
    assert persisted[0].turn_id == started["turnId"]
    duplicate = api.review_changes({"clientRequestId": "review-1"})
    assert duplicate["duplicate"] is True
    assert duplicate["sessionId"] == started["sessionId"]


def test_review_retry_and_branch_keep_read_only_manifest_scope(monkeypatch, tmp_path: Path) -> None:
    workspace = make_repository(tmp_path)
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    state: dict = {}
    api = new_api(monkeypatch, tmp_path, workspace, state)
    api.session = Session(workspace, session_id="review-source")

    started = api.review_changes({"command": "/review", "clientRequestId": "review-retry"})
    wait_idle(api)
    review_session_id = started["sessionId"]
    change_id = SessionStore(workspace).metadata(review_session_id)["review"]["change_id"]

    retried = api.retry({"srcIndex": 1})
    assert retried["ok"] is True
    wait_idle(api)
    retry_run = state["runs"][-1]
    assert retry_run["mode"] == "review"
    assert retry_run["display_prompt"] == "/review"
    assert retry_run["tools"] == {"list_files", "read_file", "read_review_diff", "search_text"}
    assert SessionStore(workspace).metadata(review_session_id)["review"]["change_id"] == change_id

    branched = api.branch({"srcIndex": 1})
    assert branched["ok"] is True
    branch_id = branched["sessionId"]
    assert SessionStore(workspace).metadata(branch_id)["review"]["change_id"] == change_id
    followed_up = api.send({"prompt": "check the deletion risk", "clientRequestId": "review-followup"})
    assert followed_up["ok"] is True
    wait_idle(api)
    followup_run = state["runs"][-1]
    assert followup_run["mode"] == "review"
    assert followup_run["tools"] == {"list_files", "read_file", "read_review_diff", "search_text"}

    api._review_service.store.change_dir(change_id).joinpath("manifest.json").unlink()
    run_count = len(state["runs"])
    expired = api.send({"prompt": "try after artifact expiry", "clientRequestId": "review-expired"})
    assert expired["ok"] is True
    wait_idle(api)
    assert len(state["runs"]) == run_count
    assert SessionStore(workspace).metadata(branch_id)["review"]["state"] == "error"


def test_cancelled_review_persists_cancelled_state(monkeypatch, tmp_path: Path) -> None:
    workspace = make_repository(tmp_path)
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    entered = threading.Event()

    class CancelAgent:
        def __init__(self, _settings, *, mode, extra_tool_contracts=(), **_kwargs):
            assert mode == "review"
            self.last_model_messages: list[Message] = []
            self.tool_contracts = {contract.name: contract for contract in extra_tool_contracts}

        def run(self, prompt, **kwargs):
            session = kwargs["session"]
            session.append(Message(
                "user",
                prompt,
                display_content=kwargs.get("display_prompt"),
                ui_kind=kwargs.get("ui_kind"),
                turn_id=kwargs.get("turn_id"),
            ))
            entered.set()
            deadline = time.time() + 5
            while not kwargs["should_cancel"]() and time.time() < deadline:
                time.sleep(0.01)
            raise RuntimeError("turn cancelled")

    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(workspace))
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setattr(desktop, "DeepSeekClient", FakeClient)
    monkeypatch.setattr(desktop, "FathomAgent", CancelAgent)
    api = desktop.DesktopApi()
    api.session = Session(workspace, session_id="cancel-source")

    started = api.review_changes({"command": "/review", "clientRequestId": "review-cancel"})
    assert entered.wait(timeout=5)
    cancelled = api.cancel({"turnId": started["turnId"]})
    assert cancelled["cancelling"] is True
    wait_idle(api)
    review = SessionStore(workspace).metadata(started["sessionId"])["review"]
    assert review["state"] == "cancelled"
    assert review["change_id"]


def test_review_terminal_event_and_metadata_mark_a_changed_workspace_stale(monkeypatch, tmp_path: Path) -> None:
    workspace = make_repository(tmp_path)
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    state: dict = {"mutate_during_review": True}
    api = new_api(monkeypatch, tmp_path, workspace, state)
    api.session = Session(workspace, session_id="stale-source")

    class Window:
        def __init__(self):
            self.events: list[dict] = []

        def evaluate_js(self, script: str) -> None:
            match = re.search(r"onNativeEvent\((.*)\);$", script)
            assert match is not None
            self.events.append(json.loads(match.group(1)))

    window = Window()
    api.bind_window(window)
    started = api.review_changes({"command": "/review", "clientRequestId": "review-stale"})
    wait_idle(api)

    review = SessionStore(workspace).metadata(started["sessionId"])["review"]
    assert review["state"] == "completed"
    assert review["stale"] is True
    assert review["stale_status"]["referenceHash"] != review["stale_status"]["currentHash"]
    done = next(event for event in window.events if event["event"] == "turn:done")
    assert done["payload"]["review"]["changeId"] == review["change_id"]
    assert done["payload"]["review"]["stale"] is True


def test_review_cannot_complete_without_reading_the_frozen_diff(monkeypatch, tmp_path: Path) -> None:
    workspace = make_repository(tmp_path)
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    state: dict = {"skip_review_diff": True}
    api = new_api(monkeypatch, tmp_path, workspace, state)
    api.session = Session(workspace, session_id="incomplete-review-source")

    started = api.review_changes({"command": "/review", "clientRequestId": "review-incomplete"})
    wait_idle(api)

    review = SessionStore(workspace).metadata(started["sessionId"])["review"]
    assert review["state"] == "error"
    persisted = SessionStore(workspace).load(started["sessionId"]).messages
    assert not any(message.role == "assistant" and message.content == "review complete" for message in persisted)


def test_review_diff_tool_rejects_a_skipped_cursor(monkeypatch, tmp_path: Path) -> None:
    workspace = make_repository(tmp_path)
    (workspace / "tracked.txt").write_text("dirty\n" * 400, encoding="utf-8")
    state: dict = {}
    api = new_api(monkeypatch, tmp_path, workspace, state)
    manifest = api._review_service.changes_from_head()
    context = {"manifest": manifest}
    contract = api._review_diff_contract(context)

    skipped = contract.handler({"cursor": "999:0", "limit": 1024})
    assert skipped is not None and skipped.ok is False
    assert "out of sequence" in skipped.output
    assert context["diff_read_complete"] is False

    cursor = None
    while True:
        page_result = contract.handler({"cursor": cursor, "limit": 1024} if cursor else {"limit": 1024})
        assert page_result is not None and page_result.ok is True
        cursor = json.loads(page_result.output)["nextCursor"]
        if cursor is None:
            break
    assert context["diff_read_complete"] is True


def test_removed_virtual_contract_cannot_delegate_out_of_review_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPSEEKFATHOM_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))

    class TextClient:
        supports_native_tools = False

        def __init__(self):
            self.calls = 0

        def chat(self, _messages):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({
                    "tool": "delegate_agent",
                    "arguments": {"name": "escape", "task": "write files", "mode": "root"},
                })
            return "Blocked: delegation is unavailable; the read-only review is complete."

    client = TextClient()
    agent = FathomAgent(desktop.get_desktop_settings(), mode="review", client=client)
    agent.tool_contracts.pop("delegate_agent", None)

    def unexpected_delegate(*_args, **_kwargs):
        raise AssertionError("delegate_agent bypassed the review tool whitelist")

    agent._run_subagent = unexpected_delegate  # type: ignore[method-assign]
    result = agent.run(
        "Review the frozen diff.",
        session=Session(tmp_path, session_id="virtual-tool-guard", persist=False),
        max_tool_rounds=2,
        require_todo=False,
    )

    assert "read-only review" in result.answer
    assert client.calls == 2
