from __future__ import annotations

import base64

from dataclasses import dataclass
import json
import os
import re
import subprocess
from typing import Any, Callable
import unicodedata

from .config import Settings
from .messages import Message
from .policy import ApprovalPolicy, ThinkingMode
from .provider import DeepSeekClient
from .session import Session
from .skills import SkillStore
from .tools import ToolError, ToolRegistry


SYSTEM_PROMPT = r"""You are DeepSeek TuLAgent, a concise coding agent running in a local workspace.
You can answer normally or request exactly one tool call by returning a single JSON object:
{"tool":"read_file","arguments":{"path":"README.md","max_bytes":12000}}

Available tools:
- ask_user(question, options?, allow_manual?, placeholder?): ask the user to choose from structured options or type a custom answer; use this when the next step needs the user's preference
- delegate_agent(name, task, mode?, thinking?/think?, max_rounds?) or delegate_agent(agents=[{name, task, mode?, thinking?/think?, max_rounds?}, ...]): run one or more isolated subagents and return summaries. mode controls permissions (plan/review/agent/trusted/yolo/root); thinking controls reasoning effort (instant/fast/balanced/deep/ultra/etc.). Omitted values inherit the parent agent.
- list_files(path?, max_entries?)
- search_text(query, path?, max_matches?)
- git_status(timeout?)
- read_file(path, max_bytes?)
- write_file(path, content)
- run_shell(command, timeout?)
- apply_patch(patch, timeout?)
- download_url(url, path, max_bytes?, timeout?)
- clone_repo(repo or url, path, branch?, timeout?)
- web_search(query, max_results?, timeout?, engines?, language?, fetch_pages?, page_chars?): search via Baidu/Bing/DuckDuckGo and return result snippets; engines may be a comma-separated override such as "bing,duckduckgo"; set fetch_pages to enrich top results with short robots-checked page text
- todo_write(todos): create or update the visible task list. todos is an array of {content, status}; status is pending, in_progress, completed, or cancelled.
- inspect_media(path, max_frames?): inspect an image or video path. For video, extracts representative frames. Use this when the user asks about screenshots, images, videos, cutting/editing video, or gives a media file path.
- start_service(name, command)
- stop_service(name)
- service_status(name)

Rules:
- Prefer reading before editing.
- Keep changes scoped to the user's request.
- Tool use must be emitted as the JSON object above. Do not put commands in bash/code fences when you want them executed.
- Never say a command, download, search, or file operation was executed unless it came from a Tool result.
- If the user asks you to inspect a live URL, GitHub repository, local files, shell state, or service state, use the appropriate tool instead of describing what you would run.
- If the user asks to clone, pull, download, or fetch a Git/GitHub repository into the workspace, prefer clone_repo over manual git clone shell commands. Windows paths like `D:\project\repo` are accepted, but clone_repo writes inside the configured workspace. Report its fallback summary and only ask for a proxy after clone_repo says all methods failed.
- Keep final replies visually plain. Avoid decorative Markdown, bold markers, and asterisk bullets unless code syntax or shell globbing requires `*`.
- Formatting preferences apply only to prose. Never alter code or file syntax to satisfy them: preserve CSS `*` selectors, glob patterns, operators, quoting, and all literal content exactly as required.
- Treat `cf`, `CF`, `ctf`, `CTF`, `cf题`, and similar short forms as Capture The Flag / challenge sandbox context. Do not ask the user to repeat that clarification.
- If the user message is only `?`, `？`, or repeated question marks, do not infer a task and do not use tools. Ask what they want to ask.
- To start a long-running/background process, use start_service(name, command). Do not use shell "&" backgrounding.
- To expose a local service publicly, check both local listening state and the public address. Prefer:
  `curl -fsS --connect-timeout 5 https://api.ipify.org || curl -fsS --connect-timeout 5 https://ifconfig.me || curl -fsS --connect-timeout 5 https://checkip.amazonaws.com`
  then verify the service with `ss -tlnp`, local `curl`, and firewall status (`ufw status` or iptables/nftables when available).
- For text search, prefer a narrow path and small max_matches. Broad searches can time out.
- Use delegate_agent proactively for multi-branch investigation, independent review, verification, research, or long workflows that can be split into focused subtasks. For multiple independent tasks, call delegate_agent once with an agents array (up to 8 subagents). Good subagent names: researcher, reviewer, verifier, implementer, debugger.
- When delegating, give each subagent a narrow task and ask for evidence plus a recommended next step. Set mode/thinking per subagent when it needs different permissions or reasoning depth. Do not delegate trivial one-step tasks.
- For non-trivial tasks, first call todo_write to list concrete task goals before doing the work. Keep exactly one item in_progress while work remains. Update todo_write immediately when starting or completing each item; do not batch all completions at the end. Skip todo_write only for very small one-step requests or purely informational answers.
- If a web_search result is empty, irrelevant, or failed and the user asked to search, request one more web_search with a clearer query or a different engines override before reporting failure.
- If no tool is needed, answer directly.
- After tool results, continue until the task is complete or clearly blocked.
"""


CONTINUE_AFTER_PROMISE_PROMPT = (
    "You said you would continue with more work, but you did not request a tool. "
    "Continue now by returning the next required tool JSON. "
    "If the task is actually complete, give the final answer instead."
)

RECOVER_AFTER_TOOL_FAILURE_PROMPT = (
    "The previous tool failed, but you stopped without trying a recovery path. "
    "Continue with one better tool call using the error details, or explicitly state that the task is blocked."
)

CONTINUE_AFTER_TODO_PROMPT = (
    "You wrote the task goals with todo_write. Do not stop at the checklist. "
    "Continue now with the first in_progress task using the required tool call. "
    "Update todo_write again when a task starts or completes."
)

REQUIRE_TODO_WRITE_PROMPT = (
    "This is a non-trivial multi-step task. Your first action must be a todo_write tool call. "
    "Do not describe the plan in prose. Return exactly one tool JSON object now, using todo_write with concrete task goals. "
    "Keep exactly one todo in_progress and the rest pending."
)

KNOWN_TOOL_NAMES = {
    "ask_user",
    "delegate_agent",
    "list_files",
    "search_text",
    "git_status",
    "read_file",
    "write_file",
    "run_shell",
    "apply_patch",
    "download_url",
    "clone_repo",
    "web_search",
    "todo_write",
    "inspect_media",
    "start_service",
    "stop_service",
    "service_status",
}
TOOL_CALL_META_KEYS = {"tool", "name", "type", "id", "function_call", "tool_calls", "arguments", "parameters", "args", "input"}
DSML_PREFIX = "<｜｜DSML｜｜"
_DSML_TAG = r"[|｜]{2}DSML[|｜]{2}"
_DSML_TOOL_BLOCK_RE = re.compile(
    rf"(?is)<{_DSML_TAG}tool_calls\b[^>]*>.*?</{_DSML_TAG}tool_calls\s*>",
)
_DSML_TOOL_OPEN_RE = re.compile(rf"(?is)<{_DSML_TAG}tool_calls\b[^>]*>.*$")


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
        ask_user: Callable[[dict[str, Any]], dict[str, Any] | str | None] | None = None,
    ):
        self.settings = settings
        self.mode = mode
        self.policy = ApprovalPolicy.from_mode(mode)
        self.thinking = ThinkingMode.resolve(thinking)
        self.client = client or DeepSeekClient(settings)
        self.tools = ToolRegistry(settings.workspace, policy=self.policy)
        self.approve = approve
        self.ask_user = ask_user
        self.last_model_messages: list[Message] = []

    def run(
        self,
        prompt: str,
        *,
        stream: bool = False,
        images: list[str] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        on_event: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        session: Session | None = None,
        max_tool_rounds: int | None = None,
        stop_after_tool: bool = False,
        goal: str | None = None,
        require_todo: bool = True,
    ) -> AgentResult:
        session = session or Session(self.settings.workspace)
        if not session.messages:
            for message in self._initial_messages():
                session.append(message)
        session.append(Message("user", prompt, images=list(images or [])))

        final_answer = ""
        rounds = 0
        last_turn_had_tool_result = False
        last_turn_had_tool_error = False
        last_tool_name: str | None = None
        last_todo_has_open_items = False
        pending_internal_prompt: str | None = None
        round_limit = max_tool_rounds or self.settings.max_tool_rounds
        complex_task = is_complex_task(prompt)
        todo_required = require_todo and complex_task
        todo_was_written = False
        todo_enforced = False
        promise_continuation_used = False
        for rounds in range(1, round_limit + 1):
            if should_cancel and should_cancel():
                raise RuntimeError("turn cancelled")
            model_source_messages = filter_internal_automation_messages(session.messages)
            compacted_messages = compact_context_messages(
                model_source_messages,
                self.settings.model,
                on_event=on_event,
                client=self.client,
                context_limit=self.settings.context_window_tokens,
                threshold_percent=self.settings.compact_threshold_percent,
            )
            if compacted_messages is not model_source_messages:
                session.messages = compacted_messages
                session.rewrite()
                model_source_messages = compacted_messages
            if pending_internal_prompt:
                model_source_messages = model_source_messages + [Message("user", pending_internal_prompt)]
                pending_internal_prompt = None
            model_messages = model_source_messages
            if rounds == 1 and complex_task:
                model_messages = model_messages + [Message("user", private_execution_hint())]
            model_messages = self._with_internal_thinking(model_messages, on_event=on_event)
            self.last_model_messages = list(model_messages)
            if stream:
                parts: list[str] = []
                held_parts: list[str] = []
                # Live streaming to the UI is only enabled when on_final is provided
                # (desktop). Deltas are emitted only up to a "safe" boundary: any tail
                # that looks like the start of a tool call (a line starting with '{' or
                # a code fence) is held back so tool JSON never leaks into the chat as
                # prose. on_final replaces the streamed text at the end either way.
                # Callers with on_delta only (CLI/TUI) keep the buffer-then-flush behavior.
                stream_live = on_final is not None
                emitted = 0
                held_notified = False
                for delta in self.client.stream_chat(model_messages):
                    if should_cancel and should_cancel():
                        raise RuntimeError("turn cancelled")
                    parts.append(delta)
                    held_parts.append(delta)
                    if stream_live and on_delta:
                        joined = "".join(parts)
                        if should_hold_stream_output(joined):
                            # the emerging output looks like a tool call; its JSON is
                            # held back from the chat. Signal the UI once so it can show
                            # a "preparing tool" indicator instead of a dead pause until
                            # the call is fully parsed at end-of-stream.
                            if not held_notified and on_event:
                                on_event("toolpending")
                                held_notified = True
                            continue
                        safe = safe_stream_emit_length(joined)
                        if safe > emitted:
                            on_delta(joined[emitted:safe])
                            emitted = safe
                assistant_text = "".join(parts)
            else:
                assistant_text = self.client.chat(model_messages)
            if should_cancel and should_cancel():
                raise RuntimeError("turn cancelled")
            tool_call = None if is_question_mark_only(prompt) else parse_tool_call(assistant_text)
            if not tool_call:
                if (
                    todo_required
                    and not todo_was_written
                    and not todo_enforced
                    and rounds == 1
                    and should_force_todo_after_prose(assistant_text)
                    and not declares_blocked_or_complete(assistant_text)
                ):
                    if stream and on_final:
                        on_final("")
                    pending_internal_prompt = REQUIRE_TODO_WRITE_PROMPT
                    todo_enforced = True
                    continue
                assistant_text = plainify_assistant_text(assistant_text)
                if (
                    last_turn_had_tool_result
                    and last_tool_name == "todo_write"
                    and last_todo_has_open_items
                    and todo_followup_is_placeholder(assistant_text)
                ):
                    if stream and on_final:
                        on_final("")
                    pending_internal_prompt = CONTINUE_AFTER_TODO_PROMPT
                    last_turn_had_tool_result = False
                    last_todo_has_open_items = False
                    continue
                if (
                    not promise_continuation_used
                    and not goal
                    and promises_more_work(assistant_text)
                    and not declares_blocked_or_complete(assistant_text)
                ):
                    if stream and on_final:
                        on_final("")
                    pending_internal_prompt = CONTINUE_AFTER_PROMISE_PROMPT
                    promise_continuation_used = True
                    last_turn_had_tool_result = False
                    continue
                if stream and on_final:
                    on_final(assistant_text)
                elif stream and held_parts and on_delta:
                    on_delta(assistant_text)
                session.append(Message("assistant", assistant_text))
                if last_turn_had_tool_error and not declares_blocked_or_complete(assistant_text):
                    pending_internal_prompt = RECOVER_AFTER_TOOL_FAILURE_PROMPT
                    last_turn_had_tool_error = False
                    continue
                if goal and not goal_answer_is_terminal(assistant_text):
                    session.append(Message("user", goal_continuation_prompt(goal)))
                    continue
                final_answer = assistant_text
                break
            name, arguments = tool_call
            # Tool call detected. If any text already streamed to the UI, replace it
            # with the prose around the tool JSON (or clear it) so the raw call never
            # stays visible as an assistant message.
            if stream and on_final is not None:
                prose = strip_tool_call_display(assistant_text)
                on_final("" if is_tool_intro_only(prose) else prose)
            session.append(Message("assistant", assistant_text))
            last_turn_had_tool_result = False
            last_turn_had_tool_error = False
            if name == "todo_write":
                todo_was_written = True
            if on_event:
                on_event(f"tool {name} {summarize_arguments(arguments)}")
            event_content: str | None = None
            try:
                tool_images: list[str] = []
                if name == "ask_user":
                    content = self._ask_user(arguments)
                elif name == "delegate_agent":
                    content = self._run_subagent(arguments, on_event=on_event, should_cancel=should_cancel)
                elif self._needs_confirmation(name) and not self._approved(name, arguments):
                    raise ToolError(f"confirmation required for tool: {name}")
                else:
                    result = self.tools.run(name, arguments)
                    content = result.to_message()
                    event_content = result.output if name == "todo_write" else content
                    if name == "todo_write":
                        last_todo_has_open_items = todo_result_has_open_items(result.output)
                    tool_images = list(getattr(result, "images", None) or [])
            except (ToolError, ValueError, OSError, subprocess.SubprocessError, TypeError) as exc:  # type: ignore[name-defined]
                content = json.dumps({"ok": False, "output": str(exc)}, ensure_ascii=False)
                event_content = content
                tool_images = []
            if on_event:
                _trimmed = trim_tool_content(event_content if event_content is not None else content)
                _b64 = base64.b64encode(_trimmed.encode("utf-8")).decode("ascii")
                on_event(f"done {name} {_b64}")
                if tool_images:
                    media_payload = json.dumps({"images": tool_images}, ensure_ascii=False)
                    on_event(f"media {name} {base64.b64encode(media_payload.encode('utf-8')).decode('ascii')}")
                if name == "todo_write":
                    on_event(f"todo {_b64}")
            session.append(Message("user", tool_result_message(name, trim_tool_content(content)), images=tool_images))
            last_turn_had_tool_result = True
            last_turn_had_tool_error = is_failed_tool_result(content)
            last_tool_name = name
            if should_cancel and should_cancel():
                raise RuntimeError("turn cancelled")
            if stop_after_tool:
                return AgentResult(session.session_id, "", rounds)
        else:
            final_answer = self._finalize_after_tool_limit(session, stream=stream, on_delta=on_delta, on_final=on_final, on_event=on_event)
            if final_answer:
                session.append(Message("assistant", final_answer))
            else:
                final_answer = "工具轮数已用完；请查看上面的工具结果，继续发送下一步指令。"

        return AgentResult(session.session_id, final_answer, rounds)

    def _initial_messages(self) -> list[Message]:
        messages = [Message("system", self._system_prompt())]
        skill_context = SkillStore(self.settings.workspace).prompt_context()
        if skill_context:
            messages.append(Message("system", skill_context))
        return messages

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
        )

    def _finalize_after_tool_limit(
        self,
        session: Session,
        *,
        stream: bool,
        on_delta: Callable[[str], None] | None,
        on_final: Callable[[str], None] | None = None,
        on_event: Callable[[str], None] | None,
    ) -> str:
        if on_event:
            on_event("tool round limit reached; finalizing")
        messages = compact_context_messages(
            filter_internal_automation_messages(session.messages),
            self.settings.model,
            on_event=on_event,
            client=self.client,
            context_limit=self.settings.context_window_tokens,
            threshold_percent=self.settings.compact_threshold_percent,
        )
        if messages is not session.messages:
            session.messages = messages
            session.rewrite()
        messages = messages + [
            Message(
                "user",
                "The tool round limit has been reached. Do not request more tools. "
                "Summarize what succeeded, what failed or remains unverified, and the exact next command or user action if needed.",
            )
        ]
        self.last_model_messages = list(messages)
        if stream:
            parts: list[str] = []
            for delta in self.client.stream_chat(messages):
                parts.append(delta)
                if on_delta and on_final is None:
                    on_delta(delta)
            final = plainify_assistant_text("".join(parts))
            if on_final:
                on_final(final)
            return final
        return plainify_assistant_text(self.client.chat(messages))

    def _needs_confirmation(self, name: str) -> bool:
        dangerous = {"write_file", "run_shell", "apply_patch", "download_url", "clone_repo", "start_service", "stop_service"}
        return name in dangerous and self.policy.require_confirmation

    def _run_subagent(
        self,
        arguments: dict[str, Any],
        on_event: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> str:
        specs = normalize_subagent_specs(arguments)
        if not specs:
            raise ToolError("delegate_agent requires task or agents")
        results = [
            self._run_one_subagent(spec, index, len(specs), on_event=on_event, should_cancel=should_cancel)
            for index, spec in enumerate(specs, start=1)
        ]
        if len(results) == 1:
            return json.dumps({"ok": True, **results[0]}, ensure_ascii=False)
        return json.dumps({"ok": True, "count": len(results), "agents": results}, ensure_ascii=False)

    def _run_one_subagent(
        self,
        spec: dict[str, Any],
        index: int,
        total: int,
        *,
        on_event: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if should_cancel and should_cancel():
            raise RuntimeError("turn cancelled")
        task = str(spec.get("task") or "").strip()
        if not task:
            raise ToolError("delegate_agent requires task")
        name = str(spec.get("name") or f"subagent-{index}").strip()[:40] or f"subagent-{index}"
        mode, thinking = normalize_subagent_mode_and_thinking(
            spec.get("mode", spec.get("permission", spec.get("permissions"))),
            spec.get("thinking", spec.get("think")),
            parent_mode=self.mode,
            parent_thinking=self.thinking.name,
        )
        max_rounds = min(max(int(spec.get("max_rounds", 4)), 1), 16)
        if on_event:
            prefix = f"{index}/{total} " if total > 1 else ""
            on_event(f"subagent {prefix}{name} mode={mode} think={thinking} rounds={max_rounds}")
        subagent = TuLAgent(self.settings, mode=mode, thinking=thinking, client=self.client, approve=self.approve, ask_user=self.ask_user)
        # forward the subagent's own events to the parent stream, tagged with its name,
        # so the UI can show what the subagent is doing (opencode-style nested activity)
        sub_on_event = None
        if on_event:
            def sub_on_event(text: str, _name=name) -> None:
                on_event("subevent " + _name + "␟" + text)
        sub_prompt = (
            f"You are subagent `{name}`. Work in an isolated context.\n"
            f"Task: {task}\n"
            "Return a concise result for the parent agent: findings, evidence, and recommended next step."
        )
        result = subagent.run(
            sub_prompt,
            max_tool_rounds=max_rounds,
            should_cancel=should_cancel,
            on_event=sub_on_event,
            require_todo=False,
            # ephemeral session: a delegated subagent must not create its own on-disk
            # conversation in the sidebar
            session=Session(self.settings.workspace, persist=False),
        )
        if on_event:
            # carry the subagent's full final summary so its card shows the complete
            # result, not just "rounds=N"
            summary_b64 = base64.b64encode((result.answer or "").encode("utf-8")).decode("ascii")
            on_event(f"subagentdone {name}␟rounds={result.rounds}␟{summary_b64}")
        return {
            "name": name,
            "task": task,
            "summary": result.answer,
            "session_id": result.session_id,
            "rounds": result.rounds,
        }

    def _ask_user(self, arguments: dict[str, Any]) -> str:
        question = str(arguments.get("question") or "").strip()
        if not question:
            raise ToolError("ask_user requires question")
        payload = normalize_user_question(arguments)
        if not self.ask_user:
            return json.dumps(
                {
                    "ok": False,
                    "error": "ask_user is only available in an interactive session",
                    "question": question,
                },
                ensure_ascii=False,
            )
        answer = self.ask_user(payload)
        if isinstance(answer, dict):
            result = {"ok": True, **answer}
        elif answer is None:
            result = {"ok": False, "cancelled": True}
        else:
            result = {"ok": True, "answer": str(answer)}
        return json.dumps(result, ensure_ascii=False)

    def _approved(self, name: str, arguments: dict[str, Any]) -> bool:
        if self.mode in {"yolo", "root"}:
            return True
        if self.approve:
            return self.approve(name, arguments)
        return False

    def _with_internal_thinking(self, messages: list[Message], on_event: Callable[[str], None] | None = None) -> list[Message]:
        # Thinking is delegated to the upstream reasoning/thinking API parameter (like
        # Codex sends reasoning:{effort}). We no longer run a separate "deliberation"
        # chat turn — that made the model emit tool-call JSON inside the private pass,
        # which then leaked into the transcript. Set DSTUL_LOCAL_DELIBERATION=1 to
        # re-enable the old behavior.
        if os.getenv("DSTUL_LOCAL_DELIBERATION") != "1":
            return messages
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
                if on_event:
                    # surface the deliberation content so the UI can show internal thinking
                    encoded = base64.b64encode(note[:6000].encode("utf-8")).decode("ascii")
                    on_event(f"thinkingnote {index + 1}/{self.thinking.deliberation_passes} {encoded}")
        if not notes:
            return messages
        joined = "\n\n".join(f"Pass {index + 1}:\n{note}" for index, note in enumerate(notes))
        return messages + [Message("system", "Private model deliberation notes for this turn:\n" + joined)]


def parse_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
    stripped = text.strip()
    dsml = parse_dsml_tool_call(stripped)
    if dsml:
        return dsml
    candidates = [stripped]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    candidates.extend(fenced)
    candidates.extend(extract_json_objects(stripped))
    for candidate in candidates:
        if tool_json_is_explanatory_example(stripped, candidate):
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        parsed = normalize_tool_call(data)
        if parsed:
            return parsed
    labelled = parse_labelled_tool_call(stripped)
    if labelled:
        return labelled
    xml = parse_xml_tool_call(stripped)
    if xml:
        return xml
    # Do NOT infer a tool from ordinary markdown code fences. Codex/opencode receive
    # tool calls as structured events; guessing from ```bash creates false positives and
    # turns normal code examples into tools. Keep action-shell parsing available only via
    # an explicit opt-in env for legacy behavior.
    if os.getenv("DSTUL_PARSE_ACTION_SHELL", "0").lower() in {"1", "true", "yes"}:
        return parse_action_shell_block(stripped)
    return None


def parse_dsml_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
    invoke = re.search(
        rf"(?is)<{_DSML_TAG}invoke\b([^>]*)>(.*?)</{_DSML_TAG}invoke\s*>",
        text,
    )
    if not invoke:
        return None
    name_match = re.search(r'''(?i)\bname\s*=\s*["']([^"']+)["']''', invoke.group(1))
    if not name_match or name_match.group(1) not in KNOWN_TOOL_NAMES:
        return None
    arguments: dict[str, Any] = {}
    for parameter in re.finditer(
        rf"(?is)<{_DSML_TAG}parameter\b([^>]*)>(.*?)</{_DSML_TAG}parameter\s*>",
        invoke.group(2),
    ):
        attrs, raw_value = parameter.groups()
        parameter_name = re.search(r'''(?i)\bname\s*=\s*["']([^"']+)["']''', attrs)
        if not parameter_name:
            continue
        value: Any = raw_value
        string_flag = re.search(r'''(?i)\bstring\s*=\s*["']true["']''', attrs)
        if not string_flag:
            try:
                value = json.loads(raw_value.strip())
            except json.JSONDecodeError:
                value = raw_value
        arguments[parameter_name.group(1)] = value
    return name_match.group(1), arguments


def tool_json_is_explanatory_example(text: str, candidate: str) -> bool:
    if text.strip() == candidate.strip():
        return False
    position = text.find(candidate)
    if position < 0:
        return False
    context = (text[max(0, position - 240):position] + text[position + len(candidate):position + len(candidate) + 80]).lower()
    cues = (
        "正确格式", "格式应该", "格式应为", "比如", "例如", "示例", "例子",
        "correct format", "format should", "for example", "example", "e.g.",
    )
    return any(cue in context for cue in cues)


# Hermes/Qwen/GLM/Kimi-style tool calls wrapped in <tool_call>…</tool_call> tags.
_TOOL_TAG_RE = re.compile(r"(?is)<+\s*tool_call\b[^>]*>(.*?)</\s*tool_call\s*>+")
_TOOL_TAG_OPEN_RE = re.compile(r"(?is)<+\s*tool_call\b[^>]*>(.*)$")


def parse_xml_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
    """Parse a <tool_call>…</tool_call> block (also tolerating an unterminated tag from a
    truncated stream). The inner payload may be JSON ({"name","arguments"}), or a
    name-then-JSON / name-then-key=value form."""
    match = _TOOL_TAG_RE.search(text) or _TOOL_TAG_OPEN_RE.search(text)
    if not match:
        return None
    inner = match.group(1).strip()
    if not inner:
        return None
    for candidate in [inner, *extract_json_objects(inner)]:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        parsed = normalize_tool_call(data)
        if parsed:
            return parsed
    # name-then-body form: "write_file\n{...}" or "write_file\ncmd=ls"
    lines = inner.splitlines()
    if lines:
        name_match = re.match(r"^([A-Za-z_][\w-]*)\s*$", lines[0].strip())
        if name_match:
            name = name_match.group(1)
            if name not in KNOWN_TOOL_NAMES:
                return None
            rest = "\n".join(lines[1:]).strip()
            for candidate in [rest, *extract_json_objects(rest)]:
                try:
                    data = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    return name, data
            kv: dict[str, Any] = {}
            for line in lines[1:]:
                kv_match = re.match(r"^([A-Za-z_][\w-]*)\s*=\s*(.*)$", line.strip())
                if kv_match:
                    kv[kv_match.group(1)] = kv_match.group(2).strip()
            if kv:
                return name, kv
    return None


def parse_labelled_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
    tool_match = re.search(r"(?im)^\s*(?:tool|工具)\s*:\s*([A-Za-z_][\w-]*)\s*$", text)
    if not tool_match:
        return None
    name = tool_match.group(1).strip()
    if name not in KNOWN_TOOL_NAMES:
        return None
    tail = text[tool_match.end():]
    args_match = re.search(r"(?is)(?:arguments|args|参数)\s*:\s*(\{.*\})", tail)
    if args_match:
        raw_args = args_match.group(1).strip()
        for candidate in [raw_args, *extract_json_objects(raw_args)]:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return name, data
    # also accept `key=value` argument lines (tool: run_shell\ncmd=ls)
    kv: dict[str, Any] = {}
    for line in tail.splitlines():
        line = line.strip()
        m = re.match(r"^([A-Za-z_][\w-]*)\s*=\s*(.*)$", line)
        if m:
            kv[m.group(1)] = m.group(2).strip()
    if kv:
        return name, kv
    return None


def is_question_mark_only(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and all(char in {"?", "？"} for char in stripped)


def tool_result_message(name: str, content: str) -> str:
    if name == "delegate_agent":
        subagent_name = "delegate_agent"
        try:
            data = json.loads(content)
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                subagent_name = data["name"]
            elif isinstance(data, dict) and isinstance(data.get("agents"), list):
                names = [str(agent.get("name")) for agent in data["agents"] if isinstance(agent, dict) and agent.get("name")]
                if names:
                    subagent_name = ",".join(names[:4])
                    if len(names) > 4:
                        subagent_name += f",+{len(names) - 4}"
        except json.JSONDecodeError:
            pass
        return f"SUBAGENT_RESULT name={subagent_name}\n{content}"
    if name == "ask_user":
        return f"USER_ANSWER\n{content}"
    return f"TOOL_RESULT name={name}\n{content}"


def normalize_user_question(arguments: dict[str, Any]) -> dict[str, Any]:
    options: list[dict[str, str]] = []
    raw_options = arguments.get("options", [])
    if isinstance(raw_options, list):
        for index, raw in enumerate(raw_options):
            if isinstance(raw, str):
                label = raw.strip()
                value = label
                description = ""
            elif isinstance(raw, dict):
                label = str(raw.get("label") or raw.get("title") or raw.get("value") or "").strip()
                value = str(raw.get("value") or label).strip()
                description = str(raw.get("description") or raw.get("detail") or "").strip()
            else:
                continue
            if not label:
                continue
            options.append({"label": label, "value": value or label, "description": description, "id": str(index)})
    allow_manual = arguments.get("allow_manual", arguments.get("manual", True))
    return {
        "question": str(arguments.get("question") or "").strip(),
        "options": options,
        "allow_manual": bool(allow_manual),
        "placeholder": str(arguments.get("placeholder") or "手动输入").strip(),
    }


def trim_tool_content(content: str, max_chars: int = 24000) -> str:
    if len(content) <= max_chars:
        return content
    head_len = max_chars // 2
    tail_len = max_chars - head_len
    omitted = len(content) - head_len - tail_len
    return content[:head_len] + f"\n[tool output trimmed: {omitted} chars omitted]\n" + content[-tail_len:]


def is_internal_automation_prompt(text: str) -> bool:
    stripped = " ".join(text.strip().split())
    return stripped in {CONTINUE_AFTER_PROMISE_PROMPT, RECOVER_AFTER_TOOL_FAILURE_PROMPT}


def filter_internal_automation_messages(messages: list[Message]) -> list[Message]:
    return [message for message in messages if not (message.role == "user" and is_internal_automation_prompt(message.content))]


def is_failed_tool_result(content: str) -> bool:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and data.get("ok") is False


def declares_blocked_or_complete(text: str) -> bool:
    lowered = text.lower()
    cues = ("blocked", "无法继续", "被阻塞", "需要用户", "已完成", "完成了", "done", "finished")
    return any(cue in lowered for cue in cues)


def goal_answer_is_terminal(text: str) -> bool:
    return declares_blocked_or_complete(text) or any(cue in text for cue in ("目标已完成", "目标完成", "已经达成目标"))


def goal_continuation_prompt(goal: str) -> str:
    return (
        f"Active goal: {goal}\n"
        "The goal is not explicitly complete or blocked. Do not stop yet. "
        "Choose the next concrete tool-backed step, or explicitly state completion/blockage with evidence."
    )


def is_complex_task(prompt: str) -> bool:
    normalized = re.sub(r"\s+", "", prompt.lower())
    cues = ("然后", "再", "并", "启动", "验证", "检查", "部署", "开放端口", "公网", "and", "then")
    return sum(1 for cue in cues if cue in normalized) >= 2


def private_execution_hint() -> str:
    return (
        "Private execution hint: this is a multi-step task. Work in small tool-backed steps. "
        "Before any other tool or prose, call todo_write with concrete task goals; this drives the visible task-goal UI. "
        "If part of the task benefits from independent research, review, debugging, or verification, use delegate_agent with a narrow subtask. "
        "After each tool result, continue with the next required tool until the requested workflow is verified or blocked. "
        "Do not merely say what you will do next."
    )


def should_force_todo_after_prose(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text.lower())
    cues = (
        "列任务目标", "任务目标", "任务清单", "todo", "待办",
        "我先列", "先列出", "计划如下", "执行计划", "开始修复", "开始处理",
    )
    return any(cue in normalized for cue in cues) or promises_more_work(text)


def promises_more_work(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) > 260:
        return False
    future_cues = (
        "接下来",
        "下一步",
        "继续",
        "马上",
        "随后",
        "然后",
        "现在继续",
        "让我先",
        "我先",
        "next",
        "continue",
    )
    action_cues = (
        "执行",
        "检查",
        "启动",
        "验证",
        "运行",
        "查看",
        "读取",
        "打开",
        "分析",
        "处理",
        "创建",
        "写入",
        "修改",
        "搜索",
        "下载",
        "放行",
        "部署",
        "execute",
        "check",
        "start",
        "verify",
        "run",
    )
    completion_cues = ("已完成", "完成了", "已经完成", "done", "finished")
    if any(cue in stripped.lower() for cue in completion_cues) and not any(cue in stripped for cue in ("接下来", "下一步", "继续", "然后")):
        return False
    return any(cue in stripped for cue in future_cues) and any(cue in stripped for cue in action_cues)


def todo_result_has_open_items(output: str) -> bool:
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return True
    todos = data.get("todos") if isinstance(data, dict) else data
    if not isinstance(todos, list):
        return True
    if not todos:
        return False
    for item in todos:
        if isinstance(item, dict) and str(item.get("status") or "pending") in {"pending", "in_progress"}:
            return True
    return False


def todo_followup_is_placeholder(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text)
    if not stripped:
        return True
    if len(stripped) > 120:
        return False
    lowered = stripped.lower()
    cues = (
        "继续处理",
        "继续执行",
        "开始处理",
        "开始执行",
        "任务已启动",
        "我会继续",
        "接下来继续",
        "继续完成",
        "continuing",
        "continue",
    )
    return any(cue in lowered for cue in cues) or promises_more_work(text)


def plainify_assistant_text(text: str) -> str:
    # never surface a raw tool-call tag as prose (e.g. a stream that ended mid-tag and
    # failed to parse) — drop closed blocks and any dangling opener
    text = _DSML_TOOL_BLOCK_RE.sub("", text)
    text = _DSML_TOOL_OPEN_RE.sub("", text)
    text = _TOOL_TAG_RE.sub("", text)
    text = _TOOL_TAG_OPEN_RE.sub("", text)
    dangling_fence = dangling_tool_json_fence_start(text)
    if dangling_fence is not None:
        text = text[:dangling_fence]
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


def should_hold_stream_output(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return True
    if DSML_PREFIX.startswith(stripped) or stripped.startswith(DSML_PREFIX):
        return len(stripped) < 32000
    # A leading '<' may be the start of a <tool_call> tag building up char-by-char.
    # Hold while the buffer is still a prefix of "<tool_call" or already opens one.
    tag = "<tool_call"
    head = stripped[: len(tag)].lower()
    if stripped[:1] == "<" and (head.startswith(tag[: len(head)]) or head == tag):
        return len(stripped) < 32000
    leading_fence = re.match(r"(?is)^```\s*([a-z0-9_-]*)[^\n]*\n?(.*)$", stripped)
    if leading_fence:
        lang = (leading_fence.group(1) or "").lower()
        body = (leading_fence.group(2) or "").lstrip()
        if lang in {"json", "tool", "tools"} and (
            not body
            or re.match(r'^\{\s*(?:"(?:tool|name|function_call|tool_calls)"?)?$', body, flags=re.IGNORECASE)
            or re.match(r'^\{\s*"(?:tool|name|function_call|tool_calls)"', body, flags=re.IGNORECASE)
        ):
            return len(stripped) < 32000
        return False
    tool_prefixes = (
        "{",
        "tool:",
        "工具:",
        "Tool:",
        "Arguments:",
        "参数:",
    )
    if any(stripped.startswith(prefix) for prefix in tool_prefixes):
        return len(stripped) < 32000
    return False


def dangling_tool_json_fence_start(text: str) -> int | None:
    fences = list(re.finditer(r"```", text))
    if not fences or len(fences) % 2 == 0:
        return None
    fence_start = fences[-1].start()
    tail = text[fence_start + 3:]
    if "\n" in tail:
        header, body = tail.split("\n", 1)
    else:
        header, body = tail, ""
    lang = header.strip().lower()
    if lang not in {"json", "tool", "tools"} and not any(
        candidate.startswith(lang) for candidate in ("json", "tool", "tools")
    ):
        return None
    # Keep the whole open JSON/tool fence buffered. Once a closing fence arrives,
    # normal JSON examples are released while parse_tool_call consumes real calls.
    return fence_start


def safe_stream_emit_length(text: str) -> int:
    """How much of a streaming buffer is safe to show without leaking a tool call.

    Models often append a tool call right after a sentence, on the SAME line
    (`好的，我来调用：<tool_call>{…}` or `… {"tool":…}`), so line-start detection alone
    leaks it. We first look for a specific tool-call marker anywhere and hold from there;
    everything before it (real prose) stays visible. on_final re-sends the full cleaned
    text at end-of-turn, so holding a false positive back only delays it, never drops it.
    """
    # A stream may split a Markdown fence one character at a time. Hold a trailing
    # one/two-backtick prefix until we know whether it becomes a tool JSON fence.
    partial_fence = re.search(r"`+$", text)
    if partial_fence:
        run_length = len(partial_fence.group(0))
        complete_fences = len(list(re.finditer(r"```", text)))
        if run_length < 3 or complete_fences % 2 == 1:
            return partial_fence.start()

    dsml = text.find(DSML_PREFIX)
    if dsml != -1:
        return dsml
    possible_dsml = text.rfind("<")
    if possible_dsml != -1 and DSML_PREFIX.startswith(text[possible_dsml:]):
        return possible_dsml

    # 1) specific, high-signal tool-call openers that may appear mid-line.
    # If the marker is inside a ```json fence, hold from the fence opener too — otherwise
    # the UI briefly renders an empty JSON/code box before the tool card appears.
    tag = re.search(r"(?i)<tool_call", text)
    if tag:
        return tag.start()
    for specific in re.finditer(r"(?i)\{\s*\"(?:tool|name|function_call|tool_calls)\"", text):
        fence_start = text.rfind("```", 0, specific.start())
        inside_tool_fence = False
        if fence_start != -1:
            after = text[fence_start:specific.start()].lower()
            inside_tool_fence = bool(re.match(r"```\s*(json|tool|tools)?\s*\n?\s*$", after))
        objects = extract_json_objects(text[specific.start():])
        if not objects:
            return fence_start if inside_tool_fence else specific.start()
        try:
            data = json.loads(objects[0])
        except json.JSONDecodeError:
            return fence_start if inside_tool_fence else specific.start()
        if not normalize_tool_call(data):
            continue
        if inside_tool_fence:
            return fence_start
        return specific.start()
    # 1b) our labelled format at the start of a line (Tool:/工具: name)
    labelled = re.search(r"(?im)^[ \t]*(?:tool|工具)[ \t]*:[ \t]*[A-Za-z_][\w-]*[ \t]*$", text)
    if labelled:
        return labelled.start()
    dangling_fence = dangling_tool_json_fence_start(text)
    if dangling_fence is not None:
        return dangling_fence
    fences = list(re.finditer(r"(?m)^[ \t]*```", text))
    if len(fences) % 2 == 1:
        fence_start = fences[-1].start()
        open_fence = re.match(r"(?is)[ \t]*```\s*([a-z0-9_-]*)[^\n]*\n?(.*)$", text[fence_start:])
        if open_fence:
            lang = (open_fence.group(1) or "").lower()
            body = (open_fence.group(2) or "").lstrip()
            if lang in {"json", "tool", "tools"} and (
                not body
                or re.match(r'^\{\s*(?:"(?:tool|name|function_call|tool_calls)"?)?$', body, flags=re.IGNORECASE)
                or re.match(r'^\{\s*"(?:tool|name|function_call|tool_calls)"', body, flags=re.IGNORECASE)
            ):
                return fence_start
    fenced_spans = [(fences[i].start(), fences[i + 1].end()) for i in range(0, len(fences) - 1, 2)]

    def _inside_closed_fence(pos: int) -> bool:
        return any(start <= pos < end for start, end in fenced_spans)

    # 2) otherwise a line starting with '{' may begin a raw JSON tool call — but only
    # outside markdown code fences. A normal ```json example must never be treated as a
    # tool or partially held.
    last = None
    for match in re.finditer(r"(?m)^[ \t]*\{", text):
        if _inside_closed_fence(match.start()):
            continue
        last = match
    if last is None:
        return len(text)
    start = last.start()
    segment = text[start:].strip()
    try:
        data = json.loads(segment)
    except json.JSONDecodeError:
        return start  # incomplete JSON outside a fence — keep holding
    return start if normalize_tool_call(data) else len(text)


def is_tool_intro_only(text: str) -> bool:
    """True when the only prose around a tool call is a generic intro like
    '我来调用工具：'. Showing that as a separate assistant bubble creates duplicate copy
    rows and an awkward gap before the tool card, so drop it."""
    normalized = re.sub(r"\s+", "", (text or "").strip().strip("：:，,。.!！"))
    if not normalized:
        return True
    intro_cues = (
        "我来调用工具", "调用工具", "准备调用工具", "开始调用工具", "执行工具", "使用工具",
        "我来执行", "开始执行", "现在执行", "我来运行", "准备运行", "运行命令",
        "我来读取", "开始读取", "现在读取", "我来检查", "开始检查", "现在检查",
        "我来写入", "开始写入", "我来写文件", "开始写文件", "我来修改", "开始修改", "我来搜索", "开始搜索",
        "I'lluseatool", "Iwilluseatool", "Usingtool", "Callingtool", "Runningtool",
    )
    if normalized in intro_cues or any(normalized.startswith(cue) and len(normalized) <= 60 for cue in intro_cues):
        return True
    return any(normalized.endswith(cue) and len(normalized) <= len(cue) + 4 for cue in intro_cues)


def strip_tool_call_display(text: str) -> str:
    """Remove tool-call JSON/blocks from assistant text, keeping surrounding prose."""
    def _drop_if_tool(candidate: str, whole: str) -> str:
        try:
            if normalize_tool_call(json.loads(candidate)):
                return ""
        except json.JSONDecodeError:
            pass
        return whole

    # drop DSML / <tool_call> blocks and any dangling unterminated opener
    out = _DSML_TOOL_BLOCK_RE.sub("", text)
    out = _DSML_TOOL_OPEN_RE.sub("", out)
    out = _TOOL_TAG_RE.sub("", out)
    out = _TOOL_TAG_OPEN_RE.sub("", out)
    # scrub any remaining bare tool_call tag fragment (with extra angle brackets) and
    # empty <> pairs left behind — but never touch legit prose like "a < b"
    out = re.sub(r"(?i)<+\s*/?\s*tool_call\b[^>]*>*", "", out)
    out = re.sub(r"<\s*>", "", out)
    # our labelled format (Tool:/工具: + arguments:/参数:/key=value) — if the model uses
    # it, the label and everything after it is the tool call, not prose. Cut it out so
    # the raw tool parameters never show as text; keep the prose that precedes it.
    label_match = re.search(r"(?im)^[ \t]*(?:tool|工具)[ \t]*:[ \t]*[A-Za-z_][\w-]*[ \t]*$", out)
    if label_match and parse_labelled_tool_call(out[label_match.start():]):
        out = out[: label_match.start()]
    out = re.sub(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        lambda m: _drop_if_tool(m.group(1), m.group(0)),
        out,
        flags=re.DOTALL,
    )
    for candidate in extract_json_objects(out):
        if _drop_if_tool(candidate, candidate) == "":
            pos = out.find(candidate)
            if pos == -1:
                continue
            end = pos + len(candidate)
            # models sometimes wrap the tool JSON in an extra brace/bracket (…}}} or
            # [{…]); consume brackets/whitespace immediately adjacent to the removed
            # object so no orphan bracket is left dangling as prose
            while pos > 0 and out[pos - 1] in "{[ \t":
                pos -= 1
            while end < len(out) and out[end] in "}] \t":
                end += 1
            out = out[:pos] + out[end:]
    # also scrub any line that is now only brackets/angle brackets
    out = re.sub(r"(?m)^[ \t]*[{}\[\]<>]+[ \t]*$\n?", "", out)
    return plainify_assistant_text(out).strip()


COMPACTION_PROMPT = (
    "You are performing a CONTEXT CHECKPOINT COMPACTION. The conversation above is being "
    "handed off to another instance of yourself that will resume this exact task with no "
    "other memory of it. Write a handoff summary — not a description of the conversation, but "
    "the working state the next instance needs. Cover:\n"
    "1. Current progress and key decisions made (what has been done and why).\n"
    "2. Important context, constraints, files, commands, or user preferences.\n"
    "3. What remains to be done, as clear next steps.\n"
    "4. Any critical data, code, paths, IDs, or references needed to continue.\n"
    "If the conversation already contains an earlier handoff summary, preserve its facts by "
    "folding them into a cumulative 'Historical Context' section — never drop earlier entries. "
    "Be concise, structured, and focused on letting the next instance seamlessly continue. "
    "Respond with the summary only."
)

COMPACTION_SUMMARY_PREFIX = (
    "Another instance of you worked on this task and produced the following handoff summary of "
    "its progress and the current state. Treat it as established prior context and continue the "
    "work from here; the most recent exact messages follow after it.\n\n"
)


def compact_context_messages(
    messages: list[Message],
    model: str,
    *,
    on_event: Callable[[str], None] | None = None,
    force: bool = False,
    client: DeepSeekClient | None = None,
    context_limit: int | None = None,
    threshold_percent: float | None = None,
) -> list[Message]:
    if not force and os.getenv("DSTUL_AUTO_COMPACT", "1").lower() in {"0", "false", "no"}:
        return messages
    limit = int(context_limit or context_window_tokens(model))
    ratio = max(1.0, min(99.0, float(threshold_percent if threshold_percent is not None else 95.0))) / 100
    threshold = int(limit * ratio)
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

    # Codex-style: hand the older history to the model to write a handoff summary, and
    # replace it with that summary. Fall back to local truncation only if there is no
    # client or the summary call fails, so compaction never breaks a turn.
    summary = summarize_messages_with_model(older, client=client, on_event=on_event)
    if not summary:
        summary = summarize_messages_locally(older, max_chars=min(24000, max(4000, limit * 2)))
    compacted = head + [
        Message("system", COMPACTION_SUMMARY_PREFIX + summary)
    ] + recent
    if on_event:
        on_event(f"context compacted {estimated} -> {estimate_message_tokens(compacted)} est tokens")
    return compacted


def summarize_messages_with_model(
    messages: list[Message],
    *,
    client: DeepSeekClient | None,
    on_event: Callable[[str], None] | None = None,
) -> str:
    """Ask the model to write a Codex-style handoff summary of `messages`. Returns an
    empty string if no client is available or the call fails (caller falls back)."""
    if client is None or not messages:
        return ""
    # Strip images from the transcript we summarize (keeps the summary call cheap and
    # text-only); the summary is prose anyway.
    transcript = [Message(m.role, m.content, name=m.name) for m in messages]
    request = transcript + [Message("user", COMPACTION_PROMPT)]
    try:
        if on_event:
            on_event("context compacting (model summary)")
        summary = client.chat(request).strip()
        return summary
    except Exception:  # network / provider error — fall back to local summary
        return ""


def context_window_tokens(model: str) -> int:
    return context_window_info(model)["tokens"]


def context_window_info(model: str) -> dict[str, Any]:
    lowered = model.lower()
    explicit = explicit_context_window_tokens(lowered)
    if explicit:
        return {"tokens": explicit, "source": "model-name"}

    # Provider/model family heuristics. Third-party OpenAI-compatible endpoints often
    # only expose a model string, so prefer recognizable current families and fall back
    # conservatively when the provider does not publish metadata via /models.
    rules: list[tuple[tuple[str, ...], int, str]] = [
        (("gpt-5.5", "gpt-5.4", "gpt-5 ", "gpt-5-", "gpt-5.", "gpt-5"), 1_000_000, "openai"),
        (("gpt-5.4-mini", "gpt-5-mini", "gpt-5.4-nano", "gpt-5-nano"), 400_000, "openai"),
        (("gpt-4.1", "gpt-4.5", "o3", "o4"), 1_000_000, "openai"),
        (("gpt-4o", "chatgpt-4o", "gpt-4-turbo"), 128_000, "openai"),
        (("claude-fable-5", "claude-mythos-5", "claude-opus-4", "claude-sonnet-5", "claude-sonnet-4"), 1_000_000, "anthropic"),
        (("claude-haiku-4", "claude-3-7", "claude-3.7", "claude-3-5", "claude-3.5", "claude-3-opus", "claude-3-sonnet"), 200_000, "anthropic"),
        (("gemini-3", "gemini-2.5", "gemini-2.0", "gemini-pro-latest", "gemini-flash-latest"), 1_000_000, "google"),
        (("gemini-1.5-pro",), 2_000_000, "google"),
        (("gemini-1.5",), 1_000_000, "google"),
        (("deepseek-v4",), 1_000_000, "deepseek"),
        (("deepseek-chat", "deepseek-reasoner"), 1_000_000, "deepseek"),
        (("deepseek-v3.1", "deepseek-v3-1", "deepseek-r1", "deepseek-v3"), 128_000, "deepseek"),
        (("qwen3.7", "qwen3-7", "qwen3.6", "qwen3-6", "qwen3.5", "qwen3-5", "qwen3-max", "qwen3-plus", "qwen-max", "qwen-plus", "qwen-long"), 1_000_000, "qwen"),
        (("qwen3-coder", "qwen3-turbo", "qwen3", "qwen-3", "qwen2.5", "qwen-2.5"), 128_000, "qwen"),
        (("kimi-k2.7", "kimi-k2-7", "kimi-k2.6", "kimi-k2-6"), 256_000, "moonshot"),
        (("kimi-k2", "kimi-k1.5", "moonshot-v1", "moonshot", "kimi"), 128_000, "moonshot"),
        (("glm-5.2", "glm-5-2"), 1_000_000, "zhipu"),
        (("glm-5.1", "glm-5-1", "glm-5-turbo", "glm-5", "glm-4.7", "glm-4-7", "glm-4.6", "glm-4-6"), 200_000, "zhipu"),
        (("glm-4.5", "glm-4-plus", "glm-4-air", "glm-4", "chatglm", "zhipu"), 128_000, "zhipu"),
        (("minimax-m3",), 1_000_000, "minimax"),
        (("minimax-m2.7", "minimax-m2.5", "minimax-m2.1", "minimax-m2", "abab6.5", "abab6", "minimax"), 204_800, "minimax"),
        (("doubao-1.6", "doubao-1.5", "doubao-pro", "doubao-seed", "doubao", "豆包"), 256_000, "volcengine"),
        (("hunyuan-turbos", "hunyuan-turbo", "hunyuan-large", "hunyuan", "混元"), 256_000, "tencent"),
        (("ernie-4.5", "ernie-x1", "ernie-speed", "ernie", "文心", "千帆"), 128_000, "baidu"),
        (("yi-large", "yi-lightning", "yi-1.5", "yi-", "零一"), 200_000, "01ai"),
        (("baichuan4", "baichuan3", "baichuan", "百川"), 128_000, "baichuan"),
        (("internlm3", "internlm2.5", "internlm", "书生"), 200_000, "internlm"),
        (("step-2", "step-1", "stepfun", "阶跃"), 128_000, "stepfun"),
        (("sensechat", "日日新", "商汤"), 128_000, "sense"),
        (("spark-max", "spark-pro", "讯飞星火", "xinghuo"), 128_000, "iflytek"),
        (("llama-4", "llama4", "llama-3.3", "llama-3.1"), 128_000, "llama"),
        (("mistral-large", "pixtral-large", "codestral"), 128_000, "mistral"),
        (("grok-4", "grok-3", "grok-2"), 128_000, "xai"),
    ]
    for needles, tokens, source in rules:
        if any(needle in lowered for needle in needles):
            return {"tokens": tokens, "source": source}
    return {"tokens": 64_000, "source": "fallback"}


def explicit_context_window_tokens(model: str) -> int | None:
    patterns = [
        r"(?<!\d)(\d+(?:\.\d+)?)\s*(m|k)\b",
        r"(?:context|ctx|window)[-_ ]?(\d+(?:\.\d+)?)\s*(m|k)\b",
        r"(\d+(?:\.\d+)?)\s*(m|k)[-_ ]?(?:context|ctx|window)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, model, flags=re.IGNORECASE):
            number = float(match.group(1))
            unit = match.group(2).lower()
            tokens = int(number * (1_000_000 if unit == "m" else 1_000))
            if 4_000 <= tokens <= 10_000_000:
                return tokens
    return None


def estimate_message_tokens(messages: list[Message]) -> int:
    if not messages:
        return 0
    total = 0
    for message in messages:
        ascii_chars = 0
        wide_chars = 0
        other_chars = 0
        for char in message.content:
            if char.isascii():
                ascii_chars += 1
            elif unicodedata.east_asian_width(char) in {"W", "F"}:
                wide_chars += 1
            else:
                other_chars += 1
        total += (ascii_chars + 3) // 4
        total += wide_chars
        total += (other_chars + 1) // 2
        total += 4  # role and message framing
        # Providers charge images by dimensions/detail rather than base64 length.
        total += 1024 * len(message.images)
    return max(1, total)


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
        name = data["tool"]
        arguments = normalize_tool_arguments(data)
        return (name, arguments) if name in KNOWN_TOOL_NAMES and arguments is not None else None
    if isinstance(data.get("name"), str) and ("input" in data or "arguments" in data or "parameters" in data or "args" in data):
        name = data["name"]
        arguments = normalize_arguments(data.get("input", data.get("arguments", data.get("parameters", data.get("args", {})))))
        return (name, arguments) if name in KNOWN_TOOL_NAMES and arguments is not None else None
    function_call = data.get("function_call")
    if isinstance(function_call, dict) and isinstance(function_call.get("name"), str):
        name = function_call["name"]
        arguments = normalize_arguments(function_call.get("arguments", {}))
        return (name, arguments) if name in KNOWN_TOOL_NAMES and arguments is not None else None
    tool_calls = data.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        first = tool_calls[0]
        if isinstance(first, dict):
            function = first.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                name = function["name"]
                arguments = normalize_arguments(function.get("arguments", {}))
                return (name, arguments) if name in KNOWN_TOOL_NAMES and arguments is not None else None
            if isinstance(first.get("name"), str):
                name = first["name"]
                arguments = normalize_arguments(first.get("arguments", first.get("input", first.get("parameters", first.get("args", {})))))
                return (name, arguments) if name in KNOWN_TOOL_NAMES and arguments is not None else None
    return None


def normalize_tool_arguments(data: dict[str, Any]) -> dict[str, Any] | None:
    raw = data.get("arguments", data.get("parameters", data.get("args", None)))
    if raw is None:
        arguments = {key: value for key, value in data.items() if key not in TOOL_CALL_META_KEYS}
    else:
        arguments = normalize_arguments(raw)
    if arguments is None:
        return None
    for key in ("timeout", "max_results", "max_bytes", "max_matches"):
        if key in data and key not in arguments:
            arguments[key] = data[key]
    return arguments


def normalize_arguments(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return {} if value is None else None


def normalize_subagent_specs(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    raw_agents = arguments.get("agents", arguments.get("subagents", arguments.get("tasks")))
    if isinstance(raw_agents, list):
        specs: list[dict[str, Any]] = []
        for index, item in enumerate(raw_agents, start=1):
            if isinstance(item, dict):
                spec = dict(item)
            else:
                spec = {"task": str(item)}
            if "name" not in spec:
                spec["name"] = f"subagent-{index}"
            specs.append(spec)
        return specs[:8]
    return [dict(arguments)] if arguments.get("task") else []


def normalize_subagent_mode_and_thinking(
    mode_value: Any,
    think_value: Any,
    *,
    parent_mode: str,
    parent_thinking: str,
) -> tuple[str, str]:
    valid_modes = {"plan", "review", "agent", "trusted", "yolo", "root"}
    valid_thinking = set(ThinkingMode.names())
    mode = str(mode_value or parent_mode).strip().lower()
    thinking = str(think_value or parent_thinking).strip().lower()

    if mode in valid_thinking and mode not in valid_modes:
        thinking = mode
        mode = parent_mode if parent_mode in valid_modes else "plan"
    elif mode not in valid_modes:
        mode = parent_mode if parent_mode in valid_modes else "plan"

    if thinking not in valid_thinking:
        thinking = parent_thinking if parent_thinking in valid_thinking else "fast"
    return mode, thinking


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
