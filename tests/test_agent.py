from __future__ import annotations

from pathlib import Path
import json
import io
import os
import re
import subprocess
import zipfile

from deepseek_tulagent.agent import RECOVER_AFTER_TOOL_FAILURE_PROMPT, TuLAgent, compact_context_messages, filter_internal_automation_messages, is_question_mark_only, normalize_subagent_mode_and_thinking, normalize_subagent_specs, normalize_user_question, parse_tool_call, plainify_assistant_text, promises_more_work, trim_tool_content, tool_result_message
from deepseek_tulagent.cli import main
from deepseek_tulagent.config import Settings, get_settings, merge_file_config, resolve_model
from deepseek_tulagent.messages import Message
from deepseek_tulagent.policy import ApprovalPolicy, ThinkingMode
from deepseek_tulagent.provider import apply_thinking_payload
from deepseek_tulagent.session import SessionStore
from deepseek_tulagent.skills import SkillStore
from deepseek_tulagent.tui import ChatTui, TuiState
from deepseek_tulagent.ui import ThinkingSpinner, composer_display_text, composer_prompt, display_width, filter_slash_items, format_agent_event, palette_footer_text, print_box, read_bracketed_paste, read_escape_suffix, read_raw_char, redraw_composer, selected_window_start, should_submit_newline, tail_for_width, slash_selection_insertion
from deepseek_tulagent.tools import ToolError, ToolRegistry, normalize_bing_url


class FakeClient:
    def __init__(self, replies: list[str]):
        self.replies = replies

    def chat(self, messages):
        return self.replies.pop(0)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        workspace=tmp_path,
        max_tool_rounds=4,
        max_tokens=2048,
        request_timeout=180,
        default_mode="root",
        default_thinking="fast",
    )


def test_parse_plain_json_tool_call():
    call = parse_tool_call('{"tool":"read_file","arguments":{"path":"README.md"}}')
    assert call == ("read_file", {"path": "README.md"})


def test_parse_fenced_json_tool_call():
    call = parse_tool_call('```json\n{"tool":"run_shell","arguments":{"command":"pwd"}}\n```')
    assert call == ("run_shell", {"command": "pwd"})


def test_parse_deepseek_function_call_shape():
    call = parse_tool_call('{"function_call":{"name":"write_file","arguments":"{\\"path\\":\\"a.txt\\",\\"content\\":\\"ok\\"}"}}')
    assert call == ("write_file", {"path": "a.txt", "content": "ok"})


def test_parse_standard_tool_calls_shape():
    call = parse_tool_call(
        '{"tool_calls":[{"function":{"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}]}'
    )
    assert call == ("read_file", {"path": "README.md"})


def test_parse_text_wrapped_tool_json():
    call = parse_tool_call('I will use a tool.\n{"name":"search_text","input":{"query":"DeepSeek"}}')
    assert call == ("search_text", {"query": "DeepSeek"})


def test_parse_tool_json_with_trailing_fence_noise():
    text = (
        "现在修改文件。\n"
        '{"tool":"apply_patch","arguments":{"patch":"--- a/index.html\\n+++ b/index.html\\n@@\\n-old\\n+new\\n"},"timeout":10}}\n'
        "```"
    )
    call = parse_tool_call(text)
    assert call == (
        "apply_patch",
        {"patch": "--- a/index.html\n+++ b/index.html\n@@\n-old\n+new\n", "timeout": 10},
    )


def test_plainify_assistant_text_removes_decorative_stars():
    text = "**标题**\n* 项目\n```bash\necho *.py\n```"
    cleaned = plainify_assistant_text(text)
    assert "**" not in cleaned
    assert "- 项目" in cleaned
    assert "echo *.py" in cleaned


def test_parse_action_bash_block_as_shell_tool():
    call = parse_tool_call("我现在检查仓库。\n\n```bash\nprintf repo-ok\n```")
    assert call == ("run_shell", {"command": "printf repo-ok"})


def test_parse_ordinary_bash_example_is_not_tool_call():
    call = parse_tool_call("可以这样手动运行：\n\n```bash\necho hello\n```")
    assert call is None


def test_parse_multiple_action_bash_blocks_as_one_shell_tool():
    call = parse_tool_call(
        "我来检查本机所有端口：\n\n"
        "```bash\nss -tuln\n```\n\n"
        "同时查看连接：\n\n"
        "```bash\nss -tun\n```"
    )
    assert call == ("run_shell", {"command": "ss -tuln\nss -tun"})


def test_parse_labelled_tool_arguments():
    call = parse_tool_call(
        'Tool: clone_repo\nArguments: {"repo/url": "https://github.com/esengine/DeepSeek-Reasonix", "path": "/root/DeepSeek-Reasonix"}'
    )
    assert call == (
        "clone_repo",
        {"repo/url": "https://github.com/esengine/DeepSeek-Reasonix", "path": "/root/DeepSeek-Reasonix"},
    )


def test_parse_ask_user_tool_call():
    call = parse_tool_call(
        '{"tool":"ask_user","arguments":{"question":"用什么语言开发？","options":["Python","Go"],"allow_manual":true}}'
    )
    assert call == ("ask_user", {"question": "用什么语言开发？", "options": ["Python", "Go"], "allow_manual": True})


def test_normalize_user_question_options():
    question = normalize_user_question(
        {
            "question": "选择语言",
            "options": [
                "Python",
                {"label": "Go", "value": "go", "description": "单二进制"},
                {"value": "Rust"},
            ],
        }
    )
    assert question["question"] == "选择语言"
    assert question["options"] == [
        {"label": "Python", "value": "Python", "description": "", "id": "0"},
        {"label": "Go", "value": "go", "description": "单二进制", "id": "1"},
        {"label": "Rust", "value": "Rust", "description": "", "id": "2"},
    ]
    assert question["allow_manual"] is True


def test_agent_runs_read_tool_loop(tmp_path: Path):
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    client = FakeClient([
        '{"tool":"read_file","arguments":{"path":"README.md"}}',
        "README says hello.",
    ])
    result = TuLAgent(settings(tmp_path), client=client).run("summarize")
    assert result.answer == "README says hello."
    assert result.rounds == 2
    assert (tmp_path / ".deepseek-tulagent" / "sessions").exists()


def test_agent_delegates_to_subagent_with_isolated_context(tmp_path: Path):
    class DelegateClient:
        def __init__(self):
            self.calls = 0
            self.subagent_saw_parent_prompt = False

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"delegate_agent","arguments":{"name":"researcher","task":"检查 README","mode":"plan"}}'
            if self.calls == 2:
                joined = "\n".join(message.content for message in messages)
                self.subagent_saw_parent_prompt = "主任务秘密" in joined
                return "子代理结论：README 不存在。"
            assert "SUBAGENT_RESULT name=researcher" in messages[-1].content
            assert "子代理结论" in messages[-1].content
            return "主代理总结：已收到子代理结果。"

    client = DelegateClient()
    result = TuLAgent(settings(tmp_path), mode="root", client=client).run("主任务秘密：委派检查")
    assert result.answer == "主代理总结：已收到子代理结果。"
    assert client.subagent_saw_parent_prompt is False


def test_subagent_treats_thinking_mode_in_mode_field_as_thinking(tmp_path: Path):
    class DelegateClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"delegate_agent","arguments":{"name":"researcher","task":"检查 README","mode":"fast","max_rounds":1}}'
            if self.calls == 2:
                return "子代理 fast 结论。"
            assert "SUBAGENT_RESULT name=researcher" in messages[-1].content
            assert "子代理 fast 结论" in messages[-1].content
            return "主代理收到。"

    result = TuLAgent(settings(tmp_path), mode="root", thinking="fast", client=DelegateClient()).run("委派检查")
    assert result.answer == "主代理收到。"


def test_agent_delegates_to_multiple_subagents_in_one_tool_call(tmp_path: Path):
    class MultiDelegateClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return (
                    '{"tool":"delegate_agent","arguments":{"agents":['
                    '{"name":"researcher","task":"检查 README","mode":"plan","max_rounds":1},'
                    '{"name":"reviewer","task":"检查测试","mode":"fast","max_rounds":1}'
                    ']}}'
                )
            if self.calls == 2:
                return "researcher 结论。"
            if self.calls == 3:
                return "reviewer 结论。"
            assert "SUBAGENT_RESULT name=researcher,reviewer" in messages[-1].content
            assert "researcher 结论" in messages[-1].content
            assert "reviewer 结论" in messages[-1].content
            return "主代理收到两个子代理结果。"

    result = TuLAgent(settings(tmp_path), mode="root", thinking="fast", client=MultiDelegateClient()).run("并行委派检查")
    assert result.answer == "主代理收到两个子代理结果。"


def test_agent_delegate_respects_cancel_before_subagent_runs(tmp_path: Path):
    class DelegateClient:
        def __init__(self):
            self.calls = 0

        def chat(self, _messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"delegate_agent","arguments":{"name":"researcher","task":"检查 README","max_rounds":1}}'
            return "子代理不应该运行。"

    cancelled = {"value": False}

    def should_cancel():
        return cancelled["value"]

    def on_event(_text: str):
        cancelled["value"] = True

    try:
        TuLAgent(settings(tmp_path), mode="root", client=DelegateClient()).run(
            "委派检查",
            on_event=on_event,
            should_cancel=should_cancel,
        )
    except RuntimeError as exc:
        assert str(exc) == "turn cancelled"
    else:
        raise AssertionError("delegate_agent did not honor cancellation")


def test_normalize_subagent_mode_and_thinking_handles_swapped_mode():
    assert normalize_subagent_mode_and_thinking("fast", None, parent_mode="root", parent_thinking="careful") == ("root", "fast")
    assert normalize_subagent_mode_and_thinking("nonsense", "bad", parent_mode="root", parent_thinking="careful") == ("plan", "careful")


def test_normalize_subagent_specs_accepts_agents_and_tasks():
    specs = normalize_subagent_specs({"agents": [{"name": "one", "task": "a"}, "b"]})
    assert specs == [{"name": "one", "task": "a"}, {"task": "b", "name": "subagent-2"}]
    assert normalize_subagent_specs({"task": "single"}) == [{"task": "single"}]


def test_subagents_slash_item_is_hidden_from_quick_palette(tmp_path: Path):
    from deepseek_tulagent.cli import slash_items

    labels = [label for label, _description in slash_items(settings(tmp_path))]
    assert "/subagents" not in labels
    assert slash_selection_insertion("/subagents") is None


def test_palette_footer_explains_quit_keys():
    footer = palette_footer_text()
    assert "ctrl-c" in footer.lower()
    assert "ctrl-d" in footer.lower()
    assert "esc" in footer.lower()


def test_agent_ask_user_feeds_answer_back_to_model(tmp_path: Path):
    class AskClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"ask_user","arguments":{"question":"用什么语言开发？","options":[{"label":"Python","value":"python"},{"label":"Go","value":"go"}]}}'
            assert "USER_ANSWER" in messages[-1].content
            assert '"answer": "python"' in messages[-1].content
            return "已选择 Python。"

    answers = []

    def ask_user(question):
        answers.append(question)
        return {"answer": "python", "label": "Python"}

    result = TuLAgent(settings(tmp_path), mode="root", client=AskClient(), ask_user=ask_user).run("创建程序")
    assert result.answer == "已选择 Python。"
    assert answers[0]["question"] == "用什么语言开发？"
    assert answers[0]["options"][0]["label"] == "Python"


def test_streamed_tool_json_is_not_printed_as_visible_delta(tmp_path: Path):
    class StreamingToolClient:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, _messages):
            self.calls += 1
            if self.calls == 1:
                yield '{"tool":"read_file",'
                yield '"arguments":{"path":"README.md"}}'
            else:
                yield "读取完成。"

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    visible: list[str] = []
    events: list[str] = []
    client = StreamingToolClient()
    result = TuLAgent(settings(tmp_path), mode="root", client=client).run(
        "读取 README",
        stream=True,
        on_delta=visible.append,
        on_event=events.append,
    )
    assert result.answer == "读取完成。"
    assert "".join(visible) == "读取完成。"
    assert any(event.startswith("tool read_file") for event in events)
    assert '{"tool"' not in "".join(visible)


def test_streamed_fenced_tool_json_is_not_printed_as_visible_delta(tmp_path: Path):
    class StreamingFencedToolClient:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, _messages):
            self.calls += 1
            if self.calls == 1:
                yield '```json\n{"tool":"read_file",'
                yield '"arguments":{"path":"README.md"}}\n```'
            else:
                yield "完成。"

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    visible: list[str] = []
    result = TuLAgent(settings(tmp_path), mode="root", client=StreamingFencedToolClient()).run(
        "读取 README",
        stream=True,
        on_delta=visible.append,
    )
    assert result.answer == "完成。"
    assert "".join(visible) == "完成。"
    assert "```json" not in "".join(visible)


def test_streamed_tool_call_with_preface_is_not_printed_as_visible_delta(tmp_path: Path):
    class StreamingPrefaceToolClient:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, _messages):
            self.calls += 1
            if self.calls == 1:
                yield "我先继续检查。\n\n"
                yield '```json\n{"tool":"read_file","arguments":{"path":"README.md"}}\n```'
            else:
                yield "完成。"

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    visible: list[str] = []
    result = TuLAgent(settings(tmp_path), mode="root", client=StreamingPrefaceToolClient()).run(
        "读取 README",
        stream=True,
        on_delta=visible.append,
    )
    assert result.answer == "完成。"
    assert "".join(visible) == "完成。"
    assert "我先继续检查" not in "".join(visible)
    assert '{"tool"' not in "".join(visible)


def test_initial_messages_keep_large_system_prompt_cacheable(tmp_path: Path):
    SkillStore(tmp_path).create("repo-debug", "Debug this repository.", "Run tests.")
    agent = TuLAgent(settings(tmp_path), client=FakeClient(["done"]))
    initial = agent._initial_messages()
    assert [message.role for message in initial] == ["system", "system"]
    assert "Available tools:" in initial[0].content
    assert "cf题" in initial[0].content
    assert "Use delegate_agent proactively" in initial[0].content
    assert "repo-debug" not in initial[0].content
    assert "repo-debug" in initial[1].content


def test_tool_result_message_has_stable_prefix():
    assert tool_result_message("run_shell", '{"ok": true}').startswith('TOOL_RESULT name=run_shell\n{"ok": true}')
    assert tool_result_message("delegate_agent", '{"ok": true, "name": "reviewer"}').startswith('SUBAGENT_RESULT name=reviewer')


def test_tool_result_content_is_trimmed_with_head_and_tail():
    content = "a" * 40000 + "TAIL"
    trimmed = trim_tool_content(content, max_chars=1000)
    assert len(trimmed) < 1400
    assert "[tool output trimmed" in trimmed
    assert "TAIL" in trimmed


def test_agent_continues_after_assistant_promises_next_tool(tmp_path: Path):
    class ContinueClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"write_file","arguments":{"path":"login.html","content":"ok"}}'
            if self.calls == 2:
                return "文件已写入。接下来继续执行后续步骤：检查网络环境、启动服务器、验证运行状态。"
            if self.calls == 3:
                assert "you did not request a tool" in messages[-1].content.lower()
                return '{"tool":"start_service","arguments":{"name":"login","command":"python3 -m http.server 8097"}}'
            return "服务器已启动。"

    result = TuLAgent(settings(tmp_path), mode="root", client=ContinueClient()).run("写登录 HTML，然后启动服务")
    assert result.answer == "服务器已启动。"
    assert result.rounds == 4


def test_goal_mode_does_not_stop_on_intermediate_answer(tmp_path: Path):
    class GoalClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return "我先检查一下。"
            if self.calls == 2:
                assert "Active goal" in messages[-1].content
                return '{"tool":"write_file","arguments":{"path":"done.txt","content":"ok"}}'
            return "目标已完成：done.txt 已写入。"

    result = TuLAgent(settings(tmp_path), mode="root", client=GoalClient()).run(
        "开始",
        goal="写出 done.txt",
    )
    assert result.answer == "目标已完成：done.txt 已写入。"
    assert result.rounds == 3
    assert (tmp_path / "done.txt").read_text(encoding="utf-8") == "ok"


def test_goal_mode_allows_explicit_block(tmp_path: Path):
    result = TuLAgent(settings(tmp_path), mode="root", client=FakeClient(["被阻塞：缺少目标路径。"])).run(
        "开始",
        goal="完成未知文件",
    )
    assert "被阻塞" in result.answer


def test_agent_retries_after_tool_failure_when_model_stops(tmp_path: Path):
    class RetryClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"read_file","arguments":{"path":"missing.txt"}}'
            if self.calls == 2:
                return "读取失败，文件不存在。"
            if self.calls == 3:
                assert "tool failed" in messages[-1].content.lower()
                return '{"tool":"list_files","arguments":{"path":"."}}'
            return "已改为列目录确认文件不存在。"

    result = TuLAgent(settings(tmp_path), mode="root", client=RetryClient()).run("读取 missing.txt，如果失败就检查目录")
    assert result.answer == "已改为列目录确认文件不存在。"
    assert result.rounds == 4
    session_text = "\n".join(message.content for message in SessionStore(tmp_path).load(result.session_id).messages)
    assert RECOVER_AFTER_TOOL_FAILURE_PROMPT not in session_text


def test_internal_automation_prompts_are_filtered_from_context():
    messages = [
        Message("user", "真实用户输入"),
        Message("user", RECOVER_AFTER_TOOL_FAILURE_PROMPT),
        Message("assistant", "回答"),
    ]
    filtered = filter_internal_automation_messages(messages)
    assert [message.content for message in filtered] == ["真实用户输入", "回答"]


def test_complex_task_gets_private_execution_hint(tmp_path: Path):
    class InspectClient:
        def chat(self, messages):
            assert "Private execution hint" in messages[-1].content
            assert "delegate_agent" in messages[-1].content
            return "ok"

    result = TuLAgent(settings(tmp_path), mode="root", client=InspectClient()).run("写一个 HTML，然后启动服务，再检查端口并验证公网访问")
    assert result.answer == "ok"


def test_agent_finalizes_instead_of_pausing_after_tool_limit(tmp_path: Path):
    class LimitClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls <= 2:
                return '{"tool":"read_file","arguments":{"path":"README.md"}}'
            assert "tool round limit" in messages[-1].content.lower()
            return "工具轮数已到。README 已读取，但还没完成更多验证。"

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    result = TuLAgent(settings(tmp_path), mode="root", client=LimitClient()).run("连续检查", max_tool_rounds=2)
    assert result.answer == "工具轮数已到。README 已读取，但还没完成更多验证。"
    assert "Paused after tool execution" not in result.answer


def test_promises_more_work_detection_is_narrow():
    assert promises_more_work("接下来继续执行后续步骤：检查网络环境、启动服务器、验证运行状态。")
    assert not promises_more_work("文件已写入成功。")
    assert not promises_more_work("已经完成。端口 8097 正在运行。")


def test_agent_can_continue_search_after_empty_result(tmp_path: Path):
    class SearchRetryClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"web_search","arguments":{"query":"美国 近况","max_results":5}}'
            if self.calls == 2:
                assert "no web search results parsed" in messages[-1].content
                return '{"tool":"web_search","arguments":{"query":"美国 最新新闻 2026","max_results":5}}'
            assert "Reuters" in messages[-1].content
            return "美国近况总结。"

    class FakeSearchTools:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0

        def run(self, name, arguments):
            self.calls += 1
            assert name == "web_search"
            if self.calls == 1:
                return type("Result", (), {"to_message": lambda _self: '{"ok": false, "output": "no web search results parsed"}'})()
            return type("Result", (), {"to_message": lambda _self: '{"ok": true, "output": "- Reuters\\n  https://example.com\\n  news"}'})()

    agent = TuLAgent(settings(tmp_path), mode="root", client=SearchRetryClient())
    agent.tools = FakeSearchTools()
    result = agent.run("搜索美国近况用必应")
    assert result.answer == "美国近况总结。"
    assert result.rounds == 3


def test_question_mark_only_goes_to_model_but_ignores_tools(tmp_path: Path):
    class QuestionClient:
        def __init__(self):
            self.calls = 0

        def chat(self, _messages):
            self.calls += 1
            return '{"tool":"list_files","arguments":{"path":"."}}'

    client = QuestionClient()
    result = TuLAgent(settings(tmp_path), client=client).run("？")
    assert client.calls == 1
    assert result.rounds == 1
    assert result.answer == '{"tool":"list_files","arguments":{"path":"."}}'
    transcript = next((tmp_path / ".deepseek-tulagent" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "TOOL_RESULT" not in transcript
    assert is_question_mark_only("???") is True


def test_agent_executes_action_bash_block_instead_of_fake_execution(tmp_path: Path):
    client = FakeClient([
        "我现在检查。\n\n```bash\nprintf repo-ok\n```",
        "工具结果是 repo-ok。",
    ])
    result = TuLAgent(settings(tmp_path), mode="root", client=client).run("检查仓库")
    transcript = next((tmp_path / ".deepseek-tulagent" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "TOOL_RESULT name=run_shell" in transcript
    assert "repo-ok" in transcript
    assert result.answer == "工具结果是 repo-ok。"


def test_tool_result_is_sent_as_user_context_not_tool_role(tmp_path: Path):
    class InspectingClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"read_file","arguments":{"path":"README.md"}}'
            assert messages[-1].role == "user"
            assert "TOOL_RESULT name=read_file" in messages[-1].content
            return "done"

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    result = TuLAgent(settings(tmp_path), mode="root", client=InspectingClient()).run("read")
    assert result.answer == "done"


def test_stop_after_tool_does_not_call_model_again(tmp_path: Path):
    class OneToolClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            return '{"tool":"read_file","arguments":{"path":"README.md"}}'

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    client = OneToolClient()
    result = TuLAgent(settings(tmp_path), mode="root", client=client).run("read", stop_after_tool=True)
    assert client.calls == 1
    assert result.answer == ""


def test_deepseek_v4_model_aliases():
    assert resolve_model("pro") == "deepseek-v4-pro"
    assert resolve_model("v4-flash") == "deepseek-v4-flash"
    assert resolve_model("deepseek-v4-pro") == "deepseek-v4-pro"


def test_doctor_reports_default_v4_pro(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config-home"))
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 2
    assert '"model": "deepseek-v4-flash"' in out


def test_approval_policy_maps_codex_style_modes():
    assert ApprovalPolicy.from_mode("plan").allow_read is True
    assert ApprovalPolicy.from_mode("plan").allow_write is False
    assert ApprovalPolicy.from_mode("agent").require_confirmation is True
    assert ApprovalPolicy.from_mode("yolo").require_confirmation is False


def test_thinking_mode_resolves_model_and_budget():
    fast = ThinkingMode.resolve("fast")
    deep = ThinkingMode.resolve("deep")
    ultra = ThinkingMode.resolve("ultra")
    assert fast.model_hint == "deepseek-v4-flash"
    assert fast.max_tokens == 384000
    assert deep.max_tokens == 384000
    assert ultra.max_tokens == 384000
    assert ultra.reasoning_effort == "max"
    assert deep.deliberation_passes > 0
    assert deep.system_hint


def test_deepseek_payload_includes_thinking_controls(tmp_path: Path):
    settings_obj = settings(tmp_path).with_runtime(thinking_enabled=True, reasoning_effort="max")
    payload = {}
    apply_thinking_payload(payload, settings_obj)
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"

    payload = {}
    apply_thinking_payload(payload, settings(tmp_path).with_runtime(thinking_enabled=False))
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload

    payload = {}
    apply_thinking_payload(payload, settings(tmp_path).with_runtime(thinking_enabled=True))
    assert "thinking" in payload
    openai_settings = Settings(
        api_key="test",
        base_url="https://example.com",
        model="gpt-4o",
        workspace=tmp_path,
        max_tool_rounds=4,
        max_tokens=8192,
        request_timeout=180,
        default_mode="root",
        default_thinking="fast",
        provider_format="openai-compatible",
    )
    payload = {}
    apply_thinking_payload(payload, openai_settings)
    assert payload == {}


def test_codex_style_workspace_tools(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('deepseek')\n", encoding="utf-8")
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("plan"))
    listed = tools.run("list_files", {"path": "."}).output
    found = tools.run("search_text", {"query": "deepseek"}).output
    assert "src/app.py" in listed
    assert "src/app.py" in found


def test_search_text_uses_bounded_search(tmp_path: Path):
    (tmp_path / "a.txt").write_text("美国\n", encoding="utf-8")
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("search_text", {"query": "美国", "path": ".", "max_matches": 10, "timeout": 2})
    assert result.ok is True
    assert "a.txt" in result.output


def test_write_file_is_atomic_on_replace_failure(monkeypatch, tmp_path: Path):
    target = tmp_path / "file.txt"
    target.write_text("old", encoding="utf-8")
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))

    def fail_replace(src, dst):
        raise OSError("simulated replace crash")

    monkeypatch.setattr("deepseek_tulagent.tools.os.replace", fail_replace)
    try:
        tools.run("write_file", {"path": "file.txt", "content": "new"})
    except OSError:
        pass
    assert target.read_text(encoding="utf-8") == "old"


def test_run_shell_background_command_starts_service(tmp_path: Path):
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("run_shell", {"command": "python3 -m http.server 0 &", "name": "test-http"})
    assert result.ok is True
    assert "Started test-http" in result.output
    assert (tmp_path / ".deepseek-tulagent" / "services" / "test-http.pid").exists()
    tools.run("stop_service", {"name": "test-http"})


def test_agent_mode_requires_confirmation_for_shell(tmp_path: Path):
    client = FakeClient([
        '{"tool":"run_shell","arguments":{"command":"echo should-not-run"}}',
        "Shell was blocked.",
    ])
    result = TuLAgent(settings(tmp_path), mode="agent", client=client).run("run shell")
    transcript = next((tmp_path / ".deepseek-tulagent" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "confirmation required" in transcript
    assert result.answer == "Shell was blocked."


def test_yolo_mode_auto_approves_shell(tmp_path: Path):
    client = FakeClient([
        '{"tool":"run_shell","arguments":{"command":"echo ran"}}',
        "Shell ran.",
    ])
    result = TuLAgent(settings(tmp_path), mode="yolo", client=client).run("run shell")
    transcript = next((tmp_path / ".deepseek-tulagent" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "ran" in transcript
    assert result.answer == "Shell ran."


def test_agent_mode_can_approve_selected_tool(tmp_path: Path):
    client = FakeClient([
        '{"tool":"run_shell","arguments":{"command":"echo approved"}}',
        "Shell approved.",
    ])
    result = TuLAgent(
        settings(tmp_path),
        mode="agent",
        client=client,
        approve=lambda name, args: name == "run_shell",
    ).run("run shell")
    transcript = next((tmp_path / ".deepseek-tulagent" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "approved" in transcript
    assert result.answer == "Shell approved."


def test_trusted_mode_allows_download_tool_when_approved(tmp_path: Path):
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("trusted"))
    assert "download_url" in tools.names
    assert "web_search" in tools.names


def test_clone_repo_rejects_non_empty_target(tmp_path: Path):
    target = tmp_path / "repo"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("clone_repo", {"repo": "ffffff233/deepseek-tulagent", "path": "repo"})
    assert result.ok is False
    assert "not empty" in result.output
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_windows_style_workspace_path_is_normalized_on_posix(tmp_path: Path):
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("write_file", {"path": r"D:\deepseek项目\open-design\README.md", "content": "ok"})
    assert result.ok is True
    assert (tmp_path / "deepseek项目" / "open-design" / "README.md").read_text(encoding="utf-8") == "ok"


def test_workspace_absolute_path_inside_workspace_is_allowed(tmp_path: Path):
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    target = tmp_path / "absolute.txt"
    result = tools.run("write_file", {"path": str(target), "content": "ok"})
    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "ok"


def test_clone_repo_uses_github_archive_fallback(monkeypatch, tmp_path: Path):
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("open-design-main/README.md", "hello archive")

    clone_commands: list[list[str]] = []
    requested_urls: list[str] = []

    def fake_run(command, **_kwargs):
        clone_commands.append(command)
        return subprocess.CompletedProcess(command, 128, "", "clone failed")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return archive_bytes.getvalue()

    def fake_urlopen(request, timeout=0):
        requested_urls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr("deepseek_tulagent.tools.subprocess.run", fake_run)
    monkeypatch.setattr("deepseek_tulagent.tools.urllib.request.urlopen", fake_urlopen)

    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("clone_repo", {"repo": "https://github.com/nexu-io/open-design.git", "path": "open-design", "branch": "main"})

    assert result.ok is True
    assert (tmp_path / "open-design" / "README.md").read_text(encoding="utf-8") == "hello archive"
    assert any("github.com/nexu-io/open-design.git" in command for command in clone_commands[0])
    assert requested_urls[0] == "https://github.com/nexu-io/open-design/archive/refs/heads/main.zip"
    assert "archive fallback" in result.output


def test_clone_repo_accepts_repo_url_argument_alias(monkeypatch, tmp_path: Path):
    clone_commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        clone_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr("deepseek_tulagent.tools.subprocess.run", fake_run)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("clone_repo", {"repo/url": "https://github.com/esengine/DeepSeek-Reasonix", "path": str(tmp_path / "Reasonix")})
    assert result.ok is True
    assert clone_commands[0][-2] == "https://github.com/esengine/DeepSeek-Reasonix.git"


def test_system_prompt_mentions_clone_repo(tmp_path: Path):
    prompt = TuLAgent(settings(tmp_path), client=FakeClient(["ok"]))._system_prompt()
    assert "clone_repo(repo or url, path, branch?, timeout?)" in prompt
    assert "prefer clone_repo over manual git clone" in prompt
    assert "Windows paths" in prompt


def test_normalize_bing_redirect_url():
    url = "https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9uZXdzP2E9MQ"
    assert normalize_bing_url(url) == "https://example.com/news?a=1"


def test_skill_store_discovers_and_creates_workspace_skills(tmp_path: Path):
    store = SkillStore(tmp_path, home=tmp_path / "home")
    created = store.create("repo-debug", "Use when debugging this repository.", "Run tests first.")
    assert created.name == "repo-debug"
    skills = store.list()
    assert [skill.name for skill in skills] == ["repo-debug"]
    assert "debugging this repository" in skills[0].description


def test_root_mode_has_no_confirmation_gate():
    policy = ApprovalPolicy.from_mode("root")
    assert policy.allow_network is True
    assert policy.require_confirmation is False


def test_settings_read_local_config_file(monkeypatch, tmp_path: Path):
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.json").write_text(
        json.dumps({
            "api_key": "sk-test",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    settings_obj = get_settings()
    assert settings_obj.api_key == "sk-test"
    assert settings_obj.model == "deepseek-v4-flash"
    assert settings_obj.default_mode == "root"
    assert settings_obj.max_tool_rounds == 256


def test_empty_cli_defaults_to_root_fast_start(monkeypatch, tmp_path: Path):
    captured = {}
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config"))

    def fake_interactive(settings_obj, mode, thinking, yes, resume=None):
        captured["mode"] = mode
        captured["thinking"] = thinking
        captured["resume"] = resume
        return 0

    monkeypatch.setattr("deepseek_tulagent.cli.interactive", fake_interactive)
    assert main([]) == 0
    assert captured == {"mode": "root", "thinking": "fast", "resume": None}


def test_cli_desktop_command_invokes_desktop(monkeypatch):
    import deepseek_tulagent.desktop.app as desktop

    called = {}
    monkeypatch.setattr(desktop, "main", lambda: called.setdefault("ok", True))
    assert main(["desktop"]) == 0
    assert called["ok"] is True


def test_desktop_api_boot_and_runtime(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    api = desktop.DesktopApi()
    boot = api.boot()
    assert boot["version"]
    assert boot["mode"] == "root"
    assert "fast" in boot["thinkingModes"]

    updated = api.set_runtime({"mode": "plan", "thinking": "deep", "model": "deepseek-v4-pro"})
    assert updated["mode"] == "plan"
    assert updated["thinking"] == "deep"
    assert updated["model"] == "deepseek-v4-pro"
    assert updated["running"] is False


def test_desktop_api_rejects_parallel_turn(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api._running = True
    result = api.send({"prompt": "hello"})
    assert result == {"ok": False, "error": "turn already running"}
    cancelled = api.cancel()
    assert cancelled["ok"] is True
    assert cancelled["running"] is True


def test_desktop_upload_saves_file(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    result = api.save_upload({"name": "../hello.txt", "content": "data:text/plain;base64,aGVsbG8="})
    assert result["ok"] is True
    assert Path(result["path"]).read_text(encoding="utf-8") == "hello"
    assert ".." not in result["name"]


def test_desktop_configure_merges_existing_key(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config-home"))
    merge_file_config({"api_key": "sk-old", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"})
    api = desktop.DesktopApi()
    result = api.configure({"baseUrl": "https://example.com/v1", "model": "gpt-4o", "providerFormat": "openai-compatible"})
    settings_obj = get_settings()
    assert settings_obj.api_key == "sk-old"
    assert settings_obj.base_url == "https://example.com/v1"
    assert settings_obj.model == "gpt-4o"
    assert settings_obj.provider_format == "openai-compatible"
    assert result["providerFormat"] == "openai-compatible"


def test_desktop_manual_compact(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.desktop.app as desktop
    from deepseek_tulagent.messages import Message
    from deepseek_tulagent.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="s")
    api.session.messages = [Message("system", "system")] + [Message("user", "old " + "x" * 200) for _ in range(20)]
    result = api.compact()
    assert result["ok"] is True
    assert result["after"] > 0
    assert len(api.session.messages) < 21
    assert "Auto-compressed earlier conversation context" in api.session.messages[1].content
    assert isinstance(result["messages"], list)


def test_desktop_session_metadata_pin_and_rename(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.desktop.app as desktop
    from deepseek_tulagent.messages import Message
    from deepseek_tulagent.session import Session, SessionStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config-home"))
    session = Session(tmp_path, session_id="abc")
    session.append(Message("user", "请检查这个项目并修复测试失败的问题"))
    api = desktop.DesktopApi()
    renamed = api.rename_session("abc", "项目测试修复")
    assert renamed["ok"] is True
    pinned = api.pin_session("abc", True)
    assert pinned["ok"] is True
    rows = SessionStore(tmp_path).list()
    assert rows[0]["title"] == "项目测试修复"
    assert rows[0]["pinned"] is True


def test_session_title_from_text():
    from deepseek_tulagent.session import session_title_from_text

    assert session_title_from_text("  你好   世界  ") == "你好 世界"
    assert session_title_from_text("") == "未命名会话"


def test_desktop_event_parser():
    from deepseek_tulagent.desktop.app import parse_agent_event

    assert parse_agent_event("tool run_shell command=ls") == {"kind": "tool", "name": "run_shell", "detail": "command=ls"}
    assert parse_agent_event("thinking pass 1/2")["kind"] == "thinking"
    assert parse_agent_event("skill repo-debug")["kind"] == "skill"
    assert parse_agent_event("done read_file") == {"kind": "done", "name": "read_file", "detail": ""}


def test_session_handoff_prints_resume_command(capsys):
    from deepseek_tulagent.cli import print_session_handoff

    print_session_handoff("abc-123")
    err = capsys.readouterr().err
    assert "[session] abc-123" in err
    assert "deepseekTul start --resume abc-123" in err


def test_slash_palette_prints_commands_and_tools(tmp_path: Path, monkeypatch, capsys):
    from deepseek_tulagent.cli import print_palette

    monkeypatch.chdir(tmp_path)
    print_palette(settings(tmp_path))
    out = capsys.readouterr().out
    assert "Command Palette" in out
    assert "/mode <name>" in out
    assert "/subagents" in out
    assert "Tools" in out
    assert "delegate_agent" in out
    assert "write_file" in out


def test_compact_history_hides_tool_noise(capsys):
    from deepseek_tulagent.cli import print_recent_session_messages
    from deepseek_tulagent.messages import Message
    from deepseek_tulagent.session import Session

    session = Session(Path("/tmp"), session_id="s")
    session.messages = [
        Message("assistant", '{"tool":"run_shell","arguments":{}}'),
        Message("assistant", '你说得对，我继续检查。\n\n```json\n{"tool":"run_shell","arguments":{"command":"file app"}}\n```'),
        Message("user", "TOOL_RESULT name=run_shell\n{}"),
        Message("user", RECOVER_AFTER_TOOL_FAILURE_PROMPT),
        Message("user", "在本机上开一个新端口。"),
        Message("assistant", "服务已经启动。"),
    ]
    print_recent_session_messages(session)
    out = capsys.readouterr().out
    assert "TOOL_RESULT" not in out
    assert "previous tool failed" not in out.lower()
    assert '{"tool"' not in out
    assert "你说得对" not in out
    assert "在本机上开一个新端口" in out


def test_session_handoff_is_not_printed_after_run(monkeypatch, tmp_path: Path, capsys):
    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            from deepseek_tulagent.agent import AgentResult

            return AgentResult("abc-123", "done", 1)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("deepseek_tulagent.cli.TuLAgent", FakeAgent)
    code = main(["run", "--mode", "plan", "hello"])
    captured = capsys.readouterr()
    assert code == 0
    assert "done" in captured.out
    assert "[resume]" not in captured.err


def test_interactive_model_command_uses_picker(monkeypatch, tmp_path: Path, capsys):
    import deepseek_tulagent.cli as cli

    prompts = iter(["/model", "/exit"])
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config"))

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def ping(self):
            return {"model_available": True}

        def models(self):
            return ["deepseek-v4-flash", "deepseek-v4-pro"]

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "choose_palette", lambda rows, title="commands": rows[1][0])
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert "model set to deepseek-v4-pro" in out
    assert get_settings().model == "deepseek-v4-pro"


def test_interactive_think_command_uses_picker(monkeypatch, tmp_path: Path, capsys):
    import deepseek_tulagent.cli as cli

    prompts = iter(["/think", "/exit"])
    monkeypatch.setenv("DSTUL_CONFIG_HOME", str(tmp_path / "config"))

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def ping(self):
            return {"model_available": True}

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "choose_palette", lambda rows, title="commands": "deep")
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert "thinking set to deep" in out
    assert "model=deepseek-v4-flash" in out
    assert "internal_passes=2" in out
    assert get_settings().default_thinking == "deep"


def test_interactive_subagents_command_returns_to_prompt(monkeypatch, tmp_path: Path, capsys):
    import deepseek_tulagent.cli as cli

    prompts = iter(["/subagents", "/exit"])

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def ping(self):
            return {"model_available": True}

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "maybe_prompt_update", lambda: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert "Subagents" in out
    assert "delegate_agent" in out


def test_interactive_cancel_command_returns_to_normal_prompt(monkeypatch, tmp_path: Path, capsys):
    import deepseek_tulagent.cli as cli

    prompts = iter(["/cancel", "/exit"])

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def ping(self):
            return {"model_available": True}

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "maybe_prompt_update", lambda: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert "back to normal input" in out
    assert "mode=root, think=fast" in out


def test_interactive_goal_command_passes_goal_to_agent(monkeypatch, tmp_path: Path, capsys):
    import deepseek_tulagent.cli as cli

    prompts = iter(["/goal 完成部署", "继续", "/exit"])
    captured = {}

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def ping(self):
            return {"model_available": True}

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **kwargs):
            from deepseek_tulagent.agent import AgentResult
            from deepseek_tulagent.messages import Message
            from deepseek_tulagent.session import Session

            captured["goal"] = kwargs.get("goal")
            session = Session(tmp_path, session_id="abc-123")
            session.append(Message("assistant", "目标已完成。"))
            return AgentResult(session.session_id, "目标已完成。", 1)

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)
    monkeypatch.setattr(cli, "TuLAgent", FakeAgent)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert "goal     : 完成部署" in out
    assert captured["goal"] == "完成部署"


def test_interactive_line_mode_streams_agent_output(monkeypatch, tmp_path: Path, capsys):
    import deepseek_tulagent.cli as cli

    prompts = iter(["检查", "/exit"])
    captured = {}

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def ping(self):
            return {"model_available": True}

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **kwargs):
            from deepseek_tulagent.agent import AgentResult
            from deepseek_tulagent.messages import Message
            from deepseek_tulagent.session import Session

            captured["stream"] = kwargs.get("stream")
            kwargs["on_delta"]("流")
            kwargs["on_delta"]("式")
            session = Session(tmp_path, session_id="abc-123")
            session.append(Message("assistant", "流式"))
            return AgentResult(session.session_id, "流式", 1)

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)
    monkeypatch.setattr(cli, "TuLAgent", FakeAgent)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert captured["stream"] is True
    assert "流式" in out


def test_interactive_line_mode_uses_spinner_until_first_delta(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.cli as cli

    prompts = iter(["检查", "/exit"])
    events: list[str] = []

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def ping(self):
            return {"model_available": True}

    class FakeSpinner:
        def __init__(self, label):
            self.label = label

        def __enter__(self):
            events.append(f"enter:{self.label}")
            return self

        def __exit__(self, *_args):
            events.append("exit")

        def stop(self):
            events.append("stop")

        @classmethod
        def clear_active_line(cls):
            events.append("clear")

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **kwargs):
            from deepseek_tulagent.agent import AgentResult
            from deepseek_tulagent.messages import Message
            from deepseek_tulagent.session import Session

            events.append("run")
            kwargs["on_delta"]("ok")
            session = Session(tmp_path, session_id="abc-123")
            session.append(Message("assistant", "ok"))
            return AgentResult(session.session_id, "ok", 1)

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)
    monkeypatch.setattr(cli, "ThinkingSpinner", FakeSpinner)
    monkeypatch.setattr(cli, "TuLAgent", FakeAgent)

    assert cli.interactive(settings(tmp_path), "root", "fast", True) == 0
    assert events[:3] == ["enter:thinking:fast", "run", "stop"]
    assert "clear" not in events[:4]


def test_interactive_startup_prints_version(monkeypatch, tmp_path: Path, capsys):
    import deepseek_tulagent.cli as cli

    prompts = iter(["/exit"])

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def ping(self):
            return {"model_available": True}

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "maybe_prompt_update", lambda: print("version  : test (latest)"))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert "app      : DeepSeek TuLAgent" in out
    assert "version  : test (latest)" in out


def test_auto_thinking_uses_model_choice(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.cli as cli

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def chat(self, _messages):
            return "deep"

    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)
    selected = cli.choose_auto_thinking(settings(tmp_path), "hard debugging task")
    assert selected.name == "deep"


def test_internal_thinking_runs_extra_model_pass(tmp_path: Path):
    class ThinkingClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls <= 2:
                return f"private note {self.calls}"
            assert "Private model deliberation notes" in messages[-1].content
            return "final answer"

    client = ThinkingClient()
    result = TuLAgent(settings(tmp_path), thinking="deep", client=client).run("solve")
    assert client.calls == 3
    assert result.answer == "final answer"


def test_context_compaction_keeps_recent_messages(monkeypatch):
    from deepseek_tulagent.messages import Message
    import deepseek_tulagent.agent as agent

    messages = [Message("system", "system")]
    messages.extend(Message("user", "old " + str(index) + " " + ("x" * 200)) for index in range(20))
    monkeypatch.setattr(agent, "context_window_tokens", lambda _model: 200)

    compacted = compact_context_messages(messages, "tiny", force=True)
    assert len(compacted) < len(messages)
    assert "Auto-compressed earlier conversation context" in compacted[1].content
    assert "old 19" in compacted[-1].content


def test_auto_context_compaction_can_be_disabled(monkeypatch):
    from deepseek_tulagent.messages import Message
    import deepseek_tulagent.agent as agent

    messages = [Message("system", "system")]
    messages.extend(Message("user", "old " + ("x" * 200)) for _ in range(20))
    monkeypatch.setattr(agent, "context_window_tokens", lambda _model: 200)
    monkeypatch.setenv("DSTUL_AUTO_COMPACT", "0")

    assert compact_context_messages(messages, "tiny") is messages


def test_update_version_comparison():
    from deepseek_tulagent.updates import is_newer, normalize_version

    assert normalize_version("v0.1.2") == "0.1.2"
    assert is_newer("v0.1.2", "0.1.1") is True
    assert is_newer("v0.1.1", "0.1.1") is False


def test_update_refuses_dirty_source_tree(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.updates as updates

    subprocesses = [
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ]
    for command in subprocesses:
        import subprocess

        subprocess.run(command, cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "dirty.txt").write_text("dirty", encoding="utf-8")
    monkeypatch.setattr(updates, "source_root", lambda: tmp_path)

    ok, output = updates.update_to("0.1.2")
    assert ok is False
    assert "avoid overwriting user edits" in output


def test_update_non_git_install_uses_tarball_not_git(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.updates as updates
    import sys

    captured = {}
    monkeypatch.setattr(updates, "source_root", lambda: tmp_path)

    def fake_run(command, **kwargs):
        captured["command"] = command
        return type("Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    ok, output = updates.update_to("0.1.2")
    assert ok is True
    assert captured["command"][0] == sys.executable
    assert any("archive/refs/tags/v0.1.2.tar.gz" in str(part) for part in captured["command"])
    assert not any(str(part).startswith("git+") for part in captured["command"])


def test_windows_terminal_module_fallbacks(monkeypatch):
    import builtins
    import deepseek_tulagent.ui as ui

    monkeypatch.setattr(ui, "termios", None)
    monkeypatch.setattr(ui, "tty", None)
    monkeypatch.setattr(builtins, "input", lambda prompt: prompt + "hello")

    assert ui.read_composer("p> ") == "p> hello"
    assert ui.choose_palette([("/model", "choose model")]) is None


def test_update_git_failure_falls_back_to_tarball(monkeypatch, tmp_path: Path):
    import deepseek_tulagent.updates as updates

    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.setattr(updates, "source_root", lambda: tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "status", "--porcelain"]:
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if command[:2] == ["git", "fetch"]:
            return type("Completed", (), {"returncode": 128, "stdout": "", "stderr": "proxy error"})()
        return type("Completed", (), {"returncode": 0, "stdout": "pip ok", "stderr": ""})()

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    ok, output = updates.update_to("0.1.2")
    assert ok is True
    assert "tarball fallback succeeded" in output
    assert any(any("archive/refs/tags/v0.1.2.tar.gz" in str(part) for part in command) for command in calls)


def test_update_command_runs_updater(monkeypatch, capsys):
    import deepseek_tulagent.cli as cli
    from deepseek_tulagent.updates import UpdateInfo

    monkeypatch.setattr(cli, "check_for_update", lambda current, timeout=5.0: UpdateInfo(current, "0.1.2", "url"))
    monkeypatch.setattr(cli, "update_to", lambda version: (True, f"updated {version}"))

    assert cli.update_command(check_only=False) == 0
    out = capsys.readouterr().out
    assert "0.1.0" not in out
    assert "updated 0.1.2" in out


def test_session_store_lists_and_loads_messages(tmp_path: Path):
    client = FakeClient(["hello"])
    result = TuLAgent(settings(tmp_path), mode="plan", client=client).run("say hello")
    store = SessionStore(tmp_path)
    listed = store.list()
    assert listed[0]["session_id"] == result.session_id
    loaded = store.load(result.session_id)
    assert [message.role for message in loaded.messages] == ["system", "system", "user", "assistant"]


def test_resume_global_session_appends_to_original_file(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home_session_dir = home / ".deepseek-tulagent" / "sessions"
    home_session_dir.mkdir(parents=True)
    session_id = "00000000-0000-4000-8000-000000000001"
    session_file = home_session_dir / f"{session_id}.jsonl"
    session_file.write_text(
        json.dumps({"session_id": session_id, "created_at": "now", "message": {"role": "user", "content": "old", "name": None}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    workspace.mkdir()
    loaded = SessionStore(workspace).load(session_id)
    loaded.append(type(loaded.messages[0])(role="assistant", content="new"))
    assert "new" in session_file.read_text(encoding="utf-8")
    assert not (workspace / ".deepseek-tulagent" / "sessions" / f"{session_id}.jsonl").exists()


class FakeWindow:
    def __init__(self, height=12, width=48):
        self.height = height
        self.width = width

    def erase(self): pass
    def getmaxyx(self): return self.height, self.width
    def attron(self, *_): pass
    def attroff(self, *_): pass
    def addnstr(self, y, x, text, n, *args):
        if y >= self.height or x >= self.width - 1 or n > self.width - x:
            raise Exception("unsafe write")
    def move(self, y, x):
        if y >= self.height or x >= self.width:
            raise Exception("unsafe move")
    def refresh(self): pass


def test_tui_draw_avoids_bottom_right_curses_error(monkeypatch):
    monkeypatch.setattr("deepseek_tulagent.tui.curses.color_pair", lambda _n: 0)
    state = TuiState(model="deepseek-v4-flash", mode="root", thinking="fast")
    ChatTui(state, lambda _text, _state: None, lambda _cmd, _state: False)._draw(FakeWindow())


def test_tui_ctrl_c_exits_when_idle():
    state = TuiState(model="deepseek-v4-flash", mode="root", thinking="fast", input_text="正在输入")
    tui = ChatTui(state, lambda _text, _state: None, lambda _cmd, _state: False)
    assert tui._handle_key(3) is True
    assert state.status == "exit"


def test_tui_ctrl_c_cancels_running_turn_not_exit():
    state = TuiState(model="deepseek-v4-flash", mode="root", thinking="fast", input_text="正在输入", status="thinking")
    tui = ChatTui(state, lambda _text, _state: None, lambda _cmd, _state: False)
    assert tui._handle_key(3) is False
    assert state.status == "cancelled"


def test_slash_filter_matches_command_initial():
    items = [("/model", "list models"), ("/think fast", "fast"), ("/mode root", "root")]
    assert filter_slash_items(items, "m")[0][0] == "/model"
    assert filter_slash_items(items, "mo")[0][0] == "/model"
    assert filter_slash_items(items, "t")[0][0] == "/think fast"


def test_slash_items_include_manual_compact(tmp_path: Path):
    import deepseek_tulagent.cli as cli

    commands = [command for command, _description in cli.slash_items(settings(tmp_path))]
    assert "/compact" in commands
    assert commands.index("/goal ") < commands.index("/goal")


def test_slash_skill_selection_inserts_agent_prompt():
    assert slash_selection_insertion("/skill repo-debug") == "Use skill repo-debug: "
    assert slash_selection_insertion("/goal ") == "/goal "
    assert slash_selection_insertion("/goal") == "/goal "
    assert slash_selection_insertion("/model") is None


def test_agent_event_formatter_labels_tools():
    assert "run_shell" in format_agent_event("tool run_shell command=ls")
    assert "done" in format_agent_event("done run_shell")
    assert "subagent" in format_agent_event("subagent reviewer mode=plan")


def test_spinner_clear_active_line_is_safe_without_active_spinner():
    ThinkingSpinner.active = None
    ThinkingSpinner.clear_active_line()


def test_spinner_stop_is_idempotent(monkeypatch):
    spinner = ThinkingSpinner("thinking:test")
    cleared = []
    spinner.thread = object()  # type: ignore[assignment]
    ThinkingSpinner.active = spinner
    monkeypatch.setattr(spinner.stop_event, "set", lambda: None)
    monkeypatch.setattr(spinner, "clear_line", lambda: cleared.append("clear"))

    class FakeThread:
        def join(self, timeout=None):
            return None

    spinner.thread = FakeThread()  # type: ignore[assignment]
    spinner.stop()
    spinner.stop()
    assert cleared == ["clear"]


def test_slash_selected_window_scrolls_with_selection():
    assert selected_window_start(total=12, selected=0, window_size=6) == 0
    assert selected_window_start(total=12, selected=5, window_size=6) == 0
    assert selected_window_start(total=12, selected=6, window_size=6) == 1
    assert selected_window_start(total=12, selected=11, window_size=6) == 6


def test_redraw_composer_clears_entire_line(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    redraw_composer("prompt> ", list("abc"))
    text = output.getvalue()
    assert "\r\033[2K" in text
    assert text.endswith("prompt> abc")


def test_redraw_composer_handles_wide_chinese_text(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    redraw_composer("prompt> ", list("画画"))
    redraw_composer("prompt> ", list("画"))
    text = output.getvalue()
    assert text.count("\r\033[2K") == 2
    assert text.endswith("prompt> 画")


def test_tail_for_width_keeps_single_line_window():
    assert tail_for_width("abcdef", 4) == "…def"
    chinese = tail_for_width("画画画画", 5)
    assert chinese.startswith("…")
    assert display_width(chinese) <= 5


def test_plain_ui_uses_ascii_for_windows_safe_layout(monkeypatch, capsys):
    monkeypatch.setenv("DSTUL_PLAIN_UI", "1")
    print_box("Session", ["workspace /tmp/project", "model deepseek-v4-flash"])
    out = capsys.readouterr().out
    assert "[Session]" in out
    assert "╭" not in out
    assert "│" not in out
    assert "\033[" not in out

    assert format_agent_event("tool run_shell command=ls") == "  [tool] run_shell | command=ls"
    assert format_agent_event("done run_shell") == "  [done] run_shell"
    assert composer_prompt("deepseek-v4-flash", "root", "fast", "abcdef123456") == "[deepseek-v4-flash mode=root think=fast abcdef12] > "
    assert tail_for_width("abcdef", 4) == "...f"


def test_slash_select_draw_clips_to_terminal_width(monkeypatch):
    from deepseek_tulagent.ui import draw_slash_select

    for columns in (20, 42, 100):
        output = io.StringIO()
        monkeypatch.setattr("sys.stdout", output)
        monkeypatch.setattr("shutil.get_terminal_size", lambda _fallback, columns=columns: os.terminal_size((columns, 20)))
        lines = draw_slash_select(
            [
                ("/model", "choose model / show live DeepSeek models with a very long tail"),
                ("/mode root", "highest permission, all tools approved with a very long tail"),
            ],
            "",
            0,
        )
        text = re.sub(r"\033\[[0-9;]*[A-Za-z]", "", output.getvalue()).replace("\r", "")
        visible_lines = [line for line in text.splitlines() if line.strip()]
        assert lines == 5
        assert "\r\n" in output.getvalue()
        assert visible_lines
        assert all(len(line) <= columns for line in visible_lines)
        assert sum("/model" in line for line in visible_lines) == 1


def test_escape_suffix_reads_arrow_bytes_from_fd():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"[B")
        assert read_escape_suffix(read_fd) == "[B"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_raw_char_reads_complete_utf8_character():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, "你".encode("utf-8"))
        assert read_raw_char(read_fd) == "你"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_bracketed_paste_keeps_newlines_in_buffer():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, "第一行\n第二行\x1b[201~".encode("utf-8"))
        buffer: list[str] = []
        read_bracketed_paste(read_fd, buffer)
        assert "".join(buffer) == "第一行\n第二行"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_composer_display_collapses_multiline_paste_to_single_line():
    display = composer_display_text("第一行\n第二行\n第三行", 80)
    assert display == "[pasted 3 lines] 第三行"
    assert "\n" not in display


def test_composer_display_tails_long_multiline_paste():
    display = composer_display_text("a\n" + "x" * 80, 24)
    assert display.startswith("[pasted 2 lines] ")
    assert display_width(display) <= 24
    assert "\n" not in display


def test_newline_with_pending_input_is_treated_as_paste_not_submit():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"next")
        assert should_submit_newline(read_fd) is False
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_newline_without_pending_input_submits():
    read_fd, write_fd = os.pipe()
    try:
        assert should_submit_newline(read_fd) is True
    finally:
        os.close(read_fd)
        os.close(write_fd)
