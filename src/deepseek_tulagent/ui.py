from __future__ import annotations

import sys
import time
import shutil
import os
import select
import subprocess
import termios
import tty
import atexit
from threading import Event, Thread
from collections.abc import Callable


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


def install_terminal_safety() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    force_terminal_sane()
    atexit.register(force_terminal_sane)


def force_terminal_sane() -> None:
    if sys.stdout.isatty():
        try:
            sys.stdout.write("\033[?25h\033[0m\033[?1049l")
            sys.stdout.flush()
        except OSError:
            pass
    if sys.stdin.isatty():
        try:
            subprocess.run(["stty", "sane"], stdin=sys.stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        except Exception:
            pass


def startup_animation(enabled: bool = True) -> None:
    if not enabled or not sys.stdout.isatty():
        print("DeepSeek TuLAgent")
        return
    width = min(shutil.get_terminal_size((88, 24)).columns, 96)
    stages = [
        ("kernel", "load terminal runtime"),
        ("model", "bind DeepSeek flash route"),
        ("tools", "mount shell, files, patch, services"),
        ("skills", "scan local SKILL.md directories"),
        ("policy", "apply root approval profile"),
        ("session", "open conversation ledger"),
    ]
    capabilities = [
        ("READ", "files/search/git"),
        ("WRITE", "patch/create/edit"),
        ("SHELL", "commands/services"),
        ("NET", "download/model API"),
        ("SKILL", "local workflows"),
    ]

    print("\033[2J\033[H", end="")
    print(color("╭" + "─" * (width - 2) + "╮", BRIGHT_CYAN))
    print(center_line(color("DEEPSEEK", BRIGHT_CYAN + BOLD) + " " + color("TuLAGENT", BRIGHT_MAGENTA + BOLD), width))
    print(center_line(color("root fast boot", GREEN) + color(" · ", GRAY) + color("terminal coding cockpit", WHITE), width))
    print(color("├" + "─" * (width - 2) + "┤", BRIGHT_CYAN))
    for index, (name, detail) in enumerate(stages, 1):
        bar_width = max(width - 36, 12)
        filled = int(bar_width * index / len(stages))
        bar = color("█" * filled, stage_color(index)) + color("░" * (bar_width - filled), GRAY)
        line = " " + color(f"{name:<8}", stage_color(index) + BOLD) + " " + bar + " " + color(detail, DIM + WHITE)
        print(color("│", BRIGHT_CYAN) + pad_ansi(line, width - 2) + color("│", BRIGHT_CYAN), flush=True)
        time.sleep(0.055)
    stream = " ".join(
        [
            color("model", BRIGHT_GREEN),
            color("tools", YELLOW),
            color("skills", BRIGHT_MAGENTA),
            color("cache", BRIGHT_CYAN),
            color("root", GREEN),
        ]
    )
    for offset in range(3):
        tracer = color("  signal ", GRAY) + color("═" * (8 + offset * 4) + "▶ ", CYAN) + stream
        print(color("│", BRIGHT_CYAN) + pad_ansi(tracer, width - 2) + color("│", BRIGHT_CYAN), flush=True)
        time.sleep(0.045)
    print(color("├" + "─" * (width - 2) + "┤", BRIGHT_CYAN))
    print(center_line(color("capability matrix", YELLOW + BOLD), width))
    for left, right in zip(capabilities[::2], capabilities[1::2] + [("", "")]):
        left_text = capability(left[0], left[1]) if left[0] else ""
        right_text = capability(right[0], right[1]) if right[0] else ""
        print(color("│ ", BRIGHT_CYAN) + pad_ansi(f"{left_text}  {right_text}", width - 4) + color(" │", BRIGHT_CYAN))
    print(center_line(color("press ", GRAY) + color("/", YELLOW + BOLD) + color(" inside chat for commands and skills", GRAY), width))
    print(color("╰" + "─" * (width - 2) + "╯", BRIGHT_CYAN))
    time.sleep(0.12)


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
        if not sys.stderr.isatty():
            return self
        ThinkingSpinner.active = self
        self.thread = Thread(target=self._spin, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=0.2)
        if ThinkingSpinner.active is self:
            ThinkingSpinner.active = None
        self.clear_line()

    def clear_line(self) -> None:
        if sys.stderr.isatty():
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
    if text.startswith("tool "):
        rest = text.removeprefix("tool ").strip()
        name, _, args = rest.partition(" ")
        return (
            color("  ╭─", CYAN)
            + color(" tool ", YELLOW + BOLD)
            + color(name, BRIGHT_CYAN + BOLD)
            + (color(" · ", GRAY) + color(args, WHITE) if args else "")
        )
    if text.startswith("done "):
        name = text.removeprefix("done ").strip()
        return color("  ╰─", CYAN) + color(" done ", GREEN + BOLD) + color(name, BRIGHT_GREEN)
    if text.startswith("subagent "):
        return color("  ◆ ", BRIGHT_MAGENTA) + color(text, BRIGHT_CYAN)
    if text.startswith("thinking pass "):
        return color("  ◇ ", BRIGHT_MAGENTA) + color(text, GRAY)
    if text.startswith("context compacted"):
        return color("  ◈ ", YELLOW) + color(text, GRAY)
    return color("  • ", CYAN) + color(text, GRAY)


def print_slash_palette(commands: list[tuple[str, str]], skills: list[tuple[str, str]]) -> None:
    print_box("Command Palette", [f"{name:<18} {description}" for name, description in commands])
    skill_lines = [f"/skill {name:<11} {description}" for name, description in skills] or ["none discovered"]
    print_box("Skills", skill_lines)


def print_tool_palette(tools: dict[str, str]) -> None:
    print_box("Tools", [f"{name:<16} {description}" for name, description in tools.items()])


def print_box(title: str, lines: list[str]) -> None:
    width = min(shutil.get_terminal_size((88, 24)).columns, 96)
    print(color("╭─ ", CYAN) + color(title, BOLD + WHITE) + color(" " + "─" * max(width - visible_len(title) - 5, 0) + "╮", CYAN))
    for line in lines:
        clipped = strip_ansi(line)[: max(width - 4, 1)] if visible_len(line) > width - 4 else line
        print(color("│ ", CYAN) + pad_ansi(clipped, width - 4) + color(" │", CYAN))
    print(color("╰" + "─" * (width - 2) + "╯", CYAN))


def center_line(text: str, width: int) -> str:
    inner = width - 2
    visible = visible_len(text)
    if visible > inner:
        text = strip_ansi(text)[:inner]
        visible = visible_len(text)
    left = (inner - visible) // 2
    right = inner - visible - left
    return color("│", BRIGHT_CYAN) + " " * left + text + " " * right + color("│", BRIGHT_CYAN)


def print_header(workspace: str, endpoint: str, model: str, mode: str, thinking: str, approval: str) -> None:
    lines = [
        label("workspace") + workspace,
        label("endpoint") + color(endpoint, BRIGHT_CYAN),
        label("model") + color(model, BRIGHT_GREEN if "flash" in model else BRIGHT_MAGENTA),
        label("mode") + color(mode, YELLOW if mode == "root" else GREEN),
        label("thinking") + color(thinking, BRIGHT_MAGENTA),
        label("approval") + color(approval, GREEN if approval == "all yes" else YELLOW),
    ]
    print_box("Session", lines)


def status_bar(model: str, mode: str, thinking: str, session_id: str | None = None) -> str:
    suffix = f" session={session_id}" if session_id else ""
    return (
        color("[model ", GRAY) + color(model, BRIGHT_GREEN if "flash" in model else BRIGHT_MAGENTA) + color("] ", GRAY)
        + color("[mode ", GRAY) + color(mode, YELLOW if mode == "root" else GREEN) + color("] ", GRAY)
        + color("[think ", GRAY) + color(thinking, BRIGHT_MAGENTA) + color("]", GRAY)
        + color(suffix, GRAY)
    )


def composer_prompt(model: str, mode: str, thinking: str, session_id: str | None = None) -> str:
    session = f" · {session_id[:8]}" if session_id else ""
    if sys.stdout.isatty() and not os.getenv("NO_COLOR"):
        return (
            color("▌ ", CYAN + BOLD)
            + color(f"{model}", BRIGHT_GREEN if "flash" in model else BRIGHT_MAGENTA)
            + color(f" {mode}/{thinking}{session}", GRAY)
            + color(" › ", CYAN + BOLD)
        )
    return f"[{model} {mode} {thinking}{session}] > "


def read_composer(prompt: str, slash_items: list[tuple[str, str]] | None = None) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(prompt)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buffer: list[str] = []
    try:
        tty.setraw(fd)
        sys.stdout.write("\033[?2004h")
        redraw_composer(prompt, buffer)
        while True:
            char = read_raw_char(fd)
            if char == "\x1b":
                suffix = read_escape_suffix(fd)
                if suffix == "[200~":
                    read_bracketed_paste(fd, buffer)
                    redraw_composer(prompt, buffer)
                continue
            if char in {"\r", "\n"}:
                if buffer and not should_submit_newline(fd):
                    buffer.append("\n")
                    redraw_composer(prompt, buffer)
                    continue
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "".join(buffer)
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x04":
                raise EOFError
            if char in {"\x7f", "\b"}:
                if buffer:
                    buffer.pop()
                    redraw_composer(prompt, buffer)
                continue
            if char == "/" and not buffer and slash_items:
                selected = slash_select(slash_items)
                if selected:
                    insertion = slash_selection_insertion(selected)
                    if insertion is not None:
                        buffer.extend(insertion)
                        redraw_composer(prompt, buffer)
                        continue
                    sys.stdout.write(prompt + selected + "\r\n")
                    sys.stdout.flush()
                    return selected
                redraw_composer(prompt, buffer)
                continue
            if char.isprintable():
                buffer.append(char)
                redraw_composer(prompt, buffer)
                continue
    finally:
        sys.stdout.write("\033[?2004l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSANOW, old)


def redraw_composer(prompt: str, buffer: list[str]) -> None:
    width = max(shutil.get_terminal_size((88, 24)).columns, 20)
    available = max(width - visible_len(prompt) - 1, 4)
    text = "".join(buffer)
    display = tail_for_width(text, available)
    sys.stdout.write("\033[?25h\r\033[2K")
    sys.stdout.write(prompt + display)
    sys.stdout.flush()


def tail_for_width(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    prefix = "…"
    remaining = max(width - display_width(prefix), 1)
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
    if (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE10 <= code <= 0xFE19
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
    ):
        return 2
    return 1


def choose_palette(items: list[tuple[str, str]], title: str = "commands") -> str | None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return slash_select(items, title=title)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)


def slash_selection_insertion(selection: str) -> str | None:
    if selection.startswith("/") and selection.endswith(" "):
        return selection
    if selection.startswith("/skill "):
        name = selection.split(maxsplit=1)[1].strip()
        if name:
            return f"Use skill {name}: "
    return None


def slash_select(items: list[tuple[str, str]], title: str = "commands") -> str | None:
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
            last_lines = draw_slash_select(filtered, query, selected, last_lines, title=title)
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
                    selected = max(0, selected - 1)
                    continue
                if next_chars in {"[B", "OB"}:
                    selected = min(max(len(filtered) - 1, 0), selected + 1)
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
            if char == "\x03":
                raise KeyboardInterrupt
            if char in {"k", "K"} and not query:
                selected = max(0, selected - 1)
                continue
            if char in {"j", "J"} and not query:
                selected = min(max(len(filtered) - 1, 0), selected + 1)
                continue
            if char.isprintable():
                query += char
                selected = 0
    finally:
        exit_palette_screen()


def filter_slash_items(items: list[tuple[str, str]], query: str) -> list[tuple[str, str]]:
    if not query:
        return items
    lowered = query.lower()
    command_matches = [
        item for item in items
        if item[0].lstrip("/").lower().startswith(lowered)
        or item[0].lower().startswith("/" + lowered)
    ]
    description_matches = [
        item for item in items
        if item not in command_matches and lowered in item[1].lower()
    ]
    return command_matches + description_matches


def draw_slash_select(items: list[tuple[str, str]], query: str, selected: int, previous_lines: int = 0, title: str = "commands") -> int:
    width, height = shutil.get_terminal_size((88, 24))
    width = max(width, 12)
    height = max(height, 12)
    inner_width = width
    window_size = 6
    start = selected_window_start(len(items), selected, window_size)
    visible = items[start : start + window_size]
    local_selected = selected - start
    total_lines = 3 + max(len(visible), 1)
    top = max((height - total_lines) // 4, 1)
    sys.stdout.write("\033[H\033[2J")
    sys.stdout.write("\r\n" * top)
    title_text = f"{title} /{query}" if query else title
    sys.stdout.write(color(clip_visible(title_text, inner_width), BOLD + WHITE) + "\r\n")
    command_width = min(max(max((len(item[0]) for item in visible), default=8), 12), 22)
    for index, (command, description) in enumerate(visible):
        marker = ">" if index == local_selected else " "
        desc_width = max(inner_width - command_width - 3, 8)
        desc = clip_visible(description, desc_width)
        line = f"{marker} {command:<{command_width}} {desc}"
        line = clip_visible(line, inner_width)
        if index == selected:
            line = color(line, BOLD + WHITE)
        else:
            line = color(line, GRAY)
        sys.stdout.write(line + "\r\n")
    if not visible:
        sys.stdout.write(color("no matches", GRAY) + "\r\n")
    footer = clip_visible("enter: run | up/down or j/k: select | esc/backspace: cancel", inner_width)
    sys.stdout.write(color(footer, GRAY) + "\r\n")
    sys.stdout.flush()
    return total_lines


def selected_window_start(total: int, selected: int, window_size: int) -> int:
    if total <= window_size:
        return 0
    return min(max(selected - window_size + 1, 0), total - window_size)


def clear_slash_select(lines: int) -> None:
    if not lines:
        return
    sys.stdout.write(f"\033[{lines}F")
    for _ in range(lines):
        sys.stdout.write("\033[2K\r\033[1E")
    sys.stdout.write(f"\033[{lines}F")
    sys.stdout.flush()


def enter_palette_screen() -> None:
    sys.stdout.write("\033[?1049h\033[?25l\033[H\033[2J")
    sys.stdout.flush()


def exit_palette_screen() -> None:
    sys.stdout.write("\033[?25h\033[?1049l")
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
        data.extend(os.read(fd, needed - len(data)))
    return bytes(data).decode("utf-8", errors="ignore")


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
    ready, _, _ = select.select([fd], [], [], 0.015)
    return not ready


def read_escape_suffix(fd: int) -> str:
    chars: list[str] = []
    for timeout in (0.8, 0.25, 0.08, 0.03):
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            break
        chars.append(read_raw_char(fd))
        if "".join(chars) in {"[A", "[B", "[C", "[D", "OA", "OB", "OC", "OD"}:
            break
    return "".join(chars)


def assistant_prefix() -> str:
    if sys.stdout.isatty() and not os.getenv("NO_COLOR"):
        return color("assistant", BRIGHT_MAGENTA + BOLD) + color(" › ", GRAY)
    return "assistant> "


def color(text: str, code: str) -> str:
    if not sys.stdout.isatty() or os.getenv("NO_COLOR"):
        return text
    return f"{code}{text}{RESET}"


def stream_color(text: str, code: str, stream) -> str:
    if not stream.isatty() or os.getenv("NO_COLOR"):
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
    return len(strip_ansi(text))


def clip_visible(text: str, width: int) -> str:
    plain = strip_ansi(text)
    if len(plain) <= width:
        return text
    if width <= 1:
        return plain[:width]
    return plain[: width - 1] + "…"


def strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\033\[[0-9;]*m", "", text)


def pad_ansi(text: str, width: int) -> str:
    visible = visible_len(text)
    if visible >= width:
        return text
    return text + " " * (width - visible)
