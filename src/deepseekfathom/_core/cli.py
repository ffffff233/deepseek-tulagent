from __future__ import annotations

import argparse
import atexit
import base64
import binascii
import json
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .agent import FathomAgent, compact_context_messages, estimate_message_tokens, is_internal_automation_prompt, parse_tool_call
from .config import get_settings, load_file_config, save_file_config
from .extensions import ExtensionRuntime, UserMCPServerNotFoundError, get_user_mcp_server, save_user_mcp_server
from .hooks import SESSION_END, set_hook_enabled, trust_project
from .mcp import MCPError, MCPHost
from .messages import Message
from .native_plugins import enabled_native_commands, is_native_plugin, native_plugin_entries, resolve_native_command, set_native_plugin_enabled
from .policy import ApprovalPolicy, ThinkingMode
from .plugins import install_local_plugin, set_plugin_enabled
from .provider import DeepSeekClient
from .session import SessionStore
from .skills import SkillStore
from .tool_contracts import ToolContract, normalize_tool_schema
from .tools import ToolRegistry, ToolResult
from .updates import check_for_update, schedule_legacy_distribution_cleanup, update_to
from .ui import ThinkingSpinner, ask_user_choice, assistant_prefix, choose_palette, composer_status, confirm_tool, format_agent_event, install_terminal_safety, plain_terminal, print_box, print_header, print_slash_palette, print_tool_palette, read_composer, startup_animation


BANNER = r"""
DeepSeekFathom
V4 Pro native terminal agent
tools: shell | read | write | patch
"""

MODES = ["plan", "review", "agent", "trusted", "yolo", "root"]
THINKING = ThinkingMode.user_selectable_names()
THINKING_HELP = "thinking depth (max is the highest): " + ", ".join(THINKING)
MANDATORY_CONFIRM_TOOLS = frozenset({"configure_mcp_server"})


class CliExtensionRuntime:
    def __init__(self, workspace: Path):
        self.closed = False
        self.pending_refresh = False
        self.last_session_id: str | None = None
        self._mcp_connect_thread: threading.Thread | None = None
        self.extensions = ExtensionRuntime(workspace)
        self.host = MCPHost(self.extensions.active_mcp_configs())
        self._atexit_callback = lambda runtime_ref=weakref.ref(self): close_cli_extension_runtime(runtime_ref)
        atexit.register(self._atexit_callback)

    @property
    def workspace(self) -> Path:
        return self.extensions.workspace

    @property
    def skill_store(self) -> SkillStore:
        return SkillStore(self.workspace, extra_roots=self.extensions.skill_roots)

    def agent_kwargs(self) -> dict[str, Any]:
        return {
            "extra_tool_contracts": [*self._mcp_management_contracts(), *self._mcp_tool_contracts()],
            "extra_skill_roots": self.extensions.skill_roots,
            "extra_instruction_files": self.extensions.instruction_files,
            "hook_runner": self.extensions.new_hook_runner(),
        }

    def diagnostics(self) -> dict[str, Any]:
        report = self.extensions.diagnostics()
        mcp = report.get("mcp") if isinstance(report.get("mcp"), dict) else {}
        live = {str(item.get("name") or ""): item for item in self.host.status()}
        entries = []
        for raw in mcp.get("entries", []) if isinstance(mcp.get("entries"), list) else []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            status = live.get(str(raw.get("name") or ""))
            if status:
                item.update(status)
            elif not item.get("active"):
                item.update({"state": "disabled", "connected": False, "message": "disabled or not trusted"})
            else:
                item.update({"state": "configured", "connected": False, "message": "waiting to connect"})
            entries.append(item)
        report["mcp"] = {**mcp, "live": True, "entries": entries}
        report["nativePlugins"] = native_plugin_entries(self.extensions.home)
        return report

    def refresh(self, *, connect: bool = False) -> dict[str, Any]:
        previous = self.host
        connected_names = [
            str(item.get("name") or "")
            for item in previous.status()
            if item.get("connected") and item.get("name")
        ]
        connecting_all = bool(self._mcp_connect_thread and self._mcp_connect_thread.is_alive())
        report = self.extensions.refresh()
        replacement = MCPHost(self.extensions.active_mcp_configs())
        try:
            if connect:
                replacement.connect_all()
        except BaseException:
            replacement.close()
            raise
        self.host = replacement
        previous.close()
        self._mcp_connect_thread = None
        if not connect and (connecting_all or connected_names):
            self.connect_mcp_background(None if connecting_all else connected_names)
        self.pending_refresh = False
        return report.to_dict()

    def apply_pending_refresh(self) -> None:
        if self.pending_refresh:
            self.refresh()

    def close(self, session_id: str | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        self.last_session_id = session_id or self.last_session_id
        try:
            atexit.unregister(self._atexit_callback)
        except Exception:
            pass
        if self.last_session_id:
            try:
                self.extensions.new_hook_runner().run(SESSION_END, {"sessionId": self.last_session_id})
            except Exception:
                pass
        try:
            self.host.close()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def connect_mcp(self, action: str = "connect_all", name: str = "") -> None:
        if action == "connect_all":
            self.host.connect_all()
        elif action == "connect":
            self.host.connect(name)
        elif action == "disconnect":
            self.host.disconnect(name)
        elif action == "reconnect":
            self.host.reconnect(name)
        else:
            raise ValueError(f"unsupported MCP action: {action}")

    def connect_mcp_background(self, names: list[str] | None = None) -> None:
        if self._mcp_connect_thread is not None and self._mcp_connect_thread.is_alive():
            return
        host = self.host
        requested = tuple(name for name in (names or ()) if name)

        def connect() -> None:
            if not requested:
                try:
                    host.connect_all()
                except Exception:
                    pass
                return
            for name in requested:
                try:
                    host.connect(name)
                except Exception:
                    continue

        thread = threading.Thread(target=connect, name="cli-mcp-connect", daemon=True)
        self._mcp_connect_thread = thread
        thread.start()

    def set_plugin(self, name: str, enabled: bool) -> None:
        if is_native_plugin(name):
            set_native_plugin_enabled(name, enabled, self.extensions.home)
            return
        package = next(
            (item for item in self.extensions.report.plugins if item.installed.name == name),
            None,
        )
        if package is None:
            raise ValueError(f"plugin not found: {name}")
        if package.scope == "project":
            if enabled:
                install_local_plugin(Path(package.installed.root), self.extensions.home, enabled=True)
                self.refresh()
                return
            raise ValueError("project-discovered plugins are already inactive; disable the installed user plugin instead")
        set_plugin_enabled(name, enabled, self.extensions.home)
        self.refresh()

    def set_hook(self, hook_id: str, enabled: bool) -> None:
        hook = next(
            (item for item in self.extensions.report.hooks.hooks if item.hook_id == hook_id),
            None,
        )
        if hook is None:
            raise ValueError(f"hook not found: {hook_id}")
        if hook.scope == "plugin":
            raise ValueError("plugin hooks follow their plugin state and cannot be changed individually")
        if hook.scope not in {"project", "global"} or hook.source is None:
            raise ValueError("this hook is extension-managed and cannot be changed individually")
        if hook.scope == "project" and enabled and not self.extensions.report.hooks.project_trusted:
            raise ValueError("trust this workspace's hooks before enabling a project hook")
        set_hook_enabled(
            hook.source,
            hook.event,
            hook.match,
            enabled,
            self.workspace,
            self.extensions.home,
            hook_id=hook.hook_id,
        )
        self.refresh()

    def _mcp_tool_contracts(self) -> list[ToolContract]:
        definitions = self.host.tool_definitions()
        specs = {spec.name: spec for spec in self.extensions.mcp_specs}
        contracts: list[ToolContract] = []
        schema_budget = 512_000
        for definition in definitions:
            name = str(definition.get("name") or "")
            origin = definition.get("origin") if isinstance(definition.get("origin"), dict) else {}
            server = str(origin.get("server") or "")
            raw_name = str(origin.get("tool") or "")
            spec = specs.get(server)
            trusted = bool(spec and (
                raw_name in spec.trusted_read_only_tools
                or name in spec.trusted_read_only_tools
            ))
            schema = normalize_tool_schema(definition.get("schema"))
            schema_bytes = len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if not name or schema_bytes > schema_budget or len(contracts) >= 128:
                continue
            schema_budget -= schema_bytes
            contracts.append(ToolContract(
                name=name,
                description=str(definition.get("description") or name)[:1000],
                schema=schema,
                handler=lambda arguments, _host=self.host, _name=name: cli_mcp_result_to_tool_result(
                    _host.call_tool(_name, arguments)
                ),
                origin=f"mcp:{server}",
                read_only=bool(definition.get("read_only")),
                trusted_read_only=trusted,
            ))
        return contracts

    def _mcp_management_contracts(self) -> list[ToolContract]:
        return [
            ToolContract(
                name="list_mcp_servers",
                description="List configured MCP servers and connection state without exposing secrets.",
                schema={"type": "object", "properties": {}, "additionalProperties": False},
                handler=self._list_mcp_servers_tool,
                origin="native:mcp-manager",
                read_only=True,
                trusted_read_only=True,
            ),
            ToolContract(
                name="configure_mcp_server",
                description="Persist one user-owned MCP server after an explicit user request.",
                schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "transport": {"type": "string", "enum": ["http", "stdio"]},
                        "url": {"type": "string"},
                        "command": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}, "maxItems": 256},
                        "env": {"type": "object", "additionalProperties": {"type": "string"}},
                        "cwd": {"type": "string"},
                        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                        "enabled": {"type": "boolean"},
                        "originalName": {"type": "string"},
                        "replace": {"type": "boolean"},
                    },
                    "required": ["name", "transport"],
                    "additionalProperties": False,
                },
                handler=self._configure_mcp_server_tool,
                origin="native:mcp-manager",
                always_confirm=True,
            ),
        ]

    def _list_mcp_servers_tool(self, _arguments: dict[str, Any]) -> ToolResult:
        entries = self.diagnostics().get("mcp", {}).get("entries", [])
        safe = [{
            "name": str(item.get("name") or ""),
            "transport": str(item.get("transport") or ""),
            "connected": bool(item.get("connected")),
            "state": str(item.get("state") or ""),
            "sourceScope": str(item.get("sourceScope") or ""),
            "urlHost": str(item.get("urlHost") or ""),
            "command": str(item.get("command") or ""),
            "headerKeys": list(item.get("headerKeys") or []),
            "envKeys": list(item.get("envKeys") or []),
        } for item in entries if isinstance(item, dict)]
        return ToolResult(True, json.dumps({"servers": safe}, ensure_ascii=False))

    def _configure_mcp_server_tool(self, arguments: dict[str, Any]) -> ToolResult:
        allowed = {"name", "transport", "url", "command", "args", "env", "cwd", "headers", "enabled"}
        server = {key: arguments[key] for key in allowed if key in arguments}
        name = str(server.get("name") or "").strip()
        original_name = str(arguments.get("originalName") or "").strip() or None
        replace_existing = bool(arguments.get("replace"))
        try:
            get_user_mcp_server(name, self.extensions.home)
        except UserMCPServerNotFoundError:
            exists = False
        else:
            exists = True
        if exists and original_name is None and not replace_existing:
            raise ValueError(f'MCP server "{name}" exists; provide originalName or replace=true')
        if exists and replace_existing and original_name is None:
            original_name = name
        saved = save_user_mcp_server(server, self.extensions.home, original_name=original_name)
        self.pending_refresh = True
        return ToolResult(True, json.dumps({
            "ok": True,
            "name": saved["name"],
            "transport": saved["transport"],
            "headerKeys": list(saved.get("headerKeys") or []),
            "message": "Saved. Run /mcp to connect the updated MCP runtime.",
        }, ensure_ascii=False))


def close_cli_extension_runtime(runtime_ref: "weakref.ReferenceType[CliExtensionRuntime]") -> None:
    runtime = runtime_ref()
    if runtime is not None:
        runtime.close()


def confirm_mcp_configuration(arguments: dict[str, Any]) -> bool:
    print("\nMCP configuration requires explicit confirmation:")
    print(f"  name: {str(arguments.get('name') or '').strip()}")
    print(f"  transport: {str(arguments.get('transport') or '').strip()}")
    url = str(arguments.get("url") or "").strip()
    if url:
        try:
            parsed = urlsplit(url)
            endpoint = parsed.hostname or "configured endpoint"
            if parsed.port:
                endpoint += f":{parsed.port}"
        except ValueError:
            endpoint = "configured endpoint"
        print(f"  endpoint: {endpoint}")
    command = str(arguments.get("command") or "").strip()
    if command:
        print(f"  command: {Path(command).name or 'configured command'}")
    raw_args = arguments.get("args")
    if isinstance(raw_args, list):
        print(f"  arguments: {len(raw_args)} item(s)")
    for field, label in (("headers", "header keys"), ("env", "environment keys")):
        values = arguments.get(field)
        if isinstance(values, dict) and values:
            print(f"  {label}: {', '.join(sorted(str(key) for key in values))}")
    answer = input("type yes to approve> ").strip().lower()
    return answer == "yes"


def cli_approver(*, auto_approve: bool, fallback=None, allow_mandatory_prompt: bool = True):
    if not auto_approve and fallback is None:
        return fallback

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        if name in MANDATORY_CONFIRM_TOOLS:
            if not allow_mandatory_prompt:
                return False
            return confirm_mcp_configuration(arguments)
        if auto_approve:
            return True
        return bool(fallback and fallback(name, arguments))

    return approve


def cli_mcp_result_to_tool_result(result: dict[str, Any]) -> ToolResult:
    content = result.get("content", []) if isinstance(result, dict) else []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        content = []
    text_parts: list[str] = []
    images: list[str] = []
    image_bytes = 0
    allowed_images = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    for block in content[:100]:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif kind == "resource" and isinstance(block.get("resource"), dict) and isinstance(block["resource"].get("text"), str):
            text_parts.append(block["resource"]["text"])
        elif kind == "image" and len(images) < 4:
            mime = str(block.get("mimeType") or block.get("mime_type") or "").lower()
            data = block.get("data")
            if mime not in allowed_images or not isinstance(data, str):
                continue
            try:
                size = len(base64.b64decode(data, validate=True))
            except (binascii.Error, ValueError):
                continue
            if size > 8 * 1024 * 1024 or image_bytes + size > 20 * 1024 * 1024:
                continue
            image_bytes += size
            images.append(f"data:{mime};base64,{data}")
        else:
            try:
                text_parts.append(json.dumps(block, ensure_ascii=False))
            except (TypeError, ValueError):
                continue
    output = "\n".join(part for part in text_parts if part).strip()
    if not output:
        output = "MCP tool completed." if not result.get("isError") else "MCP tool failed."
    return ToolResult(not bool(result.get("isError")), output[:100_000], images=images)


def main(argv: list[str] | None = None) -> int:
    schedule_legacy_distribution_cleanup()
    install_terminal_safety()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        settings = get_settings()
        default_thinking = settings.default_thinking if settings.default_thinking in THINKING else "fast"
        argv = ["start", "--mode", settings.default_mode, "--think", default_thinking]
    parser = argparse.ArgumentParser(prog="deepseekfathom")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_parser = sub.add_parser("run", help="run a one-shot DeepSeekFathom task")
    run_parser.add_argument("prompt")
    run_parser.add_argument("--mode", choices=MODES, default="agent")
    run_parser.add_argument("--think", choices=THINKING, default="balanced", help=THINKING_HELP)
    run_parser.add_argument("--json", action="store_true", help="print machine-readable result")
    run_parser.add_argument("--stream", action="store_true", default=True, help="stream assistant text (default)")
    run_parser.add_argument("--yes", action="store_true", help="approve every confirmation-gated tool")

    start_parser = sub.add_parser("start", help="start an interactive DeepSeekFathom session")
    start_parser.add_argument("--mode", choices=MODES)
    start_parser.add_argument("--think", choices=THINKING, help=THINKING_HELP)
    start_parser.add_argument("--yes", action="store_true", help="approve every confirmation-gated tool")
    start_parser.add_argument("--resume", help="resume a previous session id")

    doctor_parser = sub.add_parser("doctor", help="check local configuration")
    doctor_parser.add_argument("--live", action="store_true", help="also call DeepSeek API")

    sub.add_parser("models", help="list live DeepSeek models")
    sub.add_parser("version", help="print DeepSeekFathom version")
    sub.add_parser("desktop", help="start the desktop app")
    extensions_parser = sub.add_parser("extensions", help="show MCP, plugins, hooks, and skills")
    extensions_parser.add_argument("--json", action="store_true", help="print machine-readable details")
    mcp_parser = sub.add_parser("mcp", help="inspect or control configured MCP servers")
    mcp_parser.add_argument("action", nargs="?", choices=["list", "trust", "connect", "connect-all", "disconnect", "reconnect", "reload"], default="connect-all")
    mcp_parser.add_argument("name", nargs="?")
    plugins_parser = sub.add_parser("plugins", help="inspect or control official and user plugins")
    plugins_parser.add_argument("action", nargs="?", choices=["list", "enable", "disable", "reload"], default="list")
    plugins_parser.add_argument("name", nargs="?")
    hooks_parser = sub.add_parser("hooks", help="inspect hooks or trust this workspace")
    hooks_parser.add_argument("action", nargs="?", choices=["list", "trust", "enable", "disable", "reload"], default="list")
    hooks_parser.add_argument("name", nargs="?")
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
    session_resume.add_argument("--think", choices=THINKING, help=THINKING_HELP)

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

    if args.cmd in {"extensions", "mcp", "plugins", "hooks"}:
        return extensions_command(settings, args)

    if args.cmd == "update":
        return update_command(check_only=args.check)

    if args.cmd == "config":
        return config_command(args)

    if args.cmd == "skills":
        return skills_command(settings, args)

    if args.cmd == "sessions":
        return sessions_command(settings, args)

    if args.cmd == "run":
        return run_once(settings, args)

    if args.cmd == "start":
        return interactive(
            settings,
            args.mode or settings.default_mode,
            args.think or settings.default_thinking,
            args.yes,
            args.resume,
        )

    return 1


def run_once(settings, args) -> int:
    thinking = ThinkingMode.resolve(args.think)
    extensions = CliExtensionRuntime(settings.workspace)
    extensions.connect_mcp("connect_all")
    result = None
    run_prompt = args.prompt
    display_prompt = None
    ui_kind = None
    run_mode = args.mode
    native_command = resolve_native_command(run_prompt.split(maxsplit=1)[0], extensions.extensions.home)
    if native_command is not None:
        display_prompt = run_prompt
        _, _, command_details = run_prompt.partition(" ")
        run_prompt = native_command.prompt
        if command_details.strip():
            run_prompt += "\n\nUser request details:\n" + command_details.strip()
        ui_kind = "command"
        run_mode = native_command.mode
        thinking = ThinkingMode.resolve(native_command.thinking)
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

    try:
        approver = cli_approver(
            auto_approve=args.yes or run_mode in {"yolo", "root"},
            allow_mandatory_prompt=bool(not args.json and sys.stdin.isatty()),
        )
        if thinking.name == "auto":
            thinking = choose_auto_thinking(runtime_settings, run_prompt)
            runtime_settings = runtime_settings.with_runtime(
                max_tokens=thinking.max_tokens,
                thinking_enabled=thinking.api_thinking,
                reasoning_effort=thinking.reasoning_effort,
            )
        streamed_parts: list[str] = []
        should_stream = bool(args.stream and not args.json)
        agent_kwargs = extensions.agent_kwargs()
        if should_stream:
            with ThinkingSpinner(f"thinking:{thinking.name}") as spinner:
                raw_delta = delta

                def streaming_delta(text: str) -> None:
                    spinner.stop()
                    raw_delta(text)

                result = FathomAgent(
                    runtime_settings,
                    mode=run_mode,
                    thinking=thinking.name,
                    approve=approver,
                    ask_user=ask_user_choice,
                    **agent_kwargs,
                ).run(
                    run_prompt,
                    stream=True,
                    on_delta=streaming_delta,
                    on_event=event,
                    display_prompt=display_prompt,
                    ui_kind=ui_kind,
                )
        else:
            with ThinkingSpinner(f"thinking:{thinking.name}"):
                result = FathomAgent(
                    runtime_settings,
                    mode=run_mode,
                    thinking=thinking.name,
                    approve=approver,
                    ask_user=ask_user_choice,
                    **agent_kwargs,
                ).run(
                    run_prompt,
                    stream=False,
                    on_event=event if not args.json else None,
                    display_prompt=display_prompt,
                    ui_kind=ui_kind,
                )
        if args.json:
            print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
        else:
            if not should_stream or (should_stream and not streamed_parts):
                print(result.answer)
            print(f"\n[session] {result.session_id}", file=sys.stderr)
        return 0
    finally:
        extensions.close(result.session_id if result is not None else None)


def update_command(check_only: bool = False) -> int:
    try:
        info = check_for_update(__version__, timeout=5.0)
    except Exception as exc:
        print(f"update check failed: {exc}", file=sys.stderr)
        return 2
    if not info:
        print(f"deepseekfathom is up to date: {__version__}")
        return 0
    print(f"update available: {info.current} -> {info.latest}")
    print(info.url)
    if check_only:
        return 0
    ok, output = update_to(info.latest)
    print(output)
    return 0 if ok else 2


def extensions_command(settings, args) -> int:
    runtime = CliExtensionRuntime(settings.workspace)
    try:
        if args.cmd == "extensions":
            report = runtime.diagnostics()
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print_extension_summary(runtime)
            return 0
        if args.cmd == "mcp":
            action = args.action
            if action == "trust":
                trust_project(settings.workspace, runtime.extensions.home, "mcp")
                runtime.refresh(connect=True)
            elif action == "reload":
                runtime.refresh(connect=True)
            elif action != "list":
                if action != "connect-all" and not args.name:
                    print(f"mcp {action} requires a server name", file=sys.stderr)
                    return 2
                runtime.connect_mcp("connect_all" if action == "connect-all" else action, args.name or "")
            print_mcp_status(runtime)
            return 0
        if args.cmd == "plugins":
            if args.action == "reload":
                runtime.refresh()
            elif args.action in {"enable", "disable"}:
                if not args.name:
                    print(f"plugins {args.action} requires a plugin name", file=sys.stderr)
                    return 2
                runtime.set_plugin(args.name, args.action == "enable")
            print_plugin_status(runtime)
            return 0
        if args.cmd == "hooks":
            if args.action == "trust":
                trust_project(settings.workspace, runtime.extensions.home, "hooks")
                runtime.refresh()
            elif args.action in {"enable", "disable"}:
                if not args.name:
                    print(f"hooks {args.action} requires a hook id", file=sys.stderr)
                    return 2
                runtime.set_hook(args.name, args.action == "enable")
            elif args.action == "reload":
                runtime.refresh()
            print_hook_status(runtime)
            return 0
    except (MCPError, OSError, ValueError) as exc:
        print(f"extension error: {exc}", file=sys.stderr)
        return 2
    finally:
        runtime.close()
    return 1


def print_extension_summary(runtime: CliExtensionRuntime) -> None:
    report = runtime.diagnostics()
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    native = report.get("nativePlugins") if isinstance(report.get("nativePlugins"), list) else []
    skills = runtime.skill_store.list()
    print("extensions:")
    print(f"  mcp      {summary.get('activeMcpServers', 0)}/{summary.get('mcpServers', 0)} active")
    print(f"  plugins  {summary.get('enabledPlugins', 0)} user/project, {sum(bool(item.get('enabled')) for item in native if isinstance(item, dict))} official")
    print(f"  hooks    {summary.get('activeHooks', 0)}/{summary.get('hooks', 0)} active")
    print(f"  skills   {len(skills)} discovered")
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    if issues:
        print(f"  issues   {len(issues)}; run `deepseekfathom extensions --json` for details")


def print_mcp_status(runtime: CliExtensionRuntime) -> None:
    report = runtime.diagnostics()
    entries = report.get("mcp", {}).get("entries", []) if isinstance(report.get("mcp"), dict) else []
    if not entries:
        print("mcp: no configured servers")
        return
    print("mcp:")
    for item in entries:
        if not isinstance(item, dict):
            continue
        detail = f"{item.get('name')}  {item.get('state')}  tools={item.get('toolCount', 0)}  scope={item.get('sourceScope', '')}"
        print("  " + detail.rstrip())
        if item.get("lastError"):
            print("    error: " + str(item["lastError"])[:500])


def print_plugin_status(runtime: CliExtensionRuntime) -> None:
    report = runtime.diagnostics()
    entries: list[dict[str, Any]] = []
    native = report.get("nativePlugins") if isinstance(report.get("nativePlugins"), list) else []
    entries.extend(item for item in native if isinstance(item, dict))
    plugins = report.get("plugins") if isinstance(report.get("plugins"), dict) else {}
    entries.extend(item for item in plugins.get("entries", []) if isinstance(item, dict))
    if not entries:
        print("plugins: none")
        return
    print("plugins:")
    for item in entries:
        state = "enabled" if item.get("enabled") and not item.get("error") else "error" if item.get("error") else "disabled"
        print(f"  {item.get('name')}  {state}  scope={item.get('scope', '')}  kind={item.get('manifestKind', '')}")


def print_hook_status(runtime: CliExtensionRuntime) -> None:
    report = runtime.diagnostics()
    hooks = report.get("hooks") if isinstance(report.get("hooks"), dict) else {}
    entries = hooks.get("entries") if isinstance(hooks.get("entries"), list) else []
    if not entries:
        print("hooks: none")
        return
    print(f"hooks: project_trusted={bool(hooks.get('projectTrusted'))}")
    for item in entries:
        if not isinstance(item, dict):
            continue
        state = "enabled" if item.get("enabled") else "disabled"
        print(
            f"  {item.get('id')}  {item.get('event')}  {state}  "
            f"scope={item.get('scope', '')}  match={item.get('match', '*')}"
        )


def handle_extension_prompt(prompt: str, runtime: CliExtensionRuntime, settings) -> bool:
    parts = prompt.split()
    if not parts or parts[0] not in {"/extensions", "/mcp", "/plugins", "/plugin", "/hooks"}:
        return False
    command = parts[0]
    try:
        if command == "/extensions":
            if len(parts) > 1 and parts[1] == "reload":
                runtime.refresh()
            elif len(parts) > 1:
                raise ValueError("usage: /extensions [reload]")
            print_extension_summary(runtime)
            return True
        if command == "/mcp":
            action = parts[1] if len(parts) > 1 else "connect-all"
            name = parts[2] if len(parts) > 2 else ""
            if action == "trust":
                trust_project(settings.workspace, runtime.extensions.home, "mcp")
                runtime.refresh(connect=True)
            elif action == "reload":
                runtime.refresh(connect=True)
            elif action == "connect-all":
                runtime.connect_mcp("connect_all")
            elif action in {"connect", "disconnect", "reconnect"}:
                if not name:
                    raise ValueError(f"/mcp {action} requires a server name")
                runtime.connect_mcp(action, name)
            elif action != "list":
                raise ValueError("usage: /mcp [list|trust|connect-all|connect NAME|disconnect NAME|reconnect NAME|reload]")
            print_mcp_status(runtime)
            return True
        if command in {"/plugins", "/plugin"}:
            if command == "/plugin" and len(parts) >= 3:
                name, action = parts[1], parts[2]
            else:
                action = parts[1] if len(parts) > 1 else "list"
                name = parts[2] if len(parts) > 2 else ""
            if action == "reload":
                runtime.refresh()
            elif action in {"enable", "disable"}:
                if not name:
                    raise ValueError(f"/plugins {action} requires a plugin name")
                runtime.set_plugin(name, action == "enable")
            elif action != "list":
                raise ValueError("usage: /plugins [list|enable NAME|disable NAME|reload]")
            print_plugin_status(runtime)
            return True
        action = parts[1] if len(parts) > 1 else "list"
        name = parts[2] if len(parts) > 2 else ""
        if action == "trust":
            trust_project(settings.workspace, runtime.extensions.home, "hooks")
            runtime.refresh()
        elif action in {"enable", "disable"}:
            if not name:
                raise ValueError(f"/hooks {action} requires a hook id")
            runtime.set_hook(name, action == "enable")
        elif action == "reload":
            runtime.refresh()
        elif action != "list":
            raise ValueError("usage: /hooks [list|trust|enable ID|disable ID|reload]")
        print_hook_status(runtime)
        return True
    except (MCPError, OSError, ValueError) as exc:
        print(f"extension error: {exc}")
        return True


def interactive(settings, mode: str, thinking_name: str, yes: bool, resume: str | None = None) -> int:
    if thinking_name not in THINKING:
        thinking_name = "fast"
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
        print(f"DeepSeekFathom{sep}{settings.model}{sep}{mode}/{thinking.name}{sep}{settings.workspace.name or 'workspace'}")
    else:
        print_header(str(settings.workspace), settings.base_url, settings.model, mode, thinking.name, approval_text)
    print(f"app      : DeepSeekFathom {__version__}")
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
        DeepSeekClient(settings).ping()
    except Exception as exc:
        print(f"live     : failed: {exc}", file=sys.stderr)
        return 2
    try:
        extensions = CliExtensionRuntime(settings.workspace)
    except Exception as exc:
        print(f"extensions: failed to initialize: {exc}", file=sys.stderr)
        return 2
    extensions.connect_mcp_background()
    extension_summary = extensions.diagnostics().get("summary", {})
    extension_tools = len(extensions.agent_kwargs()["extra_tool_contracts"])
    toolkit = ToolRegistry(settings.workspace)
    maybe_prompt_update()
    skills = extensions.skill_store
    discovered_skills = skills.list()
    print(
        "ready    : "
        f"{len(toolkit.names) + extension_tools} tools · {len(discovered_skills)} skills · "
        f"MCP {extension_summary.get('activeMcpServers', 0)}/{extension_summary.get('mcpServers', 0)} · "
        f"plugins {extension_summary.get('enabledPlugins', 0)} · "
        f"hooks {extension_summary.get('activeHooks', 0)}"
    )
    print()

    current_mode = mode
    active_goal: str | None = None
    last_session_id = session.session_id if session else None
    extensions.last_session_id = last_session_id
    last_submitted_prompt = ""
    last_submitted_at = 0.0
    while True:
        try:
            prompt = read_composer(
                "",
                slash_items=slash_items(settings, extensions),
                frame_title="DeepSeekFathom",
                frame_status=composer_status(settings.model, current_mode, thinking.name, last_session_id),
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            if last_session_id:
                print_session_handoff(last_session_id)
            extensions.close(last_session_id)
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
            extensions.close(last_session_id)
            return 0
        if prompt in {"/cancel", "/stop"}:
            active_goal = None
            print(f"cancel   : back to normal input; mode={current_mode}, think={thinking.name}")
            continue
        if prompt == "/":
            print()
            print_palette(settings, extensions)
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
        if prompt == "/mode" or prompt.startswith("/mode "):
            if prompt == "/mode":
                rows = [(name, mode_description(name)) for name in MODES]
                requested = choose_palette(rows, title="permissions") or ""
                if not requested:
                    print("permission mode unchanged")
                    continue
            else:
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
        if handle_extension_prompt(prompt, extensions, settings):
            continue
        if prompt == "/skills":
            discovered = extensions.skill_store.list()
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
            skill = extensions.skill_store.get(name)
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

        display_prompt = None
        ui_kind = None
        run_mode = current_mode
        native_command = resolve_native_command(prompt.split(maxsplit=1)[0], extensions.extensions.home)
        if native_command is not None:
            raw_command = prompt
            _, _, command_details = prompt.partition(" ")
            prompt = native_command.prompt
            if command_details.strip():
                prompt += "\n\nUser request details:\n" + command_details.strip()
            display_prompt = raw_command
            ui_kind = "command"
            run_mode = native_command.mode
            run_thinking = ThinkingMode.resolve(native_command.thinking)
            print(f"plugin   : /{native_command.name} ({native_command.mode}/{native_command.thinking})")
        else:
            run_thinking = thinking
        approver = cli_approver(
            auto_approve=yes or run_mode in {"yolo", "root"},
            fallback=confirm_tool,
            allow_mandatory_prompt=sys.stdin.isatty(),
        )
        run_settings = settings.with_runtime(
            max_tokens=run_thinking.max_tokens,
            thinking_enabled=run_thinking.api_thinking,
            reasoning_effort=run_thinking.reasoning_effort,
        )
        if run_thinking.name == "auto":
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

                result = FathomAgent(
                    run_settings,
                    mode=run_mode,
                    thinking=run_thinking.name,
                    approve=approver,
                    ask_user=ask_user_choice,
                    **extensions.agent_kwargs(),
                ).run(
                    prompt,
                    stream=True,
                    on_delta=streaming_delta,
                    on_event=event,
                    session=session,
                    goal=active_goal,
                    display_prompt=display_prompt,
                    ui_kind=ui_kind,
                )
        except KeyboardInterrupt:
            print("\ninterrupted")
            continue
        except Exception as exc:
            print(f"error: {exc}")
            continue
        finally:
            try:
                extensions.apply_pending_refresh()
            except Exception as exc:
                print(f"extensions: refresh failed: {exc}")
        if result.answer:
            print()
        if session is None:
            session = SessionStore(settings.workspace).load(result.session_id)
        last_session_id = result.session_id
        extensions.last_session_id = last_session_id
    print()


def maybe_prompt_update() -> None:
    if os.getenv("DEEPSEEKFATHOM_NO_UPDATE_CHECK", os.getenv("DSTUL_NO_UPDATE_CHECK")):
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
        from .tui import ChatTui, TuiState, TuiUnavailableError
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
            body = "/exit /mode <name> /models /doctor /skills\n/think " + " | ".join(THINKING)
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
                persist_default("default_thinking", resolved.name)
            else:
                tui_state.messages.append(("system", "thinking must be one of: " + ", ".join(THINKING)))
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

        approver = cli_approver(
            auto_approve=yes or current["mode"] in {"yolo", "root"},
            fallback=confirm_tool,
            allow_mandatory_prompt=sys.stdin.isatty(),
        )
        result = FathomAgent(current["settings"], mode=current["mode"], thinking=current["thinking"].name, approve=approver, ask_user=ask_user_choice).run(
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
    except TuiUnavailableError as exc:
        print(f"tui      : {exc}; using line mode")
        fallback_session = current["session"]
        return interactive(
            current["settings"],
            current["mode"],
            current["thinking"].name,
            yes,
            fallback_session.session_id if fallback_session else None,
        )
    except BaseException:
        active_session = current["session"]
        handoff_id = state.session_id or (active_session.session_id if active_session else None)
        if handoff_id:
            print_session_handoff(handoff_id)
        raise
    if state.session_id:
        print_session_handoff(state.session_id)
    return 0


def skills_command(settings, args) -> int:
    store = cli_skill_store(settings)
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
        thinking = args.think or settings.default_thinking
        return interactive(settings, args.mode, thinking, yes=args.mode in {"yolo", "root"}, resume=args.session_id)
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


def cli_skill_store(settings, extensions: CliExtensionRuntime | None = None) -> SkillStore:
    if extensions is not None:
        return extensions.skill_store
    catalog = ExtensionRuntime(settings.workspace)
    return SkillStore(settings.workspace, extra_roots=catalog.skill_roots)


def native_command_rows(extensions: CliExtensionRuntime | None = None) -> list[tuple[str, str]]:
    home = extensions.extensions.home if extensions is not None else None
    return [
        (f"/{item['name']}", str(item.get("description") or item.get("title") or "official plugin command"))
        for item in enabled_native_commands(home)
        if isinstance(item, dict) and item.get("name")
    ]


def print_palette(settings, extensions: CliExtensionRuntime | None = None) -> None:
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
        ("/extensions", "show MCP, plugins, hooks, and skills"),
        ("/mcp", "connect all configured MCP servers"),
        ("/plugins", "show or enable plugins"),
        ("/hooks", "list or enable hooks by stable id"),
    ]
    commands.extend(native_command_rows(extensions))
    skill_rows = [(skill.name, skill.description) for skill in cli_skill_store(settings, extensions).list()]
    print_slash_palette(commands, skill_rows)
    tools = ToolRegistry(settings.workspace).describe()
    tools["delegate_agent"] = "virtual: run isolated subagent and return summary"
    print_tool_palette(tools)


def slash_items(settings, extensions: CliExtensionRuntime | None = None) -> list[tuple[str, str]]:
    items = native_command_rows(extensions)
    items.extend([
        ("/model", "choose model / show live DeepSeek models"),
        ("/think", "choose thinking depth"),
        ("/mode", "choose permission mode"),
        ("/compact", "compress older conversation context now"),
        ("/goal", "show active goal"),
        ("/goal <text>", "set active goal"),
        ("/goal clear", "clear active goal"),
        ("/extensions", "show MCP, plugins, hooks, and skills"),
        ("/mcp", "connect all configured MCP servers"),
        ("/plugins", "show official and user plugins"),
        ("/hooks", "show configured hooks and stable ids"),
        ("/skills", "list discovered skills"),
        ("/doctor", "check live DeepSeek config"),
        ("/exit", "leave and print resume command"),
    ])
    for skill in cli_skill_store(settings, extensions).list():
        items.append((f"/skill {skill.name}", skill.description))
    return items


def thinking_description(name: str) -> str:
    mode = ThinkingMode.resolve(name)
    if name == "auto":
        return "automatic · choose the depth for each request"
    if name == "off":
        return "reasoning off · direct response"
    if name == "instant":
        return "reasoning off · fastest response"
    effort = mode.reasoning_effort or "off"
    passes = f"{mode.deliberation_passes} internal pass" + ("" if mode.deliberation_passes == 1 else "es")
    return f"{effort} · {passes} · {mode.model_hint}"


def mode_description(name: str) -> str:
    descriptions = {
        "plan": "read-only planning",
        "review": "read-only review; shell actions require confirmation",
        "agent": "edit and run tools with gated confirmations",
        "trusted": "network access with gated confirmations",
        "yolo": "full tool access; MCP configuration still confirms",
        "root": "full tool access; MCP configuration still confirms",
    }
    return descriptions.get(name, name)


def choose_auto_thinking(settings, prompt: str) -> ThinkingMode:
    candidates = [name for name in THINKING if name not in {"auto", "off"}]
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
    print(f"[resume] deepseekfathom start --resume {session_id}", file=sys.stderr)


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
