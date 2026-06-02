from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import subprocess
from typing import Any, Callable

from .config import Settings
from .messages import Message
from .policy import ApprovalPolicy, ThinkingMode
from .provider import DeepSeekClient
from .session import Session
from .skills import SkillStore
from .tools import ToolError, ToolRegistry


SYSTEM_PROMPT = """You are DeepSeek TuLAgent, a concise coding agent running in a local workspace.
You can answer normally or request exactly one tool call by returning a single JSON object:
{"tool":"read_file","arguments":{"path":"README.md","max_bytes":12000}}

Available tools:
- list_files(path?, max_entries?)
- search_text(query, path?, max_matches?)
- git_status(timeout?)
- read_file(path, max_bytes?)
- write_file(path, content)
- run_shell(command, timeout?)
- apply_patch(patch, timeout?)
- download_url(url, path, max_bytes?, timeout?)
- web_search(query, max_results?, timeout?)
- start_service(name, command)
- stop_service(name)
- service_status(name)

Rules:
- Prefer reading before editing.
- Keep changes scoped to the user's request.
- Tool use must be emitted as the JSON object above. Do not put commands in bash/code fences when you want them executed.
- Never say a command, download, search, or file operation was executed unless it came from a Tool result.
- If the user asks you to inspect a live URL, GitHub repository, local files, shell state, or service state, use the appropriate tool instead of describing what you would run.
- Keep final replies visually plain. Avoid decorative Markdown, bold markers, and asterisk bullets unless code syntax or shell globbing requires `*`.
- If the user message is only `?`, `？`, or repeated question marks, do not infer a task and do not use tools. Ask what they want to ask.
- To start a long-running/background process, use start_service(name, command). Do not use shell "&" backgrounding.
- For text search, prefer a narrow path and small max_matches. Broad searches can time out.
- If a web_search result is empty, irrelevant, or failed and the user asked to search, request one more web_search with a clearer query instead of saying you will search again.
- If no tool is needed, answer directly.
- After tool results, continue until the task is complete or clearly blocked.
"""


@dataclass(frozen=True)
class AgentResult:
    session_id: str
    answer: str
    rounds: int


class TuLAgent:
    def __init__(
        self,
        settings: Settings,
        mode: str = "agent",
        thinking: str = "fast",
        client: DeepSeekClient | None = None,
        approve: Callable[[str, dict[str, Any]], bool] | None = None,
    ):
        self.settings = settings
        self.mode = mode
        self.policy = ApprovalPolicy.from_mode(mode)
        self.thinking = ThinkingMode.resolve(thinking)
        self.client = client or DeepSeekClient(settings)
        self.tools = ToolRegistry(settings.workspace, policy=self.policy)
        self.approve = approve

    def run(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
        on_event: Callable[[str], None] | None = None,
        session: Session | None = None,
        max_tool_rounds: int | None = None,
        stop_after_tool: bool = False,
    ) -> AgentResult:
        session = session or Session(self.settings.workspace)
        if not session.messages:
            session.append(Message("system", self._system_prompt()))
        session.append(Message("user", prompt))

        final_answer = ""
        rounds = 0
        round_limit = max_tool_rounds or self.settings.max_tool_rounds
        for rounds in range(1, round_limit + 1):
            model_messages = compact_context_messages(session.messages, self.settings.model, on_event=on_event)
            model_messages = self._with_internal_thinking(model_messages, on_event=on_event)
            if stream:
                parts: list[str] = []
                for delta in self.client.stream_chat(model_messages):
                    parts.append(delta)
                    if on_delta:
                        on_delta(delta)
                assistant_text = "".join(parts)
            else:
                assistant_text = self.client.chat(model_messages)
            tool_call = None if is_question_mark_only(prompt) else parse_tool_call(assistant_text)
            if not tool_call:
                assistant_text = plainify_assistant_text(assistant_text)
                session.append(Message("assistant", assistant_text))
                final_answer = assistant_text
                break
            session.append(Message("assistant", assistant_text))
            name, arguments = tool_call
            if on_event:
                on_event(f"tool {name} {summarize_arguments(arguments)}")
            try:
                if self._needs_confirmation(name) and not self._approved(name, arguments):
                    raise ToolError(f"confirmation required for tool: {name}")
                result = self.tools.run(name, arguments)
                content = result.to_message()
            except (ToolError, OSError, subprocess.SubprocessError) as exc:  # type: ignore[name-defined]
                content = json.dumps({"ok": False, "output": str(exc)}, ensure_ascii=False)
            if on_event:
                on_event(f"done {name}")
            session.append(Message("user", f"Tool result from {name}:\n{content}"))
            if stop_after_tool:
                return AgentResult(session.session_id, "", rounds)
        else:
            final_answer = "Paused after tool execution. Review the result above, then send the next instruction if needed."

        return AgentResult(session.session_id, final_answer, rounds)

    def _system_prompt(self) -> str:
        mode_hint = {
            "plan": "Current mode: plan. Read-only investigation only; do not request write_file, run_shell, or apply_patch.",
            "review": "Current mode: review. Read files, inspect git state, and run non-mutating diagnostics only.",
            "agent": "Current mode: agent. You may use tools when needed; destructive steps require a careful explanation.",
            "trusted": "Current mode: trusted. You may use workspace and network-capable tools, but preserve reversible changes.",
            "yolo": "Current mode: yolo. You may use tools without asking for confirmation.",
            "root": "Current mode: root. Highest authority mode; execute needed tools directly without confirmation.",
        }[self.mode]
        policy_hint = (
            f"Policy: write={self.policy.allow_write}, shell={self.policy.allow_shell}, "
            f"network={self.policy.allow_network}, confirmation={self.policy.require_confirmation}."
        )
        return (
            f"{SYSTEM_PROMPT}\nWorkspace: {self.settings.workspace}\n{mode_hint}\n"
            f"Thinking mode: {self.thinking.name}. {self.thinking.system_hint}\n{policy_hint}\n"
            f"{SkillStore(self.settings.workspace).prompt_context()}\n"
        )

    def _needs_confirmation(self, name: str) -> bool:
        dangerous = {"write_file", "run_shell", "apply_patch", "download_url", "start_service", "stop_service"}
        return name in dangerous and self.policy.require_confirmation

    def _approved(self, name: str, arguments: dict[str, Any]) -> bool:
        if self.mode in {"yolo", "root"}:
            return True
        if self.approve:
            return self.approve(name, arguments)
        return False

    def _with_internal_thinking(self, messages: list[Message], on_event: Callable[[str], None] | None = None) -> list[Message]:
        if self.thinking.deliberation_passes <= 0:
            return messages
        notes: list[str] = []
        for index in range(self.thinking.deliberation_passes):
            if on_event:
                on_event(f"thinking pass {index + 1}/{self.thinking.deliberation_passes}")
            planning_messages = messages + [
                Message(
                    "user",
                    "Internal deliberation pass. Think privately about the task, risks, missing context, and next tool/action. "
                    "Do not answer the user. Do not request tools. Return concise private notes only.",
                )
            ]
            note = self.client.chat(planning_messages).strip()
            if note:
                notes.append(note[:6000])
        if not notes:
            return messages
        joined = "\n\n".join(f"Pass {index + 1}:\n{note}" for index, note in enumerate(notes))
        return messages + [Message("system", "Private model deliberation notes for this turn:\n" + joined)]


def parse_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
    stripped = text.strip()
    candidates = [stripped]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    candidates.extend(fenced)
    candidates.extend(extract_json_objects(stripped))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        parsed = normalize_tool_call(data)
        if parsed:
            return parsed
    return parse_action_shell_block(stripped)


def is_question_mark_only(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and all(char in {"?", "？"} for char in stripped)


def plainify_assistant_text(text: str) -> str:
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    cleaned: list[str] = []
    for part in parts:
        if part.startswith("```"):
            cleaned.append(part)
            continue
        part = part.replace("**", "")
        part = re.sub(r"(?m)^(\s*)\*\s+", r"\1- ", part)
        cleaned.append(part)
    return "".join(cleaned)


def compact_context_messages(
    messages: list[Message],
    model: str,
    *,
    on_event: Callable[[str], None] | None = None,
    force: bool = False,
) -> list[Message]:
    if not force and os.getenv("DSTUL_AUTO_COMPACT", "1").lower() in {"0", "false", "no"}:
        return messages
    limit = context_window_tokens(model)
    threshold = int(limit * 0.92)
    estimated = estimate_message_tokens(messages)
    if not force and estimated <= threshold:
        return messages
    if len(messages) <= 10:
        return messages

    head: list[Message] = [messages[0]] if messages and messages[0].role == "system" else []
    body = messages[1:] if head else messages
    recent = body[-8:]
    older = body[:-8]
    if not older:
        return messages

    summary = summarize_messages_locally(older, max_chars=min(24000, max(4000, limit * 2)))
    compacted = head + [
        Message(
            "system",
            "Auto-compressed earlier conversation context because the estimated context window was near the model limit. "
            "Use this summary as prior context; recent exact messages follow.\n\n" + summary,
        )
    ] + recent
    if on_event:
        on_event(f"context compacted {estimated} -> {estimate_message_tokens(compacted)} est tokens")
    return compacted


def context_window_tokens(model: str) -> int:
    lowered = model.lower()
    if "v4" in lowered:
        return 1_000_000
    if "pro" in lowered:
        return 128_000
    if "flash" in lowered:
        return 128_000
    return 64_000


def estimate_message_tokens(messages: list[Message]) -> int:
    total_chars = sum(len(message.content) + len(message.role) + 8 for message in messages)
    return max(1, total_chars // 4)


def summarize_messages_locally(messages: list[Message], max_chars: int) -> str:
    lines: list[str] = []
    remaining = max_chars
    for message in messages:
        content = " ".join(message.content.split())
        entry = f"[{message.role}] {content}"
        if len(entry) > 1200:
            entry = entry[:1197] + "..."
        if len(entry) + 1 > remaining:
            lines.append("...")
            break
        lines.append(entry)
        remaining -= len(entry) + 1
    return "\n".join(lines)


ACTION_SHELL_CUES = (
    "我现在",
    "我来",
    "我会",
    "开始",
    "直接",
    "通过",
    "执行",
    "运行",
    "检查",
    "获取",
    "验证",
    "查询",
    "拉取",
    "下载",
    "搜索",
    "inspect",
    "check",
    "fetch",
    "get",
    "run",
    "execute",
    "verify",
)

EXAMPLE_SHELL_CUES = (
    "可以这样",
    "手动",
    "示例",
    "例子",
    "例如",
    "如果",
    "你可以",
    "建议",
    "example",
    "for example",
    "manually",
    "you can",
)


def parse_action_shell_block(text: str) -> tuple[str, dict[str, Any]] | None:
    blocks = re.findall(r"```(?:bash|sh|shell)\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if not blocks:
        return None
    prefix = text[: text.find("```")].lower()
    if any(cue in prefix for cue in EXAMPLE_SHELL_CUES):
        return None
    if not any(cue in prefix for cue in ACTION_SHELL_CUES):
        return None
    command = "\n".join(normalize_shell_block(block) for block in blocks)
    command = "\n".join(line for line in command.splitlines() if line.strip())
    if not command:
        return None
    return "run_shell", {"command": command}


def normalize_shell_block(block: str) -> str:
    lines: list[str] = []
    for raw_line in block.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("$ "):
            line = line[2:].strip()
        lines.append(line)
    return "\n".join(lines)


def normalize_tool_call(data: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("tool"), str):
        return data["tool"], normalize_tool_arguments(data)
    if isinstance(data.get("name"), str) and ("input" in data or "arguments" in data):
        return data["name"], normalize_arguments(data.get("input", data.get("arguments", {})))
    function_call = data.get("function_call")
    if isinstance(function_call, dict) and isinstance(function_call.get("name"), str):
        return function_call["name"], normalize_arguments(function_call.get("arguments", {}))
    tool_calls = data.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        first = tool_calls[0]
        if isinstance(first, dict):
            function = first.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                return function["name"], normalize_arguments(function.get("arguments", {}))
            if isinstance(first.get("name"), str):
                return first["name"], normalize_arguments(first.get("arguments", first.get("input", {})))
    return None


def normalize_tool_arguments(data: dict[str, Any]) -> dict[str, Any]:
    arguments = normalize_arguments(data.get("arguments", {}))
    for key in ("timeout", "max_results", "max_bytes", "max_matches"):
        if key in data and key not in arguments:
            arguments[key] = data[key]
    return arguments


def normalize_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def extract_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    starts = [index for index, char in enumerate(text) if char == "{"]
    for start in starts:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    objects.append(text[start : index + 1])
                    break
    return objects


def summarize_arguments(arguments: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in arguments.items():
        text = str(value).replace("\n", "\\n")
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{key}={text}")
    return " ".join(parts)
