from __future__ import annotations

import sys
import time
import shutil
import os
import select
import subprocess
import atexit
import unicodedata
import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from collections.abc import Callable

try:
    import termios
    import tty
except ImportError:  # Windows has no Unix raw-terminal modules.
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
BRIGHT_CYAN = "\033[96m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
BRIGHT_MAGENTA = "\033[95m"
GREEN = "\033[32m"
BRIGHT_GREEN = "\033[92m"
YELLOW = "\033[33m"
WHITE = "\033[97m"
GRAY = "\033[90m"
DIFF_DELETE = "\033[38;2;255;123;114m\033[48;2;64;24;28m"
DIFF_ADD = "\033[38;2;126;231;135m\033[48;2;22;55;32m"

_TERMINAL_SAFETY_REGISTERED = False


@dataclass(frozen=True)
class ComposerFrame:
    width: int
    status: str
    placeholder: str = "输入任务，/ 查看命令"


_PROMPT_SESSION_FACTORY: Callable[..., object] | None = None
_PROMPT_TOOLKIT_HISTORY: object | None = None
_PROMPT_TOOLKIT_FALLBACK = object()


def stream_is_tty(stream) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def configure_utf8_stdio() -> None:
    """Use UTF-8 for Windows console and redirected standard streams."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        input_handle = ctypes.windll.kernel32.GetStdHandle(-10)
        input_mode = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetConsoleMode(input_handle, ctypes.byref(input_mode)):
            enable_extended_flags = 0x0080
            enable_quick_edit_mode = 0x0040
            next_mode = (input_mode.value | enable_extended_flags) & ~enable_quick_edit_mode
            ctypes.windll.kernel32.SetConsoleMode(input_handle, next_mode)
    except Exception:
        pass
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        options: dict[str, object] = {"encoding": "utf-8", "errors": "replace"}
        if name != "stdin":
            options["write_through"] = True
        try:
            reconfigure(**options)
        except TypeError:
            options.pop("write_through", None)
            try:
                reconfigure(**options)
            except (OSError, ValueError, TypeError):
                pass
        except (OSError, ValueError):
            pass


def _enable_windows_vt(stream) -> bool:
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_ulong()
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
            return False
        enable_virtual_terminal_processing = 0x0004
        if mode.value & enable_virtual_terminal_processing:
            return True
        return bool(
            kernel32.SetConsoleMode(
                ctypes.c_void_p(handle),
                mode.value | enable_virtual_terminal_processing,
            )
        )
    except Exception:
        return False


def terminal_supports_ansi(stream=None) -> bool:
    stream = sys.stdout if stream is None else stream
    if os.getenv("DEEPSEEKFATHOM_PLAIN_UI", os.getenv("DSTUL_PLAIN_UI")) or not stream_is_tty(stream):
        return False
    if os.getenv("TERM", "").lower() == "dumb":
        return False
    if os.name == "nt":
        return _enable_windows_vt(stream)
    return True


def plain_terminal(stream=None) -> bool:
    return not terminal_supports_ansi(sys.stdout if stream is None else stream)


def install_terminal_safety() -> None:
    global _TERMINAL_SAFETY_REGISTERED
    configure_utf8_stdio()
    if not stream_is_tty(sys.stdin) or not stream_is_tty(sys.stdout):
        return
    force_terminal_sane()
    if not _TERMINAL_SAFETY_REGISTERED:
        atexit.register(force_terminal_sane)
        _TERMINAL_SAFETY_REGISTERED = True


def force_terminal_sane() -> None:
    if terminal_supports_ansi(sys.stdout):
        try:
            sys.stdout.write("\033[?25h\033[0m\033[?1049l")
            sys.stdout.flush()
        except (AttributeError, OSError, ValueError):
            pass
    if stream_is_tty(sys.stdin) and os.name != "nt":
        try:
            subprocess.run(["stty", "sane"], stdin=sys.stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        except Exception:
            pass


def startup_animation(enabled: bool = True) -> None:
    del enabled  # Kept for compatibility with callers that distinguish resume startup.
    print(color("DeepSeekFathom", BOLD + CYAN))


def confirm_tool(name: str, arguments: dict) -> bool:
    print(f"\nTool requires confirmation: {name}")
    for key, value in arguments.items():
        preview = str(value)
        if len(preview) > 500:
            preview = preview[:500] + "..."
        print(f"  {key}: {preview}")
    answer = input("type yes to approve> ").strip().lower()
    return answer == "yes"


class ThinkingSpinner:
    active: "ThinkingSpinner | None" = None

    def __init__(self, label: str = "thinking"):
        self.label = label
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.clear_width = 96

    def __enter__(self):
        if not terminal_supports_ansi(sys.stderr):
            return self
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def start(self) -> None:
        if not terminal_supports_ansi(sys.stderr) or self.thread is not None:
            return
        self.stop_event.clear()
        ThinkingSpinner.active = self
        self.thread = Thread(target=self._spin, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        was_running = self.thread is not None or ThinkingSpinner.active is self
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=0.2)
            self.thread = None
        if ThinkingSpinner.active is self:
            ThinkingSpinner.active = None
        if was_running:
            self.clear_line()

    def clear_line(self) -> None:
        if terminal_supports_ansi(sys.stderr):
            print("\r\033[2K", end="", file=sys.stderr, flush=True)

    @classmethod
    def clear_active_line(cls) -> None:
        if cls.active is not None:
            cls.active.clear_line()

    def _spin(self) -> None:
        frames = [
            stream_color("thinking", BRIGHT_MAGENTA + BOLD, sys.stderr) + stream_color("  ◐ ", CYAN, sys.stderr) + stream_color("reasoning", GRAY, sys.stderr),
            stream_color("thinking", BRIGHT_MAGENTA + BOLD, sys.stderr) + stream_color("  ◓ ", CYAN, sys.stderr) + stream_color("planning", GRAY, sys.stderr),
            stream_color("thinking", BRIGHT_MAGENTA + BOLD, sys.stderr) + stream_color("  ◑ ", CYAN, sys.stderr) + stream_color("routing", GRAY, sys.stderr),
            stream_color("thinking", BRIGHT_MAGENTA + BOLD, sys.stderr) + stream_color("  ◒ ", CYAN, sys.stderr) + stream_color("checking", GRAY, sys.stderr),
        ]
        index = 0
        while not self.stop_event.is_set():
            self.clear_line()
            print("\r" + frames[index % len(frames)], end="", file=sys.stderr, flush=True)
            index += 1
            time.sleep(0.12)


def format_agent_event(text: str) -> str:
    if plain_terminal():
        if text.startswith("tool "):
            rest = text.removeprefix("tool ").strip()
            name, _, args = rest.partition(" ")
            if name in {"write_file", "apply_patch"}:
                path = file_path_from_tool_event(name, args)
                return f"  [edit] {path or 'preparing file change'}"
            return f"  [tool] {name}" + (f" | {args}" if args else "")
        if text.startswith("done "):
            name, payload = decode_done_event(text)
            file_change = file_change_from_payload(payload)
            if file_change is not None:
                return format_file_change(file_change, use_color=False)
            return f"  [done] {name}"
        if text.startswith("subagent "):
            return f"  [subagent] {text.removeprefix('subagent ').strip()}"
        if text.startswith("thinking pass "):
            return f"  [thinking] {text.removeprefix('thinking ').strip()}"
        if text.startswith("context compacted"):
            return f"  [context] {text}"
        return f"  [event] {text}"
    if text.startswith("tool "):
        rest = text.removeprefix("tool ").strip()
        name, _, args = rest.partition(" ")
        if name in {"write_file", "apply_patch"}:
            path = file_path_from_tool_event(name, args)
            return color("  ✎ ", CYAN + BOLD) + color(path or "preparing file change", WHITE)
        return (
            color("  ╭─", CYAN)
            + color(" tool ", YELLOW + BOLD)
            + color(name, BRIGHT_CYAN + BOLD)
            + (color(" · ", GRAY) + color(args, WHITE) if args else "")
        )
    if text.startswith("done "):
        name, payload = decode_done_event(text)
        file_change = file_change_from_payload(payload)
        if file_change is not None:
            return format_file_change(file_change, use_color=True)
        return color("  ╰─", CYAN) + color(" done ", GREEN + BOLD) + color(name, BRIGHT_GREEN)
    if text.startswith("subagent "):
        return color("  ◆ ", BRIGHT_MAGENTA) + color(text, BRIGHT_CYAN)
    if text.startswith("thinking pass "):
        return color("  ◇ ", BRIGHT_MAGENTA) + color(text, GRAY)
    if text.startswith("context compacted"):
        return color("  ◈ ", YELLOW) + color(text, GRAY)
    return color("  • ", CYAN) + color(text, GRAY)


def decode_done_event(text: str) -> tuple[str, dict[str, object] | None]:
    rest = text.removeprefix("done ").strip()
    name, _, encoded = rest.partition(" ")
    if not encoded:
        return name, None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
        payload = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return name, None
    return name, payload if isinstance(payload, dict) else None


def file_change_from_payload(payload: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    ui = payload.get("ui")
    if not isinstance(ui, dict) or ui.get("kind") != "file_change":
        return None
    return ui


def file_path_from_tool_event(name: str, detail: str) -> str:
    if name == "write_file":
        match = re.search(r"(?:^|\s)path=(.*?)(?=\s+[A-Za-z_][\w-]*=|$)", detail)
        return match.group(1).strip("'\"") if match else ""
    match = re.search(r"\+\+\+\s+(?:b/)?([^\\\s]+)", detail)
    if match and match.group(1) != "/dev/null":
        return match.group(1)
    return ""


def format_file_change(ui: dict[str, object], *, use_color: bool) -> str:
    use_color = bool(use_color and terminal_supports_ansi(sys.stdout) and not os.getenv("NO_COLOR"))
    path = str(ui.get("path") or "file")
    operation = "created" if ui.get("operation") == "created" else "modified"
    additions = safe_nonnegative_int(ui.get("additions"))
    deletions = safe_nonnegative_int(ui.get("deletions"))
    if use_color:
        header = (
            color("  ✎ ", CYAN + BOLD)
            + color(f"{operation} ", WHITE)
            + color(path, BRIGHT_CYAN + BOLD)
            + color(f"  +{additions} -{deletions}", GRAY)
        )
    else:
        header = f"  [edit] {operation} {path}  +{additions} -{deletions}"
    diff = str(ui.get("diff") or "")
    rows = parse_unified_diff(diff)
    if not rows:
        return header + "\n    no content changes"
    max_old = max((row[1] or 0 for row in rows), default=0)
    max_new = max((row[2] or 0 for row in rows), default=0)
    number_width = min(max(len(str(max(max_old, max_new, 1))), 3), 7)
    terminal_width = max(shutil.get_terminal_size((100, 24)).columns, 20)
    rendered = [header]
    for kind, old_line, new_line, mark, body in rows:
        if kind in {"meta", "hunk"}:
            line = f"    {body}"
            rendered.append(color(clip_visible(line, terminal_width), GRAY if kind == "meta" else CYAN) if use_color else clip_visible(line, terminal_width))
            continue
        old_text = str(old_line) if old_line is not None else ""
        new_text = str(new_line) if new_line is not None else ""
        gutter = f"  {old_text:>{number_width}} {new_text:>{number_width}} {mark} "
        line = clip_visible(gutter + body.expandtabs(4), terminal_width)
        if use_color and kind == "delete":
            line = f"{DIFF_DELETE}{line}{RESET}"
        elif use_color and kind == "add":
            line = f"{DIFF_ADD}{line}{RESET}"
        rendered.append(line)
    if ui.get("truncated") and "diff lines omitted" not in diff:
        rendered.append(f"    ... {safe_nonnegative_int(ui.get('omitted_lines'))} diff lines omitted ...")
    return "\n".join(rendered)


def safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_unified_diff(diff: str) -> list[tuple[str, int | None, int | None, str, str]]:
    old_line: int | None = None
    new_line: int | None = None
    parsed: list[tuple[str, int | None, int | None, str, str]] = []
    for line in diff.splitlines():
        if re.fullmatch(r"\.\.\. \d+ diff lines omitted \.\.\.", line):
            old_line = None
            new_line = None
            parsed.append(("meta", None, None, "", line))
            continue
        if line.startswith(("diff ", "index ", "--- ", "+++ ")):
            parsed.append(("meta", None, None, "", line))
            continue
        if line.startswith("@@"):
            match = re.match(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@", line)
            if match:
                old_line = int(match.group(1))
                new_line = int(match.group(2))
            parsed.append(("hunk", None, None, "", line))
            continue
        if line.startswith("+"):
            parsed.append(("add", None, new_line, "+", line[1:]))
            if new_line is not None:
                new_line += 1
            continue
        if line.startswith("-"):
            parsed.append(("delete", old_line, None, "-", line[1:]))
            if old_line is not None:
                old_line += 1
            continue
        if line.startswith(" "):
            parsed.append(("context", old_line, new_line, " ", line[1:]))
            if old_line is not None:
                old_line += 1
            if new_line is not None:
                new_line += 1
            continue
        parsed.append(("meta", None, None, "", line))

    ordered: list[tuple[str, int | None, int | None, str, str]] = []
    index = 0
    while index < len(parsed):
        if parsed[index][0] not in {"add", "delete"}:
            ordered.append(parsed[index])
            index += 1
            continue
        changed: list[tuple[str, int | None, int | None, str, str]] = []
        while index < len(parsed) and parsed[index][0] in {"add", "delete"}:
            changed.append(parsed[index])
            index += 1
        ordered.extend(row for row in changed if row[0] == "delete")
        ordered.extend(row for row in changed if row[0] == "add")
    return ordered


def print_slash_palette(commands: list[tuple[str, str]], skills: list[tuple[str, str]]) -> None:
    print_box("Command Palette", [f"{name:<18} {description}" for name, description in commands])
    skill_lines = [f"/skill {name:<11} {description}" for name, description in skills] or ["none discovered"]
    print_box("Skills", skill_lines)


def print_tool_palette(tools: dict[str, str]) -> None:
    print_box("Tools", [f"{name:<16} {description}" for name, description in tools.items()])


def print_box(title: str, lines: list[str]) -> None:
    width = max(1, min(shutil.get_terminal_size((88, 24)).columns, 96))
    if plain_terminal() or width < 8:
        print(f"[{title}]")
        for line in lines:
            print(f"  {clip_visible(strip_ansi(line), max(width - 2, 1))}")
        return
    print(color("╭─ ", CYAN) + color(title, BOLD + WHITE) + color(" " + "─" * max(width - visible_len(title) - 5, 0) + "╮", CYAN))
    for line in lines:
        clipped = clip_visible(line, max(width - 4, 1))
        print(color("│ ", CYAN) + pad_ansi(clipped, width - 4) + color(" │", CYAN))
    print(color("╰" + "─" * (width - 2) + "╯", CYAN))


def center_line(text: str, width: int) -> str:
    inner = max(width - 2, 0)
    visible = visible_len(text)
    if visible > inner:
        text = clip_visible(text, inner)
        visible = visible_len(text)
    left = (inner - visible) // 2
    right = inner - visible - left
    return color("│", BRIGHT_CYAN) + " " * left + text + " " * right + color("│", BRIGHT_CYAN)


def print_header(workspace: str, endpoint: str, model: str, mode: str, thinking: str, approval: str) -> None:
    workspace_name = Path(workspace).name or "workspace"
    print(label("workspace") + color(workspace_name, WHITE))
    print(
        label("session")
        + color(model, BRIGHT_GREEN if "flash" in model else BRIGHT_MAGENTA)
        + color(" | ", GRAY)
        + color(thinking, BRIGHT_MAGENTA)
        + color(" | ", GRAY)
        + color(mode, YELLOW if mode == "root" else GREEN)
        + color(" | ", GRAY)
        + color(approval, GREEN if approval == "all yes" else YELLOW)
    )


def status_bar(model: str, mode: str, thinking: str, session_id: str | None = None) -> str:
    suffix = f" session={session_id}" if session_id else ""
    return (
        color("[model ", GRAY) + color(model, BRIGHT_GREEN if "flash" in model else BRIGHT_MAGENTA) + color("] ", GRAY)
        + color("[mode ", GRAY) + color(mode, YELLOW if mode == "root" else GREEN) + color("] ", GRAY)
        + color("[think ", GRAY) + color(thinking, BRIGHT_MAGENTA) + color("]", GRAY)
        + color(suffix, GRAY)
    )


def composer_prompt(model: str, mode: str, thinking: str, session_id: str | None = None) -> str:
    session = f" {session_id[:8]}" if session_id and plain_terminal() else f" · {session_id[:8]}" if session_id else ""
    if terminal_supports_ansi(sys.stdout) and not os.getenv("NO_COLOR"):
        return (
            color("▌ ", CYAN + BOLD)
            + color(f"{model}", BRIGHT_GREEN if "flash" in model else BRIGHT_MAGENTA)
            + color(f" mode={mode} think={thinking}{session}", GRAY)
            + color(" › ", CYAN + BOLD)
        )
    return f"[{model} mode={mode} think={thinking}{session}] > "


def composer_status(model: str, mode: str, thinking: str, session_id: str | None = None) -> str:
    session = f" · {session_id[:8]}" if session_id else ""
    return f"{model} · {mode} · {thinking}{session}"


def open_composer_frame(title: str, status: str) -> ComposerFrame | None:
    if not terminal_supports_ansi(sys.stdout):
        return None
    columns = max(shutil.get_terminal_size((88, 24)).columns, 1)
    if columns < 28:
        return None
    width = min(columns - 1, 96)
    return ComposerFrame(width=width, status=status)


def close_composer_frame(frame: ComposerFrame) -> None:
    sys.stdout.write("\033[1B\r\n")
    sys.stdout.flush()


def read_composer(
    prompt: str,
    slash_items: list[tuple[str, str]] | None = None,
    *,
    frame_title: str | None = None,
    frame_status: str = "",
) -> str:
    if _prompt_toolkit_terminal_ready():
        result = _read_prompt_toolkit_composer(slash_items or [], frame_status)
        if result is not _PROMPT_TOOLKIT_FALLBACK:
            return str(result)

    frame = open_composer_frame(frame_title, frame_status) if frame_title else None
    active_prompt = (
        color("› ", YELLOW + BOLD) + prompt
        if frame is not None
        else prompt
    )
    try:
        if not stream_is_tty(sys.stdin) or not stream_is_tty(sys.stdout):
            return input(active_prompt)
        if os.name == "nt":
            try:
                msvcrt = _load_msvcrt()
            except ImportError:
                return input(active_prompt)
            return read_windows_composer(active_prompt, slash_items=slash_items, msvcrt=msvcrt, frame=frame)
        if not terminal_supports_ansi(sys.stdout) or termios is None or tty is None:
            return input(active_prompt)

        try:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
        except (AttributeError, OSError, ValueError):
            return input(active_prompt)
        buffer: list[str] = []
        try:
            tty.setraw(fd)
        except (OSError, ValueError):
            return input(active_prompt)
        try:
            sys.stdout.write("\033[?2004h")
            redraw_composer(active_prompt, buffer, frame=frame)
            while True:
                char = read_raw_char(fd)
                if not char:
                    raise EOFError
                if char == "\x1b":
                    suffix = read_escape_suffix(fd)
                    if suffix == "[200~":
                        read_bracketed_paste(fd, buffer)
                        redraw_composer(active_prompt, buffer, frame=frame)
                    continue
                if char in {"\r", "\n"}:
                    if buffer and not should_submit_newline(fd):
                        buffer.append("\n")
                        redraw_composer(active_prompt, buffer, frame=frame)
                        continue
                    submit_composer_line(active_prompt, buffer, frame)
                    return "".join(buffer)
                if char == "\x03":
                    if buffer:
                        buffer.clear()
                        redraw_composer(active_prompt, buffer, frame=frame)
                        continue
                    submit_composer_line(active_prompt, [], frame)
                    return "/cancel"
                if char == "\x04":
                    raise EOFError
                if char in {"\x7f", "\b"}:
                    if buffer:
                        buffer.pop()
                        redraw_composer(active_prompt, buffer, frame=frame)
                    continue
                if char == "/" and not buffer and slash_items:
                    selected = slash_select(
                        slash_items,
                        on_query=lambda query: redraw_composer(
                            active_prompt,
                            list("/" + query),
                            frame=frame,
                        ),
                    )
                    if selected:
                        insertion = slash_selection_insertion(selected)
                        if insertion is not None:
                            buffer.extend(insertion)
                            redraw_composer(active_prompt, buffer, frame=frame)
                            continue
                        submit_composer_line(active_prompt, list(selected), frame)
                        return selected
                    redraw_composer(active_prompt, buffer, frame=frame)
                    continue
                if char.isprintable():
                    buffer.append(char)
                    redraw_composer(active_prompt, buffer, frame=frame)
        finally:
            try:
                sys.stdout.write("\033[?2004l")
                sys.stdout.flush()
            except (AttributeError, OSError, ValueError):
                pass
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old)
            except (OSError, ValueError):
                pass
    finally:
        if frame is not None:
            close_composer_frame(frame)


def _prompt_toolkit_terminal_ready() -> bool:
    if not stream_is_tty(sys.stdin) or not stream_is_tty(sys.stdout):
        return False
    if os.getenv("DEEPSEEKFATHOM_PLAIN_UI", os.getenv("DSTUL_PLAIN_UI")):
        return False
    if os.getenv("TERM", "").casefold() == "dumb":
        return False
    for stream in (sys.stdin, sys.stdout):
        try:
            if int(stream.fileno()) < 0:
                return False
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    return True


def _read_prompt_toolkit_composer(
    slash_items: list[tuple[str, str]],
    status: str,
) -> str | object:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.shortcuts import CompleteStyle
        from prompt_toolkit.styles import Style

        history = _prompt_toolkit_history(InMemoryHistory)
        completer = _prompt_toolkit_completer(slash_items)
        key_bindings = _prompt_toolkit_key_bindings()
        style = Style.from_dict({
            "prompt": "fg:ansiyellow bold",
            "placeholder": "fg:ansibrightblack",
            "status": "fg:ansiwhite bg:ansidefault noreverse",
            "completion-menu": "bg:ansidefault",
            "completion-menu.completion": "fg:ansiwhite bg:ansidefault noreverse",
            "completion-menu.completion.current": "fg:ansibrightcyan bg:ansidefault bold noreverse",
            "completion-menu.meta.completion": "fg:ansiwhite bg:ansidefault noreverse",
            "completion-menu.meta.completion.current": "fg:ansibrightcyan bg:ansidefault noreverse",
            "completion-menu.scrollbar.background": "bg:ansidefault",
            "completion-menu.scrollbar.button": "bg:ansibrightblack",
        })
        clean_status = " ".join(str(status or "").split()) or "就绪"
        factory = _PROMPT_SESSION_FACTORY or PromptSession
        session = factory(
            history=history,
            completer=completer,
            key_bindings=key_bindings,
            style=style,
            message=FormattedText([("class:prompt", "› ")]),
            placeholder=FormattedText([("class:placeholder", "输入任务，/ 查看命令")]),
            rprompt=FormattedText([("class:status", clean_status)]),
            complete_style=CompleteStyle.COLUMN,
            complete_while_typing=True,
            complete_in_thread=False,
            reserve_space_for_menu=0,
            multiline=False,
            wrap_lines=True,
            enable_history_search=True,
            search_ignore_case=True,
            erase_when_done=False,
            mouse_support=False,
            include_default_pygments_style=False,
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return _PROMPT_TOOLKIT_FALLBACK

    try:
        value = session.prompt()
    except KeyboardInterrupt:
        return "/cancel"
    except EOFError:
        raise
    except (OSError, RuntimeError):
        return _PROMPT_TOOLKIT_FALLBACK
    return value if isinstance(value, str) else _PROMPT_TOOLKIT_FALLBACK


def _prompt_toolkit_history(history_type):
    global _PROMPT_TOOLKIT_HISTORY
    if _PROMPT_TOOLKIT_HISTORY is None:
        _PROMPT_TOOLKIT_HISTORY = history_type()
    return _PROMPT_TOOLKIT_HISTORY


def _prompt_toolkit_completer(items: list[tuple[str, str]]):
    from prompt_toolkit.completion import Completer, Completion

    class SlashCompleter(Completer):
        def get_completions(self, document, _complete_event):
            before_cursor = document.text_before_cursor
            if not before_cursor.startswith("/") or "\n" in before_cursor or "\r" in before_cursor:
                return
            query = before_cursor[1:]
            for command, description in filter_slash_items(items, query):
                insertion = slash_selection_insertion(command)
                yield Completion(
                    insertion if insertion is not None else command,
                    start_position=-len(before_cursor),
                    display=command,
                    display_meta=description,
                )

    return SlashCompleter()


def _prompt_toolkit_key_bindings():
    from prompt_toolkit.filters import has_completions
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()
    bindings.add("c-c", eager=True)(_prompt_toolkit_ctrl_c)
    bindings.add("c-d", eager=True)(_prompt_toolkit_ctrl_d)
    bindings.add("/", eager=True)(_prompt_toolkit_insert_slash)
    bindings.add("c-h", eager=True)(_prompt_toolkit_backspace)
    bindings.add("enter", filter=has_completions, eager=True)(_prompt_toolkit_accept_completion)
    return bindings


def _prompt_toolkit_ctrl_c(event) -> None:
    buffer = event.app.current_buffer
    if buffer.text:
        buffer.reset()
        return
    event.app.exit(result="/cancel")


def _prompt_toolkit_ctrl_d(event) -> None:
    event.app.exit(exception=EOFError)


def _prompt_toolkit_insert_slash(event) -> None:
    buffer = event.app.current_buffer
    buffer.insert_text("/")
    if buffer.text == "/" and buffer.cursor_position == 1:
        buffer.start_completion(select_first=False)


def _prompt_toolkit_backspace(event) -> None:
    buffer = event.app.current_buffer
    if buffer.complete_state is not None:
        buffer.cancel_completion()
    buffer.delete_before_cursor(count=1)
    before_cursor = buffer.document.text_before_cursor
    if before_cursor.startswith("/") and not any(char.isspace() for char in before_cursor):
        buffer.start_completion(select_first=False)


def _prompt_toolkit_accept_completion(event) -> None:
    buffer = event.app.current_buffer
    state = buffer.complete_state
    completion = state.current_completion if state is not None else None
    if completion is None:
        buffer.validate_and_handle()
        return
    buffer.apply_completion(completion)
    if buffer.text.endswith(" ") or not buffer.text.startswith("/"):
        return
    buffer.validate_and_handle()


def _load_msvcrt():
    import msvcrt

    return msvcrt


def read_windows_composer(
    prompt: str,
    slash_items: list[tuple[str, str]] | None,
    msvcrt,
    *,
    frame: ComposerFrame | None = None,
) -> str:
    buffer: list[str] = []
    cursor_index = 0
    use_ansi = terminal_supports_ansi(sys.stdout)
    previous_width = 0
    skip_lf_after_pasted_cr = False

    def redraw() -> None:
        nonlocal previous_width
        if use_ansi:
            redraw_composer(prompt, buffer, frame=frame, cursor_index=cursor_index)
        else:
            previous_width = redraw_plain_composer(
                prompt,
                buffer,
                previous_width,
                cursor_index=cursor_index,
            )

    redraw()
    while True:
        try:
            key = read_windows_console_key(msvcrt)
        except KeyboardInterrupt:
            key = "\x03"
        if skip_lf_after_pasted_cr:
            skip_lf_after_pasted_cr = False
            if key == "\n":
                continue
        if key in {"\r", "\n"}:
            if buffer and _windows_input_pending(msvcrt):
                buffer.insert(cursor_index, "\n")
                cursor_index += 1
                skip_lf_after_pasted_cr = key == "\r"
                redraw()
                continue
            submit_composer_line(prompt, buffer, frame)
            return "".join(buffer)
        if key == "\x03":
            if buffer:
                buffer.clear()
                cursor_index = 0
                redraw()
                continue
            submit_composer_line(prompt, [], frame)
            return "/cancel"
        if key in {"\x04", "\x1a", ""}:
            raise EOFError
        if key in {"\x08", "\x7f"}:
            if cursor_index > 0:
                del buffer[cursor_index - 1]
                cursor_index -= 1
                redraw()
            continue
        if key == "<DELETE>":
            if cursor_index < len(buffer):
                del buffer[cursor_index]
                redraw()
            continue
        if key == "<LEFT>":
            cursor_index = max(cursor_index - 1, 0)
            redraw()
            continue
        if key == "<RIGHT>":
            cursor_index = min(cursor_index + 1, len(buffer))
            redraw()
            continue
        if key == "<HOME>":
            cursor_index = 0
            redraw()
            continue
        if key == "<END>":
            cursor_index = len(buffer)
            redraw()
            continue
        if key == "/" and not buffer and slash_items:
            selected = windows_slash_select(
                slash_items,
                msvcrt,
                use_ansi=use_ansi,
                previous_width=previous_width,
                on_query=(
                    lambda query: redraw_composer(
                        prompt,
                        list("/" + query),
                        frame=frame,
                        cursor_index=len(query) + 1,
                    )
                    if use_ansi
                    else None
                ),
            )
            previous_width = 0
            if selected:
                insertion = slash_selection_insertion(selected)
                if insertion is not None:
                    buffer.extend(insertion)
                    cursor_index = len(buffer)
                    redraw()
                    continue
                if use_ansi:
                    submit_composer_line(prompt, list(selected), frame)
                else:
                    width = max(shutil.get_terminal_size((88, 24)).columns, 1)
                    sys.stdout.write(_composer_rendered_line(prompt, list(selected), width) + "\r\n")
                    sys.stdout.flush()
                return selected
            redraw()
            continue
        if len(key) == 1 and key.isprintable():
            buffer.insert(cursor_index, key)
            cursor_index += 1
            redraw()


def read_windows_console_key(msvcrt) -> str:
    char = msvcrt.getwch()
    if len(char) == 1 and 0xD800 <= ord(char) <= 0xDBFF:
        low = msvcrt.getwch()
        if len(low) == 1 and 0xDC00 <= ord(low) <= 0xDFFF:
            codepoint = 0x10000 + ((ord(char) - 0xD800) << 10) + (ord(low) - 0xDC00)
            return chr(codepoint)
        return ""
    if char not in {"\x00", "\xe0"}:
        return char
    extended = msvcrt.getwch()
    return {
        "H": "<UP>",
        "P": "<DOWN>",
        "K": "<LEFT>",
        "M": "<RIGHT>",
        "G": "<HOME>",
        "O": "<END>",
        "S": "<DELETE>",
    }.get(extended, "<SPECIAL>")


def _windows_input_pending(msvcrt) -> bool:
    try:
        return bool(msvcrt.kbhit())
    except (AttributeError, OSError):
        return False


def _composer_rendered_line(prompt: str, buffer: list[str], columns: int) -> str:
    rendered, _cursor = _composer_rendered_state(prompt, buffer, columns, len(buffer))
    return rendered


def _composer_rendered_state(
    prompt: str,
    buffer: list[str],
    columns: int,
    cursor_index: int,
) -> tuple[str, int]:
    usable = max(columns - 1, 1)
    if visible_len(prompt) < usable:
        visible_prompt = prompt
    else:
        visible_prompt = clip_visible(prompt, max(usable - 1, 1))
    available = max(usable - visible_len(visible_prompt), 0)
    text = "".join(buffer)
    cursor_index = min(max(cursor_index, 0), len(buffer))
    display, input_cursor = composer_cursor_display(text, available, cursor_index)
    rendered = clip_visible(visible_prompt + display, usable)
    cursor_column = min(visible_len(visible_prompt) + input_cursor, visible_len(rendered), usable)
    return rendered, cursor_column


def redraw_composer(
    prompt: str,
    buffer: list[str],
    *,
    frame: ComposerFrame | None = None,
    place_cursor: bool = True,
    cursor_index: int | None = None,
) -> None:
    width = frame.width if frame is not None else max(shutil.get_terminal_size((88, 24)).columns, 1)
    active_cursor = len(buffer) if cursor_index is None else cursor_index
    rendered, cursor_column = _composer_rendered_state(prompt, buffer, width, active_cursor)
    sys.stdout.write("\033[?25h\r\033[2K")
    if frame is None:
        sys.stdout.write(rendered)
    else:
        if not buffer:
            placeholder_width = max(width - visible_len(rendered), 0)
            rendered += color(clip_visible(frame.placeholder, placeholder_width), GRAY)
        sys.stdout.write(clip_visible(rendered, width))
        status = clip_visible("  " + frame.status, width)
        sys.stdout.write("\r\n\033[2K" + color(status, GRAY))
        sys.stdout.write("\033[1A\r")
    if place_cursor and (frame is not None or cursor_index is not None):
        sys.stdout.write("\r" + (f"\033[{cursor_column}C" if cursor_column else ""))
    sys.stdout.flush()


def submit_composer_line(prompt: str, buffer: list[str], frame: ComposerFrame | None) -> None:
    if frame is not None:
        redraw_composer(prompt, buffer, frame=frame, place_cursor=False)
    else:
        sys.stdout.write("\r\n")
    sys.stdout.flush()


def redraw_plain_composer(
    prompt: str,
    buffer: list[str],
    previous_width: int = 0,
    *,
    cursor_index: int | None = None,
) -> int:
    width = max(shutil.get_terminal_size((88, 24)).columns, 1)
    active_cursor = len(buffer) if cursor_index is None else cursor_index
    rendered, cursor_column = _composer_rendered_state(prompt, buffer, width, active_cursor)
    line = strip_ansi(rendered)
    line_width = redraw_plain_line(line, previous_width, width)
    if cursor_index is not None and cursor_column < line_width:
        sys.stdout.write("\b" * (line_width - cursor_column))
        sys.stdout.flush()
    return line_width


def redraw_plain_line(text: str, previous_width: int, columns: int | None = None) -> int:
    width = max(columns or shutil.get_terminal_size((88, 24)).columns, 1)
    usable = max(width - 1, 1)
    line = take_prefix_for_width(strip_ansi(text), usable)
    line_width = display_width(line)
    clear_width = min(max(previous_width, line_width), usable)
    sys.stdout.write("\r" + " " * clear_width + "\r" + line)
    sys.stdout.flush()
    return line_width


def composer_display_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if "\n" not in text and "\r" not in text:
        return tail_for_width(text, width)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    suffix = lines[-1] if lines else ""
    prefix = f"[pasted {len(lines)} lines] "
    if display_width(prefix) >= width:
        return take_prefix_for_width(prefix, width)
    available = width - display_width(prefix)
    return prefix + tail_for_width(suffix, available)


def composer_cursor_display(text: str, width: int, cursor_index: int) -> tuple[str, int]:
    if width <= 0:
        return "", 0
    cursor_index = min(max(cursor_index, 0), len(text))
    if "\n" in text or "\r" in text:
        display = composer_display_text(text, width)
        return display, display_width(display)
    if display_width(text) <= width:
        return text, display_width(text[:cursor_index])

    before = text[:cursor_index]
    after = text[cursor_index:]
    after_budget = min(display_width(after), max(width // 2, 1)) if after else 0
    before_display = tail_for_width(before, max(width - after_budget, 0))
    remaining = max(width - display_width(before_display), 0)
    after_display = clip_visible(after, remaining) if after else ""
    return before_display + after_display, display_width(before_display)


def tail_for_width(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    prefix = "..." if plain_terminal() else "…"
    if display_width(prefix) >= width:
        return take_prefix_for_width(prefix, width)
    remaining = width - display_width(prefix)
    chars: list[str] = []
    used = 0
    for char in reversed(text):
        char_width = char_display_width(char)
        if used + char_width > remaining:
            break
        chars.append(char)
        used += char_width
    return prefix + "".join(reversed(chars))


def display_width(text: str) -> int:
    return sum(char_display_width(char) for char in text)


def char_display_width(char: str) -> int:
    code = ord(char)
    if code == 0:
        return 0
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if unicodedata.category(char) in {"Mn", "Me", "Cf"}:
        return 0
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 2
    return 1


def take_prefix_for_width(text: str, width: int) -> str:
    if width <= 0:
        return ""
    chars: list[str] = []
    used = 0
    for char in text:
        char_width = char_display_width(char)
        if used + char_width > width:
            break
        chars.append(char)
        used += char_width
    return "".join(chars)


def pad_display(text: str, width: int) -> str:
    clipped = take_prefix_for_width(text, max(width, 0))
    return clipped + " " * max(width - display_width(clipped), 0)


def choose_palette(items: list[tuple[str, str]], title: str = "commands") -> str | None:
    if not stream_is_tty(sys.stdin) or not stream_is_tty(sys.stdout):
        return None
    if os.name == "nt":
        try:
            msvcrt = _load_msvcrt()
        except ImportError:
            return None
        return windows_slash_select(
            items,
            msvcrt,
            use_ansi=terminal_supports_ansi(sys.stdout),
            title=title,
        )
    if not terminal_supports_ansi(sys.stdout) or termios is None or tty is None:
        return None
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except (AttributeError, OSError, ValueError):
        return None
    try:
        tty.setraw(fd)
    except (OSError, ValueError):
        return None
    try:
        return slash_select(items, title=title)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old)
        except (OSError, ValueError):
            pass


def ask_user_choice(question: dict) -> dict[str, str] | None:
    text = str(question.get("question") or "").strip()
    options = question.get("options") if isinstance(question.get("options"), list) else []
    allow_manual = bool(question.get("allow_manual", True))
    placeholder = str(question.get("placeholder") or "手动输入").strip()
    if text:
        print(color("? ", YELLOW + BOLD) + color(text, WHITE))
    rows: list[tuple[str, str]] = []
    value_by_label: dict[str, dict[str, str]] = {}
    for option in options:
        if not isinstance(option, dict):
            continue
        label_text = str(option.get("label") or option.get("value") or "").strip()
        if not label_text:
            continue
        value = str(option.get("value") or label_text)
        description = str(option.get("description") or "")
        rows.append((label_text, description))
        value_by_label[label_text] = {"answer": value, "label": label_text}
    manual_label = placeholder or "手动输入"
    if allow_manual:
        rows.append((manual_label, "输入自定义答案"))
    selected = choose_palette(rows, title="question") if rows else None
    if selected and selected != manual_label:
        return value_by_label.get(selected, {"answer": selected, "label": selected})
    if selected is None and rows and not allow_manual:
        return None
    answer = input(f"{manual_label}> ").strip()
    if not answer:
        return None
    return {"answer": answer, "label": manual_label, "manual": "true"}


def slash_selection_insertion(selection: str) -> str | None:
    if selection == "/goal <text>":
        return "/goal "
    if selection.startswith("/") and selection.endswith(" "):
        return selection
    if selection.startswith("/skill "):
        name = selection.split(maxsplit=1)[1].strip()
        if name:
            return f"Use skill {name}: "
    return None


def windows_slash_select(
    items: list[tuple[str, str]],
    msvcrt,
    *,
    use_ansi: bool,
    previous_width: int = 0,
    title: str = "commands",
    on_query: Callable[[str], None] | None = None,
) -> str | None:
    query = ""
    selected = 0
    plain_width = previous_width
    last_lines = 0
    if use_ansi:
        enter_palette_screen()
    try:
        while True:
            filtered = filter_slash_items(items, query)
            if selected >= len(filtered):
                selected = max(len(filtered) - 1, 0)
            if on_query is not None:
                on_query(query)
            if use_ansi:
                last_lines = draw_slash_select(
                    filtered,
                    query,
                    selected,
                    last_lines,
                    title=title,
                    show_query=on_query is None,
                )
            else:
                plain_width = draw_plain_slash_select(filtered, query, selected, plain_width, title=title)

            try:
                key = read_windows_console_key(msvcrt)
            except KeyboardInterrupt:
                key = "\x03"
            if key in {"\r", "\n"}:
                return filtered[selected][0] if filtered else None
            if key == "<UP>":
                selected = (selected - 1) % len(filtered) if filtered else 0
                continue
            if key == "<DOWN>":
                selected = (selected + 1) % len(filtered) if filtered else 0
                continue
            if key in {"\x1b", "\x03", "\x04", "\x1a", ""}:
                return None
            if key in {"\x08", "\x7f"}:
                if not query:
                    return None
                query = query[:-1]
                selected = 0
                continue
            if len(key) == 1 and key.isprintable():
                query += key
                selected = 0
    finally:
        if use_ansi:
            exit_palette_screen(last_lines)
        else:
            redraw_plain_line("", plain_width)


def draw_plain_slash_select(
    items: list[tuple[str, str]],
    query: str,
    selected: int,
    previous_width: int = 0,
    *,
    title: str = "commands",
) -> int:
    if items:
        command, description = items[selected]
        position = f"{selected + 1}/{len(items)}"
        line = f"{title} /{query} [{position}] {command}"
        if description:
            line += f" - {description}"
    else:
        line = f"{title} /{query} [no matches]"
    return redraw_plain_line(line, previous_width)


def slash_select(
    items: list[tuple[str, str]],
    title: str = "commands",
    on_query: Callable[[str], None] | None = None,
) -> str | None:
    query = ""
    selected = 0
    last_lines = 0
    fd = sys.stdin.fileno()
    enter_palette_screen()
    try:
        while True:
            filtered = filter_slash_items(items, query)
            if selected >= len(filtered):
                selected = 0
            if on_query is not None:
                on_query(query)
            last_lines = draw_slash_select(
                filtered,
                query,
                selected,
                last_lines,
                title=title,
                show_query=on_query is None,
            )
            char = read_raw_char(fd)
            if char in {"\r", "\n"}:
                if not filtered:
                    return None
                command = filtered[selected][0]
                if command.startswith("/mode "):
                    return command
                if command.startswith("/think "):
                    return command
                if command in {"/models", "/doctor", "/skills", "/exit"}:
                    return command
                return command
            if char == "\x1b":
                next_chars = read_escape_suffix(fd)
                if next_chars in {"[A", "OA"}:
                    selected = (selected - 1) % len(filtered) if filtered else 0
                    continue
                if next_chars in {"[B", "OB"}:
                    selected = (selected + 1) % len(filtered) if filtered else 0
                    continue
                if next_chars.startswith(("[", "O")):
                    continue
                return None
            if char in {"\x7f", "\b"}:
                if not query:
                    return None
                query = query[:-1]
                selected = 0
                continue
            if char in {"\x03", "\x04"}:
                return None
            if char.isprintable():
                query += char
                selected = 0
    finally:
        exit_palette_screen(last_lines)


def filter_slash_items(items: list[tuple[str, str]], query: str) -> list[tuple[str, str]]:
    needle = " ".join(query.casefold().lstrip("/").split())
    if not needle:
        return items

    query_tokens = _slash_search_tokens(needle)
    ranked: list[tuple[tuple[int, int, int, int], tuple[str, str]]] = []
    for original_index, item in enumerate(items):
        command = " ".join(item[0].casefold().lstrip("/").split())
        description = item[1].casefold()
        command_tokens = _slash_search_tokens(command)
        if command == needle:
            rank = (0, 0, 0, original_index)
        else:
            token_start = _token_prefix_start(command_tokens, query_tokens)
            if token_start is not None:
                rank = (1, token_start, 0, original_index)
            elif needle in command:
                rank = (2, command.find(needle), 0, original_index)
            elif needle in description:
                rank = (3, description.find(needle), 0, original_index)
            else:
                continue
        ranked.append((rank, item))
    ranked.sort(key=lambda value: value[0])
    return [item for _rank, item in ranked]


def _slash_search_tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[\s/_-]+", value) if token]


def _token_prefix_start(command_tokens: list[str], query_tokens: list[str]) -> int | None:
    if not query_tokens or len(query_tokens) > len(command_tokens):
        return None
    limit = len(command_tokens) - len(query_tokens) + 1
    for start in range(limit):
        if all(command_tokens[start + offset].startswith(token) for offset, token in enumerate(query_tokens)):
            return start
    return None


def draw_slash_select(
    items: list[tuple[str, str]],
    query: str,
    selected: int,
    previous_lines: int = 0,
    title: str = "commands",
    *,
    show_query: bool = True,
) -> int:
    width, height = shutil.get_terminal_size((88, 24))
    width = min(max(width - 1, 1), 96)
    height = max(height, 1)
    selected = min(max(selected, 0), max(len(items) - 1, 0))
    if height <= 3:
        if items:
            command, description = items[selected]
            position = f"{selected + 1}/{len(items)}"
            compact = f" {title} /{query} [{position}] {command}"
            if description:
                compact += " - " + description
        else:
            compact = f" {title} /{query} [0/0] no matches"
        lines = [color(clip_visible(compact, width), GRAY)]
        clear_slash_select(previous_lines)
        sys.stdout.write("\r\n\033[2K" + lines[0] + "\033[1A\r")
        sys.stdout.flush()
        return 1

    header_lines = 1 if show_query else 0
    window_size = min(8, max(height - header_lines, 1))
    start = selected_window_start(len(items), selected, window_size)
    visible = items[start : start + window_size]
    local_selected = selected - start
    lines: list[str] = []
    if show_query:
        filter_text = f"  /{query}" if query else ""
        lines.append(color(clip_visible(f"  {title}{filter_text}", width), GRAY))
    maximum_command_width = max(width - 5, 0)
    command_width = min(
        max(max((display_width(item[0]) for item in visible), default=0), min(12, maximum_command_width)),
        22,
        maximum_command_width,
    )
    for index, (command, description) in enumerate(visible):
        desc_width = max(width - command_width - 3, 0)
        command_part = pad_display(command, command_width)
        description_part = clip_visible(description, desc_width) if desc_width else ""
        if index == local_selected:
            line = "  " + color(command_part, BRIGHT_CYAN + BOLD)
            if description_part:
                line += " " + color(description_part, BRIGHT_CYAN)
        else:
            line = "  " + color(command_part, WHITE)
            if description_part:
                line += " " + color(description_part, GRAY)
        lines.append(line)
    if not visible:
        lines.append(color("  no matches", GRAY))
    clear_slash_select(previous_lines)
    for line in lines:
        sys.stdout.write("\r\n\033[2K" + line)
    if lines:
        sys.stdout.write(f"\033[{len(lines)}A\r")
    sys.stdout.flush()
    return len(lines)


def palette_footer_text() -> str:
    return "enter: run/insert | up/down: select | esc/backspace/ctrl-c/ctrl-d: cancel"


def selected_window_start(total: int, selected: int, window_size: int) -> int:
    if total <= window_size:
        return 0
    return min(max(selected - window_size + 1, 0), total - window_size)


def clear_slash_select(lines: int) -> None:
    if not lines:
        return
    sys.stdout.write("\r")
    for _ in range(lines):
        sys.stdout.write("\033[1B\r\033[2K")
    sys.stdout.write(f"\033[{lines}A\r")
    sys.stdout.flush()


def enter_palette_screen() -> None:
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()


def exit_palette_screen(lines: int = 0) -> None:
    clear_slash_select(lines)
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


def read_raw_char(fd: int) -> str:
    first = os.read(fd, 1)
    if not first:
        return ""
    lead = first[0]
    if lead < 0x80:
        return first.decode("utf-8", errors="ignore")
    if 0xC0 <= lead < 0xE0:
        needed = 2
    elif 0xE0 <= lead < 0xF0:
        needed = 3
    elif 0xF0 <= lead < 0xF8:
        needed = 4
    else:
        return ""
    data = bytearray(first)
    while len(data) < needed:
        chunk = os.read(fd, needed - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data).decode("utf-8", errors="replace")


def read_bracketed_paste(fd: int, buffer: list[str]) -> None:
    tail = ""
    while True:
        char = read_raw_char(fd)
        if not char:
            return
        tail += char
        if tail.endswith("\x1b[201~"):
            payload = tail[: -len("\x1b[201~")]
            buffer.extend(payload)
            return
        if len(tail) > len("\x1b[201~"):
            buffer.append(tail[0])
            tail = tail[1:]


def should_submit_newline(fd: int) -> bool:
    return not wait_for_fd_input(fd, 0.015)


def read_escape_suffix(fd: int) -> str:
    chars: list[str] = []
    for timeout in (0.04, 0.02, 0.02, 0.02):
        if not wait_for_fd_input(fd, timeout):
            break
        chars.append(read_raw_char(fd))
        if "".join(chars) in {"[A", "[B", "[C", "[D", "OA", "OB", "OC", "OD"}:
            break
    return "".join(chars)


def wait_for_fd_input(fd: int, timeout: float) -> bool:
    """Wait for terminal/pipe input on both POSIX and Windows file descriptors."""
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
        return bool(ready)
    except (OSError, ValueError):
        if os.name != "nt":
            raise
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if windows_fd_has_input(fd):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.005, remaining))


def windows_fd_has_input(fd: int) -> bool:
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        available = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.PeekNamedPipe(
            ctypes.c_void_p(handle), None, 0, None, ctypes.byref(available), None
        )
        if ok:
            return available.value > 0
        if fd == sys.stdin.fileno():
            return bool(msvcrt.kbhit())
    except (ImportError, OSError, ValueError):
        return False
    return False


def assistant_prefix() -> str:
    if terminal_supports_ansi(sys.stdout) and not os.getenv("NO_COLOR"):
        return color("assistant", BRIGHT_MAGENTA + BOLD) + color(" › ", GRAY)
    return "assistant> "


def color(text: str, code: str) -> str:
    if not terminal_supports_ansi(sys.stdout) or os.getenv("NO_COLOR"):
        return text
    return f"{code}{text}{RESET}"


def stream_color(text: str, code: str, stream) -> str:
    if not terminal_supports_ansi(stream) or os.getenv("NO_COLOR"):
        return text
    return f"{code}{text}{RESET}"


def label(text: str) -> str:
    return color(f"{text:<10}", GRAY)


def capability(name: str, value: str) -> str:
    colors = {
        "READ": BRIGHT_CYAN,
        "WRITE": BRIGHT_MAGENTA,
        "SHELL": YELLOW,
        "NET": GREEN,
        "SKILL": BLUE,
    }
    return color(f"{name:<6}", colors.get(name, WHITE) + BOLD) + color(value.ljust(24), WHITE)


def stage_color(index: int) -> str:
    return [BRIGHT_CYAN, BRIGHT_GREEN, YELLOW, BRIGHT_MAGENTA, GREEN, BLUE][(index - 1) % 6]


def visible_len(text: str) -> int:
    return display_width(strip_ansi(text))


def clip_visible(text: str, width: int) -> str:
    plain = strip_ansi(text)
    if width <= 0:
        return ""
    if display_width(plain) <= width:
        return text
    marker = "..." if plain_terminal() else "…"
    if display_width(marker) >= width:
        return take_prefix_for_width(marker, width)
    return take_prefix_for_width(plain, width - display_width(marker)) + marker


def strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\033\[[0-?]*[ -/]*[@-~]", "", text)


def pad_ansi(text: str, width: int) -> str:
    visible = visible_len(text)
    if visible > width:
        return clip_visible(text, width)
    if visible == width:
        return text
    return text + " " * (width - visible)
