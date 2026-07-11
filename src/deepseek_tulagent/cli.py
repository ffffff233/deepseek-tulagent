from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .agent import TuLAgent, compact_context_messages, estimate_message_tokens, is_internal_automation_prompt, parse_tool_call
from .config import get_settings, load_file_config, save_file_config
from .messages import Message
from .policy import ApprovalPolicy, ThinkingMode
from .provider import DeepSeekClient
from .session import SessionStore
from .skills import SkillStore
from .tools import ToolRegistry
from .updates import check_for_update, update_to
from .ui import ThinkingSpinner, ask_user_choice, assistant_prefix, choose_palette, composer_prompt, confirm_tool, format_agent_event, install_terminal_safety, plain_terminal, print_box, print_header, print_slash_palette, print_tool_palette, read_composer, startup_animation


BANNER = r"""
DeepSeek TuLAgent
V4 Pro native terminal agent
tools: shell | read | write | patch
"""

MODES = ["plan", "review", "agent", "trusted", "yolo", "root"]
THINKING = ThinkingMode.names()


def main(argv: list[str] | None = None) -> int:
    install_terminal_safety()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        settings = get_settings()
        argv = ["start", "--mode", settings.default_mode, "--think", settings.default_thinking]
    parser = argparse.ArgumentParser(prog="dstul")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_parser = sub.add_parser("run", help="run a one-shot DeepSeek TuLAgent task")
    run_parser.add_argument("prompt")
    run_parser.add_argument("--mode", choices=MODES, default="agent")
    run_parser.add_argument("--think", choices=THINKING, default="balanced")
    run_parser.add_argument("--json", action="store_true", help="print machine-readable result")
    run_parser.add_argument("--stream", action="store_true", default=True, help="stream assistant text (default)")
    run_parser.add_argument("--yes", action="store_true", help="approve every confirmation-gated tool")

    start_parser = sub.add_parser("start", help="start an interactive DeepSeek TuLAgent session")
    start_parser.add_argument("--mode", choices=MODES)
    start_parser.add_argument("--think", choices=THINKING)
    start_parser.add_argument("--yes", action="store_true", help="approve every confirmation-gated tool")
    start_parser.add_argument("--resume", help="resume a previous session id")

    doctor_parser = sub.add_parser("doctor", help="check local configuration")
    doctor_parser.add_argument("--live", action="store_true", help="also call DeepSeek API")

    sub.add_parser("models", help="list live DeepSeek models")
    sub.add_parser("version", help="print DeepSeek TuLAgent version")
    sub.add_parser("desktop", help="start the desktop app")
    update_parser = sub.add_parser("update", help="check for and install the latest tagged version")
    update_parser.add_argument("--check", action="store_true", help="only check; do not install")

    auth_parser = sub.add_parser("config", help="manage default local config")
    auth_sub = auth_parser.add_subparsers(dest="config_cmd", required=True)
    set_parser = auth_sub.add_parser("set", help="save DeepSeek defaults locally")
    set_parser.add_argument("--api-key")
    set_parser.add_argument("--base-url")
    set_parser.add_argument("--model")
    auth_sub.add_parser("show", help="show local config with API key redacted")

    skills_parser = sub.add_parser("skills", help="manage local skill directories")
    skills_sub = skills_parser.add_subparsers(dest="skills_cmd", required=True)
    skills_sub.add_parser("list", help="list discovered skills")
    show_parser = skills_sub.add_parser("show", help="show one skill")
    show_parser.add_argument("name")
    new_parser = skills_sub.add_parser("new", help="create a workspace skill")
    new_parser.add_argument("name")
    new_parser.add_argument("--description", required=True)
    new_parser.add_argument("--body", default="")

    sessions_parser = sub.add_parser("sessions", help="list, show, or resume conversations")
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_cmd", required=True)
    sessions_sub.add_parser("list", help="list conversation sessions")
    session_show = sessions_sub.add_parser("show", help="show a session transcript")
    session_show.add_argument("session_id")
    session_resume = sessions_sub.add_parser("resume", help="resume a session interactively")
    session_resume.add_argument("session_id")
    session_resume.add_argument("--mode", choices=MODES, default="root")
    session_resume.add_argument("--think", choices=THINKING, default="fast")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.cmd == "doctor":
        status = {
            "workspace": str(settings.workspace),
            "base_url": settings.base_url,
            "model": settings.model,
            "api_key": "set" if settings.api_key else "missing",
            "max_tool_rounds": settings.max_tool_rounds,
            "max_tokens": settings.max_tokens,
            "request_timeout": settings.request_timeout,
        }
        if args.live and settings.api_key:
            try:
                status["live"] = DeepSeekClient(settings).ping()
            except Exception as exc:
                status["live"] = {"ok": False, "error": str(exc)}
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if settings.api_key else 2

    if args.cmd == "models":
        models = DeepSeekClient(settings).models()
        for model in models:
            marker = " (current)" if model == settings.model else ""
            print(f"{model}{marker}")
        return 0

    if args.cmd == "version":
        print(__version__)
        return 0

    if args.cmd == "desktop":
        from .desktop.app import main as desktop_main

        desktop_main()
        return 0

    if args.cmd == "update":
        return update_command(check_only=args.check)

    if args.cmd == "config":
        return config_command(args)

    if args.cmd == "skills":
        return skills_command(settings, args)

    if args.cmd == "sessions":
        return sessions_command(settings, args)

    if args.cmd == "run":
        thinking = ThinkingMode.resolve(args.think)
        runtime_settings = settings.with_runtime(
            max_tokens=thinking.max_tokens,
            thinking_enabled=thinking.api_thinking,
            reasoning_effort=thinking.reasoning_effort,
        )

        def delta(text: str) -> None:
            streamed_parts.append(text)
            print(text, end="", flush=True)

        def event(text: str) -> None:
            ThinkingSpinner.clear_active_line()
            print("\n" + format_agent_event(text), file=sys.stderr)

        approver = (lambda _name, _args: True) if args.yes or args.mode in {"yolo", "root"} else None
        if thinking.name == "auto":
            thinking = choose_auto_thinking(runtime_settings, args.prompt)
            runtime_settings = runtime_settings.with_runtime(
                max_tokens=thinking.max_tokens,
                thinking_enabled=thinking.api_thinking,
                reasoning_effort=thinking.reasoning_effort,
            )
        streamed_parts: list[str] = []
        should_stream = bool(args.stream and not args.json)
        if should_stream:
            with ThinkingSpinner(f"thinking:{thinking.name}") as spinner:
                raw_delta = delta

                def streaming_delta(text: str) -> None:
                    spinner.stop()
                    raw_delta(text)

                result = TuLAgent(runtime_settings, mode=args.mode, thinking=thinking.name, approve=approver, ask_user=ask_user_choice).run(
                    args.prompt,
                    stream=True,
                    on_delta=streaming_delta,
                    on_event=event,
                )
        else:
            with ThinkingSpinner(f"thinking:{thinking.name}"):
                result = TuLAgent(runtime_settings, mode=args.mode, thinking=thinking.name, approve=approver, ask_user=ask_user_choice).run(
                    args.prompt,
                    stream=False,
                    on_event=event if not args.json else None,
                )
        if args.json:
            print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
        else:
            if not should_stream or (should_stream and not streamed_parts):
                print(result.answer)
            print(f"\n[session] {result.session_id}", file=sys.stderr)
        return 0

    if args.cmd == "start":
        return interactive(
            settings,
            args.mode or settings.default_mode,
            args.think or settings.default_thinking,
            args.yes,
            args.resume,
        )

    return 1


def update_command(check_only: bool = False) -> int:
    try:
        info = check_for_update(__version__, timeout=5.0)
    except Exception as exc:
        print(f"update check failed: {exc}", file=sys.stderr)
        return 2
    if not info:
        print(f"deepseekTul is up to date: {__version__}")
        return 0
    print(f"update available: {info.current} -> {info.latest}")
    print(info.url)
    if check_only:
        return 0
    ok, output = update_to(info.latest)
    print(output)
    return 0 if ok else 2


def interactive(settings, mode: str, thinking_name: str, yes: bool, resume: str | None = None) -> int:
    thinking = ThinkingMode.resolve(thinking_name)
    settings = settings.with_runtime(
        max_tokens=thinking.max_tokens,
        thinking_enabled=thinking.api_thinking,
        reasoning_effort=thinking.reasoning_effort,
    )
    startup_animation(enabled=resume is None)
    approval_text = "all yes" if yes or mode in {"yolo", "root"} else "manual yes for gated tools"
    if resume:
        sep = " | " if plain_terminal() else " · "
        print(f"DeepSeek TuLAgent{sep}{settings.model}{sep}{mode}/{thinking.name}{sep}{settings.workspace}")
    else:
        print_header(str(settings.workspace), settings.base_url, settings.model, mode, thinking.name, approval_text)
    print(f"limits   : {settings.max_tool_rounds} tool rounds, {settings.max_tokens} max tokens, {settings.request_timeout:g}s timeout")
    print(f"app      : DeepSeek TuLAgent {__version__}")
    toolkit = ToolRegistry(settings.workspace)
    print(f"toolkit  : {len(toolkit.names)} tools loaded; type / to inspect")
    session = None
    if resume:
        try:
            session = SessionStore(settings.workspace).load(resume)
            session.messages.append(Message(role="user", content="Resume note: preserve this conversation. If older tool history shows a background shell command timed out, do not assume the service failed; verify with service_status, ss, or curl. Prefer start_service for new background processes."))
            sep = " | " if plain_terminal() else " · "
            print(f"resumed  : {session.session_id[:8]}{sep}{len(session.messages)} messages")
            print_recent_session_messages(session)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if not settings.api_key:
        print("api key  : missing DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    try:
        live = DeepSeekClient(settings).ping()
        available = "yes" if live["model_available"] else "no"
        print(f"live     : ok, model available: {available}")
    except Exception as exc:
        print(f"live     : failed: {exc}", file=sys.stderr)
        return 2
    maybe_prompt_update()
    skills = SkillStore(settings.workspace)
    discovered_skills = skills.list()
    if discovered_skills:
        print("skills   : " + ", ".join(skill.name for skill in discovered_skills))
    else:
        print("skills   : none")
    print("skilldir : " + str(skills.writable_dir))
    print("commands : type / for command palette")
    print()

    current_mode = mode
    active_goal: str | None = None
    last_session_id = session.session_id if session else None
    last_submitted_prompt = ""
    last_submitted_at = 0.0
    while True:
        try:
            prompt = read_composer(
                composer_prompt(settings.model, current_mode, thinking.name, last_session_id),
                slash_items=slash_items(settings),
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            if last_session_id:
                print_session_handoff(last_session_id)
            return 0
        if not prompt:
            continue
        now = time.monotonic()
        if prompt == last_submitted_prompt and now - last_submitted_at < 1.0:
            print("input    : duplicate ignored")
            continue
        last_submitted_prompt = prompt
        last_submitted_at = now
        if prompt in {"/exit", "/quit"}:
            if last_session_id:
                print_session_handoff(last_session_id)
            return 0
        if prompt in {"/cancel", "/stop"}:
            active_goal = None
            print(f"cancel   : back to normal input; mode={current_mode}, think={thinking.name}")
            continue
        if prompt == "/":
            print()
            print_palette(settings)
            print()
            continue
        if prompt == "/goal":
            print(f"goal     : {active_goal or 'none'}")
            continue
        if prompt.startswith("/goal "):
            requested_goal = prompt.split(maxsplit=1)[1].strip()
            if requested_goal in {"clear", "off", "none"}:
                active_goal = None
                print("goal     : cleared")
            else:
                active_goal = requested_goal
                print(f"goal     : {active_goal}")
            continue
        if prompt.startswith("/mode "):
            requested = prompt.split(maxsplit=1)[1].strip()
            if requested not in set(MODES):
                print("mode must be one of: " + ", ".join(MODES))
                continue
            current_mode = requested
            persist_default("default_mode", current_mode)
            print_header(str(settings.workspace), settings.base_url, settings.model, current_mode, thinking.name, "all yes" if yes or current_mode in {"yolo", "root"} else "manual yes for gated tools")
            print(f"mode set to {current_mode}")
            continue
        if prompt.startswith("/think "):
            requested = prompt.split(maxsplit=1)[1].strip()
            if requested not in set(THINKING):
                print("thinking must be one of: " + ", ".join(THINKING))
                continue
            thinking = ThinkingMode.resolve(requested)
            settings = settings.with_runtime(
                max_tokens=thinking.max_tokens,
                thinking_enabled=thinking.api_thinking,
                reasoning_effort=thinking.reasoning_effort,
            )
            persist_default("default_thinking", thinking.name)
            print_header(str(settings.workspace), settings.base_url, settings.model, current_mode, thinking.name, "all yes" if yes or current_mode in {"yolo", "root"} else "manual yes for gated tools")
            print(f"thinking set to {thinking.name}; model={settings.model}; max_tokens={settings.max_tokens}; api_thinking={settings.thinking_enabled}; reasoning_effort={settings.reasoning_effort}; internal_passes={thinking.deliberation_passes}")
            continue
        if prompt == "/think":
            rows = [(name, thinking_description(name)) for name in THINKING]
            selected_thinking = choose_palette(rows, title="thinking")
            if not selected_thinking:
                print("thinking unchanged")
                continue
            thinking = ThinkingMode.resolve(selected_thinking)
            settings = settings.with_runtime(
                max_tokens=thinking.max_tokens,
                thinking_enabled=thinking.api_thinking,
                reasoning_effort=thinking.reasoning_effort,
            )
            persist_default("default_thinking", thinking.name)
            print(f"thinking set to {thinking.name}; model={settings.model}; max_tokens={settings.max_tokens}; api_thinking={settings.thinking_enabled}; reasoning_effort={settings.reasoning_effort}; internal_passes={thinking.deliberation_passes}")
            continue
        if prompt == "/models":
            for model in DeepSeekClient(settings).models():
                marker = " (current)" if model == settings.model else ""
                print(f"{model}{marker}")
            continue
        if prompt == "/model":
            models = DeepSeekClient(settings).models()
            rows = [(model, "current" if model == settings.model else "available") for model in models]
            selected_model = choose_palette(rows, title="models")
            if not selected_model:
                print("model unchanged")
                continue
            settings = settings.with_runtime(model=selected_model)
            persist_default("model", settings.model)
            print(f"model set to {settings.model}")
            continue
        if prompt == "/skills":
            discovered = SkillStore(settings.workspace).list()
            if not discovered:
                print("no skills discovered")
            for skill in discovered:
                print(skill.summary())
            continue
        if prompt == "/subagents":
            print_box("Subagents", [
                "delegate_agent(name, task, mode?, thinking?/think?, max_rounds?)",
                "delegate_agent(agents=[{name, task, mode?, thinking?/think?, max_rounds?}, ...])",
                "mode controls permissions; thinking controls reasoning effort; omitted values inherit parent",
                "isolated context; best for research, review, verification, and multi-branch decomposition",
            ])
            continue
        if prompt == "/compact":
            if not session or len(session.messages) <= 2:
                print("compact: no conversation context yet")
                continue
            before = estimate_message_tokens(session.messages)
            session.messages = compact_context_messages(session.messages, settings.model, force=True)
            session.rewrite()
            after = estimate_message_tokens(session.messages)
            print(f"compact: {before} -> {after} est tokens; recent messages kept exact")
            continue
        if prompt.startswith("/skill "):
            name = prompt.split(maxsplit=1)[1].strip()
            skill = SkillStore(settings.workspace).get(name)
            if not skill:
                print(f"skill not found: {name}")
            else:
                print(skill.path)
                print(skill.body)
            continue
        if prompt == "/doctor":
            print(json.dumps(DeepSeekClient(settings).ping(), ensure_ascii=False, indent=2))
            continue
        if prompt.startswith("/tool "):
            tool_text = prompt.split(maxsplit=1)[1]
            direct_tool = parse_tool_call(tool_text)
            if not direct_tool:
                print("tool: could not parse tool JSON")
                continue
            name, arguments = direct_tool
            try:
                result = ToolRegistry(settings.workspace, policy=ApprovalPolicy.from_mode(current_mode)).run(name, arguments)
                print(f"tool {name}: {'ok' if result.ok else 'failed'}")
                if result.output:
                    print(result.output[:4000])
            except Exception as exc:
                print(f"tool {name}: error: {exc}")
            continue

        def event(text: str) -> None:
            ThinkingSpinner.clear_active_line()
            print(format_agent_event(text), flush=True)

        def delta(text: str) -> None:
            print(text, end="", flush=True)

        approver = (lambda _name, _args: True) if yes or current_mode in {"yolo", "root"} else confirm_tool
        run_thinking = thinking
        run_settings = settings
        if thinking.name == "auto":
            run_thinking = choose_auto_thinking(settings, prompt)
            run_settings = settings.with_runtime(
                max_tokens=run_thinking.max_tokens,
                thinking_enabled=run_thinking.api_thinking,
                reasoning_effort=run_thinking.reasoning_effort,
            )
            print(f"auto think -> {run_thinking.name}; model={run_settings.model}; max_tokens={run_settings.max_tokens}")
        try:
            with ThinkingSpinner(f"thinking:{run_thinking.name}") as spinner:
                raw_delta = delta

                def streaming_delta(text: str) -> None:
                    spinner.stop()
                    raw_delta(text)

                result = TuLAgent(run_settings, mode=current_mode, thinking=run_thinking.name, approve=approver, ask_user=ask_user_choice).run(
                    prompt,
                    stream=True,
                    on_delta=streaming_delta,
                    on_event=event,
                    session=session,
                    goal=active_goal,
                )
        except KeyboardInterrupt:
            print("\ninterrupted")
            continue
        except Exception as exc:
            print(f"error: {exc}")
            continue
        if result.answer:
            print()
        if session is None:
            session = SessionStore(settings.workspace).load(result.session_id)
        last_session_id = result.session_id
    print()


def maybe_prompt_update() -> None:
    if os.getenv("DSTUL_NO_UPDATE_CHECK"):
        print(f"version  : {__version__} (update check disabled)")
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(f"version  : {__version__}")
        return
    try:
        info = check_for_update(__version__, timeout=1.5)
    except Exception:
        print(f"version  : {__version__} (update check failed)")
        return
    if not info:
        print(f"version  : {__version__} (latest)")
        return
    print(f"version  : {__version__}; update available: {info.latest}")
    choice = choose_palette(
        [
            ("update", f"install v{info.latest}; config, API key, model and skills stay untouched"),
            ("skip", "do not update now"),
        ],
        title=f"update {info.current} -> {info.latest}",
    )
    if choice != "update":
        print("update   : skipped")
        return
    ok, output = update_to(info.latest)
    print(("update   : done" if ok else "update   : failed") + f"\n{output}")


def interactive_tui(settings, mode: str, thinking: ThinkingMode, yes: bool, session) -> int:
    try:
        from .tui import ChatTui, TuiState
    except Exception as exc:
        print(f"tui      : unavailable on this platform ({exc}); using line mode")
        return interactive(settings, mode, thinking.name, yes, session.session_id if session else None)

    state = TuiState(model=settings.model, mode=mode, thinking=thinking.name, session_id=session.session_id if session else None)
    if session:
        for message in session.messages[-12:]:
            if message.role in {"user", "assistant", "tool"}:
                state.messages.append((message.role, message.content[:2000]))

    current = {"mode": mode, "thinking": thinking, "settings": settings, "session": session}

    def on_command(command: str, tui_state: TuiState) -> bool:
        if command == "/":
            skills = SkillStore(current["settings"].workspace).list()
            body = "/exit /mode <name> /think <name> /models /doctor /skills"
            if skills:
                body += "\n" + "\n".join(f"/skill {skill.name} - {skill.description}" for skill in skills)
            tui_state.messages.append(("system", body))
            return False
        if command.startswith("/mode "):
            requested = command.split(maxsplit=1)[1].strip()
            if requested in set(MODES):
                current["mode"] = requested
                tui_state.mode = requested
                tui_state.status = "mode changed"
            return False
        if command.startswith("/think "):
            requested = command.split(maxsplit=1)[1].strip()
            if requested in set(THINKING):
                resolved = ThinkingMode.resolve(requested)
                current["thinking"] = resolved
                current["settings"] = current["settings"].with_runtime(
                    max_tokens=resolved.max_tokens,
                    thinking_enabled=resolved.api_thinking,
                    reasoning_effort=resolved.reasoning_effort,
                )
                tui_state.thinking = resolved.name
                tui_state.model = current["settings"].model
                tui_state.status = "thinking changed"
            return False
        if command == "/exit" or command == "/quit":
            return True
        tui_state.messages.append(("system", f"unknown command: {command}"))
        return False

    def on_submit(text: str, tui_state: TuiState) -> None:
        tui_state.messages.append(("user", text))
        tui_state.status = "thinking"

        def collect(delta: str) -> None:
            if not tui_state.messages or tui_state.messages[-1][0] != "assistant":
                tui_state.messages.append(("assistant", ""))
            role, content = tui_state.messages[-1]
            tui_state.messages[-1] = (role, content + delta)

        approver = (lambda _name, _args: True) if yes or current["mode"] in {"yolo", "root"} else confirm_tool
        result = TuLAgent(current["settings"], mode=current["mode"], thinking=current["thinking"].name, approve=approver, ask_user=ask_user_choice).run(
            text,
            stream=True,
            on_delta=collect,
            session=current["session"],
        )
        if current["session"] is None:
            current["session"] = SessionStore(current["settings"].workspace).load(result.session_id)
        tui_state.session_id = result.session_id
        tui_state.status = "ready"

    try:
        ChatTui(state, on_submit, on_command).run()
    finally:
        if state.session_id:
            print_session_handoff(state.session_id)
    return 0


def skills_command(settings, args) -> int:
    store = SkillStore(settings.workspace)
    if args.skills_cmd == "list":
        for skill in store.list():
            print(f"{skill.name}\t{skill.description}\t{skill.path}")
        return 0
    if args.skills_cmd == "show":
        skill = store.get(args.name)
        if not skill:
            print(f"skill not found: {args.name}", file=sys.stderr)
            return 1
        print(skill.path)
        print()
        print(skill.body)
        return 0
    if args.skills_cmd == "new":
        skill = store.create(args.name, args.description, args.body)
        print(f"created {skill.name}: {skill.path}")
        return 0
    return 1


def sessions_command(settings, args) -> int:
    store = SessionStore(settings.workspace)
    if args.sessions_cmd == "list":
        rows = store.list()
        if not rows:
            print("no sessions")
            return 0
        for row in rows:
            print(f"{row['session_id']}\t{row['messages']} messages\t{row['title']}\t{row['path']}")
        return 0
    if args.sessions_cmd == "show":
        session = store.load(args.session_id)
        for message in session.messages:
            name = f":{message.name}" if message.name else ""
            print(f"[{message.role}{name}]")
            print(message.content)
            print()
        return 0
    if args.sessions_cmd == "resume":
        return interactive(settings, args.mode, args.think, yes=args.mode in {"yolo", "root"}, resume=args.session_id)
    return 1


def config_command(args) -> int:
    data = load_file_config()
    if args.config_cmd == "show":
        redacted = dict(data)
        if redacted.get("api_key"):
            redacted["api_key"] = "set"
        print(json.dumps(redacted, ensure_ascii=False, indent=2))
        return 0
    if args.config_cmd == "set":
        if args.api_key:
            data["api_key"] = args.api_key
        if args.base_url:
            data["base_url"] = args.base_url.rstrip("/")
        if args.model:
            data["model"] = args.model
        path = save_file_config(data)
        print(f"saved {path}")
        return 0
    return 1


def persist_default(key: str, value: str) -> None:
    data = load_file_config()
    data[key] = value
    save_file_config(data)


def print_palette(settings) -> None:
    commands = [
        ("/exit", "leave the session"),
        ("/mode <name>", "switch permission mode"),
        ("/think <name>", "switch thinking mode"),
        ("/models", "list live DeepSeek models"),
        ("/doctor", "check live DeepSeek config"),
        ("/skills", "list discovered skills"),
        ("/compact", "compress older conversation context now"),
        ("/goal <text>", "set persistent objective; continue until complete or blocked"),
        ("/subagents", "show subagent delegation capability"),
        ("/skill <name>", "show a skill body"),
        ("/tool <json>", "execute a tool JSON object directly"),
    ]
    skill_rows = [(skill.name, skill.description) for skill in SkillStore(settings.workspace).list()]
    print_slash_palette(commands, skill_rows)
    tools = ToolRegistry(settings.workspace).describe()
    tools["delegate_agent"] = "virtual: run isolated subagent and return summary"
    print_tool_palette(tools)


def slash_items(settings) -> list[tuple[str, str]]:
    items = [
        ("/model", "choose model / show live DeepSeek models"),
        ("/think", "choose thinking depth"),
        ("/mode root", "highest permission, all tools approved"),
        ("/mode agent", "agent mode with manual gated approvals"),
        ("/mode plan", "read-only planning mode"),
        ("/doctor", "check live DeepSeek config"),
        ("/skills", "list discovered skills"),
        ("/compact", "compress older conversation context now"),
        ("/goal ", "set active goal"),
        ("/goal", "show active goal"),
        ("/goal clear", "clear active goal"),
        ("/exit", "leave and print resume command"),
    ]
    for name in THINKING:
        items.append((f"/think {name}", thinking_description(name)))
    for skill in SkillStore(settings.workspace).list():
        items.append((f"/skill {skill.name}", skill.description))
    return items


def thinking_description(name: str) -> str:
    mode = ThinkingMode.resolve(name)
    passes = f"{mode.deliberation_passes} internal pass" + ("" if mode.deliberation_passes == 1 else "es")
    return f"{mode.model_hint}, {mode.max_tokens} max tokens, {passes}"


def choose_auto_thinking(settings, prompt: str) -> ThinkingMode:
    candidates = ["instant", "fast", "standard", "balanced", "careful", "deep", "deeper", "max", "ultra"]
    selector_settings = settings.with_runtime(
        model="deepseek-v4-flash",
        max_tokens=512,
        thinking_enabled=False,
        reasoning_effort=None,
    )
    try:
        choice = DeepSeekClient(selector_settings).chat(
            [
                Message("system", "Choose one thinking mode for the user's task. Return only one mode name, no punctuation."),
                Message("user", "Modes: " + ", ".join(candidates) + "\nTask:\n" + prompt[:8000]),
            ]
        ).strip().lower()
    except Exception:
        return ThinkingMode.resolve("balanced")
    for name in candidates:
        if name in choice.split() or choice == name:
            return ThinkingMode.resolve(name)
    return ThinkingMode.resolve("balanced")


def print_session_handoff(session_id: str) -> None:
    print(f"\n[session] {session_id}", file=sys.stderr)
    print(f"[resume] deepseekTul start --resume {session_id}", file=sys.stderr)


def print_recent_session_messages(session, limit: int = 3) -> None:
    visible = [
        message for message in session.messages
        if message.role in {"user", "assistant"} and is_human_visible_history(message.content)
    ][-limit:]
    if not visible:
        print("recent   : none")
        return
    print("recent   :")
    for message in visible:
        text = compact_history_text(message.content)
        role = "you" if message.role == "user" else "assistant"
        print(f"  {role:<9} {text}")


def is_human_visible_history(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("Tool result from ") or stripped.startswith("TOOL_RESULT ") or stripped.startswith("SUBAGENT_RESULT "):
        return False
    if is_internal_automation_prompt(stripped):
        return False
    if stripped.startswith('{"tool"') or stripped.startswith("```json") or parse_tool_call(stripped):
        return False
    return True


def compact_history_text(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) > 72:
        return cleaned[:69] + "..."
    return cleaned


if __name__ == "__main__":
    raise SystemExit(main())
