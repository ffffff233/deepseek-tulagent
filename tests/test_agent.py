from __future__ import annotations

from pathlib import Path
import base64
import json
import io
import os
import re
import subprocess
import urllib.parse
import zipfile
from types import SimpleNamespace
import pytest

from deepseekfathom._core.agent import RECOVER_AFTER_TOOL_FAILURE_PROMPT, FathomAgent, compact_context_messages, context_window_info, context_window_tokens, estimate_message_tokens, filter_internal_automation_messages, is_question_mark_only, normalize_subagent_mode_and_thinking, normalize_subagent_specs, normalize_user_question, parse_tool_call, plainify_assistant_text, promises_more_work, trim_tool_content, tool_result_message
from deepseekfathom._core.capabilities import collect_capability_report
from deepseekfathom._core.cli import main
from deepseekfathom._core.config import Settings, get_settings, merge_file_config, resolve_model
from deepseekfathom._core.messages import Message
from deepseekfathom._core.policy import ApprovalPolicy, ThinkingMode
from deepseekfathom._core.provider import UsageStats, apply_anthropic_cache_control, apply_thinking_payload, cache_affinity_headers, extract_error_message, parse_usage_stats, prompt_cache_key
from deepseekfathom._core.session import Session, SessionStore
from deepseekfathom._core.skills import SkillStore
from deepseekfathom._core.tui import ChatTui, TuiState, render_messages, wrap_display_text
from deepseekfathom._core.ui import ThinkingSpinner, clip_visible, composer_display_text, composer_prompt, configure_utf8_stdio, display_width, filter_slash_items, format_agent_event, palette_footer_text, plain_terminal, print_box, read_bracketed_paste, read_composer, read_escape_suffix, read_raw_char, redraw_composer, selected_window_start, should_submit_newline, tail_for_width, slash_selection_insertion
from deepseekfathom._core.tools import ToolError, ToolRegistry, normalize_bing_url, normalize_duckduckgo_url


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


def test_parse_deepseek_dsml_tool_call_with_multiline_html():
    text = '''<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="write_file">
<｜｜DSML｜｜parameter name="content" string="true"><!doctype html>
<html><body><script>const game = true;</script></body></html></｜｜DSML｜｜parameter>
<｜｜DSML｜｜parameter name="path">C:\\Users\\admin\\Desktop\\snake.html</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>'''
    assert parse_tool_call(text) == (
        "write_file",
        {
            "content": '<!doctype html>\n<html><body><script>const game = true;</script></body></html>',
            "path": r"C:\Users\admin\Desktop\snake.html",
        },
    )


def test_explanatory_tool_json_example_is_not_executed():
    text = '''正确格式应该是纯 JSON 对象，比如：
```json
{"tool":"write_file","arguments":{"path":"...","content":"..."}}
```'''
    assert parse_tool_call(text) is None


def test_parse_provider_usage_stats():
    chat = parse_usage_stats(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 60},
            }
        },
        "upstream",
    )
    assert chat == UsageStats(
        input_tokens=100,
        output_tokens=20,
        cached_input_tokens=60,
        cache_miss_input_tokens=40,
        total_tokens=120,
        source="upstream",
    )

    responses = parse_usage_stats(
        {
            "response": {
                "usage": {
                    "input_tokens": 90,
                    "output_tokens": 10,
                    "total_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 30},
                }
            }
        },
        "upstream",
    )
    assert responses.input_tokens == 90
    assert responses.output_tokens == 10
    assert responses.cached_input_tokens == 30
    assert responses.cache_miss_input_tokens == 60
    assert responses.total_tokens == 100

    cached_gateway = parse_usage_stats(
        {
            "usage": {
                "prompt_tokens": 1236,
                "completion_tokens": 95,
                "prompt_tokens_details": {"cached_tokens": 140_000},
            }
        },
        "upstream",
    )
    assert cached_gateway.input_tokens == 141_236
    assert cached_gateway.cached_input_tokens == 140_000
    assert cached_gateway.cache_miss_input_tokens == 1236
    assert cached_gateway.total_tokens == 141_331

    deepseek_cache = parse_usage_stats(
        {"usage": {"prompt_tokens": 1236, "prompt_cache_hit_tokens": 140_000, "prompt_cache_miss_tokens": 1236}},
        "upstream",
    )
    assert deepseek_cache.input_tokens == 141_236
    assert deepseek_cache.cached_input_tokens == 140_000
    assert deepseek_cache.cache_miss_input_tokens == 1236

    deepseek_hit_only_gateway = parse_usage_stats(
        {"usage": {"prompt_tokens": 1236, "completion_tokens": 95, "prompt_cache_hit_tokens": 140_000}},
        "upstream",
    )
    assert deepseek_hit_only_gateway.input_tokens == 141_236
    assert deepseek_hit_only_gateway.cached_input_tokens == 140_000
    assert deepseek_hit_only_gateway.cache_miss_input_tokens == 1236

    gemini_cache = parse_usage_stats(
        {
            "usageMetadata": {
                "promptTokenCount": 141_236,
                "candidatesTokenCount": 95,
                "cachedContentTokenCount": 140_000,
                "totalTokenCount": 141_331,
            }
        },
        "upstream",
    )
    assert gemini_cache.input_tokens == 141_236
    assert gemini_cache.cached_input_tokens == 140_000
    assert gemini_cache.cache_miss_input_tokens == 1236
    assert gemini_cache.total_tokens == 141_331


def test_openai_native_tool_schema_and_stream_fragments_are_normalized():
    from deepseekfathom._core.provider import (
        append_openai_tool_call_deltas,
        finalize_openai_tool_call_deltas,
        openai_tool_definitions,
        parse_openai_tool_calls,
    )

    definitions = openai_tool_definitions(["read_file", "write_file"])
    assert [item["function"]["name"] for item in definitions] == ["read_file", "write_file"]
    assert definitions[0]["function"]["parameters"]["required"] == ["path"]

    parsed = parse_openai_tool_calls({
        "tool_calls": [{
            "id": "call-read",
            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
        }]
    })
    assert parsed[0].name == "read_file"
    assert parsed[0].arguments == {"path": "README.md"}
    assert parsed[0].call_id == "call-read"

    parts = {}
    append_openai_tool_call_deltas(parts, {"tool_calls": [{"index": 0, "id": "call-stream", "function": {"name": "read_", "arguments": '{"path":'}}]})
    append_openai_tool_call_deltas(parts, {"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": '"README.md"}'}}]})
    streamed = finalize_openai_tool_call_deltas(parts)
    assert streamed[0].name == "read_file"
    assert streamed[0].arguments == {"path": "README.md"}
    assert streamed[0].call_id == "call-stream"


def test_native_tool_history_preserves_call_ids_after_session_reload(tmp_path: Path):
    from deepseekfathom._core.agent import append_native_tool_call_record
    from deepseekfathom._core.provider import NativeToolCall, openai_messages

    session = Session(tmp_path, session_id="native-history")
    session.append(
        Message(
            "assistant",
            append_native_tool_call_record(
                "Checking both files.",
                [
                    NativeToolCall("read_file", {"path": "a.txt"}, "call_a"),
                    NativeToolCall("read_file", {"path": "b.txt"}, "call_b"),
                ],
            ),
        )
    )
    session.append(Message("user", 'TOOL_RESULT name=read_file\n{"ok":true,"output":"alpha"}'))
    session.append(Message("user", 'TOOL_RESULT name=read_file\n{"ok":true,"output":"beta"}'))

    payload = openai_messages(SessionStore(tmp_path).load(session.session_id).messages)
    assistant = next(message for message in payload if message.get("tool_calls"))
    results = [message for message in payload if message["role"] == "tool"]

    assert [call["id"] for call in assistant["tool_calls"]] == ["call_a", "call_b"]
    assert [message["tool_call_id"] for message in results] == ["call_a", "call_b"]
    assert [json.loads(message["content"])["output"] for message in results] == ["alpha", "beta"]


@pytest.mark.parametrize(
    ("provider_format", "expected"),
    [
        ("deepseek", True),
        ("openai", True),
        ("openai-responses", False),
        ("anthropic", False),
        ("gemini", False),
    ],
)
def test_client_reports_native_tools_only_for_chat_completions(
    tmp_path: Path,
    provider_format: str,
    expected: bool,
):
    from dataclasses import replace

    from deepseekfathom._core.provider import DeepSeekClient

    client = DeepSeekClient(replace(settings(tmp_path), provider_format=provider_format))
    assert client.supports_native_tools is expected


def test_agent_executes_multiple_native_tool_calls(tmp_path: Path):
    from deepseekfathom._core.provider import NativeToolCall

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")

    class NativeClient:
        supports_native_tools = True

        def __init__(self):
            self.calls = 0
            self.last_tool_calls = []
            self.tool_names = []

        def chat(self, _messages, *, tool_names=None):
            self.calls += 1
            self.tool_names = list(tool_names or [])
            if self.calls == 1:
                self.last_tool_calls = [
                    NativeToolCall("list_files", {"path": ".", "max_entries": 5}, "call-list"),
                    NativeToolCall("read_file", {"path": "README.md"}, "call-read"),
                ]
                return ""
            return "Both tools completed."

    client = NativeClient()
    result = FathomAgent(settings(tmp_path), client=client).run("inspect README", require_todo=False)
    loaded = SessionStore(tmp_path).load(result.session_id)
    tool_results = [message.content for message in loaded.messages if message.content.startswith("TOOL_RESULT")]
    assert result.answer == "Both tools completed."
    assert "read_file" in client.tool_names and "list_files" in client.tool_names
    assert len(tool_results) == 2
    assert any("hello" in content for content in tool_results)


def test_client_keeps_latest_usage_separate_from_cumulative(tmp_path: Path):
    from deepseekfathom._core.provider import DeepSeekClient

    client = DeepSeekClient(settings(tmp_path))
    client._record_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
    client._record_usage({"usage": {"prompt_tokens": 180, "completion_tokens": 30, "total_tokens": 210}})

    assert client.usage.input_tokens == 280
    assert client.usage.total_tokens == 330
    assert client.last_usage.input_tokens == 180
    assert client.last_usage.output_tokens == 30

    http = client._http()
    assert http.timeout.connect == 10.0
    assert http.timeout.read == 180.0
    client.close()
    assert http.is_closed is True

    anthropic = parse_usage_stats(
        {"usage": {"input_tokens": 80, "output_tokens": 12, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 25}},
        "upstream",
    )
    assert anthropic.input_tokens == 145
    assert anthropic.cached_input_tokens == 25
    assert anthropic.cache_miss_input_tokens == 120
    anthropic_stream = parse_usage_stats({"message": {"usage": {"input_tokens": 81, "cache_read_input_tokens": 26}}}, "upstream")
    assert anthropic_stream.input_tokens == 107
    assert anthropic_stream.cached_input_tokens == 26
    assert anthropic_stream.cache_miss_input_tokens == 81

    gemini = parse_usage_stats(
        {"usageMetadata": {"promptTokenCount": 70, "candidatesTokenCount": 8, "totalTokenCount": 78}},
        "upstream",
    )
    assert gemini.input_tokens == 70
    assert gemini.output_tokens == 8
    assert gemini.total_tokens == 78

    first_uncached = parse_usage_stats(
        {"usage": {"prompt_tokens": 100, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 0}}},
        "upstream",
    )
    assert first_uncached.cached_input_tokens == 0
    assert first_uncached.cache_miss_input_tokens == 100

    reasoning = parse_usage_stats(
        {
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 30,
                "completion_tokens_details": {"reasoning_tokens": 18},
            }
        },
        "upstream",
    )
    assert reasoning.reasoning_tokens == 18


def test_provider_cache_affinity_is_stable_and_non_secret(tmp_path: Path):
    cfg = settings(tmp_path)
    key1 = prompt_cache_key(cfg)
    key2 = prompt_cache_key(cfg)
    assert key1 == key2
    assert cfg.api_key not in key1
    assert cache_affinity_headers(cfg) == {"Session_id": key1}


def test_anthropic_cache_control_marks_stable_prefixes():
    payload = {
        "system": "stable system prompt",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ],
    }
    apply_anthropic_cache_control(payload)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][2]["content"] == "second"


def test_extract_provider_error_message_from_compatible_shapes():
    assert extract_error_message('{"error":{"message":"bad request","code":"invalid"}}') == "bad request (invalid)"
    assert extract_error_message('{"detail":[{"msg":"field required"},{"message":"bad type"}]}') == "field required; bad type"
    assert extract_error_message('{"error_description":"quota exceeded"}') == "quota exceeded"


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


def test_parse_xml_tool_call_variants():
    from deepseekfathom._core.agent import should_hold_stream_output, safe_stream_emit_length, strip_tool_call_display

    # Hermes/Qwen name+arguments JSON inside <tool_call> tags
    v1 = '<tool_call>\n{"name": "write_file", "arguments": {"path": "a.txt", "content": "hi"}}\n</tool_call>'
    assert parse_tool_call(v1) == ("write_file", {"path": "a.txt", "content": "hi"})

    # name-then-JSON form
    v2 = '<tool_call>write_file\n{"path": "b.txt", "content": "x"}\n</tool_call>'
    assert parse_tool_call(v2) == ("write_file", {"path": "b.txt", "content": "x"})

    # inline, {"tool":...} shape, with prose before it
    v4 = '我来写文件。\n<tool_call>{"tool":"run_shell","arguments":{"command":"ls"}}</tool_call>'
    assert parse_tool_call(v4) == ("run_shell", {"command": "ls"})

    # streaming: a leading '<' (possibly building toward <tool_call>) is held back
    assert should_hold_stream_output("<tool_call") is True
    assert should_hold_stream_output("<html>hi") is False

    # prose then a tool tag: only the prose is safe to emit
    t = "我来写文件。\n<tool_call>{\"name\":\"write_file\""
    assert t[: safe_stream_emit_length(t)] == "我来写文件。\n"

    # the raw tag never survives into displayed prose
    assert strip_tool_call_display(v4) == "我来写文件。"
    # a stream that ended mid-tag is scrubbed rather than shown raw
    assert "<tool_call" not in plainify_assistant_text('<tool_call>\n{"name":"write_file"')


def test_stream_holds_mid_line_tool_calls():
    """A tool call appended to the SAME line as prose must still be held back — only the
    prose before the marker is safe to stream."""
    from deepseekfathom._core.agent import safe_stream_emit_length

    # <tool_call> tag right after a sentence
    t1 = '好的，我来调用：<tool_call>{"name":"write_file","arguments":{}}'
    assert t1[: safe_stream_emit_length(t1)] == "好的，我来调用："

    # inline tool JSON mid-line
    t2 = '结果是这样 {"tool":"run_shell","arguments":{"command":"ls"}}'
    assert t2[: safe_stream_emit_length(t2)] == "结果是这样 "

    # non-tool braces stream normally (no false hold)
    t3 = "价格是 {100} 元"
    assert safe_stream_emit_length(t3) == len(t3)


def test_unknown_name_input_json_is_not_treated_as_tool_call():
    from deepseekfathom._core.agent import safe_stream_emit_length

    text = '模型参数示例：{"name":"temperature","input":{"value":0.2},"arguments":"not a tool"}'
    assert parse_tool_call(text) is None
    assert safe_stream_emit_length(text) == len(text)


def test_tool_call_requires_object_arguments():
    assert parse_tool_call('{"tool":"run_shell","arguments":"echo should-not-run"}') is None
    assert parse_tool_call('{"name":"run_shell","input":"echo should-not-run"}') is None


def test_parse_top_level_tool_parameters_from_tool_fence():
    call = parse_tool_call('```tool\n{"tool":"run_shell","command":"printf ok","timeout":15}\n```')
    assert call == ("run_shell", {"command": "printf ok", "timeout": 15})


def test_parse_parameters_alias_for_tool_arguments():
    call = parse_tool_call('{"name":"write_file","parameters":{"path":"a.txt","content":"ok"}}')
    assert call == ("write_file", {"path": "a.txt", "content": "ok"})


def test_strip_leaves_no_bracket_or_tag_residue():
    """Parsing succeeds but a stray brace/angle-bracket must not be left as prose."""
    from deepseekfathom._core.agent import strip_tool_call_display as strip

    # extra trailing brace after the tool JSON
    assert strip('现在执行。\n{"tool":"run_shell","arguments":{"command":"ls"}}}') == "现在执行。"
    # extra angle bracket after a </tool_call>
    assert strip('好的。<tool_call>{"name":"read_file","arguments":{"path":"x"}}</tool_call>>') == "好的。"
    # empty <> pair and lone bracket lines
    assert strip("执行。<tool_call>{\"name\":\"x\",\"arguments\":{}}</tool_call>\n<") == "执行。"
    # legit prose with < > and braces is untouched (no tool call present)
    assert strip("判断 a < b 且 c > d，用 {} 表示空集") == "判断 a < b 且 c > d，用 {} 表示空集"


def test_strip_removes_labelled_tool_format():
    """Our labelled Tool:/工具: format must be stripped from display too, not just JSON."""
    from deepseekfathom._core.agent import strip_tool_call_display as strip

    assert strip('好的，我来运行命令。\nTool: run_shell\nArguments: {"command":"ls"}') == "好的，我来运行命令。"
    assert strip("我检查一下。\n工具: read_file\n参数: {\"path\":\"x\"}") == "我检查一下。"
    # a message that merely mentions 工具 as a word is not a tool call
    assert strip("工具很好用，我们来讨论一下。") == "工具很好用，我们来讨论一下。"


def test_parse_action_bash_block_is_not_inferred_as_tool_by_default():
    # Codex/opencode-style: normal markdown code blocks are display content, not tools.
    # Tool calls must arrive as explicit structured JSON/<tool_call>/Tool: blocks.
    call = parse_tool_call("我现在检查仓库。\n\n```bash\nprintf repo-ok\n```")
    assert call is None


def test_parse_ordinary_bash_example_is_not_tool_call():
    call = parse_tool_call("可以这样手动运行：\n\n```bash\necho hello\n```")
    assert call is None


def test_parse_multiple_action_bash_blocks_are_not_inferred_as_tool_by_default():
    call = parse_tool_call(
        "我来检查本机所有端口：\n\n"
        "```bash\nss -tuln\n```\n\n"
        "同时查看连接：\n\n"
        "```bash\nss -tun\n```"
    )
    assert call is None


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
    result = FathomAgent(settings(tmp_path), client=client).run("summarize")
    assert result.answer == "README says hello."
    assert result.rounds == 2
    assert (tmp_path / ".deepseekfathom" / "sessions").exists()


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
    result = FathomAgent(settings(tmp_path), mode="root", client=client).run("主任务秘密：委派检查")
    assert result.answer == "主代理总结：已收到子代理结果。"
    assert client.subagent_saw_parent_prompt is False
    # the delegated subagent must NOT create its own on-disk conversation: only the
    # parent's session file should exist in the sessions directory
    session_files = list((tmp_path / ".deepseekfathom" / "sessions").glob("*.jsonl"))
    assert len(session_files) == 1, session_files


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

    result = FathomAgent(settings(tmp_path), mode="root", thinking="fast", client=DelegateClient()).run("委派检查")
    assert result.answer == "主代理收到。"


def test_subagent_inherits_parent_mode_and_accepts_thinking_field(tmp_path: Path):
    class DelegateClient:
        def __init__(self):
            self.calls = 0
            self.subagent_system = ""

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"delegate_agent","arguments":{"name":"debugger","task":"检查 shell 权限","thinking":"deep","max_rounds":1}}'
            if self.calls == 2:
                self.subagent_system = messages[0].content
                return "子代理看到 root/deep。"
            return "主代理收到。"

    client = DelegateClient()
    result = FathomAgent(settings(tmp_path), mode="root", thinking="fast", client=client).run("委派检查权限")
    assert result.answer == "主代理收到。"
    assert "Current mode: root" in client.subagent_system
    assert "Policy: write=True, shell=True, network=True, confirmation=False." in client.subagent_system
    assert "Thinking mode: deep." in client.subagent_system


def test_subagent_honors_explicit_mode_and_thinking(tmp_path: Path):
    class DelegateClient:
        def __init__(self):
            self.calls = 0
            self.subagent_system = ""

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"delegate_agent","arguments":{"name":"reviewer","task":"只读检查","mode":"review","thinking":"balanced","max_rounds":1}}'
            if self.calls == 2:
                self.subagent_system = messages[0].content
                return "子代理看到 review/balanced。"
            return "主代理收到。"

    client = DelegateClient()
    FathomAgent(settings(tmp_path), mode="root", thinking="fast", client=client).run("委派只读检查")
    assert "Current mode: review" in client.subagent_system
    assert "Policy: write=False, shell=True, network=False, confirmation=True." in client.subagent_system
    assert "Thinking mode: balanced." in client.subagent_system


def test_subagent_forks_client_with_its_own_upstream_thinking(tmp_path: Path):
    captured = {}

    class ChildClient:
        def __init__(self, child_settings):
            captured["settings"] = child_settings
            self.closed = False

        def chat(self, _messages):
            return "子代理完成。"

        def close(self):
            self.closed = True
            captured["closed"] = True

    class ParentClient:
        def fork_for_settings(self, child_settings):
            return ChildClient(child_settings)

    parent_settings = settings(tmp_path).with_runtime(
        thinking_enabled=True,
        reasoning_effort="low",
    )
    agent = FathomAgent(parent_settings, mode="root", thinking="fast", client=ParentClient())
    result = agent._run_one_subagent(
        {"name": "max-check", "task": "检查参数", "thinking": "max", "max_rounds": 1},
        1,
        1,
    )

    assert result["summary"] == "子代理完成。"
    assert captured["settings"].reasoning_effort == "max"
    payload = {}
    apply_thinking_payload(payload, captured["settings"])
    assert payload["reasoning_effort"] == "max"
    assert captured["closed"] is True


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

    result = FathomAgent(settings(tmp_path), mode="root", thinking="fast", client=MultiDelegateClient()).run("并行委派检查")
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
        FathomAgent(settings(tmp_path), mode="root", client=DelegateClient()).run(
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
    assert normalize_subagent_mode_and_thinking("nonsense", "bad", parent_mode="root", parent_thinking="careful") == ("root", "careful")
    assert normalize_subagent_mode_and_thinking(None, "deep", parent_mode="root", parent_thinking="careful") == ("root", "deep")


@pytest.mark.parametrize("parent_mode", ["plan", "review", "agent", "trusted"])
def test_subagent_cannot_escalate_to_root(parent_mode: str):
    mode, thinking = normalize_subagent_mode_and_thinking(
        "root",
        "deep",
        parent_mode=parent_mode,
        parent_thinking="fast",
    )
    assert mode == parent_mode
    assert thinking == "deep"


def test_subagent_can_request_a_more_restricted_mode():
    assert normalize_subagent_mode_and_thinking(
        "plan",
        None,
        parent_mode="agent",
        parent_thinking="fast",
    ) == ("plan", "fast")


def test_normalize_subagent_specs_accepts_agents_and_tasks():
    specs = normalize_subagent_specs({"agents": [{"name": "one", "task": "a"}, "b"]})
    assert specs == [{"name": "one", "task": "a"}, {"task": "b", "name": "subagent-2"}]
    assert normalize_subagent_specs({"task": "single"}) == [{"task": "single"}]


def test_subagents_slash_item_is_hidden_from_quick_palette(tmp_path: Path):
    from deepseekfathom._core.cli import slash_items

    labels = [label for label, _description in slash_items(settings(tmp_path))]
    assert "/subagents" not in labels
    assert slash_selection_insertion("/subagents") is None


def test_palette_footer_explains_quit_keys():
    footer = palette_footer_text()
    assert "ctrl-c" in footer.lower()
    assert "ctrl-d" in footer.lower()
    assert "esc" in footer.lower()
    assert "j/k" not in footer.lower()


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

    result = FathomAgent(settings(tmp_path), mode="root", client=AskClient(), ask_user=ask_user).run("创建程序")
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
    result = FathomAgent(settings(tmp_path), mode="root", thinking="instant", client=client).run(
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
    result = FathomAgent(settings(tmp_path), mode="root", client=StreamingFencedToolClient()).run(
        "读取 README",
        stream=True,
        on_delta=visible.append,
    )
    assert result.answer == "完成。"
    assert "".join(visible) == "完成。"
    assert "```json" not in "".join(visible)


def test_character_split_tool_fence_is_removed_from_desktop_stream(tmp_path: Path):
    class CharacterStreamingClient:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, _messages):
            self.calls += 1
            text = (
                '```json\n{"tool":"read_file","arguments":{"path":"README.md"}}\n```'
                if self.calls == 1 else "完成。"
            )
            yield from text

    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    visible: list[str] = []
    finals: list[str] = []
    result = FathomAgent(settings(tmp_path), mode="root", client=CharacterStreamingClient()).run(
        "读取 README",
        stream=True,
        on_delta=visible.append,
        on_final=finals.append,
    )

    assert result.answer == "完成。"
    assert "".join(visible) == "完成。"
    assert finals[0] == ""
    assert finals[-1] == "完成。"
    assert "`" not in "".join(visible)


def test_character_split_dsml_executes_without_leaking_markup(tmp_path: Path):
    target = tmp_path / "snake.html"

    class DsmlClient:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, _messages):
            self.calls += 1
            text = (
                '<｜｜DSML｜｜tool_calls>\n'
                '<｜｜DSML｜｜invoke name="write_file">\n'
                '<｜｜DSML｜｜parameter name="content" string="true"><html>game</html></｜｜DSML｜｜parameter>\n'
                f'<｜｜DSML｜｜parameter name="path">{target}</｜｜DSML｜｜parameter>\n'
                '</｜｜DSML｜｜invoke>\n'
                '</｜｜DSML｜｜tool_calls>'
                if self.calls == 1 else "文件已经写入。"
            )
            yield from text

    visible: list[str] = []
    finals: list[str] = []
    result = FathomAgent(settings(tmp_path), mode="root", client=DsmlClient()).run(
        "在桌面写一个 HTML 游戏",
        stream=True,
        on_delta=visible.append,
        on_final=finals.append,
        require_todo=False,
    )

    assert result.answer == "文件已经写入。"
    assert target.read_text(encoding="utf-8") == "<html>game</html>"
    assert finals[0] == ""
    assert "DSML" not in "".join(visible)


def test_promised_attachment_read_continues_without_user_nudge(tmp_path: Path):
    class PromiseThenToolClient:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, _messages):
            self.calls += 1
            replies = [
                "让我先读取您提及的附件。",
                '{"tool":"read_file","arguments":{"path":"CHANGELOG.md"}}',
                "附件已经读取完成。",
            ]
            yield replies[self.calls - 1]

    (tmp_path / "CHANGELOG.md").write_text("release notes", encoding="utf-8")
    visible: list[str] = []
    finals: list[str] = []
    client = PromiseThenToolClient()
    result = FathomAgent(settings(tmp_path), mode="root", client=client).run(
        "请读取附件 CHANGELOG.md",
        stream=True,
        on_delta=visible.append,
        on_final=finals.append,
    )

    assert client.calls == 3
    assert result.answer == "附件已经读取完成。"
    assert finals[0] == ""
    assert finals[-1] == "附件已经读取完成。"
    loaded = SessionStore(tmp_path).load(result.session_id)
    assert any(message.content.startswith("TOOL_RESULT name=read_file") for message in loaded.messages)


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
    result = FathomAgent(settings(tmp_path), mode="root", client=StreamingPrefaceToolClient()).run(
        "读取 README",
        stream=True,
        on_delta=visible.append,
    )
    assert result.answer == "完成。"
    assert "".join(visible) == "完成。"
    assert "我先继续检查" not in "".join(visible)
    assert '{"tool"' not in "".join(visible)


def test_local_create_requires_real_tool_result_and_holds_long_arguments(tmp_path: Path):
    false_claim = (
        "我来先在桌面路径下创建一个简单的小游戏 HTML 文件。先确认桌面位置。\n\n"
        "好的，小游戏 HTML 已创建在桌面，文件名 `game.html`。这是一个躲避小游戏。"
    )

    for case, chunk_size in (("characters", 1), ("single-chunk", 100_000)):
        workspace = tmp_path / case
        workspace.mkdir()
        target = workspace / "game.html"
        html = "<!doctype html><html><body>" + ("<main>小游戏内容</main>" * 320) + "</body></html>"
        tool_reply = "抱歉，刚才只是口头说创建，没有实际写文件。现在马上做。\n\n" + json.dumps(
            {"tool": "write_file", "arguments": {"path": str(target), "content": html}},
            ensure_ascii=False,
            separators=(",", ":"),
        )

        class RealConversationClient:
            def __init__(self):
                self.calls = 0

            def stream_chat(self, _messages):
                replies = [false_claim, tool_reply, "文件已经真实创建完成。"]
                text = replies[self.calls]
                self.calls += 1
                for start in range(0, len(text), chunk_size):
                    yield text[start:start + chunk_size]

        visible: list[str] = []
        finals: list[str] = []
        timeline: list[str] = []
        client = RealConversationClient()

        def delta(text: str) -> None:
            visible.append(text)
            timeline.append("delta:" + text)

        def final(text: str) -> None:
            finals.append(text)
            timeline.append("final:" + text)

        def event(text: str) -> None:
            timeline.append("event:" + text)

        result = FathomAgent(settings(workspace), mode="root", client=client).run(
            "在桌面上创建一个小游戏HTML",
            stream=True,
            on_delta=delta,
            on_final=final,
            on_event=event,
            require_todo=False,
        )

        assert client.calls == 3
        assert result.answer == "文件已经真实创建完成。"
        assert target.read_text(encoding="utf-8") == html
        assert false_claim not in "".join(visible)
        assert '"arguments"' not in "".join(visible)
        assert "<main>小游戏内容</main>" not in "".join(visible)
        assert '"arguments"' not in "".join(finals)
        pending_index = timeline.index("event:toolpending")
        tool_index = next(i for i, item in enumerate(timeline) if item.startswith("event:tool write_file"))
        assert pending_index < tool_index


def test_ordinary_markdown_stream_does_not_lock_or_emit_toolpending(tmp_path: Path):
    answer = (
        "使用 `print()` 输出。\n\n"
        "```python\nprint('hello')\n```\n\n"
        "普通 JSON 示例：\n```json\n{\"name\":\"DeepSeekFathom\",\"arguments\":\"not a tool\"}\n```\n"
        "以上内容都只是示例。"
    )

    class MarkdownClient:
        def stream_chat(self, _messages):
            yield from answer

    visible: list[str] = []
    finals: list[str] = []
    events: list[str] = []
    result = FathomAgent(settings(tmp_path), mode="root", client=MarkdownClient()).run(
        "解释代码和 JSON 示例",
        stream=True,
        on_delta=visible.append,
        on_final=finals.append,
        on_event=events.append,
        require_todo=False,
    )

    assert result.answer == answer
    assert "".join(visible) == answer
    assert finals == [answer]
    assert "toolpending" not in events


def test_repeated_fake_local_completion_is_replaced_with_honest_failure(tmp_path: Path):
    class FakeCompletionClient:
        def __init__(self):
            self.calls = 0

        def chat(self, _messages):
            self.calls += 1
            return "文件已经创建好了。"

    result = FathomAgent(settings(tmp_path), mode="root", client=FakeCompletionClient()).run(
        "在桌面创建 result.txt",
        require_todo=False,
    )

    assert result.answer == "未执行请求：模型没有调用完成本机操作所需的工具。"
    assert not (tmp_path / "result.txt").exists()


def test_local_tool_evidence_detection_covers_natural_create_wording():
    from deepseekfathom._core.agent import claims_local_action_completed, requires_local_tool_action

    assert requires_local_tool_action("在桌面做一个小游戏 HTML")
    assert requires_local_tool_action("帮我生成 result.txt 文件")
    assert requires_local_tool_action("build a local desktop app")
    assert not requires_local_tool_action("如何创建 result.txt 文件？")
    assert claims_local_action_completed("game.html 创建成功，可以双击打开。")
    assert claims_local_action_completed("刚才失败了，现在 game.html 已创建。")
    assert not claims_local_action_completed("game.html 未创建成功，权限不足。")


def test_initial_messages_keep_large_system_prompt_cacheable(tmp_path: Path):
    SkillStore(tmp_path).create("repo-debug", "Debug this repository.", "Run tests.")
    agent = FathomAgent(settings(tmp_path), client=FakeClient(["done"]))
    initial = agent._initial_messages()
    assert [message.role for message in initial] == ["system", "system"]
    assert "Available tools:" in initial[0].content
    assert "cf题" in initial[0].content
    assert "Use delegate_agent proactively" in initial[0].content
    assert "preserve CSS `*` selectors" in initial[0].content
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

    result = FathomAgent(settings(tmp_path), mode="root", client=ContinueClient()).run("写登录 HTML，然后启动服务")
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

    result = FathomAgent(settings(tmp_path), mode="root", client=GoalClient()).run(
        "开始",
        goal="写出 done.txt",
    )
    assert result.answer == "目标已完成：done.txt 已写入。"
    assert result.rounds == 3
    assert (tmp_path / "done.txt").read_text(encoding="utf-8") == "ok"


def test_goal_mode_allows_explicit_block(tmp_path: Path):
    result = FathomAgent(settings(tmp_path), mode="root", client=FakeClient(["被阻塞：缺少目标路径。"])).run(
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

    result = FathomAgent(settings(tmp_path), mode="root", client=RetryClient()).run("读取 missing.txt，如果失败就检查目录")
    assert result.answer == "已改为列目录确认文件不存在。"
    assert result.rounds == 4
    session_text = "\n".join(message.content for message in SessionStore(tmp_path).load(result.session_id).messages)
    assert RECOVER_AFTER_TOOL_FAILURE_PROMPT not in session_text
    assert "读取失败，文件不存在。" not in session_text


def test_desktop_buffers_failed_tool_recovery_draft(tmp_path: Path):
    class RetryClient:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, messages):
            self.calls += 1
            replies = [
                '{"tool":"read_file","arguments":{"path":"missing.txt"}}',
                "读取失败，文件不存在。",
                '{"tool":"list_files","arguments":{"path":"."}}',
                "已改为列目录确认文件不存在。",
            ]
            if self.calls == 3:
                assert "tool failed" in messages[-1].content.lower()
            yield replies[self.calls - 1]

    visible: list[str] = []
    finals: list[str] = []
    result = FathomAgent(settings(tmp_path), mode="root", client=RetryClient()).run(
        "读取 missing.txt，如果失败就检查目录",
        stream=True,
        on_delta=visible.append,
        on_final=finals.append,
        require_todo=False,
    )

    assert result.answer == "已改为列目录确认文件不存在。"
    assert "读取失败" not in "".join(visible)
    assert "读取失败" not in "".join(finals)
    assert finals[-1] == result.answer


def test_agent_does_not_retry_when_only_one_tool_in_batch_failed(tmp_path: Path):
    class PartialSuccessClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({
                    "tool_calls": [
                        {"name": "read_file", "arguments": {"path": "missing.txt"}},
                        {"name": "list_files", "arguments": {"path": "."}},
                    ]
                })
            if self.calls == 2:
                return "目录检查完成；目标文件不存在，其他目录信息已取得。"
            raise AssertionError("a partial-success batch must not start an unsolicited recovery round")

    client = PartialSuccessClient()
    result = FathomAgent(settings(tmp_path), mode="root", client=client).run(
        "检查目标文件和当前目录",
        require_todo=False,
    )

    assert client.calls == 2
    assert result.answer == "目录检查完成；目标文件不存在，其他目录信息已取得。"
    loaded = SessionStore(tmp_path).load(result.session_id)
    assert loaded.messages[-1].content == result.answer


def test_internal_automation_prompts_are_filtered_from_context():
    messages = [
        Message("user", "真实用户输入"),
        Message("user", RECOVER_AFTER_TOOL_FAILURE_PROMPT),
        Message("assistant", "回答"),
    ]
    filtered = filter_internal_automation_messages(messages)
    assert [message.content for message in filtered] == ["真实用户输入", "回答"]


def test_orphaned_legacy_tool_protocol_is_not_sent_back_to_model():
    dsml = '''<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="write_file">
<｜｜DSML｜｜parameter name="path">C:\\Users\\admin\\Desktop\\snake.html</｜｜DSML｜｜parameter>
<｜｜DSML｜｜parameter name="content" string="true"><html></html></｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>'''
    messages = [
        Message("system", "system"),
        Message("user", "写文件"),
        Message("assistant", dsml),
        Message("user", "你没有执行"),
        Message("assistant", "正确格式例如 JSON。"),
        Message("user", 'TOOL_RESULT name=write_file\n{"ok":false}'),
        Message("user", "继续"),
    ]

    filtered = filter_internal_automation_messages(messages)
    contents = [message.content for message in filtered]
    assert dsml not in contents
    assert not any(content.startswith("TOOL_RESULT") for content in contents)
    assert contents == ["system", "写文件", "你没有执行", "正确格式例如 JSON。", "继续"]


def test_valid_tool_protocol_pair_stays_in_model_context():
    call = '{"tool":"read_file","arguments":{"path":"README.md"}}'
    result = 'TOOL_RESULT name=read_file\n{"ok":true,"output":"hello"}'
    filtered = filter_internal_automation_messages([
        Message("assistant", call),
        Message("user", result),
    ])
    assert [message.content for message in filtered] == [call, result]


def test_desktop_marks_orphaned_legacy_tool_as_not_executed():
    from deepseekfathom._core.desktop.app import serialize_messages

    call = '{"tool":"write_file","arguments":{"path":"snake.html","content":"game"}}'
    visible = serialize_messages([
        Message("user", "写文件"),
        Message("assistant", call),
        Message("user", "你没有执行"),
    ])

    tool = next(item for item in visible if item["role"] == "tool")
    assert tool["orphaned"] is True
    assert json.loads(tool["output"]) == {
        "ok": False,
        "output": "没有执行结果，已按未执行处理。",
    }


def test_desktop_pairs_multiple_native_tool_calls_with_each_result():
    from deepseekfathom._core.desktop.app import serialize_messages

    calls = json.dumps({
        "tool_calls": [
            {"type": "function", "function": {"name": "list_files", "arguments": '{"path":"."}'}},
            {"type": "function", "function": {"name": "read_file", "arguments": '{"path":"README.md"}'}},
        ]
    })
    visible = serialize_messages([
        Message("assistant", calls),
        Message("user", 'TOOL_RESULT name=list_files\n{"ok":true,"output":"README.md"}'),
        Message("user", 'TOOL_RESULT name=read_file\n{"ok":true,"output":"hello"}'),
    ])
    tools = [item for item in visible if item["role"] == "tool"]
    assert [item["name"] for item in tools] == ["list_files", "read_file"]
    assert "README.md" in tools[0]["output"]
    assert "hello" in tools[1]["output"]
    assert not any(item.get("orphaned") for item in tools)


def test_session_markdown_exports_visible_transcript_without_protocol(tmp_path: Path):
    from deepseekfathom._core.desktop.app import session_markdown
    from deepseekfathom._core.session import Session

    session = Session(tmp_path, session_id="export-test", created_at="2026-07-11T12:00:00+00:00", persist=False)
    session.messages = [
        Message("system", "SECRET SYSTEM PROMPT"),
        Message("user", "请读取文件", images=["data:image/png;base64,SECRET_IMAGE"]),
        Message("assistant", '{"tool":"read_file","arguments":{"path":"README.md"}}'),
        Message("user", 'TOOL_RESULT name=read_file\n{"ok":true,"output":"hello ``` world"}'),
        Message("assistant", "已经读取完成。"),
    ]

    exported = session_markdown(session, title="导出测试")

    assert exported.startswith("# 导出测试\n")
    assert "`export-test`" in exported
    assert "## 用户" in exported and "## DeepSeekFathom" in exported
    assert "### 工具 · `read_file` · 成功" in exported
    assert "hello ``` world" in exported
    assert "附带图片：1 张" in exported
    assert "SECRET SYSTEM PROMPT" not in exported
    assert "SECRET_IMAGE" not in exported
    assert '"tool":"read_file"' not in exported
    assert "TOOL_RESULT" not in exported


def test_markdown_export_filename_is_windows_safe():
    from deepseekfathom._core.desktop.app import safe_markdown_filename

    assert safe_markdown_filename(' 项目：A/B*? ') == "项目：A_B__.md"
    assert safe_markdown_filename('Project:A') == "Project_A.md"
    assert safe_markdown_filename("CON") == "_CON.md"
    assert safe_markdown_filename("CON.txt") == "_CON.txt.md"
    assert safe_markdown_filename("notes.md") == "notes.md"
    assert safe_markdown_filename("   ") == "DeepSeekFathom-会话.md"


def test_desktop_export_session_uses_native_save_dialog(monkeypatch, tmp_path: Path):
    import webview

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    session = Session(tmp_path, session_id="native-export")
    session.append(Message("user", "导出这段对话"))
    session.append(Message("assistant", "已经准备好。"))
    destination = tmp_path / "saved conversation"

    class Window:
        def __init__(self):
            self.calls = []

        def create_file_dialog(self, dialog_type, **kwargs):
            self.calls.append((dialog_type, kwargs))
            return (str(destination),)

    window = Window()
    api = desktop.DesktopApi()
    api.bind_window(window)
    result = api.export_session(session.session_id)

    exported_path = destination.with_suffix(".md")
    assert result["ok"] is True
    assert result["path"] == str(exported_path)
    assert exported_path.read_text(encoding="utf-8").startswith("# 导出这段对话\n")
    assert window.calls == [(webview.FileDialog.SAVE, {
        "save_filename": "导出这段对话.md",
        "file_types": ("Markdown (*.md)",),
    })]
    assert not list(tmp_path.glob(".*.tmp-*"))


def test_desktop_export_session_cancel_does_not_write(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    session = Session(tmp_path, session_id="cancel-export")
    session.append(Message("user", "不要保存"))

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return None

    api = desktop.DesktopApi()
    api.bind_window(Window())
    assert api.export_session(session.session_id) == {"ok": False, "cancelled": True}
    assert list(tmp_path.glob("*.md")) == []


def test_desktop_export_session_rejects_active_generation(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="active-export")
    api._running = True
    api._active_turn_session_id = api.session.session_id

    result = api.export_session(api.session.session_id)

    assert result == {"ok": False, "error": "当前会话正在生成，回复完成后再导出"}


def test_complex_task_gets_private_execution_hint(tmp_path: Path):
    class InspectClient:
        def chat(self, messages):
            assert "Private execution hint" in messages[-1].content
            assert "delegate_agent" in messages[-1].content
            return "ok"

    result = FathomAgent(settings(tmp_path), mode="root", client=InspectClient()).run("写一个 HTML，然后启动服务，再检查端口并验证公网访问")
    assert result.answer == "ok"


def test_complex_task_requires_todo_write_before_prose(tmp_path: Path):
    class PlanOnlyThenTodoClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return "我先列任务目标，然后开始修复。"
            if self.calls == 2:
                assert "todo_write" in messages[-1].content
                assert "Do not describe the plan in prose" in messages[-1].content
                return '{"tool":"todo_write","arguments":{"todos":[{"content":"定位问题","status":"in_progress"},{"content":"修复并验证","status":"pending"}]}}'
            assert "todo_write" in messages[-1].content
            return "已完成：任务目标已列出。"

    events: list[str] = []
    result = FathomAgent(settings(tmp_path), mode="root", client=PlanOnlyThenTodoClient()).run(
        "检查这个复杂 bug，然后修复，再运行测试验证",
        on_event=events.append,
        max_tool_rounds=3,
    )
    assert result.answer == "已完成：任务目标已列出。"
    assert any(event.startswith("todo ") for event in events)


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
    result = FathomAgent(settings(tmp_path), mode="root", client=LimitClient()).run("连续检查", max_tool_rounds=2)
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

    agent = FathomAgent(settings(tmp_path), mode="root", client=SearchRetryClient())
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
    result = FathomAgent(settings(tmp_path), client=client).run("？")
    assert client.calls == 1
    assert result.rounds == 1
    assert result.answer == '{"tool":"list_files","arguments":{"path":"."}}'
    transcript = next((tmp_path / ".deepseekfathom" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "TOOL_RESULT" not in transcript
    assert is_question_mark_only("???") is True


def test_agent_executes_explicit_json_tool_call(tmp_path: Path):
    client = FakeClient([
        '{"tool":"run_shell","arguments":{"command":"printf repo-ok"}}',
        "工具结果是 repo-ok。",
    ])
    result = FathomAgent(settings(tmp_path), mode="root", client=client).run("检查仓库")
    transcript = next((tmp_path / ".deepseekfathom" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "TOOL_RESULT name=run_shell" in transcript
    assert "repo-ok" in transcript
    assert result.answer == "工具结果是 repo-ok。"


def test_write_file_supports_empty_content_and_rejects_directory(tmp_path: Path):
    import pytest

    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    empty = tools.run("write_file", {"path": "empty.txt", "content": ""})
    assert empty.ok is True
    assert (tmp_path / "empty.txt").read_bytes() == b""

    with pytest.raises(ToolError, match="target is a directory"):
        tools.run("write_file", {"path": str(tmp_path), "content": "wrong"})
    assert not list(tmp_path.parent.glob(f".{tmp_path.name}.tmp-*"))


def test_write_file_rejects_ellipsis_placeholder_path(tmp_path: Path):
    import pytest

    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    for placeholder in ("...", "…"):
        with pytest.raises(ToolError, match="placeholder"):
            tools.run("write_file", {"path": placeholder, "content": "..."})

    assert list(tmp_path.iterdir()) == []


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
    result = FathomAgent(settings(tmp_path), mode="root", client=InspectingClient()).run("read")
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
    result = FathomAgent(settings(tmp_path), mode="root", client=client).run("read", stop_after_tool=True)
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
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
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
    assert fast.reasoning_effort == "low"
    assert ThinkingMode.resolve("balanced").reasoning_effort == "medium"
    assert deep.reasoning_effort == "high"
    assert ultra.reasoning_effort == "xhigh"
    assert ThinkingMode.resolve("max").reasoning_effort == "max"
    assert ThinkingMode.user_selectable_names() == ["fast", "balanced", "deep", "ultra", "max"]
    assert deep.deliberation_passes > 0
    assert deep.system_hint


def test_deepseek_payload_includes_thinking_controls(tmp_path: Path):
    settings_obj = settings(tmp_path).with_runtime(thinking_enabled=True, reasoning_effort="xhigh")
    payload = {}
    apply_thinking_payload(payload, settings_obj)
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "xhigh"

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


def test_thinking_payload_per_format_shapes(tmp_path: Path):
    """Each provider format must send reasoning in its own native shape, not just chat."""
    from dataclasses import replace as _replace

    def probe(fmt):
        s = settings(tmp_path).with_runtime(thinking_enabled=True, reasoning_effort="high")
        s = _replace(s, provider_format=fmt)
        payload = {"max_tokens": 1200}
        if fmt == "gemini":
            payload["generationConfig"] = {"maxOutputTokens": 1200}
        apply_thinking_payload(payload, s)
        return payload

    # OpenAI Responses API wants the nested reasoning:{effort}, not top-level
    resp = probe("openai-responses")
    assert resp["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in resp

    # Anthropic extended thinking: budget_tokens, strictly below max_tokens
    ant = probe("anthropic")
    assert ant["thinking"]["type"] == "enabled"
    assert 1024 <= ant["thinking"]["budget_tokens"] < 1200

    # Gemini: generationConfig.thinkingConfig.thinkingBudget
    gem = probe("gemini")
    assert gem["generationConfig"]["thinkingConfig"]["thinkingBudget"] > 0


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

    monkeypatch.setattr("deepseekfathom._core.tools.os.replace", fail_replace)
    try:
        tools.run("write_file", {"path": "file.txt", "content": "new"})
    except OSError:
        pass
    assert target.read_text(encoding="utf-8") == "old"


def test_write_file_returns_replayable_line_diff(tmp_path: Path):
    target = tmp_path / "notes.txt"
    target.write_text("keep\nold\n", encoding="utf-8")
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))

    result = tools.run("write_file", {"path": "notes.txt", "content": "keep\nnew\n"})
    payload = json.loads(result.to_message())

    assert payload["ui"]["kind"] == "file_change"
    assert payload["ui"]["path"] == "notes.txt"
    assert "-old" in payload["ui"]["diff"]
    assert "+new" in payload["ui"]["diff"]


def test_write_file_marks_new_file_as_created(tmp_path: Path):
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("write_file", {"path": "new.txt", "content": "first\nsecond\n"})

    assert result.ui["operation"] == "created"
    assert "+first" in result.ui["diff"]
    assert "+second" in result.ui["diff"]


def test_run_shell_background_command_starts_service(tmp_path: Path):
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("run_shell", {"command": "python3 -m http.server 0 &", "name": "test-http"})
    assert result.ok is True
    assert "Started test-http" in result.output
    assert (tmp_path / ".deepseekfathom" / "services" / "test-http.pid").exists()
    tools.run("stop_service", {"name": "test-http"})


def test_start_service_uses_hidden_process_launcher(monkeypatch, tmp_path: Path):
    captured = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("deepseekfathom._core.tools.shell_invocation", lambda command: (["shell", command], False))
    monkeypatch.setattr("deepseekfathom._core.tools.popen_hidden", fake_popen)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))

    result = tools.run("start_service", {"name": "server", "command": "serve --port 8000"})

    assert result.ok is True
    assert captured["command"] == ["shell", "serve --port 8000"]
    assert captured["shell"] is False
    assert captured["stderr"] is subprocess.STDOUT
    assert (tmp_path / ".deepseekfathom" / "services" / "server.pid").read_text() == "4242"


def test_run_shell_handles_none_stdout_and_stderr(monkeypatch, tmp_path: Path):
    def fake_run(*_args, **_kwargs):
        return type("Completed", (), {"returncode": 0, "stdout": None, "stderr": None})()

    monkeypatch.setattr("deepseekfathom._core.tools.subprocess.run", fake_run)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("run_shell", {"command": "echo ok"})
    assert result.ok is True
    assert result.output == "clean"


def test_desktop_error_summary_hides_trace_noise():
    from deepseekfathom._core.desktop.app import user_error_summary

    assert user_error_summary("API error 400: invalid model") == "上游 API 返回错误：400: invalid model"
    assert "旧版本处理空输出时崩溃" in user_error_summary("'NoneType' object is not subscriptable")


def test_agent_mode_requires_confirmation_for_shell(tmp_path: Path):
    client = FakeClient([
        '{"tool":"run_shell","arguments":{"command":"echo should-not-run"}}',
        "Shell was blocked.",
    ])
    result = FathomAgent(settings(tmp_path), mode="agent", client=client).run("run shell")
    transcript = next((tmp_path / ".deepseekfathom" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "confirmation required" in transcript
    assert result.answer == "Shell was blocked."


def test_yolo_mode_auto_approves_shell(tmp_path: Path):
    client = FakeClient([
        '{"tool":"run_shell","arguments":{"command":"echo ran"}}',
        "Shell ran.",
    ])
    result = FathomAgent(settings(tmp_path), mode="yolo", client=client).run("run shell")
    transcript = next((tmp_path / ".deepseekfathom" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "ran" in transcript
    assert result.answer == "Shell ran."


def test_agent_executes_tool_fence_with_top_level_parameters(tmp_path: Path):
    client = FakeClient([
        '```tool\n{"tool":"run_shell","command":"printf top-level-ok","timeout":5}\n```',
        "done",
    ])
    result = FathomAgent(settings(tmp_path), mode="root", client=client).run("run shell")
    transcript = next((tmp_path / ".deepseekfathom" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "top-level-ok" in transcript
    assert "Missing string argument: command" not in transcript
    assert result.answer == "done"


def test_agent_mode_can_approve_selected_tool(tmp_path: Path):
    client = FakeClient([
        '{"tool":"run_shell","arguments":{"command":"echo approved"}}',
        "Shell approved.",
    ])
    result = FathomAgent(
        settings(tmp_path),
        mode="agent",
        client=client,
        approve=lambda name, args: name == "run_shell",
    ).run("run shell")
    transcript = next((tmp_path / ".deepseekfathom" / "sessions").glob("*.jsonl")).read_text(encoding="utf-8")
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
    result = tools.run("clone_repo", {"repo": "ffffff233/DeepSeekFathom", "path": "repo"})
    assert result.ok is False
    assert "not empty" in result.output
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_windows_style_workspace_path_is_normalized_on_posix(tmp_path: Path):
    import pytest

    if os.name == "nt":
        pytest.skip("Windows drive paths are real absolute paths on Windows")
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


def test_restricted_mode_blocks_outside_workspace(tmp_path: Path):
    import pytest

    ws = tmp_path / "proj"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    tools = ToolRegistry(ws, policy=ApprovalPolicy.from_mode("agent"))
    with pytest.raises(ToolError, match="escapes workspace"):
        tools.run("read_file", {"path": str(outside)})


def test_full_access_reaches_outside_workspace(tmp_path: Path):
    """完全访问 / root lifts the workspace confinement — file tools reach anywhere,
    matching the shell, and path display never crashes on outside paths."""
    ws = tmp_path / "proj"
    ws.mkdir()
    outside = tmp_path / "elsewhere" / "note.txt"
    outside.parent.mkdir()
    outside.write_text("hello outside", encoding="utf-8")
    tools = ToolRegistry(ws, policy=ApprovalPolicy.from_mode("root"))

    read = tools.run("read_file", {"path": str(outside)})
    assert read.ok and read.output == "hello outside"
    written = tools.run("write_file", {"path": str(outside.parent / "new.txt"), "content": "x"})
    assert written.ok and (outside.parent / "new.txt").read_text(encoding="utf-8") == "x"
    listed = tools.run("list_files", {"path": str(outside.parent)})
    assert listed.ok and "note.txt" in listed.output


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

    monkeypatch.setattr("deepseekfathom._core.tools.subprocess.run", fake_run)
    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", fake_urlopen)

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

    monkeypatch.setattr("deepseekfathom._core.tools.subprocess.run", fake_run)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("clone_repo", {"repo/url": "https://github.com/esengine/DeepSeek-Reasonix", "path": str(tmp_path / "Reasonix")})
    assert result.ok is True
    assert clone_commands[0][-2] == "https://github.com/esengine/DeepSeek-Reasonix.git"


def test_system_prompt_mentions_clone_repo(tmp_path: Path):
    prompt = FathomAgent(settings(tmp_path), client=FakeClient(["ok"]))._system_prompt()
    assert "clone_repo(repo or url, path, branch?, timeout?)" in prompt
    assert "prefer clone_repo over manual git clone" in prompt
    assert "Windows paths" in prompt
    assert "todo_write(todos)" in prompt


def test_normalize_bing_redirect_url():
    url = "https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9uZXdzP2E9MQ"
    assert normalize_bing_url(url) == "https://example.com/news?a=1"


def test_normalize_duckduckgo_redirect_url():
    url = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews%3Fa%3D1"
    assert normalize_duckduckgo_url(url) == "https://example.com/news?a=1"


def test_todo_write_normalizes_visible_task_list(tmp_path: Path):
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run(
        "todo_write",
        {
            "todos": [
                {"content": "分析问题", "status": "in_progress"},
                {"content": "修复代码", "status": "in_progress"},
                {"content": "运行测试", "status": "completed"},
                {"content": "", "status": "pending"},
            ]
        },
    )
    assert result.ok is True
    data = json.loads(result.output)
    assert data["todos"] == [
        {"id": "todo-1", "content": "分析问题", "status": "in_progress"},
        {"id": "todo-2", "content": "修复代码", "status": "pending"},
        {"id": "todo-3", "content": "运行测试", "status": "completed"},
    ]


def test_inspect_media_attaches_image_to_tool_result(tmp_path: Path):
    image = tmp_path / "shot.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lH9dtwAAAABJRU5ErkJggg=="))
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("inspect_media", {"path": "shot.png"})
    assert result.ok is True
    assert result.images and result.images[0].startswith("data:image/png;base64,")
    assert "attached 1 visual frame" in result.output


def test_agent_emits_todo_event_for_todo_write(tmp_path: Path):
    from deepseekfathom._core.desktop.app import parse_agent_event

    events: list[str] = []
    client = FakeClient(
        [
            '{"tool":"todo_write","arguments":{"todos":[{"content":"分析问题","status":"in_progress"},{"content":"修复","status":"pending"}]}}',
            "继续处理。",
            "目标已完成：已修复。",
        ]
    )
    result = FathomAgent(settings(tmp_path), mode="root", thinking="instant", client=client).run(
        "修一个复杂 bug",
        goal="完成复杂 bug 修复",
        on_event=events.append,
    )
    assert result.answer == "目标已完成：已修复。"
    todo_events = [event for event in events if event.startswith("todo ")]
    assert todo_events
    parsed = parse_agent_event(todo_events[0])
    assert parsed["kind"] == "todo"
    assert "分析问题" in parsed["detail"]
    detail = json.loads(parsed["detail"])
    assert detail["todos"][0]["content"] == "分析问题"


def test_agent_continues_after_required_todo_write(tmp_path: Path):
    class TodoThenWorkClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return "我先列任务目标，然后开始处理。"
            if self.calls == 2:
                return '{"tool":"todo_write","arguments":{"todos":[{"content":"定位问题","status":"in_progress"},{"content":"修复问题","status":"pending"}]}}'
            if self.calls == 3:
                assert "TOOL_RESULT name=todo_write" in messages[-1].content
                return '{"tool":"write_file","arguments":{"path":"done.txt","content":"ok"}}'
            assert "TOOL_RESULT name=write_file" in messages[-1].content
            return "已完成：done.txt 已写入。"

    result = FathomAgent(settings(tmp_path), mode="root", client=TodoThenWorkClient()).run(
        "检查这个复杂 bug，然后修复，再验证",
        max_tool_rounds=4,
    )
    assert (tmp_path / "done.txt").read_text(encoding="utf-8") == "ok"
    assert result.answer == "已完成：done.txt 已写入。"


def test_agent_preserves_substantive_answer_after_completed_todo_write(tmp_path: Path):
    finals: list[str] = []

    class CompletedTodoThenSummaryClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"tool":"todo_write","arguments":{"todos":[{"content":"整理结果","status":"completed"}]}}'
            assert "TOOL_RESULT name=todo_write" in messages[-1].content
            return (
                "处理结果如下：任务目标已经更新到完成态，前端会保留这条最终说明，"
                "不会因为刚刚收到 todo_write 的完成事件而把已经流式显示的回答撤回。"
            )

        def stream_chat(self, messages):
            yield self.chat(messages)

    result = FathomAgent(settings(tmp_path), mode="root", client=CompletedTodoThenSummaryClient()).run(
        "整理这个复杂问题，然后给出最终说明",
        stream=True,
        on_final=finals.append,
        max_tool_rounds=3,
    )

    assert result.answer.startswith("处理结果如下")
    assert finals.count("") == 1
    assert finals[-1] == result.answer


def test_web_search_uses_baidu_first(monkeypatch, tmp_path: Path):
    requested: list[str] = []

    class FakeResponse:
        def __init__(self, body: str):
            self.body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.body

    def fake_urlopen(request, timeout=10):
        url = request.full_url
        requested.append(url)
        assert url.startswith("https://www.baidu.com/s?")
        assert "wd=%E6%B5%8B%E8%AF%95" in url
        return FakeResponse(
            '<html><h3 class="t"><a href="https://example.com/a">标题</a></h3>'
            '<div class="c-abstract">摘要</div></html>'
        )

    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", fake_urlopen)
    monkeypatch.delenv("DSTUL_SEARCH_ENGINES", raising=False)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("web_search", {"query": "测试", "max_results": 1})
    assert result.ok is True
    assert "Baidu" in result.output
    assert "https://example.com/a" in result.output
    assert len(requested) == 1


def test_web_search_falls_back_from_baidu_to_bing(monkeypatch, tmp_path: Path):
    requested: list[str] = []

    class FakeResponse:
        def __init__(self, body: str):
            self.body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.body

    def fake_urlopen(request, timeout=10):
        url = request.full_url
        requested.append(url)
        if "baidu.com" in url:
            return FakeResponse("<html><body>captcha or empty</body></html>")
        assert "bing.com/search?" in url
        return FakeResponse(
            '<html><li class="b_algo"><h2><a href="https://example.com/b">必应结果</a></h2>'
            '<div class="b_caption"><p>必应摘要</p></div></li></html>'
        )

    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", fake_urlopen)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("web_search", {"query": "测试", "max_results": 1})
    assert result.ok is True
    assert "Bing" in result.output
    assert "https://example.com/b" in result.output
    assert len(requested) == 2


def test_web_search_accepts_engine_override(monkeypatch, tmp_path: Path):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return (
                '<html><li class="b_algo"><h2><a href="https://example.com/news">新闻</a></h2>'
                '<div class="b_caption"><p>摘要</p></div></li></html>'
            ).encode()

    def fake_urlopen(request, timeout=10):
        assert "bing.com/search?" in request.full_url
        parsed = urllib.parse.urlparse(request.full_url)
        captured.update(urllib.parse.parse_qs(parsed.query))
        return FakeResponse()

    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", fake_urlopen)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run(
        "web_search",
        {
            "query": "测试",
            "engines": "bing",
            "language": "zh-TW",
        },
    )
    assert result.ok is True
    assert captured["mkt"] == ["zh-TW"]
    assert captured["setlang"] == ["zh"]
    assert "Bing" in result.output


def test_web_search_supports_duckduckgo_fallback(monkeypatch, tmp_path: Path):
    requested: list[str] = []

    class FakeResponse:
        def __init__(self, body: str):
            self.body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.body

    def fake_urlopen(request, timeout=10):
        requested.append(request.full_url)
        if "duckduckgo.com" not in request.full_url:
            return FakeResponse("<html></html>")
        return FakeResponse(
            '<div class="result__body"><a rel="nofollow" class="result__a" '
            'href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fddg">DDG 结果</a>'
            '<div class="result__snippet">DDG 摘要</div></div></div>'
        )

    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", fake_urlopen)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("web_search", {"query": "测试", "max_results": 1})
    assert result.ok is True
    assert "DuckDuckGo" in result.output
    assert "https://example.com/ddg" in result.output
    assert any("baidu.com" in url for url in requested)
    assert any("bing.com" in url for url in requested)
    assert any("duckduckgo.com" in url for url in requested)


def test_web_search_can_fetch_top_result_pages(monkeypatch, tmp_path: Path):
    requested: list[str] = []

    class FakeResponse:
        def __init__(self, body: str):
            self.body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.body

    def fake_urlopen(request, timeout=10):
        url = request.full_url
        requested.append(url)
        if "bing.com" in url:
            return FakeResponse(
                '<html><li class="b_algo"><h2><a href="https://example.com/a">A</a></h2>'
                '<div class="b_caption"><p>摘要</p></div></li></html>'
            )
        if url == "https://example.com/robots.txt":
            return FakeResponse("User-agent: *\nAllow: /\n")
        if "baidu.com" in url:
            return FakeResponse("<html></html>")
        return FakeResponse("<html><title>A page</title><body><main>正文内容 " + ("x" * 200) + "</main></body></html>")

    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", fake_urlopen)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("web_search", {"query": "测试", "engines": "bing", "fetch_pages": 1})
    assert result.ok is True
    assert "[Fetched Page]" in result.output
    assert "正文内容" in result.output
    assert any(url == "https://example.com/a" for url in requested)


def test_web_search_respects_robots_when_fetching_pages(monkeypatch, tmp_path: Path):
    class FakeResponse:
        def __init__(self, body: str):
            self.body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.body

    def fake_urlopen(request, timeout=10):
        url = request.full_url
        if "bing.com" in url:
            return FakeResponse(
                '<html><li class="b_algo"><h2><a href="https://example.com/a">A</a></h2>'
                '<div class="b_caption"><p>摘要</p></div></li></html>'
            )
        if url == "https://example.com/robots.txt":
            return FakeResponse("User-agent: *\nDisallow: /\n")
        raise AssertionError(f"page should not be fetched when robots blocks it: {url}")

    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", fake_urlopen)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("web_search", {"query": "测试", "engines": "bing", "fetch_pages": 1})
    assert result.ok is True
    assert "skipped by robots.txt" in result.output
    assert "正文内容" not in result.output


def test_web_search_reports_all_engine_failures(monkeypatch, tmp_path: Path):
    def fake_urlopen(request, timeout=10):
        raise OSError("network blocked")

    monkeypatch.delenv("DSTUL_SEARCH_ENGINES", raising=False)
    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", fake_urlopen)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("web_search", {"query": "测试", "max_results": 3})
    assert result.ok is False
    assert "web search returned no parseable results" in result.output
    assert "baidu: request failed" in result.output
    assert "bing: request failed" in result.output
    assert "duckduckgo: request failed" in result.output


def test_web_search_reads_direct_url(monkeypatch, tmp_path: Path):
    class FakeResponse:
        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.body

    def fake_urlopen(request, timeout=10):
        if request.full_url == "https://example.com/robots.txt":
            return FakeResponse(b"User-agent: *\nAllow: /\n")
        return FakeResponse(b"<html><head><title>Page Title</title></head><body><h1>Hello</h1><p>Body text</p></body></html>")

    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", fake_urlopen)
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("web_search", {"query": "https://example.com/page"})
    assert result.ok is True
    assert "Page Title" in result.output
    assert "Body text" in result.output


def test_web_search_direct_url_respects_robots(monkeypatch, tmp_path: Path):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return b"User-agent: *\nDisallow: /\n"

    monkeypatch.setattr("deepseekfathom._core.tools.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("web_search", {"query": "https://example.com/page"})
    assert result.ok is False
    assert "robots.txt" in result.output


def test_skill_store_discovers_and_creates_workspace_skills(tmp_path: Path):
    store = SkillStore(tmp_path, home=tmp_path / "home")
    created = store.create("repo-debug", "Use when debugging this repository.", "Run tests first.")
    assert created.name == "repo-debug"
    skills = store.list()
    assert [skill.name for skill in skills] == ["repo-debug"]
    assert "debugging this repository" in skills[0].description
    assert skills[0].source == "user"


def test_skill_inspection_reports_winner_shadowed_and_root_priority(tmp_path: Path):
    home = tmp_path / "home"
    project_skill = tmp_path / ".deepseekfathom" / "skills" / "repo-debug" / "SKILL.md"
    user_skill = home / ".deepseekfathom" / "skills" / "repo-debug" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    user_skill.parent.mkdir(parents=True)
    project_skill.write_text("---\nname: repo-debug\ndescription: project winner\n---\n\nProject", encoding="utf-8")
    user_skill.write_text("---\nname: repo-debug\ndescription: user fallback\n---\n\nUser", encoding="utf-8")

    inspection = SkillStore(tmp_path, home=home).inspect()
    candidates = [candidate for candidate in inspection.candidates if candidate.skill.name == "repo-debug"]

    assert [candidate.status for candidate in candidates] == ["winner", "shadowed"]
    assert candidates[0].skill.path == project_skill
    assert candidates[1].winner_path == project_skill
    assert inspection.roots[0].path == (tmp_path / ".deepseekfathom" / "skills").resolve()
    assert inspection.roots[0].priority == 0


def test_skill_create_never_overwrites_existing_user_file(tmp_path: Path):
    store = SkillStore(tmp_path, home=tmp_path / "home")
    created = store.create("keep-me", "original", "first body")

    with pytest.raises(FileExistsError):
        store.create("keep-me", "replacement", "second body")

    assert created.path.read_text(encoding="utf-8").endswith("first body\n")


def test_skill_tools_search_and_load_body_on_demand(tmp_path: Path):
    skill_file = tmp_path / ".claude" / "skills" / "repo-debug" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: repo-debug\ndescription: Diagnose repository failures\n---\n\nRun the focused tests.",
        encoding="utf-8",
    )
    references = skill_file.parent / "references"
    references.mkdir()
    (references / "notes.md").write_text("Check the saved logs.", encoding="utf-8")
    scripts = skill_file.parent / "scripts"
    scripts.mkdir()
    (scripts / "verify.py").write_text("print('ok')\n", encoding="utf-8")

    store = SkillStore(tmp_path, home=tmp_path / "home")
    prompt = store.prompt_context()
    assert "repo-debug" in prompt
    assert "Run the focused tests" not in prompt

    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("plan"))
    listed = json.loads(tools.run("list_skills", {"query": "repository"}).output)
    loaded = tools.run("read_skill", {"name": "repo-debug", "arguments": "fix CI"}).output
    assert listed["skills"][0]["name"] == "repo-debug"
    assert "<skill-pin" in loaded
    assert "Run the focused tests" in loaded
    assert "Check the saved logs" in loaded
    assert "verify.py" in loaded
    assert "Arguments: fix CI" in loaded


def test_instruction_store_loads_stable_hierarchy_and_local_override(tmp_path: Path):
    from deepseekfathom._core.instructions import InstructionStore

    home = tmp_path / "home"
    project = tmp_path / "project"
    workspace = project / "packages" / "app"
    (home / ".deepseekfathom").mkdir(parents=True)
    workspace.mkdir(parents=True)
    (project / ".git").mkdir()
    (home / ".deepseekfathom" / "AGENTS.md").write_text("user rule", encoding="utf-8")
    (project / "REASONIX.md").write_text("root rule", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("workspace rule", encoding="utf-8")
    (workspace / "AGENTS.local.md").write_text("local rule", encoding="utf-8")

    context = InstructionStore(workspace, home=home).load()
    assert [document.scope for document in context.documents] == ["user", "ancestor", "project", "local"]
    assert context.prompt.index("user rule") < context.prompt.index("root rule")
    assert context.prompt.index("root rule") < context.prompt.index("workspace rule") < context.prompt.index("local rule")
    assert context.truncated is False


def test_existing_session_refreshes_runtime_context_without_losing_history(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Always run focused tests.", encoding="utf-8")
    SkillStore(tmp_path).create("repo-debug", "Debug repositories", "Read the failure first.")
    session = Session(tmp_path, session_id="legacy-runtime")
    session.messages = [
        Message("system", "old system prompt"),
        Message("system", "Available skills:\n- old: stale"),
        Message("user", "keep this request"),
        Message("assistant", "keep this answer"),
    ]
    session.rewrite()

    agent = FathomAgent(settings(tmp_path), client=FakeClient(["done"]))
    agent._refresh_runtime_context(session)

    assert "Available tools:" in session.messages[0].content
    assert any('kind="instructions"' in message.content for message in session.messages[:3])
    assert any('kind="skills"' in message.content for message in session.messages[:3])
    assert [message.content for message in session.messages if message.role != "system"] == ["keep this request", "keep this answer"]


def test_compaction_preserves_runtime_context_and_loaded_skill(tmp_path: Path):
    runtime = Message("system", '<runtime-context kind="skills" version="1">\n- demo\n</runtime-context>')
    pin = Message("user", 'TOOL_RESULT name=read_skill\n{"ok":true,"output":"<skill-pin name=\\"demo\\">follow me</skill-pin>"}')
    messages = [Message("system", "base"), runtime]
    messages.extend(Message("user" if index % 2 else "assistant", f"old {index}") for index in range(12))
    messages.append(pin)
    messages.extend(Message("user" if index % 2 else "assistant", f"recent {index}") for index in range(10))

    compacted = compact_context_messages(messages, "deepseek-chat", force=True)
    assert compacted[0].content == "base"
    assert compacted[1].content == runtime.content
    assert any("<skill-pin" in message.content for message in compacted)


def test_capability_report_is_deterministic_and_path_redacted(tmp_path: Path):
    home = tmp_path / "home"
    project_skill = tmp_path / ".deepseekfathom" / "skills" / "demo" / "SKILL.md"
    user_skill = home / ".deepseekfathom" / "skills" / "demo" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    user_skill.parent.mkdir(parents=True)
    project_skill.write_text("---\nname: demo\ndescription: project\n---\n\nProject", encoding="utf-8")
    user_skill.write_text("---\nname: demo\ndescription: user\n---\n\nUser", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("API_KEY=never-copy-this", encoding="utf-8")

    report = collect_capability_report(tmp_path, mode="agent", home=home)
    serialized = json.dumps(report, ensure_ascii=False)
    tools = report["tools"]["entries"]

    assert [tool["name"] for tool in tools] == sorted(tool["name"] for tool in tools)
    assert all(tool["schema"] and tool["schemaTokenEstimate"] > 0 for tool in tools)
    assert report["summary"]["tools"] == len(tools)
    assert report["summary"]["shadowedSkills"] == 1
    assert {issue["code"] for issue in report["issues"]} >= {"skill.shadowed"}
    assert "instruction.not_loaded" not in {issue["code"] for issue in report["issues"]}
    assert report["instructions"]["entries"][0]["loaded"] is True
    assert str(tmp_path.resolve()) not in serialized
    assert "never-copy-this" not in serialized
    assert "<workspace>/.deepseekfathom/skills/demo/SKILL.md" in serialized


def test_desktop_user_data_migration_never_overwrites_existing_files(tmp_path: Path):
    from deepseekfathom._core.desktop.app import _copy_missing_user_data

    legacy = tmp_path / "installed" / ".deepseekfathom"
    stable = tmp_path / "home" / ".deepseekfathom"
    (legacy / "sessions").mkdir(parents=True)
    (stable / "sessions").mkdir(parents=True)
    (legacy / "sessions" / "old.jsonl").write_text("legacy", encoding="utf-8")
    (legacy / "sessions" / "keep.jsonl").write_text("replace me", encoding="utf-8")
    (stable / "sessions" / "keep.jsonl").write_text("user copy", encoding="utf-8")

    _copy_missing_user_data(legacy, stable)

    assert (stable / "sessions" / "old.jsonl").read_text(encoding="utf-8") == "legacy"
    assert (stable / "sessions" / "keep.jsonl").read_text(encoding="utf-8") == "user copy"


def test_root_mode_has_no_confirmation_gate():
    policy = ApprovalPolicy.from_mode("root")
    assert policy.allow_network is True
    assert policy.require_confirmation is False


def test_failed_apply_patch_has_no_success_diff_ui(tmp_path: Path):
    tools = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root"))
    result = tools.run("apply_patch", {"patch": "this is not a unified patch"})
    assert result.ok is False
    assert result.ui is None


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
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(config_home))
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
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))

    def fake_interactive(settings_obj, mode, thinking, yes, resume=None):
        captured["mode"] = mode
        captured["thinking"] = thinking
        captured["resume"] = resume
        return 0

    monkeypatch.setattr("deepseekfathom._core.cli.interactive", fake_interactive)
    assert main([]) == 0
    assert captured == {"mode": "root", "thinking": "fast", "resume": None}


def test_cli_desktop_command_invokes_desktop(monkeypatch):
    import deepseekfathom._core.desktop.app as desktop

    called = {}
    monkeypatch.setattr(desktop, "main", lambda: called.setdefault("ok", True))
    assert main(["desktop"]) == 0
    assert called["ok"] is True


def test_desktop_api_boot_and_runtime(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    api = desktop.DesktopApi()
    assert "window" not in api.__dict__
    assert "_window" in api.__dict__
    boot = api.boot()
    assert boot["version"]
    assert boot["mode"] == "root"
    assert "fast" in boot["thinkingModes"]

    updated = api.set_runtime({"mode": "plan", "thinking": "deep", "model": "deepseek-v4-pro"})
    assert updated["mode"] == "plan"
    assert updated["thinking"] == "deep"
    assert updated["model"] == "deepseek-v4-pro"
    assert updated["running"] is False
    saved = get_settings()
    assert saved.default_mode == "plan"
    assert saved.default_thinking == "deep"
    assert saved.model == "deepseek-v4-pro"
    restarted = desktop.DesktopApi()
    assert restarted.mode == "plan"
    assert restarted.thinking.name == "deep"
    assert restarted.settings.model == "deepseek-v4-pro"


def test_desktop_windows_single_instance_mutex(monkeypatch):
    import ctypes

    import deepseekfathom._core.desktop.app as desktop

    class Call:
        def __init__(self, fn):
            self.fn = fn
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.fn(*args)

    class Kernel:
        def __init__(self):
            self.closed = []
            self.CreateMutexW = Call(lambda *_args: 123)
            self.CloseHandle = Call(lambda handle: self.closed.append(handle) or True)

    kernel = Kernel()
    errors = iter([0, desktop._ERROR_ALREADY_EXISTS])
    focused = []
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel)
    monkeypatch.setattr(ctypes, "set_last_error", lambda _value: None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: next(errors))
    monkeypatch.setattr(desktop, "focus_existing_desktop_window", lambda: focused.append(True))

    acquired, instance = desktop.acquire_desktop_instance()
    assert acquired is True
    assert instance == (kernel, 123)
    desktop.release_desktop_instance(instance)
    assert kernel.closed == [123]

    acquired_again, duplicate = desktop.acquire_desktop_instance()
    assert acquired_again is False
    assert duplicate is None
    assert kernel.closed == [123, 123]
    assert focused == [True]


def test_desktop_api_rejects_parallel_turn(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api._running = True
    api._active_turn_id = "active-turn"
    api._active_turn_session_id = "session"
    result = api.send({"prompt": "hello"})
    assert result == {"ok": False, "error": "turn already running"}
    cancelled = api.cancel()
    assert cancelled == {"ok": True, "running": True, "cancelling": True}
    assert api._running is True
    assert api._cancel_requested is True


def test_desktop_send_after_cancel_queues_next_turn(monkeypatch, tmp_path: Path):
    import time
    import threading

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.agent import AgentResult

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    entered_first = threading.Event()
    release_first = threading.Event()
    prompts: list[str] = []

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt, **kwargs):
            prompts.append(prompt)
            if prompt == "第一条":
                entered_first.set()
                assert release_first.wait(timeout=2)
                kwargs["on_delta"]("迟到输出")
            return AgentResult(kwargs["session"].session_id, "完成", 1)

    monkeypatch.setattr(desktop, "FathomAgent", FakeAgent)

    first = api.send({"prompt": "第一条"})
    assert first["ok"] is True
    assert entered_first.wait(timeout=2)
    cancelled = api.cancel()
    assert cancelled == {"ok": True, "running": True, "cancelling": True}

    second = api.send({"prompt": "第二条"})
    assert second["ok"] is True
    assert second["queued"] is True
    assert second["sessionId"] == first["sessionId"]
    assert second["turnId"] != first["turnId"]

    release_first.set()
    deadline = time.time() + 3
    while api._running and time.time() < deadline:
        time.sleep(0.02)
    assert api._running is False
    assert prompts == ["第一条", "第二条"]


def test_desktop_queued_turn_does_not_steal_later_session(monkeypatch, tmp_path: Path):
    import time
    import threading

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.agent import AgentResult
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    first_session = Session(tmp_path, session_id="first-session")
    queued_session = Session(tmp_path, session_id="queued-session")
    visible_session = Session(tmp_path, session_id="visible-session")
    for session, text in (
        (first_session, "第一段历史"),
        (queued_session, "第二段历史"),
        (visible_session, "当前可见历史"),
    ):
        session.append(Message("user", text))
    api.session = first_session

    entered_first = threading.Event()
    release_first = threading.Event()
    seen: list[tuple[str, str]] = []

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt, **kwargs):
            seen.append((prompt, kwargs["session"].session_id))
            if prompt == "第一条":
                entered_first.set()
                assert release_first.wait(timeout=2)
                kwargs["on_delta"]("迟到输出")
            return AgentResult(kwargs["session"].session_id, "完成", 1)

    monkeypatch.setattr(desktop, "FathomAgent", FakeAgent)

    api.send({"prompt": "第一条"})
    assert entered_first.wait(timeout=2)
    api.cancel()
    api.resume(queued_session.session_id)
    queued = api.send({"prompt": "第二条"})
    assert queued["queued"] is True
    api.resume(visible_session.session_id)

    release_first.set()
    deadline = time.time() + 3
    while api._running and time.time() < deadline:
        time.sleep(0.02)

    assert seen == [("第一条", "first-session"), ("第二条", "queued-session")]
    assert api.session is not None
    assert api.session.session_id == "visible-session"


def test_desktop_queued_turn_can_be_cancelled_before_it_starts(monkeypatch, tmp_path: Path):
    import time
    import threading

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.agent import AgentResult

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    entered_first = threading.Event()
    release_first = threading.Event()
    prompts: list[str] = []

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt, **kwargs):
            prompts.append(prompt)
            if prompt == "第一条":
                entered_first.set()
                assert release_first.wait(timeout=2)
                kwargs["on_delta"]("迟到输出")
            return AgentResult(kwargs["session"].session_id, "完成", 1)

    monkeypatch.setattr(desktop, "FathomAgent", FakeAgent)

    api.send({"prompt": "第一条"})
    assert entered_first.wait(timeout=2)
    api.cancel()
    queued = api.send({"prompt": "不应执行"})
    assert queued["queued"] is True

    cancelled = api.cancel({"turnId": queued["turnId"]})
    assert cancelled == {"ok": True, "running": True, "queuedCancelled": True}
    assert api._pending_turn is None

    release_first.set()
    deadline = time.time() + 3
    while api._running and time.time() < deadline:
        time.sleep(0.02)
    assert prompts == ["第一条"]


def test_desktop_rejects_deleting_a_queued_turn_session(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    pending = Session(tmp_path, session_id="pending-session")
    pending.append(Message("user", "保留"))
    api._pending_turn = {
        "prompt": "稍后执行",
        "images": [],
        "session_id": pending.session_id,
        "turn_id": "pending-turn",
        "goal": None,
    }

    deleted = api.delete_session(pending.session_id)

    assert deleted["ok"] is False
    assert "排队" in deleted["error"]
    assert pending.path.is_file()


def test_desktop_upload_saves_file(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    result = api.save_upload({"name": "../hello.txt", "content": "data:text/plain;base64,aGVsbG8="})
    assert result["ok"] is True
    assert Path(result["path"]).read_text(encoding="utf-8") == "hello"
    assert ".." not in result["name"]


def test_desktop_upload_preserves_same_named_files(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    first = api.save_upload({"name": "notes.txt", "content": "data:text/plain;base64,b25l"})
    second = api.save_upload({"name": "notes.txt", "content": "data:text/plain;base64,dHdv"})

    assert first["name"] == "notes.txt"
    assert second["name"] == "notes (2).txt"
    assert Path(first["path"]).read_text(encoding="utf-8") == "one"
    assert Path(second["path"]).read_text(encoding="utf-8") == "two"


def test_desktop_upload_rejects_invalid_base64(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    result = desktop.DesktopApi().save_upload({"name": "bad.txt", "content": "data:text/plain;base64,%%%"})
    assert result["ok"] is False
    assert "Base64" in result["error"]


def test_network_attachment_uses_server_filename_and_never_overwrites(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    class Response:
        headers = {
            "content-disposition": "attachment; filename*=UTF-8''%E6%B8%B8%E6%88%8F.html",
            "content-length": "4",
            "content-type": "text/html",
        }
        url = "https://cdn.example.test/download/opaque-id"

        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def raise_for_status(self): return None
        def iter_bytes(self, _size): yield b"game"

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def stream(self, *_args, **_kwargs): return Response()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setattr("httpx.Client", Client)
    api = desktop.DesktopApi()
    first = api.download_attachment({"url": "https://example.test/file?id=1"})
    second = api.download_attachment({"url": "https://example.test/file?id=1"})

    assert first["name"] == "游戏.html"
    assert second["name"] == "游戏 (2).html"
    assert first["sourceUrl"] == "https://example.test/file?id=1"
    assert Path(first["path"]).read_bytes() == b"game"
    assert Path(second["path"]).read_bytes() == b"game"


def test_failed_network_attachment_keeps_existing_file_and_cleans_part(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    class Response:
        headers = {
            "content-disposition": 'attachment; filename="keep.txt"',
            "content-length": str(desktop.MAX_NETWORK_ATTACHMENT_BYTES + 1),
        }
        url = "https://example.test/keep.txt"

        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def raise_for_status(self): return None

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def stream(self, *_args, **_kwargs): return Response()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setattr("httpx.Client", Client)
    api = desktop.DesktopApi()
    upload_dir = api.settings.workspace / ".deepseekfathom" / "uploads"
    upload_dir.mkdir(parents=True)
    existing = upload_dir / "keep.txt"
    existing.write_text("original", encoding="utf-8")

    result = api.download_attachment({"url": "https://example.test/keep.txt"})

    assert result["ok"] is False
    assert existing.read_text(encoding="utf-8") == "original"
    assert not list(upload_dir.glob("*.part-*"))


def test_desktop_configure_merges_existing_key(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    merge_file_config({"api_key": "sk-old", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"})
    api = desktop.DesktopApi()
    result = api.configure({
        "baseUrl": "https://example.com/v1",
        "model": "gpt-4o",
        "providerFormat": "openai-compatible",
        "requestTimeout": "45",
    })
    settings_obj = get_settings()
    assert settings_obj.api_key == "sk-old"
    assert settings_obj.base_url == "https://example.com/v1"
    assert settings_obj.model == "gpt-4o"
    assert settings_obj.provider_format == "openai-compatible"
    assert settings_obj.request_timeout == 45
    assert result["providerFormat"] == "openai-compatible"
    assert result["requestTimeout"] == 45


def test_desktop_configure_can_clear_custom_base_url(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.config import load_file_config
    from deepseekfathom._core.provider import DeepSeekClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://stale-env.example/v1")
    merge_file_config({
        "api_key": "sk-old",
        "base_url": "https://custom.example/v1",
        "provider_format": "openai",
    })
    api = desktop.DesktopApi()

    result = api.configure({"baseUrl": ""})

    assert result["baseUrl"] == ""
    assert api.settings.base_url == ""
    assert load_file_config()["base_url"] == ""
    client = DeepSeekClient(api.settings)
    try:
        assert client._base_url() == "https://api.openai.com/v1"
    finally:
        client.close()


def test_concurrent_config_merges_preserve_all_fields(monkeypatch, tmp_path: Path):
    import threading

    from deepseekfathom._core.config import load_file_config

    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    threads = [
        threading.Thread(target=merge_file_config, args=({f"field_{index}": index},))
        for index in range(24)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert load_file_config() == {f"field_{index}": index for index in range(24)}
    assert not list((tmp_path / "config-home").glob("*.tmp-*"))


def test_desktop_manual_compact(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()

    # force the model-summary path to fall back to local truncation (no network)
    class _NoNetClient:
        def __init__(self, *a, **k):
            pass

        def chat(self, messages):
            raise RuntimeError("no net")

    monkeypatch.setattr(desktop, "DeepSeekClient", _NoNetClient)
    api.session = Session(tmp_path, session_id="s")
    api.session.messages = [Message("system", "system")] + [Message("user", "old " + "x" * 200) for _ in range(20)]
    result = api.compact()
    assert result["ok"] is True
    assert result["after"] > 0
    assert len(api.session.messages) < 21
    assert "handoff summary" in api.session.messages[1].content
    assert isinstance(result["messages"], list)
    assert result["context"]["tokens"] is None
    assert result["context"]["localVisibleTokens"] == result["after"]
    assert result["context"]["usageState"] == "missing"
    reloaded = SessionStore(tmp_path).load("s")
    assert [message.content for message in reloaded.messages] == [message.content for message in api.session.messages]


def test_desktop_context_status_reports_local_context_threshold(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.settings = api.settings.with_runtime(model="custom-32k")
    api.session = Session(tmp_path, session_id="s")
    api.session.messages = [Message("system", "system"), Message("user", "x" * 8000)]

    status = api.context_status()
    assert status["ok"] is True
    assert status["tokens"] is None
    assert status["contextTokens"] is None
    assert status["localVisibleTokens"] > 1000
    assert status["inputTokens"] == 0
    assert status["outputTokens"] == 0
    assert status["cachedTokens"] == 0
    assert status["cachePercent"] is None
    assert status["cacheAvailable"] is False
    assert status["accurate"] is False
    assert status["usageAvailable"] is False
    assert status["usageState"] == "missing"
    assert status["measure"] == "上游未返回 usage，仅估算本地可见消息"
    assert status["limit"] == 32_000
    assert status["threshold"] == int(32_000 * 0.95)
    assert status["thresholdPercent"] == 95
    assert status["percent"] is None
    assert status["remainingTokens"] is None
    assert status["source"] == "model-name"

    api._usage_by_session["s"] = UsageStats(input_tokens=2000, output_tokens=300, cached_input_tokens=1500, total_tokens=2300, source="upstream")
    unchanged = api.context_status()
    assert unchanged["tokens"] is None
    assert unchanged["usageTotalTokens"] == 0
    assert unchanged["source"] == "model-name"

    api._context_by_session["s"] = {
        "model": "custom-32k",
        "tokens": 2450,
        "usage": UsageStats(input_tokens=2300, output_tokens=200, cached_input_tokens=1200, total_tokens=2500, source="upstream"),
    }
    measured = api.context_status()
    assert measured["tokens"] == 2450
    assert measured["inputTokens"] == 2300
    assert measured["cachedTokens"] == 1200
    assert measured["cacheMissTokens"] == 1100
    assert measured["cachePercent"] == round(1200 / 2300 * 100, 2)
    assert measured["accurate"] is True
    assert measured["usageAvailable"] is True
    assert measured["usageState"] == "current"
    assert measured["source"] == "upstream"
    assert measured["measure"] == "上游输入+输出实测"

    configured = api.configure_context({"contextWindowTokens": "64000", "compactThresholdPercent": "90"})
    assert configured["ok"] is True
    assert configured["context"]["limit"] == 64_000
    assert configured["context"]["threshold"] == 57_600
    assert configured["context"]["source"] == "upstream"
    assert configured["context"]["limitSource"] == "custom"
    assert configured["context"]["customLimit"] is True

    api.settings = api.settings.with_runtime(model="custom-32k")
    automatic = api.configure_context({"contextWindowTokens": "", "compactThresholdPercent": ""})
    assert automatic["ok"] is True
    assert automatic["context"]["customLimit"] is False
    assert automatic["context"]["limit"] == 32_000
    assert automatic["context"]["thresholdPercent"] == 95
    assert automatic["boot"]["model"] == "custom-32k"


def test_desktop_session_switch_returns_fresh_context(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.provider import UsageStats
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    first = Session(tmp_path, session_id="first")
    first.messages = [Message("system", "system"), Message("user", "old")]
    first.rewrite()
    second = Session(tmp_path, session_id="second")
    second.messages = [Message("system", "system"), Message("user", "new " * 400)]
    second.rewrite()
    api.session = first
    api._usage_by_session["first"] = UsageStats(input_tokens=5000, output_tokens=500, cached_input_tokens=2500, total_tokens=5500, source="upstream")

    fresh = api.new_session()
    assert fresh["context"]["sessionId"] is None
    assert fresh["context"]["tokens"] is None
    assert fresh["context"]["localVisibleTokens"] == 0
    assert fresh["context"]["accurate"] is False

    resumed = api.resume("second")
    assert resumed["context"]["sessionId"] == "second"
    assert resumed["context"]["accurate"] is False
    assert resumed["context"]["tokens"] is None
    assert 0 < resumed["context"]["localVisibleTokens"] < 5500

    old = api.resume("first")
    assert old["context"]["sessionId"] == "first"
    assert old["context"]["tokens"] is None
    assert old["context"]["usageTotalTokens"] == 0


def test_desktop_rejects_stale_session_navigation(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    for session_id in ("older", "newer"):
        session = Session(tmp_path, session_id=session_id)
        session.messages = [Message("user", session_id)]
        session.rewrite()

    api = desktop.DesktopApi()
    current = api.resume("newer", 2)
    stale = api.resume("older", 1)
    assert current["activated"] is True
    assert stale["stale"] is True and stale["activated"] is False
    assert api.session is not None and api.session.session_id == "newer"

    fresh = api.new_session(4)
    stale_after_new = api.resume("older", 3)
    assert fresh["activated"] is True
    assert stale_after_new["stale"] is True
    assert api.session is None


def test_desktop_context_usage_survives_restart(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.provider import UsageStats
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    first = desktop.DesktopApi()
    session = Session(tmp_path, session_id="persisted-usage")
    session.messages = [Message("system", "system"), Message("user", "hello"), Message("assistant", "world")]
    session.rewrite()
    first.session = session
    first._record_session_usage(
        session.session_id,
        UsageStats(input_tokens=180_000, output_tokens=800, cached_input_tokens=150_000, total_tokens=180_800, source="upstream"),
    )
    first._record_context_usage(
        session.session_id,
        UsageStats(input_tokens=140_000, output_tokens=500, cached_input_tokens=120_000, total_tokens=140_500, source="upstream"),
        list(session.messages),
        list(session.messages),
    )

    measured = first.context_status()
    assert measured["tokens"] == 140_500
    assert measured["sessionInputTokens"] == 180_000
    assert measured["outputTokens"] == 500
    assert measured["cacheHitTokens"] == 120_000
    assert measured["cacheMissTokens"] == 20_000
    assert measured["cachePercent"] == round(120_000 / 140_000 * 100, 2)
    assert measured["sessionCacheHitTokens"] == 150_000
    assert measured["sessionCacheMissTokens"] == 30_000
    assert measured["accurate"] is True

    restarted = desktop.DesktopApi()
    restored = restarted.resume(session.session_id)["context"]
    assert restored["tokens"] == 140_500
    assert restored["inputTokens"] == 140_000
    assert restored["cachedTokens"] == 120_000
    assert restored["cacheMissTokens"] == 20_000
    assert restored["sessionInputTokens"] == 180_000
    assert restored["sessionOutputTokens"] == 800
    assert restored["sessionTotalTokens"] == 180_800
    assert restored["accurate"] is True
    assert restored["usageState"] == "current"

    restarted.session.append(Message("user", "new " * 400))
    adjusted = restarted.context_status()
    assert adjusted["tokens"] > 140_500
    assert adjusted["accurate"] is False
    assert adjusted["usageAvailable"] is True
    assert adjusted["usageState"] == "adjusted"
    assert adjusted["measure"] == "上次上游输入+输出 + 校准后的当前增量"


def test_desktop_context_calibrates_local_delta_from_upstream_tokenizer(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    session = Session(tmp_path, session_id="calibrated")
    request = [Message("system", "system " * 200), Message("user", "hello " * 80)]
    session.messages = list(request)
    session.rewrite()
    api.session = session
    request_estimate = estimate_message_tokens(request)
    api._record_context_usage(
        session.session_id,
        UsageStats(input_tokens=request_estimate * 2, output_tokens=50, total_tokens=request_estimate * 2 + 50, source="upstream"),
        request,
        request,
    )

    base = api.context_status()
    assert base["tokens"] == request_estimate * 2 + 50
    assert base["calibrationFactor"] == 2.0
    session.append(Message("user", "new local tail " * 60))
    raw_delta = estimate_message_tokens(session.messages) - estimate_message_tokens(request)
    adjusted = api.context_status()
    assert adjusted["localDeltaTokens"] == raw_delta * 2
    assert adjusted["tokens"] == base["tokens"] + raw_delta * 2


def test_desktop_context_prefers_upstream_total_with_reasoning_tokens(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    session = Session(tmp_path, session_id="reasoning-total")
    request = [Message("system", "system"), Message("user", "hello")]
    session.messages = list(request)
    session.rewrite()
    api.session = session
    api._record_context_usage(
        session.session_id,
        UsageStats(
            input_tokens=1000,
            output_tokens=100,
            total_tokens=6100,
            source="upstream",
            reasoning_tokens=5000,
        ),
        request,
        request,
    )

    status = api.context_status()
    assert status["tokens"] == 6100
    assert status["usageTotalTokens"] == 6100
    assert status["reasoningTokens"] == 5000


def test_desktop_migrates_v2_context_snapshot_to_full_prompt_plus_output(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    session = Session(tmp_path, session_id="legacy-v2")
    session.messages = [Message("system", "system"), Message("user", "hello"), Message("assistant", "world")]
    session.rewrite()
    estimate = estimate_message_tokens(session.messages)
    SessionStore(tmp_path).update_metadata(
        session.session_id,
        context_usage={
            "schema": 2,
            "model": "deepseek-v4-flash",
            "tokens": 140_000,
            "current_estimate": estimate,
            "request_tokens": 140_000,
            "quality": "upstream",
            "input_tokens": 140_000,
            "output_tokens": 500,
            "cached_input_tokens": 120_000,
            "total_tokens": 140_500,
            "source": "upstream",
        },
    )

    restored = desktop.DesktopApi().resume(session.session_id)["context"]
    assert restored["tokens"] == 140_500
    assert restored["cacheHitTokens"] == 120_000
    assert restored["cacheMissTokens"] == 20_000


def test_desktop_context_marks_upstream_usage_that_is_smaller_than_sent_prompt(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    session = Session(tmp_path, session_id="underreported")
    request = [Message("system", "x" * 40_000), Message("user", "hello")]
    session.messages = list(request)
    session.rewrite()
    api.session = session
    api._record_context_usage(
        session.session_id,
        UsageStats(input_tokens=1236, output_tokens=95, total_tokens=1331, source="upstream"),
        request,
        request,
    )

    status = api.context_status()
    assert status["usageState"] == "underreported"
    assert status["accurate"] is False
    assert status["reportedInputTokens"] == 1236
    assert status["inputTokens"] > 9000
    assert status["tokens"] == status["inputTokens"] + status["outputTokens"]
    assert status["source"] == "upstream-underreported"


def test_desktop_brand_uses_transparent_whale_asset():
    root = Path(__file__).parents[1] / "src" / "deepseekfathom" / "_core" / "desktop" / "assets"
    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "style.css").read_text(encoding="utf-8")
    icon = (root / "app-icon.png").read_bytes()

    brand = html.split('<div class="brand">', 1)[1].split('<div class="brandText">', 1)[0]
    assert '<img src="app-icon.png" alt="">' in brand
    assert "<svg" not in brand
    assert 'class="introLogo" src="app-icon.png"' in html
    assert '<span id="version">v...</span>' in html
    assert 'id="settingsView"' in html and '<dialog id="settingsDialog"' not in html
    assert 'id="settingsBackTop"' in html and 'id="settingsBackBottom"' in html
    js = (root / "app.js").read_text(encoding="utf-8")
    boot_body = js.split("async function boot()", 1)[1].split("function fillSelect", 1)[0]
    assert "refreshModels().catch" in boot_body
    assert '$("requestTimeout").value = String(state.boot.requestTimeout || 60)' in js
    assert ":root[data-theme=\"light\"]" in css and "--bg: #f6f6f6" in css
    sidebar_css = css.split(".sidebarSettings {", 1)[1].split("}", 1)[0]
    assert "position: static" in sidebar_css
    assert "position: fixed" not in sidebar_css
    assert ".logo img" in css
    assert "background: transparent" in css
    assert 'id="sessionScrollbar"' in html and 'id="sessionScrollThumb"' in html
    assert 'id="ctxOutput"' in html and 'id="ctxCache"' in html and 'id="ctxSessionCache"' in html
    assert 'class="convItem" data-act="exportMd">导出 Markdown</button>' in html
    assert "window.pywebview.api.export_session(sid)" in js
    assert 'style.css?v=__DESKTOP_VERSION__' in html and 'app.js?v=__DESKTOP_VERSION__' in html
    assert 'state.currentAssistant.remove();' in js
    assert "suppressedTurnIds: new Set()" in js
    assert "state.suppressedTurnIds.add(turnId)" in js
    assert '$("cancel").hidden = !state.running || !state.activeTurnId;' in js
    assert "function syncRunControls()" in js
    assert 'event === "native:drop"' in js
    assert "sessions.slice(0, 40)" not in js
    assert "sessions.forEach" in js
    assert "function initSessionScrollbar()" in js
    assert "THINKING_TIERS = ThinkingMode.user_selectable_names()" in (root.parent / "app.py").read_text(encoding="utf-8")
    assert '"ultra": "XHigh"' in (root.parent / "app.py").read_text(encoding="utf-8")
    assert icon.startswith(b"\x89PNG\r\n\x1a\n")


def test_desktop_send_exposes_selected_local_attachment_paths(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    captured: dict[str, str] = {}

    def fake_start(prompt, **_kwargs):
        captured["prompt"] = prompt
        return {"ok": True, "sessionId": "s", "turnId": "t"}

    monkeypatch.setattr(api, "_start_turn", fake_start)
    result = api.send({
        "prompt": "处理附件",
        "attachments": [
            {"name": "plain.txt", "path": "/tmp/private/plain.txt", "size": 5, "kind": "local_file"},
            {"name": "docs", "path": "/tmp/private/docs", "size": 0, "kind": "folder"},
        ],
    })

    assert result["ok"] is True
    prompt = captured["prompt"]
    assert "plain.txt: /tmp/private/plain.txt (5 bytes)" in prompt
    assert "docs: /tmp/private/docs" in prompt
    assert "本机/网络附件路径" in prompt


def test_desktop_local_file_selection_does_not_copy_contents(tmp_path: Path):
    from deepseekfathom._core.desktop.app import describe_local_paths

    source = tmp_path / "large-local.bin"
    source.write_bytes(b"local-only")
    described = describe_local_paths([str(source), str(tmp_path / "missing.bin")])
    assert described == [{
        "ok": True,
        "name": "large-local.bin",
        "path": str(source.resolve()),
        "size": 10,
        "kind": "local_file",
    }]


def test_native_drop_extracts_pywebview_full_paths(tmp_path: Path):
    from deepseekfathom._core.desktop.app import native_drop_paths

    first = str(tmp_path / "one.txt")
    second = str(tmp_path / "two.txt")
    event = {"dataTransfer": {"files": [
        {"name": "one.txt", "pywebviewFullPath": first},
        {"name": "one.txt", "pywebviewFullPath": first},
        {"name": "two.txt", "pywebviewFullPath": second},
        {"name": "missing.txt"},
    ]}}
    assert native_drop_paths(event) == [first, second]


def test_pyinstaller_uses_checkout_assets_instead_of_stale_site_package():
    root = Path(__file__).parents[1]
    spec = (root / "DeepSeekFathom.spec").read_text(encoding="utf-8")
    preparer = (root / "scripts" / "prepare_desktop_build.py").read_text(encoding="utf-8")
    build_script = (root / "scripts" / "build_windows_exe.ps1").read_text(encoding="utf-8")
    assert "tmp_ret = collect_all('deepseekfathom')" not in spec
    assert "build\\\\desktop-release\\\\assets" in spec
    assert "pathex=['src']" in spec
    assert 'root / "src" / "deepseekfathom" / "_core" / "desktop" / "assets"' in preparer
    assert build_script.index("prepare_desktop_build.py") < build_script.index("PyInstaller")


def test_desktop_cancel_ignores_stale_turn_id(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api._running = True
    api._active_turn_id = "current-turn"
    api._active_turn_session_id = "s"

    stale = api.cancel({"turnId": "old-turn"})
    assert stale == {"ok": True, "running": True, "ignored": True}
    assert api._running is True
    assert api._active_turn_id == "current-turn"
    assert "current-turn" not in api._abandoned_turn_ids

    current = api.cancel({"turnId": "current-turn"})
    assert current == {"ok": True, "running": True, "cancelling": True}
    assert "current-turn" in api._abandoned_turn_ids
    assert api._cancel_requested is True


def test_desktop_rejects_deleting_or_compacting_active_turn_session(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="active-session")
    api.session.append(Message("user", "keep"))
    api._running = True
    api._active_turn_session_id = "active-session"

    deleted = api.delete_session("active-session")
    compacted = api.compact()

    assert deleted["ok"] is False
    assert compacted["ok"] is False
    assert api.session.path.is_file()


def test_desktop_turn_events_stay_bound_to_origin_session(monkeypatch, tmp_path: Path):
    import json
    import re
    import threading

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.agent import AgentResult
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    turn_session = Session(tmp_path, session_id="turn-session")
    other_session = Session(tmp_path, session_id="other-session")
    other_session.append(Message("user", "旧对话"))
    api.session = turn_session

    entered_run = threading.Event()
    release_run = threading.Event()

    class Window:
        def __init__(self):
            self.events = []

        def evaluate_js(self, script):
            match = re.search(r"onNativeEvent\((.*)\);$", script)
            assert match, script
            self.events.append(json.loads(match.group(1)))

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **kwargs):
            assert kwargs["session"].session_id == "turn-session"
            entered_run.set()
            assert release_run.wait(timeout=2)
            kwargs["on_event"]("subagent researcher")
            kwargs["on_delta"]("后台输出")
            kwargs["on_final"]("后台最终输出")
            return AgentResult("turn-session", "后台最终输出", 1)

    window = Window()
    api.bind_window(window)
    monkeypatch.setattr(desktop, "FathomAgent", FakeAgent)

    worker = threading.Thread(target=api._run_agent_turn, args=("新对话任务", []), daemon=True)
    worker.start()
    assert entered_run.wait(timeout=2)

    resumed = api.resume("other-session")
    assert resumed["sessionId"] == "other-session"
    release_run.set()
    worker.join(timeout=2)

    assert api.session is not None
    assert api.session.session_id == "other-session"
    emitted = [event for event in window.events if event["event"] in {"turn:start", "agent:event", "assistant:delta", "assistant:final", "turn:done"}]
    assert emitted
    assert {event["payload"].get("sessionId") for event in emitted} == {"turn-session"}
    assert all(event["payload"].get("turnId") for event in emitted)


def test_desktop_edit_resend_drops_old_tool_result_context(monkeypatch, tmp_path: Path):
    import time

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.agent import AgentResult
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="edit-session")
    api.session.append(Message("system", "system"))
    api.session.append(Message("user", "你好，帮我创建 a.txt"))
    api.session.append(Message("assistant", '{"tool":"write_file","arguments":{"path":"a.txt","content":"ok"}}'))
    api.session.append(Message("user", 'TOOL_RESULT name=write_file\n{"ok":true,"output":"created a.txt"}'))
    api.session.append(Message("assistant", "我已经创建了。"))

    captured = {}

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["messages"] = [message.content for message in kwargs["session"].messages]
            return AgentResult(kwargs["session"].session_id, "新的回答", 1)

    monkeypatch.setattr(desktop, "FathomAgent", FakeAgent)
    result = api.edit_resend({"prompt": "你好，帮我创建 b.txt", "srcIndex": 1})
    assert result["ok"] is True
    deadline = time.time() + 2
    while api._running and time.time() < deadline:
        time.sleep(0.02)
    assert api._running is False

    joined = "\n".join(captured["messages"])
    assert captured["prompt"] == "你好，帮我创建 b.txt"
    assert "created a.txt" not in joined
    assert "我已经创建了" not in joined
    assert "你好，帮我创建 a.txt" not in joined
    assert captured["messages"] == ["system"]


def test_desktop_edit_resend_preserves_later_turns_after_regeneration(monkeypatch, tmp_path: Path):
    import time

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.agent import AgentResult
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session, SessionStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="edit-preserve-session")
    for message in [
        Message("system", "system"),
        Message("user", "第一问"),
        Message("assistant", "第一答"),
        Message("user", "第二问原文"),
        Message("assistant", "第二答原文"),
        Message("user", "第三问"),
        Message("assistant", "第三答"),
    ]:
        api.session.append(message)

    captured = {}

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["messages"] = [message.content for message in kwargs["session"].messages]
            kwargs["session"].append(Message("user", prompt))
            kwargs["session"].append(Message("assistant", "第二答新版"))
            return AgentResult(kwargs["session"].session_id, "第二答新版", 1)

    monkeypatch.setattr(desktop, "FathomAgent", FakeAgent)
    result = api.edit_resend({"prompt": "第二问新版", "srcIndex": 3})
    assert result["ok"] is True
    deadline = time.time() + 2
    while api._running and time.time() < deadline:
        time.sleep(0.02)
    assert api._running is False

    assert captured["prompt"] == "第二问新版"
    assert captured["messages"] == ["system", "第一问", "第一答"]
    persisted = [message.content for message in SessionStore(tmp_path).load("edit-preserve-session").messages]
    assert persisted == ["system", "第一问", "第一答", "第二问新版", "第二答新版", "第三问", "第三答"]


def test_desktop_retry_preserves_images_and_commits_only_after_success(monkeypatch, tmp_path: Path):
    import time

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.agent import AgentResult
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session, SessionStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="retry-image-session")
    image = "data:image/png;base64,aW1hZ2U="
    api.session.append(Message("user", "describe image", images=[image]))
    api.session.append(Message("assistant", "old answer"))
    original = api.session.path.read_bytes()

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt, **kwargs):
            assert kwargs["images"] == [image]
            assert kwargs["session"].persist is False
            assert api.session.path.read_bytes() == original
            kwargs["session"].append(Message("user", prompt, images=list(kwargs["images"])))
            kwargs["session"].append(Message("assistant", "new answer"))
            return AgentResult(kwargs["session"].session_id, "new answer", 1)

    monkeypatch.setattr(desktop, "FathomAgent", FakeAgent)
    result = api.retry({"srcIndex": 1})
    assert result["ok"] is True
    deadline = time.time() + 2
    while api._running and time.time() < deadline:
        time.sleep(0.02)

    persisted = SessionStore(tmp_path).load("retry-image-session").messages
    assert [(message.role, message.content) for message in persisted] == [
        ("user", "describe image"),
        ("assistant", "new answer"),
    ]
    assert persisted[0].images == [image]


def test_desktop_retry_failure_keeps_original_jsonl_and_restores_transcript(monkeypatch, tmp_path: Path):
    import json
    import re
    import time

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="retry-rollback-session")
    api.session.append(Message("user", "keep prompt"))
    api.session.append(Message("assistant", "keep answer"))
    original = api.session.path.read_bytes()

    class Window:
        def __init__(self):
            self.events = []

        def evaluate_js(self, script):
            match = re.search(r"onNativeEvent\((.*)\);$", script)
            assert match, script
            self.events.append(json.loads(match.group(1)))

    class FailingAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt, **kwargs):
            assert api.session.path.read_bytes() == original
            kwargs["session"].append(Message("user", prompt))
            kwargs["session"].append(Message("assistant", "partial replacement"))
            raise OSError("provider failed")

    window = Window()
    api.bind_window(window)
    monkeypatch.setattr(desktop, "FathomAgent", FailingAgent)
    result = api.retry({"srcIndex": 1})
    assert result["ok"] is True
    deadline = time.time() + 2
    while api._running and time.time() < deadline:
        time.sleep(0.02)

    assert api.session.path.read_bytes() == original
    error = next(event for event in window.events if event["event"] == "turn:error")
    assert [(row["role"], row["content"]) for row in error["payload"]["messages"]] == [
        ("user", "keep prompt"),
        ("assistant", "keep answer"),
    ]


def test_desktop_cancelled_retry_keeps_original_jsonl(monkeypatch, tmp_path: Path):
    import threading
    import time

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="retry-cancel-session")
    api.session.append(Message("user", "original prompt"))
    api.session.append(Message("assistant", "original answer"))
    original = api.session.path.read_bytes()
    entered = threading.Event()

    class CancelAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt, **kwargs):
            kwargs["session"].append(Message("user", prompt))
            kwargs["session"].append(Message("assistant", "partial"))
            entered.set()
            deadline = time.time() + 2
            while not kwargs["should_cancel"]() and time.time() < deadline:
                time.sleep(0.01)
            raise RuntimeError("turn cancelled")

    monkeypatch.setattr(desktop, "FathomAgent", CancelAgent)
    result = api.retry({"srcIndex": 1})
    assert entered.wait(timeout=2)
    cancelled = api.cancel({"turnId": result["turnId"]})
    assert cancelled["cancelling"] is True
    deadline = time.time() + 2
    while api._running and time.time() < deadline:
        time.sleep(0.02)

    assert api._running is False
    assert api.session.path.read_bytes() == original


def test_desktop_branch_preserves_message_images(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session, SessionStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="branch-image-source")
    image = "data:image/png;base64,aW1hZ2U="
    api.session.append(Message("user", "look", images=[image]))
    api.session.append(Message("assistant", "seen"))

    result = api.branch({"srcIndex": 1})
    assert result["ok"] is True
    forked = SessionStore(tmp_path).load(result["sessionId"])
    assert forked.messages[0].images == [image]


def test_desktop_send_passes_goal_to_agent(monkeypatch, tmp_path: Path):
    import time

    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.agent import AgentResult
    from deepseekfathom._core.session import Session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
    api = desktop.DesktopApi()
    api.session = Session(tmp_path, session_id="goal-session")
    captured = {}

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["goal"] = kwargs.get("goal")
            return AgentResult(kwargs["session"].session_id, "目标已完成。", 1)

    monkeypatch.setattr(desktop, "FathomAgent", FakeAgent)
    result = api.send({"prompt": "继续", "goal": "完成部署"})
    assert result["ok"] is True
    deadline = time.time() + 2
    while api._running and time.time() < deadline:
        time.sleep(0.02)
    assert api._running is False

    assert captured["prompt"] == "继续"
    assert captured["goal"] == "完成部署"


def test_desktop_session_metadata_pin_and_rename(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.desktop.app as desktop
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session, SessionStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config-home"))
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
    from deepseekfathom._core.session import session_title_from_text

    assert session_title_from_text("  你好   世界  ") == "你好 世界"
    assert session_title_from_text("") == "未命名会话"


def test_desktop_event_parser():
    from deepseekfathom._core.desktop.app import parse_agent_event

    assert parse_agent_event("tool run_shell command=ls") == {"kind": "tool", "name": "run_shell", "detail": "command=ls"}
    assert parse_agent_event("thinking pass 1/2")["kind"] == "thinking"
    assert parse_agent_event("skill repo-debug")["kind"] == "skill"
    assert parse_agent_event("done read_file") == {"kind": "done", "name": "read_file", "detail": ""}

    import base64

    # subagentdone now carries the subagent's full final summary (base64) so its card
    # can show the complete result, not just "rounds=N"
    enc = base64.b64encode("结论X".encode()).decode()
    done = parse_agent_event(f"subagentdone helper␟rounds=3␟{enc}")
    assert done == {"kind": "subagentdone", "name": "helper", "detail": "结论X"}

    # held tool-call output raises a pending signal for the loading indicator
    assert parse_agent_event("toolpending") == {"kind": "toolpending", "name": "", "detail": ""}

    # a subagent's own narration is nested under its group
    note = base64.b64encode("子代理输出".encode()).decode()
    inner = parse_agent_event(f"subevent helper␟subanswer {note}")
    assert inner["kind"] == "subanswer" and inner["sub"] == "helper" and inner["detail"] == "子代理输出"


def test_session_handoff_prints_resume_command(capsys):
    from deepseekfathom._core.cli import print_session_handoff

    print_session_handoff("abc-123")
    err = capsys.readouterr().err
    assert "[session] abc-123" in err
    assert "deepseekfathom start --resume abc-123" in err


def test_slash_palette_prints_commands_and_tools(tmp_path: Path, monkeypatch, capsys):
    from deepseekfathom._core.cli import print_palette

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
    from deepseekfathom._core.cli import print_recent_session_messages
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session

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
            from deepseekfathom._core.agent import AgentResult

            return AgentResult("abc-123", "done", 1)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("deepseekfathom._core.cli.FathomAgent", FakeAgent)
    code = main(["run", "--mode", "plan", "hello"])
    captured = capsys.readouterr()
    assert code == 0
    assert "done" in captured.out
    assert "[resume]" not in captured.err


def test_interactive_model_command_uses_picker(monkeypatch, tmp_path: Path, capsys):
    import deepseekfathom._core.cli as cli

    prompts = iter(["/model", "/exit"])
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))

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
    import deepseekfathom._core.cli as cli

    prompts = iter(["/think", "/exit"])
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))

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


def test_interactive_think_command_switches_to_max(monkeypatch, tmp_path: Path, capsys):
    import deepseekfathom._core.cli as cli

    prompts = iter(["/think max", "/exit"])
    monkeypatch.setenv("DEEPSEEKFATHOM_CONFIG_HOME", str(tmp_path / "config"))

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def ping(self):
            return {"model_available": True}

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert "thinking set to max" in out
    assert "reasoning_effort=max" in out
    assert "internal_passes=6" in out
    assert get_settings().default_thinking == "max"


def test_interactive_subagents_command_returns_to_prompt(monkeypatch, tmp_path: Path, capsys):
    import deepseekfathom._core.cli as cli

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
    import deepseekfathom._core.cli as cli

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
    import deepseekfathom._core.cli as cli

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
            from deepseekfathom._core.agent import AgentResult
            from deepseekfathom._core.messages import Message
            from deepseekfathom._core.session import Session

            captured["goal"] = kwargs.get("goal")
            session = Session(tmp_path, session_id="abc-123")
            session.append(Message("assistant", "目标已完成。"))
            return AgentResult(session.session_id, "目标已完成。", 1)

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)
    monkeypatch.setattr(cli, "FathomAgent", FakeAgent)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert "goal     : 完成部署" in out
    assert captured["goal"] == "完成部署"


def test_interactive_line_mode_streams_agent_output(monkeypatch, tmp_path: Path, capsys):
    import deepseekfathom._core.cli as cli

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
            from deepseekfathom._core.agent import AgentResult
            from deepseekfathom._core.messages import Message
            from deepseekfathom._core.session import Session

            captured["stream"] = kwargs.get("stream")
            kwargs["on_delta"]("流")
            kwargs["on_delta"]("式")
            session = Session(tmp_path, session_id="abc-123")
            session.append(Message("assistant", "流式"))
            return AgentResult(session.session_id, "流式", 1)

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)
    monkeypatch.setattr(cli, "FathomAgent", FakeAgent)

    code = cli.interactive(settings(tmp_path), "root", "fast", True)
    out = capsys.readouterr().out
    assert code == 0
    assert captured["stream"] is True
    assert "流式" in out


def test_interactive_line_mode_uses_spinner_until_first_delta(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.cli as cli

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
            from deepseekfathom._core.agent import AgentResult
            from deepseekfathom._core.messages import Message
            from deepseekfathom._core.session import Session

            events.append("run")
            kwargs["on_delta"]("ok")
            session = Session(tmp_path, session_id="abc-123")
            session.append(Message("assistant", "ok"))
            return AgentResult(session.session_id, "ok", 1)

    monkeypatch.setattr(cli, "startup_animation", lambda enabled=True: None)
    monkeypatch.setattr(cli, "read_composer", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)
    monkeypatch.setattr(cli, "ThinkingSpinner", FakeSpinner)
    monkeypatch.setattr(cli, "FathomAgent", FakeAgent)

    assert cli.interactive(settings(tmp_path), "root", "fast", True) == 0
    assert events[:3] == ["enter:thinking:fast", "run", "stop"]
    assert "clear" not in events[:4]


def test_interactive_startup_prints_version(monkeypatch, tmp_path: Path, capsys):
    import deepseekfathom._core.cli as cli

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
    assert "app      : DeepSeekFathom" in out
    assert "version  : test (latest)" in out


def test_auto_thinking_uses_model_choice(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.cli as cli

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def chat(self, _messages):
            return "deep"

    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)
    selected = cli.choose_auto_thinking(settings(tmp_path), "hard debugging task")
    assert selected.name == "deep"


def test_auto_thinking_keeps_ultra_before_max(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.cli as cli

    captured = {}

    class FakeDeepSeekClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def chat(self, messages):
            captured["prompt"] = messages[-1].content
            return "max"

    monkeypatch.setattr(cli, "DeepSeekClient", FakeDeepSeekClient)
    selected = cli.choose_auto_thinking(settings(tmp_path), "complex task")

    assert selected.name == "max"
    assert captured["prompt"].index("ultra") < captured["prompt"].index("max")


def test_internal_thinking_runs_extra_model_pass(tmp_path: Path, monkeypatch):
    # local deliberation is now opt-in (thinking is normally delegated to the upstream
    # reasoning param); enable it explicitly for this legacy-behavior test
    monkeypatch.setenv("DSTUL_LOCAL_DELIBERATION", "1")
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
    result = FathomAgent(settings(tmp_path), thinking="deep", client=client).run("solve")
    assert client.calls == 3
    assert result.answer == "final answer"


def test_context_compaction_keeps_recent_messages(monkeypatch):
    from deepseekfathom._core.messages import Message
    import deepseekfathom._core.agent as agent

    messages = [Message("system", "system")]
    messages.extend(Message("user", "old " + str(index) + " " + ("x" * 200)) for index in range(20))
    monkeypatch.setattr(agent, "context_window_tokens", lambda _model: 200)

    compacted = compact_context_messages(messages, "tiny", force=True)
    assert len(compacted) < len(messages)
    assert "handoff summary" in compacted[1].content
    assert "old 19" in compacted[-1].content


def test_token_estimate_handles_cjk_and_images():
    english = estimate_message_tokens([Message("user", "a" * 400)])
    chinese = estimate_message_tokens([Message("user", "中" * 400)])
    image = estimate_message_tokens([Message("user", "看图", images=["data:image/png;base64,abc"])])

    assert 95 <= english <= 110
    assert chinese >= 400
    assert image >= 1024


def test_auto_compaction_is_persisted(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.agent as agent
    from deepseekfathom._core.session import Session

    monkeypatch.setattr(agent, "context_window_tokens", lambda _model: 200)
    session = Session(tmp_path, session_id="auto-compact")
    session.messages = [Message("system", "system")]
    session.messages.extend(Message("user", "old " + str(index) + " " + ("x" * 200)) for index in range(20))
    session.rewrite()

    result = FathomAgent(settings(tmp_path), client=FakeClient(["persisted summary", "final answer"])).run(
        "new request", session=session, require_todo=False
    )

    assert result.answer == "final answer"
    reloaded = SessionStore(tmp_path).load("auto-compact")
    assert any("persisted summary" in message.content for message in reloaded.messages if message.role == "system")
    assert len(reloaded.messages) < 23


def test_context_window_info_handles_current_global_and_china_models():
    assert context_window_tokens("custom-32k") == 32_000
    assert context_window_tokens("context-1m-2025-08-07") == 1_000_000
    assert context_window_info("gpt-5.4")["tokens"] == 1_000_000
    assert context_window_info("gpt-4o")["tokens"] == 128_000
    assert context_window_info("claude-sonnet-5")["tokens"] == 1_000_000
    assert context_window_info("gemini-2.5-pro")["tokens"] == 1_000_000
    assert context_window_info("deepseek-v4-flash")["tokens"] == 1_000_000
    assert context_window_info("qwen3.7-plus")["tokens"] == 1_000_000
    assert context_window_info("kimi-k2.6")["tokens"] == 256_000
    assert context_window_info("glm-5.2")["tokens"] == 1_000_000
    assert context_window_info("glm-4.7")["tokens"] == 200_000
    assert context_window_info("glm-4.6")["tokens"] == 200_000
    assert context_window_info("minimax-m3")["tokens"] == 1_000_000
    assert context_window_info("doubao-1.6-pro-256k")["tokens"] == 256_000
    assert context_window_info("unknown-model")["source"] == "fallback"


def test_context_compaction_uses_model_handoff_summary(monkeypatch):
    from deepseekfathom._core.messages import Message
    import deepseekfathom._core.agent as agent

    messages = [Message("system", "system")]
    messages.extend(Message("user", "old " + str(index) + " " + ("x" * 200)) for index in range(20))
    monkeypatch.setattr(agent, "context_window_tokens", lambda _model: 200)

    class SummaryClient:
        def __init__(self):
            self.saw_prompt = False

        def chat(self, msgs):
            # the last message must be the compaction instruction
            self.saw_prompt = "CONTEXT CHECKPOINT COMPACTION" in msgs[-1].content
            return "PROGRESS: did X. NEXT: do Y."

    client = SummaryClient()
    compacted = compact_context_messages(messages, "tiny", force=True, client=client)
    assert client.saw_prompt
    assert "PROGRESS: did X. NEXT: do Y." in compacted[1].content
    # the model summary replaces local truncation entirely
    assert "xxxxxxxxxx" not in compacted[1].content


def test_session_persists_and_reloads_images(tmp_path: Path):
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session, SessionStore

    img = "data:image/png;base64,iVBORw0KGgoAAAANS"
    session = Session(tmp_path, session_id="img")
    session.append(Message("user", "看这张图", images=[img]))

    reloaded = SessionStore(tmp_path).load("img")
    assert reloaded.messages[-1].images == [img]


def test_auto_context_compaction_can_be_disabled(monkeypatch):
    from deepseekfathom._core.messages import Message
    import deepseekfathom._core.agent as agent

    messages = [Message("system", "system")]
    messages.extend(Message("user", "old " + ("x" * 200)) for _ in range(20))
    monkeypatch.setattr(agent, "context_window_tokens", lambda _model: 200)
    monkeypatch.setenv("DSTUL_AUTO_COMPACT", "0")

    assert compact_context_messages(messages, "tiny") is messages


def test_update_version_comparison():
    from deepseekfathom._core.updates import is_newer, normalize_version

    assert normalize_version("v0.1.2") == "0.1.2"
    assert is_newer("v0.1.2", "0.1.1") is True
    assert is_newer("v0.1.1", "0.1.1") is False


def test_cli_and_desktop_versions_are_independent():
    import tomllib

    from deepseekfathom._core import __version__
    from deepseekfathom._core.desktop import DESKTOP_VERSION
    from deepseekfathom._core.updates import REPO
    from scripts.prepare_desktop_build import render_version_info

    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == "deepseekfathom"
    assert project["version"] == __version__ == "0.1.109"
    assert project["scripts"]["deepseekfathom"] == "deepseekfathom.cli:main"
    assert DESKTOP_VERSION == "0.1.17"
    assert REPO == "ffffff233/DeepSeekFathom"
    installer = (root / "scripts" / "windows_installer.iss").read_text(encoding="utf-8")
    assert "MyAppVersion must be provided" in installer
    assert '#define MyAppVersion "0.1.17"' not in installer
    version_info = render_version_info(DESKTOP_VERSION)
    assert 'filevers=(0, 1, 17, 0)' in version_info
    assert 'prodvers=(0, 1, 17, 0)' in version_info
    assert "StringStruct('FileVersion', '0.1.17')" in version_info
    notice = (root / "NOTICE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 Reasonix Contributors" in notice
    assert "78e9e2656ae5275cbdd29429053fdcc1cc97373c" in notice
    assert 'Source: "..\\NOTICE"; DestDir: "{app}"; DestName: "NOTICE.txt"' in installer
    assert 'Source: "..\\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"' in installer
    assert "third-party-licenses" in (root / "DeepSeekFathom.spec").read_text(encoding="utf-8")


def test_update_refuses_dirty_source_tree(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.updates as updates

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
    import deepseekfathom._core.updates as updates
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
    import deepseekfathom._core.ui as ui

    monkeypatch.setattr(ui, "termios", None)
    monkeypatch.setattr(ui, "tty", None)
    monkeypatch.setattr(builtins, "input", lambda prompt: prompt + "hello")

    assert ui.read_composer("p> ") == "p> hello"
    assert ui.choose_palette([("/model", "choose model")]) is None


@pytest.mark.parametrize("use_ansi", [True, False])
def test_windows_choose_palette_supports_vt_and_plain_consoles(monkeypatch, use_ansi):
    import deepseekfathom._core.ui as ui

    class TtyBuffer(io.StringIO):
        def isatty(self):
            return True

    class FakeMsvcrt:
        def __init__(self):
            self.keys = ["\xe0", "P", "\r"]

        def getwch(self):
            return self.keys.pop(0)

    output = TtyBuffer()
    monkeypatch.setattr(ui.os, "name", "nt")
    monkeypatch.setattr(ui.sys, "stdin", TtyBuffer())
    monkeypatch.setattr(ui.sys, "stdout", output)
    monkeypatch.setattr(ui, "_load_msvcrt", FakeMsvcrt)
    monkeypatch.setattr(ui, "terminal_supports_ansi", lambda _stream=None: use_ansi)

    selected = ui.choose_palette(
        [("deepseek-v4-flash", "current"), ("deepseek-v4-pro", "available")],
        title="models",
    )

    assert selected == "deepseek-v4-pro"
    if use_ansi:
        assert "\033[?25l" in output.getvalue()
    else:
        assert "\033[" not in output.getvalue()
        assert "models /" in output.getvalue()


def test_windows_palette_does_not_steal_j_as_navigation(monkeypatch):
    import deepseekfathom._core.ui as ui

    class TtyBuffer(io.StringIO):
        def isatty(self):
            return True

    class FakeMsvcrt:
        def __init__(self):
            self.keys = ["j", "\r"]

        def getwch(self):
            return self.keys.pop(0)

    output = TtyBuffer()
    monkeypatch.setattr(ui.sys, "stdout", output)

    selected = ui.windows_slash_select(
        [("/json", "JSON tools"), ("/zeta", "last item")],
        FakeMsvcrt(),
        use_ansi=False,
    )

    assert selected == "/json"


def test_update_git_failure_falls_back_to_tarball(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.updates as updates

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
    import deepseekfathom._core.cli as cli
    from deepseekfathom._core.updates import UpdateInfo

    monkeypatch.setattr(cli, "check_for_update", lambda current, timeout=5.0: UpdateInfo(current, "0.1.2", "url"))
    monkeypatch.setattr(cli, "update_to", lambda version: (True, f"updated {version}"))

    assert cli.update_command(check_only=False) == 0
    out = capsys.readouterr().out
    assert "0.1.0" not in out
    assert "updated 0.1.2" in out


def test_session_store_lists_and_loads_messages(tmp_path: Path):
    client = FakeClient(["hello"])
    result = FathomAgent(settings(tmp_path), mode="plan", client=client).run("say hello")
    store = SessionStore(tmp_path)
    listed = store.list()
    assert listed[0]["session_id"] == result.session_id
    loaded = store.load(result.session_id)
    assert [message.role for message in loaded.messages] == ["system", "user", "assistant"]


def test_session_list_uses_index_without_loading_large_transcript(monkeypatch, tmp_path: Path):
    session = Session(tmp_path, session_id="metadata-fast-path")
    session.append(Message("user", "large conversation"))
    store = SessionStore(tmp_path)
    store.update_metadata(
        session.session_id,
        title="Cached title",
        message_count=321,
        created_at=session.created_at,
    )
    monkeypatch.setattr(store, "load", lambda _session_id: (_ for _ in ()).throw(AssertionError("transcript loaded")))

    listed = store.list()
    assert listed[0]["title"] == "Cached title"
    assert listed[0]["messages"] == 1


def test_session_list_persistent_index_omits_images_and_preserves_recovery(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.session as session_module

    image = "data:image/png;base64," + ("A" * 250_000)
    session = Session(tmp_path, session_id="image-index")
    session.append(Message("user", "inspect this image", images=[image]))
    session.append(Message("assistant", "done"))

    index_path = session.path.with_suffix(".index.json")
    index_text = index_path.read_text(encoding="utf-8")
    assert len(index_text) < 1_000
    assert image not in index_text

    with session_module._SESSION_LIST_CACHE_LOCK:
        session_module._SESSION_LIST_CACHE.clear()

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("a current index must avoid scanning the JSONL image payload")

    monkeypatch.setattr(session_module, "_scan_session_summary", fail_scan)
    rows = SessionStore(tmp_path).list()

    assert rows[0]["session_id"] == "image-index"
    assert rows[0]["messages"] == 2
    assert rows[0]["title"] == "inspect this image"
    assert SessionStore(tmp_path).load("image-index").messages[0].images == [image]


def test_session_list_upgrades_current_legacy_metadata_without_parsing_images(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.session as session_module

    sessions_dir = tmp_path / ".deepseekfathom" / "sessions"
    sessions_dir.mkdir(parents=True)
    path = sessions_dir / "legacy-image.jsonl"
    image = "data:image/png;base64," + ("B" * 250_000)
    event = {
        "session_id": "legacy-image",
        "created_at": "2026-07-01T00:00:00+00:00",
        "message": {"role": "user", "content": "legacy image", "images": [image]},
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    store = SessionStore(tmp_path)
    store.update_metadata(
        "legacy-image",
        title="legacy image",
        message_count=1,
        created_at=event["created_at"],
    )

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("verified legacy metadata must avoid parsing image records")

    monkeypatch.setattr(session_module, "_scan_session_summary", fail_scan)
    listed = SessionStore(tmp_path).list()[0]

    assert listed["messages"] == 1
    assert listed["title"] == "legacy image"
    assert path.with_suffix(".index.json").exists()
    assert store.load("legacy-image").messages[0].images == [image]


def test_session_list_upgrades_legacy_log_and_reuses_cache_across_stores(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.session as session_module

    sessions_dir = tmp_path / ".deepseekfathom" / "sessions"
    sessions_dir.mkdir(parents=True)
    path = sessions_dir / "legacy-index.jsonl"
    events = [
        {"session_id": "legacy-index", "created_at": "2026-07-01T00:00:00+00:00", "message": {"role": "user", "content": "legacy title"}},
        {"session_id": "legacy-index", "created_at": "2026-07-01T00:00:00+00:00", "message": {"role": "assistant", "content": "answer"}},
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    original_scan = session_module._scan_session_summary
    scan_count = 0

    def counted_scan(*args, **kwargs):
        nonlocal scan_count
        scan_count += 1
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(session_module, "_scan_session_summary", counted_scan)
    first = SessionStore(tmp_path).list()
    second = SessionStore(tmp_path).list()

    assert first == second
    assert first[0]["messages"] == 2
    assert first[0]["title"] == "legacy title"
    assert scan_count == 1
    assert path.with_suffix(".index.json").exists()


def test_session_list_invalidates_stale_index_when_jsonl_changes(tmp_path: Path):
    session = Session(tmp_path, session_id="changing-index")
    session.append(Message("user", "keep my conversation"))
    store = SessionStore(tmp_path)
    store.update_metadata(session.session_id, title="Custom title", message_count=1)
    assert store.list()[0]["messages"] == 1

    event = {
        "session_id": session.session_id,
        "created_at": session.created_at,
        "message": {"role": "assistant", "content": "new answer"},
    }
    with session.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")

    refreshed = SessionStore(tmp_path).list()[0]
    assert refreshed["messages"] == 2
    assert refreshed["title"] == "Custom title"
    assert [message.content for message in store.load(session.session_id).messages] == ["keep my conversation", "new answer"]


def test_session_rewrite_refreshes_index_title_and_delete_removes_index(tmp_path: Path):
    import deepseekfathom._core.session as session_module

    session = Session(tmp_path, session_id="rewrite-index")
    session.append(Message("user", "old title"))
    session.messages[0] = Message("user", "new title")
    session.rewrite()
    store = SessionStore(tmp_path)

    assert store.list()[0]["title"] == "new title"
    index_path = session.path.with_suffix(".index.json")
    assert index_path.exists()
    index_path.write_text("{broken", encoding="utf-8")
    with session_module._SESSION_LIST_CACHE_LOCK:
        session_module._SESSION_LIST_CACHE.clear()
    assert store.list()[0]["title"] == "new title"
    assert json.loads(index_path.read_text(encoding="utf-8"))["messages"] == 1

    store.delete(session.session_id)
    assert not session.path.exists()
    assert not index_path.exists()


def test_resume_global_session_appends_to_original_file(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home_session_dir = home / ".deepseekfathom" / "sessions"
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
    assert not (workspace / ".deepseekfathom" / "sessions" / f"{session_id}.jsonl").exists()


def test_session_load_skips_corrupt_jsonl_rows_without_hiding_conversation(tmp_path: Path):
    sessions_dir = tmp_path / ".deepseekfathom" / "sessions"
    sessions_dir.mkdir(parents=True)
    session_id = "10000000-0000-4000-8000-000000000001"
    path = sessions_dir / f"{session_id}.jsonl"
    valid_user = json.dumps({"session_id": session_id, "created_at": "now", "message": {"role": "user", "content": "保留我"}}, ensure_ascii=False)
    valid_answer = json.dumps({"session_id": session_id, "created_at": "now", "message": {"role": "assistant", "content": "还在"}}, ensure_ascii=False)
    path.write_text(valid_user + "\n" + '{"message":' + "\n" + valid_answer + "\n", encoding="utf-8")

    store = SessionStore(tmp_path)
    loaded = store.load(session_id)
    listed = store.list()

    assert [message.content for message in loaded.messages] == ["保留我", "还在"]
    assert listed[0]["session_id"] == session_id
    assert listed[0]["messages"] == 2


def test_session_ids_cannot_escape_session_directory(tmp_path: Path):
    import pytest

    from deepseekfathom._core.session import SessionStore

    store = SessionStore(tmp_path)
    for unsafe in ("../outside", r"..\outside", ".", "", "id/child"):
        with pytest.raises(ValueError, match="invalid session id"):
            store.resolve_session_path(unsafe)
        with pytest.raises(ValueError, match="invalid session id"):
            store.metadata_path(unsafe)


def test_session_list_skips_invalid_legacy_filenames(tmp_path: Path):
    from deepseekfathom._core.messages import Message
    from deepseekfathom._core.session import Session, SessionStore

    valid = Session(tmp_path, session_id="valid-session")
    valid.append(Message("user", "保留的会话"))
    invalid = tmp_path / ".deepseekfathom" / "sessions" / "bad name.jsonl"
    invalid.write_text("{}\n", encoding="utf-8")

    rows = SessionStore(tmp_path).list()

    assert [row["session_id"] for row in rows] == ["valid-session"]


def test_concurrent_session_metadata_updates_preserve_all_fields(tmp_path: Path):
    import threading

    from deepseekfathom._core.session import SessionStore

    store = SessionStore(tmp_path)
    threads = [
        threading.Thread(target=store.update_metadata, args=("metadata-session",), kwargs={f"field_{index}": index})
        for index in range(24)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    metadata = store.metadata("metadata-session")
    assert metadata == {f"field_{index}": index for index in range(24)}


def test_desktop_transcript_does_not_hide_messages_before_320_limit():
    from deepseekfathom._core.desktop.app import serialize_messages

    messages = [Message("user" if index % 2 == 0 else "assistant", f"message-{index}") for index in range(402)]
    visible = serialize_messages(messages)

    assert len(visible) == 402
    assert visible[0]["content"] == "message-0"
    assert visible[-1]["content"] == "message-401"


class FakeWindow:
    def __init__(self, height=12, width=48):
        self.height = height
        self.width = width
        self.writes = []
        self.moves = []

    def erase(self): pass
    def getmaxyx(self): return self.height, self.width
    def attron(self, *_): pass
    def attroff(self, *_): pass
    def addnstr(self, y, x, text, n, *args):
        if y >= self.height or x >= self.width - 1 or n > self.width - x:
            raise Exception("unsafe write")
        self.writes.append((y, x, text, n))
    def move(self, y, x):
        if y >= self.height or x >= self.width:
            raise Exception("unsafe move")
        self.moves.append((y, x))
    def refresh(self): pass


def test_tui_draw_avoids_bottom_right_curses_error(monkeypatch):
    import deepseekfathom._core.tui as tui_module
    import pytest

    if tui_module.curses is None:
        pytest.skip("curses is not included in the Windows standard library")
    monkeypatch.setattr("deepseekfathom._core.tui.curses.color_pair", lambda _n: 0)
    state = TuiState(model="deepseek-v4-flash", mode="root", thinking="fast")
    ChatTui(state, lambda _text, _state: None, lambda _cmd, _state: False)._draw(FakeWindow())


def test_tui_draw_handles_tiny_terminals(monkeypatch):
    import deepseekfathom._core.tui as tui_module

    monkeypatch.setattr(tui_module, "curses", SimpleNamespace(A_BOLD=0))
    state = TuiState(model="deepseek-v4-flash", mode="root", thinking="max", input_text="中文")
    tui = ChatTui(state, lambda _text, _state: None, lambda _cmd, _state: False)

    for height, width in ((1, 1), (1, 2), (2, 2), (3, 5), (4, 8)):
        tui._draw(FakeWindow(height=height, width=width))


def test_tui_cursor_uses_chinese_display_cells(monkeypatch):
    import deepseekfathom._core.tui as tui_module

    monkeypatch.setattr(tui_module, "curses", SimpleNamespace(A_BOLD=0))
    state = TuiState(model="deepseek-v4-flash", mode="root", thinking="max", input_text="正在输入")
    window = FakeWindow(height=6, width=30)
    ChatTui(state, lambda _text, _state: None, lambda _cmd, _state: False)._draw(window)

    assert window.moves[-1] == (4, 10)
    assert any("DeepSeekFathom" in write[2] for write in window.writes)


def test_tui_wraps_chinese_by_display_width():
    text = "中文测试ABC"
    wrapped = wrap_display_text(text, 4)
    assert "".join(wrapped) == text
    assert all(display_width(line) <= 4 for line in wrapped)

    rendered = render_messages([("user", text), ("assistant", "完成")], 10)
    assert all(display_width(line) <= 10 for line in rendered)


def test_tui_rejects_redirected_streams_cleanly(monkeypatch):
    import deepseekfathom._core.tui as tui_module

    monkeypatch.setattr(tui_module, "curses", SimpleNamespace(error=Exception))
    monkeypatch.setattr(tui_module.sys, "stdin", io.StringIO("message\n"))
    monkeypatch.setattr(tui_module.sys, "stdout", io.StringIO())
    state = TuiState(model="deepseek-v4-flash", mode="root", thinking="max")

    with pytest.raises(RuntimeError, match="interactive terminal"):
        ChatTui(state, lambda _text, _state: None, lambda _cmd, _state: False).run()


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
    items = [
        ("/model", "list models"),
        ("/think fast", "384000 max tokens"),
        ("/think max", "384000 max tokens"),
        ("/mode root", "root"),
        ("/hooks enable", "enable one hook"),
        ("/release-notes", "draft a release"),
    ]
    assert filter_slash_items(items, "m")[0][0] == "/model"
    assert filter_slash_items(items, "mo")[0][0] == "/model"
    assert filter_slash_items(items, "t")[0][0] == "/think fast"
    assert filter_slash_items(items, "max")[0][0] == "/think max"
    assert filter_slash_items(items, "hook en")[0][0] == "/hooks enable"
    assert filter_slash_items(items, "notes")[0][0] == "/release-notes"


def test_slash_items_include_manual_compact(tmp_path: Path):
    import deepseekfathom._core.cli as cli

    commands = [command for command, _description in cli.slash_items(settings(tmp_path))]
    assert "/compact" in commands
    assert commands.index("/goal") < commands.index("/goal <text>")
    assert "/think" in commands
    assert "/think max" not in commands
    assert cli.THINKING.index("ultra") < cli.THINKING.index("max")
    assert cli.thinking_description("max").startswith("max")


def test_cli_thinking_help_includes_max(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--help"])
    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--think" in output
    assert "max is the highest" in output
    assert "max" in output
    assert "auto" not in output
    assert "instant" not in output
    assert "standard" not in output
    assert "careful" not in output
    assert "deeper" not in output

    with pytest.raises(SystemExit) as root_help:
        main(["--help"])
    assert root_help.value.code == 0
    root_output = capsys.readouterr().out
    assert "DeepSeekFathom" in root_output
    assert "TuL" not in root_output


def test_terminal_startup_uses_deepseekfathom_brand(monkeypatch, capsys):
    import deepseekfathom._core.cli as cli
    import deepseekfathom._core.ui as ui_module

    monkeypatch.setenv("DSTUL_PLAIN_UI", "1")
    ui_module.startup_animation()
    output = capsys.readouterr().out

    assert output == "DeepSeekFathom\n"
    assert "DeepSeekFathom" in cli.BANNER
    assert "TuL" not in cli.BANNER


def test_slash_skill_selection_inserts_agent_prompt():
    assert slash_selection_insertion("/skill repo-debug") == "Use skill repo-debug: "
    assert slash_selection_insertion("/goal <text>") == "/goal "
    assert slash_selection_insertion("/goal") is None
    assert slash_selection_insertion("/model") is None


def test_agent_event_formatter_labels_tools():
    assert "run_shell" in format_agent_event("tool run_shell command=ls")
    assert "done" in format_agent_event("done run_shell")
    assert "subagent" in format_agent_event("subagent reviewer mode=plan")


def test_cli_file_change_event_has_true_old_new_lines(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.ui as ui_module

    monkeypatch.setenv("DSTUL_PLAIN_UI", "1")
    target = tmp_path / "demo.txt"
    target.write_text("head\nold-a\nold-b\ntail\n", encoding="utf-8")
    result = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root")).run(
        "write_file",
        {"path": "demo.txt", "content": "head\nnew-a\ntail\n"},
    )
    encoded = base64.b64encode(result.to_message().encode("utf-8")).decode("ascii")
    rendered = format_agent_event(f"done write_file {encoded}")
    rows = ui_module.parse_unified_diff(str(result.ui["diff"]))
    changed = [row for row in rows if row[0] in {"delete", "add"}]

    assert [(row[0], row[1], row[2], row[4]) for row in changed] == [
        ("delete", 2, None, "old-a"),
        ("delete", 3, None, "old-b"),
        ("add", None, 2, "new-a"),
    ]
    assert "[edit] modified demo.txt  +1 -2" in rendered
    assert "old-a" in rendered and "new-a" in rendered
    assert encoded not in rendered
    assert "\x1b" not in rendered


def test_cli_file_change_colors_only_changed_rows(monkeypatch, tmp_path: Path):
    import deepseekfathom._core.ui as ui_module

    target = tmp_path / "demo.txt"
    target.write_text("head\nold\ntail\n", encoding="utf-8")
    result = ToolRegistry(tmp_path, policy=ApprovalPolicy.from_mode("root")).run(
        "write_file",
        {"path": "demo.txt", "content": "head\nnew\ntail\n"},
    )
    encoded = base64.b64encode(result.to_message().encode("utf-8")).decode("ascii")
    monkeypatch.delenv("DSTUL_PLAIN_UI", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(ui_module, "terminal_supports_ansi", lambda _stream=None: True)

    rendered = format_agent_event(f"done write_file {encoded}")
    context_line = next(line for line in rendered.splitlines() if line.endswith("head"))

    assert ui_module.DIFF_DELETE in rendered
    assert ui_module.DIFF_ADD in rendered
    assert "\x1b[48;2;" not in context_line


def test_cli_file_tool_start_uses_edit_label(monkeypatch):
    monkeypatch.setenv("DSTUL_PLAIN_UI", "1")
    assert format_agent_event("tool write_file path=src/app.py content=...") == "  [edit] src/app.py"
    assert "[tool]" not in format_agent_event("tool apply_patch patch=*** Begin Patch")


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


def test_windows_composer_reads_chinese_without_builtin_input(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    class TtyBuffer(io.StringIO):
        def isatty(self):
            return True

    class FakeMsvcrt:
        def __init__(self, keys):
            self.keys = list(keys)

        def getwch(self):
            return self.keys.pop(0)

        def kbhit(self):
            return bool(self.keys)

    output = TtyBuffer()
    fake_msvcrt = FakeMsvcrt(["中", "文", "\b", "国", "\r"])
    monkeypatch.setattr(ui_module.os, "name", "nt")
    monkeypatch.setattr(ui_module.sys, "stdin", TtyBuffer())
    monkeypatch.setattr(ui_module.sys, "stdout", output)
    monkeypatch.setattr(ui_module, "_load_msvcrt", lambda: fake_msvcrt)
    monkeypatch.setattr(ui_module, "terminal_supports_ansi", lambda _stream=None: False)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: pytest.fail("input() must not be used"))

    assert read_composer("prompt> ") == "中国"
    assert "\x1b" not in output.getvalue()
    assert "中国" in output.getvalue()


def test_read_composer_prefers_prompt_toolkit_with_column_commands(monkeypatch):
    import deepseekfathom._core.ui as ui_module
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document
    from prompt_toolkit.shortcuts import CompleteStyle

    sessions = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            sessions.append(self)

        def prompt(self):
            return "检查中文光标"

    monkeypatch.setattr(ui_module, "_PROMPT_SESSION_FACTORY", FakeSession)
    monkeypatch.setattr(ui_module, "_PROMPT_TOOLKIT_HISTORY", None)
    monkeypatch.setattr(ui_module, "_prompt_toolkit_terminal_ready", lambda: True)
    monkeypatch.setattr(
        ui_module,
        "open_composer_frame",
        lambda *_args, **_kwargs: pytest.fail("prompt-toolkit must run before the fallback frame"),
    )

    result = ui_module.read_composer(
        "",
        slash_items=[
            ("/think fast", "快速思考"),
            ("/think max", "最强思考"),
            ("/goal <text>", "设置目标"),
        ],
        frame_title="DeepSeekFathom",
        frame_status="deepseek-v4-pro  ·  root · max",
    )

    assert result == "检查中文光标"
    options = sessions[0].kwargs
    assert options["complete_style"] is CompleteStyle.COLUMN
    assert options["erase_when_done"] is False
    assert options["multiline"] is False
    assert options["wrap_lines"] is True
    assert options["enable_history_search"] is True
    assert "full_screen" not in options
    assert "› " in "".join(text for _style, text in options["message"])
    assert "输入任务" in "".join(text for _style, text in options["placeholder"])
    status = "".join(text for _style, text in options["rprompt"])
    assert status == "deepseek-v4-pro · root · max"
    assert "\n" not in status
    assert options["reserve_space_for_menu"] == 0
    assert "bottom_toolbar" not in options

    rules = dict(options["style"].style_rules)
    current = rules["completion-menu.completion.current"]
    assert "fg:ansibrightcyan" in current
    assert "bg:ansidefault" in current
    assert "reverse" not in current.replace("noreverse", "")

    completions = list(options["completer"].get_completions(Document("/max"), CompleteEvent()))
    assert completions[0].text == "/think max"
    assert completions[0].display_text == "/think max"
    assert completions[0].display_meta_text == "最强思考"
    goal = list(options["completer"].get_completions(Document("/goal"), CompleteEvent()))[0]
    assert goal.text == "/goal "
    assert goal.display_text == "/goal <text>"

    ui_module.read_composer("", slash_items=[], frame_status="ready")
    assert sessions[0].kwargs["history"] is sessions[1].kwargs["history"]


def test_prompt_toolkit_ctrl_c_and_ctrl_d_semantics():
    import deepseekfathom._core.ui as ui_module

    class FakeBuffer:
        def __init__(self, text):
            self.text = text
            self.reset_calls = 0

        def reset(self):
            self.text = ""
            self.reset_calls += 1

    class FakeApp:
        def __init__(self, text):
            self.current_buffer = FakeBuffer(text)
            self.exits = []

        def exit(self, **kwargs):
            self.exits.append(kwargs)

    nonempty = FakeApp("draft")
    ui_module._prompt_toolkit_ctrl_c(SimpleNamespace(app=nonempty))
    assert nonempty.current_buffer.reset_calls == 1
    assert nonempty.exits == []

    empty = FakeApp("")
    ui_module._prompt_toolkit_ctrl_c(SimpleNamespace(app=empty))
    assert empty.exits == [{"result": "/cancel"}]

    eof = FakeApp("anything")
    ui_module._prompt_toolkit_ctrl_d(SimpleNamespace(app=eof))
    assert eof.exits == [{"exception": EOFError}]


def test_prompt_toolkit_slash_and_backspace_never_trap_double_slash():
    import deepseekfathom._core.ui as ui_module

    class FakeDocument:
        def __init__(self, buffer):
            self.buffer = buffer

        @property
        def text_before_cursor(self):
            return self.buffer.text[: self.buffer.cursor_position]

    class FakeBuffer:
        def __init__(self):
            self.text = ""
            self.cursor_position = 0
            self.complete_state = None
            self.started_completion = 0

        @property
        def document(self):
            return FakeDocument(self)

        def insert_text(self, text):
            self.text = self.text[: self.cursor_position] + text + self.text[self.cursor_position :]
            self.cursor_position += len(text)

        def start_completion(self, **_kwargs):
            self.complete_state = object()
            self.started_completion += 1

        def cancel_completion(self):
            self.complete_state = None

        def delete_before_cursor(self, count=1):
            start = max(0, self.cursor_position - count)
            self.text = self.text[:start] + self.text[self.cursor_position :]
            self.cursor_position = start

    buffer = FakeBuffer()
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buffer))

    ui_module._prompt_toolkit_insert_slash(event)
    assert buffer.text == "/"
    assert buffer.complete_state is not None

    ui_module._prompt_toolkit_insert_slash(event)
    assert buffer.text == "//"

    ui_module._prompt_toolkit_backspace(event)
    assert buffer.text == "/"
    assert buffer.complete_state is not None

    ui_module._prompt_toolkit_backspace(event)
    assert buffer.text == ""


@pytest.mark.parametrize("exception, expected", [(KeyboardInterrupt, "/cancel"), (EOFError, None)])
def test_prompt_toolkit_interrupts_are_mapped(monkeypatch, exception, expected):
    import deepseekfathom._core.ui as ui_module

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self):
            raise exception

    monkeypatch.setattr(ui_module, "_PROMPT_SESSION_FACTORY", FakeSession)
    monkeypatch.setattr(ui_module, "_prompt_toolkit_terminal_ready", lambda: True)
    if exception is EOFError:
        with pytest.raises(EOFError):
            ui_module.read_composer("")
    else:
        assert ui_module.read_composer("") == expected


@pytest.mark.parametrize("use_ansi", [True, False])
def test_windows_composer_edits_at_cursor_with_chinese_text(monkeypatch, use_ansi):
    import deepseekfathom._core.ui as ui_module

    class TtyBuffer(io.StringIO):
        def isatty(self):
            return True

    class FakeMsvcrt:
        def __init__(self):
            self.keys = [
                "你", "好",
                "\xe0", "K",  # left
                "真",
                "\xe0", "G",  # home
                "\xe0", "S",  # delete 你
                "\xe0", "M",  # right
                "\b",           # backspace 真
                "\xe0", "O",  # end
                "呀",
                "\r",
            ]

        def getwch(self):
            return self.keys.pop(0)

        def kbhit(self):
            return bool(self.keys)

    output = TtyBuffer()
    monkeypatch.setattr(ui_module.os, "name", "nt")
    monkeypatch.setattr(ui_module.sys, "stdin", TtyBuffer())
    monkeypatch.setattr(ui_module.sys, "stdout", output)
    monkeypatch.setattr(ui_module, "_load_msvcrt", FakeMsvcrt)
    monkeypatch.setattr(ui_module, "terminal_supports_ansi", lambda _stream=None: use_ansi)

    result = read_composer(
        "",
        frame_title="DeepSeekFathom",
        frame_status="deepseek-v4-flash · root · max",
    )

    assert result == "好呀"
    if use_ansi:
        visible = re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", output.getvalue())
        assert "› 输入任务，/ 查看命令" in visible
        assert "╭" not in visible and "DeepSeekFathom" not in visible
    else:
        assert "\033[" not in output.getvalue()


def test_framed_composer_cursor_uses_cjk_display_cells(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    output = io.StringIO()
    monkeypatch.setattr(ui_module.sys, "stdout", output)
    monkeypatch.setattr(ui_module, "terminal_supports_ansi", lambda _stream=None: True)

    ui_module.redraw_composer(
        "│  › ",
        list("你真好"),
        frame=ui_module.ComposerFrame(width=24, status=""),
        cursor_index=2,
    )

    assert "\r\033[9C" in output.getvalue()


def test_windows_composer_has_a_separate_codex_style_input_area(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    class TtyBuffer(io.StringIO):
        def isatty(self):
            return True

    class FakeMsvcrt:
        def __init__(self):
            self.keys = [*list("检查项目"), "\r"]

        def getwch(self):
            return self.keys.pop(0)

        def kbhit(self):
            return bool(self.keys)

    output = TtyBuffer()
    monkeypatch.setattr(ui_module.os, "name", "nt")
    monkeypatch.setattr(ui_module.sys, "stdin", TtyBuffer())
    monkeypatch.setattr(ui_module.sys, "stdout", output)
    monkeypatch.setattr(ui_module, "_load_msvcrt", FakeMsvcrt)
    monkeypatch.setattr(ui_module, "terminal_supports_ansi", lambda _stream=None: True)
    monkeypatch.setattr(ui_module.shutil, "get_terminal_size", lambda _fallback: os.terminal_size((72, 24)))

    result = read_composer(
        "",
        frame_title="DeepSeekFathom",
        frame_status="deepseek-v4-flash · root · max",
    )
    visible = re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", output.getvalue())

    assert result == "检查项目"
    assert "› 检查项目" in visible
    assert "DeepSeekFathom" not in visible
    assert "╭" not in visible and "╰" not in visible
    assert "deepseek-v4-flash · root · max" in visible


def test_windows_composer_opens_plain_slash_menu(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    class TtyBuffer(io.StringIO):
        def isatty(self):
            return True

    class FakeMsvcrt:
        def __init__(self, keys):
            self.keys = list(keys)

        def getwch(self):
            return self.keys.pop(0)

        def kbhit(self):
            return bool(self.keys)

    output = TtyBuffer()
    fake_msvcrt = FakeMsvcrt(["/", *list("think max"), "\r"])
    monkeypatch.setattr(ui_module.os, "name", "nt")
    monkeypatch.setattr(ui_module.sys, "stdin", TtyBuffer())
    monkeypatch.setattr(ui_module.sys, "stdout", output)
    monkeypatch.setattr(ui_module, "_load_msvcrt", lambda: fake_msvcrt)
    monkeypatch.setattr(ui_module, "terminal_supports_ansi", lambda _stream=None: False)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: pytest.fail("input() must not be used"))

    selected = read_composer(
        "prompt> ",
        slash_items=[("/think fast", "fast"), ("/think max", "maximum")],
    )

    assert selected == "/think max"
    assert "commands /think max" in output.getvalue()
    assert "\x1b" not in output.getvalue()


def test_windows_composer_uses_vt_single_line_redraw(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    class TtyBuffer(io.StringIO):
        def isatty(self):
            return True

    class FakeMsvcrt:
        def __init__(self):
            self.keys = ["画", "\r"]

        def getwch(self):
            return self.keys.pop(0)

        def kbhit(self):
            return bool(self.keys)

    output = TtyBuffer()
    monkeypatch.setattr(ui_module.os, "name", "nt")
    monkeypatch.setattr(ui_module.sys, "stdin", TtyBuffer())
    monkeypatch.setattr(ui_module.sys, "stdout", output)
    monkeypatch.setattr(ui_module, "_load_msvcrt", FakeMsvcrt)
    monkeypatch.setattr(ui_module, "terminal_supports_ansi", lambda _stream=None: True)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: pytest.fail("input() must not be used"))

    assert read_composer("prompt> ") == "画"
    assert "\r\033[2K" in output.getvalue()


def test_posix_composer_keeps_termios_raw_path(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    class TtyInput(io.StringIO):
        def isatty(self):
            return True

        def fileno(self):
            return 42

    class TtyOutput(io.StringIO):
        def isatty(self):
            return True

    restored = []
    fake_termios = SimpleNamespace(
        TCSANOW=0,
        tcgetattr=lambda _fd: ["saved"],
        tcsetattr=lambda fd, when, state: restored.append((fd, when, state)),
    )
    raw_fds = []
    keys = iter(["你", "好", "\r"])
    output = TtyOutput()
    monkeypatch.setattr(ui_module.os, "name", "posix")
    monkeypatch.setattr(ui_module.sys, "stdin", TtyInput())
    monkeypatch.setattr(ui_module.sys, "stdout", output)
    monkeypatch.setattr(ui_module, "termios", fake_termios)
    monkeypatch.setattr(ui_module, "tty", SimpleNamespace(setraw=lambda fd: raw_fds.append(fd)))
    monkeypatch.setattr(ui_module, "terminal_supports_ansi", lambda _stream=None: True)
    monkeypatch.setattr(ui_module, "read_raw_char", lambda _fd: next(keys))
    monkeypatch.setattr(ui_module, "should_submit_newline", lambda _fd: True)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: pytest.fail("input() must not be used"))

    assert read_composer("prompt> ") == "你好"
    assert raw_fds == [42]
    assert restored == [(42, 0, ["saved"])]
    assert "\033[?2004h" in output.getvalue()
    assert "\033[?2004l" in output.getvalue()


def test_tail_for_width_keeps_single_line_window():
    prefix = "..." if plain_terminal() else "…"
    assert tail_for_width("abcdef", 4) == ("...f" if plain_terminal() else "…def")
    chinese = tail_for_width("画画画画", 5)
    assert chinese.startswith(prefix)
    assert display_width(chinese) <= 5
    assert display_width(clip_visible("中文abcdef", 5)) <= 5


def test_windows_utf8_configuration_covers_redirected_streams(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    class ReconfigurableStream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **options):
            self.calls.append(options)

    stdin = ReconfigurableStream()
    stdout = ReconfigurableStream()
    stderr = ReconfigurableStream()
    monkeypatch.setattr(ui_module.os, "name", "nt")
    monkeypatch.setattr(ui_module.sys, "stdin", stdin)
    monkeypatch.setattr(ui_module.sys, "stdout", stdout)
    monkeypatch.setattr(ui_module.sys, "stderr", stderr)

    configure_utf8_stdio()

    assert stdin.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace", "write_through": True}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace", "write_through": True}]


def test_terminal_safety_does_not_emit_ansi_when_redirected(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    stdout = io.StringIO()
    monkeypatch.setattr(ui_module.sys, "stdin", io.StringIO())
    monkeypatch.setattr(ui_module.sys, "stdout", stdout)
    ui_module.install_terminal_safety()

    assert stdout.getvalue() == ""


def test_terminal_safety_does_not_emit_ansi_without_vt(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    class TtyBuffer(io.StringIO):
        def isatty(self):
            return True

    stdout = TtyBuffer()
    monkeypatch.setattr(ui_module.os, "name", "nt")
    monkeypatch.setattr(ui_module, "_enable_windows_vt", lambda _stream: False)
    monkeypatch.setattr(ui_module.sys, "stdin", TtyBuffer())
    monkeypatch.setattr(ui_module.sys, "stdout", stdout)
    ui_module.force_terminal_sane()

    assert stdout.getvalue() == ""


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
    from deepseekfathom._core.ui import draw_slash_select

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
        assert lines == 3
        assert "\r\n" in output.getvalue()
        assert "\033[?1049h" not in output.getvalue()
        assert "\033[H\033[2J" not in output.getvalue()
        assert visible_lines
        assert all(len(line) <= columns for line in visible_lines)
        assert sum("/model" in line for line in visible_lines) == 1


def test_slash_select_draw_shows_range_and_handles_tiny_heights(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    items = [(f"/command-{index}", f"description {index}") for index in range(12)]
    output = io.StringIO()
    monkeypatch.setattr(ui_module.sys, "stdout", output)
    monkeypatch.setattr(ui_module.shutil, "get_terminal_size", lambda _fallback: os.terminal_size((40, 8)))

    lines = ui_module.draw_slash_select(items, "", 6)
    visible = re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", output.getvalue())

    assert lines <= 8
    assert "commands" in visible

    for height in (1, 2, 3):
        output = io.StringIO()
        monkeypatch.setattr(ui_module.sys, "stdout", output)
        monkeypatch.setattr(
            ui_module.shutil,
            "get_terminal_size",
            lambda _fallback, height=height: os.terminal_size((32, height)),
        )
        lines = ui_module.draw_slash_select(items, "com", 0)
        assert lines == 1
        assert lines <= height
        assert "[1/12]" in re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", output.getvalue())


def test_escape_suffix_reads_arrow_bytes_from_fd():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"[B")
        assert read_escape_suffix(read_fd) == "[B"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_escape_suffix_uses_short_escape_disambiguation(monkeypatch):
    import deepseekfathom._core.ui as ui_module

    waits = []
    monkeypatch.setattr(
        ui_module,
        "wait_for_fd_input",
        lambda _fd, timeout: waits.append(timeout) or False,
    )

    assert ui_module.read_escape_suffix(123) == ""
    assert len(waits) == 1
    assert 0.03 <= waits[0] <= 0.05


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


def test_serialize_marks_pre_tool_prose_intermediate():
    """Pre-tool narration in a turn must be flagged intermediate so it carries no
    copy/retry/branch — one turn shows one set of actions, on its final reply."""
    from deepseekfathom._core.desktop.app import serialize_messages
    from deepseekfathom._core.messages import Message

    out = serialize_messages([
        Message("user", "写个文件"),
        Message("assistant", '我来写文件。\n{"tool":"write_file","arguments":{"path":"a","content":"x"}}'),
        Message("user", 'TOOL_RESULT name=write_file\n{"ok":true}'),
        Message("assistant", "写好了。"),
    ])
    roles = [(o["role"], o.get("intermediate")) for o in out]
    assert ("assistant", True) not in roles      # generic pre-tool intro is dropped entirely
    assert out[-1]["role"] == "assistant" and not out[-1].get("intermediate")  # final reply keeps actions


def test_serialize_suppresses_legacy_adjacent_recovery_reply():
    from deepseekfathom._core.desktop.app import serialize_messages
    from deepseekfathom._core.messages import Message

    out = serialize_messages([
        Message("user", "检查本机"),
        Message("assistant", "本机检查结果。"),
        Message("assistant", "内部恢复误生成的第二条回答。"),
    ])

    assistants = [entry for entry in out if entry["role"] == "assistant"]
    assert [entry["content"] for entry in assistants] == ["本机检查结果。"]
    assert not assistants[0].get("intermediate")


def test_stream_holds_fenced_tool_call_from_fence_start():
    from deepseekfathom._core.agent import safe_stream_emit_length, strip_tool_call_display, is_tool_intro_only

    text = '我来调用工具：```json\n{"tool":"write_file","arguments":{"path":"a","content":"x"}}\n```'
    assert text[: safe_stream_emit_length(text)] == "我来调用工具："
    prose = strip_tool_call_display(text)
    assert prose == "我来调用工具："
    assert is_tool_intro_only(prose) is True


def test_stream_holds_partial_markdown_tool_fence():
    from deepseekfathom._core.agent import safe_stream_emit_length

    for text in ("`", "``", "```", "```j", "```js", "```jso", "```json"):
        assert safe_stream_emit_length(text) == 0

    normal = "```python\nprint('ok')"
    assert safe_stream_emit_length(normal) == len(normal)


def test_normal_code_blocks_are_not_inferred_or_partially_held():
    from deepseekfathom._core.agent import safe_stream_emit_length, strip_tool_call_display

    # Open ordinary code fences are visible while streaming; they are not tools.
    open_python = "我给你代码：\n```python\nprint('hello')"
    assert safe_stream_emit_length(open_python) == len(open_python)

    # JSON code can mention fields named arguments/input without becoming a tool.
    normal_json = '```json\n{"hello":"world","arguments":"not a tool"}\n```'
    assert parse_tool_call(normal_json) is None
    assert safe_stream_emit_length(normal_json) == len(normal_json)
    assert strip_tool_call_display(normal_json) == normal_json

    # Bash code is display content by default, not an inferred run_shell tool.
    normal_bash = '我现在解释：\n```bash\necho hello\n```'
    assert parse_tool_call(normal_bash) is None
    assert safe_stream_emit_length(normal_bash) == len(normal_bash)


def test_open_fenced_tool_call_is_held_from_fence_start():
    from deepseekfathom._core.agent import safe_stream_emit_length

    text = '我来调用工具：```json\n{"tool":"write_file"'
    assert text[: safe_stream_emit_length(text)] == "我来调用工具："

    midline_opener = "我来调用工具：```json"
    assert midline_opener[: safe_stream_emit_length(midline_opener)] == "我来调用工具："


def test_dangling_tool_json_fence_is_not_displayed_as_prose():
    from deepseekfathom._core.agent import plainify_assistant_text

    assert plainify_assistant_text("我来调用工具：```json").strip() == "我来调用工具："
    assert plainify_assistant_text('我来调用工具：```json\n{"tool":"write_file"').strip() == "我来调用工具："


def test_generic_pre_tool_action_intro_is_dropped():
    from deepseekfathom._core.agent import is_tool_intro_only, strip_tool_call_display

    text = '我来读取 README 并检查安装说明。```json\n{"tool":"read_file","arguments":{"path":"README.md"}}\n```'
    prose = strip_tool_call_display(text)
    assert prose == "我来读取 README 并检查安装说明。"
    assert is_tool_intro_only(prose) is True


def test_substantive_pre_tool_prose_is_not_dropped():
    from deepseekfathom._core.agent import is_tool_intro_only, strip_tool_call_display

    text = '问题可能是配置文件没有保存。我来读取 README。```json\n{"tool":"read_file","arguments":{"path":"README.md"}}\n```'
    prose = strip_tool_call_display(text)
    assert prose == "问题可能是配置文件没有保存。我来读取 README。"
    assert is_tool_intro_only(prose) is False


def test_openai_chat_native_tools_non_stream(tmp_path: Path):
    from dataclasses import replace

    import httpx

    from deepseekfathom._core.provider import DeepSeekClient

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"a.txt"}',
                                    },
                                },
                                {
                                    "id": "call_b",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"b.txt"}',
                                    },
                                },
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            },
        )

    client = DeepSeekClient(
        replace(
            settings(tmp_path),
            provider_format="openai",
            base_url="https://example.test/v1",
        )
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        reply = client.chat([Message("user", "read both")], tool_names=["read_file"])
    finally:
        client.close()

    assert reply == ""
    assert [call.name for call in client.last_tool_calls] == ["read_file", "read_file"]
    assert [call.arguments for call in client.last_tool_calls] == [
        {"path": "a.txt"},
        {"path": "b.txt"},
    ]
    assert [call.call_id for call in client.last_tool_calls] == ["call_a", "call_b"]
    definition = captured["tools"][0]["function"]
    assert definition["name"] == "read_file"
    assert definition["parameters"]["required"] == ["path"]
    assert definition["description"]


def test_openai_stream_native_multi_tool_calls_execute_without_json_leak(tmp_path: Path):
    from dataclasses import replace

    import httpx

    from deepseekfathom._core.provider import DeepSeekClient

    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")
    payloads = []

    def sse(*events: dict) -> bytes:
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return (body + "data: [DONE]\n\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_a",
                                            "function": {
                                                "name": "read_",
                                                "arguments": '{"path":"a',
                                            },
                                        },
                                        {
                                            "index": 1,
                                            "id": "call_b",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": '{"path":"b.txt"}',
                                            },
                                        },
                                    ]
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {
                                                "name": "file",
                                                "arguments": '.txt"}',
                                            },
                                        }
                                    ]
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 30, "completion_tokens": 8},
                    },
                ),
            )
        tool_results = [
            message["content"]
            for message in payload["messages"]
            if message["role"] == "tool"
        ]
        assert len(tool_results) == 2
        assert "alpha" in tool_results[0]
        assert "beta" in tool_results[1]
        native_assistant = next(message for message in payload["messages"] if message.get("tool_calls"))
        assert len(native_assistant["tool_calls"]) == 2
        assert [message["tool_call_id"] for message in payload["messages"] if message["role"] == "tool"] == [
            call["id"] for call in native_assistant["tool_calls"]
        ]
        assert [call["id"] for call in native_assistant["tool_calls"]] == ["call_a", "call_b"]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                {
                    "choices": [{"delta": {"content": "done"}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 2},
                }
            ),
        )

    client = DeepSeekClient(
        replace(
            settings(tmp_path),
            provider_format="openai",
            base_url="https://example.test/v1",
        )
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    visible = []
    events = []
    try:
        result = FathomAgent(settings=client.settings, mode="root", thinking="instant", client=client).run(
            "Read a.txt and b.txt.",
            stream=True,
            on_delta=visible.append,
            on_final=lambda _text: None,
            on_event=events.append,
            require_todo=False,
        )
    finally:
        client.close()

    assert result.answer == "done"
    assert "".join(visible) == "done"
    assert "tool_calls" not in "".join(visible)
    assert [event.split()[1] for event in events if event.startswith("tool ")] == [
        "read_file",
        "read_file",
    ]
    assert len(payloads) == 2
    assert any(item["function"]["name"] == "read_file" for item in payloads[0]["tools"])


def test_legacy_text_multiple_tool_calls_are_all_parsed():
    from deepseekfathom._core.agent import parse_tool_calls

    separate = (
        '{"tool":"read_file","arguments":{"path":"a.txt"}}\n'
        '{"tool":"read_file","arguments":{"path":"b.txt"}}'
    )
    assert parse_tool_calls(separate) == [
        ("read_file", {"path": "a.txt"}),
        ("read_file", {"path": "b.txt"}),
    ]

    standard = json.dumps(
        {
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
                {"function": {"name": "read_file", "arguments": '{"path":"b.txt"}'}},
            ]
        }
    )
    assert parse_tool_calls(standard) == [
        ("read_file", {"path": "a.txt"}),
        ("read_file", {"path": "b.txt"}),
    ]


def test_tool_call_after_example_execution_cue_is_not_suppressed():
    text = (
        "I reviewed the example above. Now execute:\n"
        '{"tool":"read_file","arguments":{"path":"README.md"}}'
    )
    assert parse_tool_call(text) == ("read_file", {"path": "README.md"})


def test_legacy_dsml_multiple_invokes_are_all_parsed():
    from deepseekfathom._core.agent import DSML_PREFIX, parse_tool_calls

    close_prefix = "</" + DSML_PREFIX[1:]
    text = (
        f'{DSML_PREFIX}tool_calls>'
        f'{DSML_PREFIX}invoke name="read_file">'
        f'{DSML_PREFIX}parameter name="path" string="true">a.txt{close_prefix}parameter>'
        f'{close_prefix}invoke>'
        f'{DSML_PREFIX}invoke name="read_file">'
        f'{DSML_PREFIX}parameter name="path" string="true">b.txt{close_prefix}parameter>'
        f'{close_prefix}invoke>'
        f'{close_prefix}tool_calls>'
    )
    assert parse_tool_calls(text) == [
        ("read_file", {"path": "a.txt"}),
        ("read_file", {"path": "b.txt"}),
    ]


def test_legacy_text_multiple_tool_calls_are_all_executed(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")

    class LegacyMultiClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "tool_calls": [
                            {"function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
                            {"function": {"name": "read_file", "arguments": '{"path":"b.txt"}'}},
                        ]
                    }
                )
            results = [message.content for message in messages if message.content.startswith("TOOL_RESULT")]
            assert len(results) == 2
            assert "alpha" in results[0]
            assert "beta" in results[1]
            return "done"

    client = LegacyMultiClient()
    result = FathomAgent(settings(tmp_path), mode="root", client=client).run(
        "Read both files.",
        require_todo=False,
    )
    assert result.answer == "done"
    assert client.calls == 2
