from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from .ui import display_width, pad_display, stream_is_tty, tail_for_width, take_prefix_for_width

try:
    import curses
except ImportError:  # Windows stdlib does not ship curses.
    curses = None  # type: ignore[assignment]


class TuiUnavailableError(RuntimeError):
    pass


@dataclass
class TuiState:
    model: str
    mode: str
    thinking: str
    session_id: str | None = None
    messages: list[tuple[str, str]] = field(default_factory=list)
    input_text: str = ""
    status: str = "ready"


class ChatTui:
    def __init__(self, state: TuiState, on_submit: Callable[[str, TuiState], None], on_command: Callable[[str, TuiState], bool]):
        self.state = state
        self.on_submit = on_submit
        self.on_command = on_command
        self._colors = False

    def run(self) -> None:
        if curses is None:
            raise TuiUnavailableError("curses TUI is unavailable on this platform")
        if not stream_is_tty(sys.stdin) or not stream_is_tty(sys.stdout):
            raise TuiUnavailableError("curses TUI requires an interactive terminal")
        try:
            curses.wrapper(self._main)
        except curses.error as exc:
            raise TuiUnavailableError(f"curses TUI could not start: {exc}") from exc

    def _main(self, stdscr) -> None:
        if curses is None:
            raise RuntimeError("curses TUI is unavailable on this platform")
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            stdscr.keypad(True)
        except curses.error:
            pass
        self._init_colors()
        while True:
            self._draw(stdscr)
            key = stdscr.get_wch() if hasattr(stdscr, "get_wch") else stdscr.getch()
            if self._handle_key(key):
                return

    def _init_colors(self) -> None:
        if curses is None:
            return
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            self._colors = True
        except curses.error:
            self._colors = False

    def _color(self, pair: int) -> int:
        if curses is None or not self._colors:
            return 0
        try:
            return curses.color_pair(pair)
        except curses.error:
            return 0

    def _handle_key(self, key: int | str) -> bool:
        if key in (4, "\x04"):
            self.state.status = "exit"
            return True
        if key in (3, "\x03"):
            if self.state.status in {"thinking", "running", "executing"}:
                self.state.input_text = ""
                self.state.status = "cancelled"
                return False
            self.state.status = "exit"
            return True
        enter_key = curses.KEY_ENTER if curses is not None else 10
        backspace_key = curses.KEY_BACKSPACE if curses is not None else 127
        if key in (enter_key, 10, 13, "\n", "\r"):
            text = self.state.input_text.strip()
            self.state.input_text = ""
            if not text:
                return False
            if text in {"/exit", "/quit"}:
                self.state.status = "exit"
                return True
            if text.startswith("/"):
                return self.on_command(text, self.state)
            try:
                self.on_submit(text, self.state)
            except KeyboardInterrupt:
                self.state.status = "cancelled"
            return False
        if key in (backspace_key, 127, 8, "\x7f", "\b"):
            self.state.input_text = self.state.input_text[:-1]
            return False
        if isinstance(key, str) and key.isprintable():
            self.state.input_text += key
        elif isinstance(key, int) and 0 <= key < 256 and chr(key).isprintable():
            self.state.input_text += chr(key)
        return False

    def _draw(self, stdscr) -> None:
        if curses is None:
            raise RuntimeError("curses TUI is unavailable on this platform")
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height <= 0 or width <= 1:
            stdscr.refresh()
            return
        usable_width = width - 1
        if height >= 3:
            title_attr = self._color(1) | curses.A_BOLD
            safe_addnstr(stdscr, 0, 0, "DeepSeekFathom", usable_width, title_attr)
            body_top = 1
            composer_y = height - 2
            body_height = max(composer_y - body_top, 0)
            visible = render_messages(self.state.messages, usable_width)[-body_height:] if body_height else []
            for index, line in enumerate(visible):
                safe_addnstr(stdscr, body_top + index, 0, line, usable_width)
        else:
            composer_y = 0

        prompt = "> "
        input_width = max(usable_width - display_width(prompt) - 1, 0)
        visible_input = tail_for_width(self.state.input_text, input_width)
        composer = prompt + visible_input
        safe_addnstr(stdscr, composer_y, 0, composer, usable_width, self._color(1))

        if height >= 2:
            status_y = height - 1
            status = f"{self.state.model} | {self.state.mode}/{self.state.thinking} | {self.state.status}"
            if self.state.session_id:
                status += f" | {self.state.session_id[:8]}"
            safe_addnstr(stdscr, status_y, 0, pad_display(status, usable_width), usable_width, self._color(2))
        cursor_x = min(display_width(composer), max(usable_width - 1, 0))
        safe_move(stdscr, composer_y, cursor_x)
        stdscr.refresh()


def render_messages(messages: list[tuple[str, str]], width: int) -> list[str]:
    if width <= 0:
        return []
    lines: list[str] = []
    for role, content in messages:
        label = {"user": "You", "assistant": "AI", "tool": "Tool", "system": "Info"}.get(role, role)
        prefix = f"{label}: "
        prefix_width = display_width(prefix)
        if prefix_width >= width:
            lines.append(take_prefix_for_width(prefix, width))
            lines.extend(wrap_display_text(content or "", width))
        else:
            content_width = width - prefix_width
            wrapped = wrap_display_text(content or "", content_width) or [""]
            lines.append(prefix + wrapped[0])
            continuation = " " * prefix_width
            lines.extend(continuation + line for line in wrapped[1:])
        lines.append("")
    return lines or wrap_display_text("Type a message. / opens commands.", width)


def wrap_display_text(text: str, width: int) -> list[str]:
    if width <= 0:
        return []
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for char in text.expandtabs(4).replace("\r\n", "\n").replace("\r", "\n"):
        if char == "\n":
            lines.append("".join(current))
            current = []
            used = 0
            continue
        char_width = display_width(char)
        if char_width > width:
            continue
        if current and used + char_width > width:
            lines.append("".join(current))
            current = []
            used = 0
        current.append(char)
        used += char_width
    if current or not lines:
        lines.append("".join(current))
    return lines


def safe_addnstr(stdscr, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
    try:
        height, width = stdscr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width - 1:
            return
        limit = max(0, min(n, width - x - 1))
        if limit <= 0:
            return
        clipped = take_prefix_for_width(text, limit)
        if not clipped:
            return
        if attr:
            stdscr.addnstr(y, x, clipped, len(clipped), attr)
        else:
            stdscr.addnstr(y, x, clipped, len(clipped))
    except Exception:
        return


def safe_move(stdscr, y: int, x: int) -> None:
    try:
        height, width = stdscr.getmaxyx()
        if height <= 0 or width <= 1:
            return
        stdscr.move(max(0, min(y, height - 1)), max(0, min(x, width - 2)))
    except Exception:
        return
