from __future__ import annotations

import curses
import textwrap
from dataclasses import dataclass, field
from typing import Callable


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

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, stdscr) -> None:
        curses.curs_set(1)
        stdscr.keypad(True)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_MAGENTA, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)
        while True:
            self._draw(stdscr)
            key = stdscr.getch()
            if self._handle_key(key):
                return

    def _handle_key(self, key: int) -> bool:
        if key == 4:
            self.state.status = "exit"
            return True
        if key == 3:
            if self.state.status in {"thinking", "running", "executing"}:
                self.state.input_text = ""
                self.state.status = "cancelled"
                return False
            self.state.status = "exit"
            return True
        if key in (curses.KEY_ENTER, 10, 13):
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
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.state.input_text = self.state.input_text[:-1]
            return False
        if 0 <= key < 256 and chr(key).isprintable():
            self.state.input_text += chr(key)
        return False

    def _draw(self, stdscr) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 10 or width < 40:
            safe_addnstr(stdscr, 0, 0, "Terminal too small for DeepSeek TuLAgent", max(width - 1, 1))
            stdscr.refresh()
            return
        width = max(width, 40)
        max_x = width - 1
        header_h = 3
        composer_h = 5
        status_h = 1
        body_top = header_h
        body_h = max(1, height - header_h - composer_h - status_h)

        title = " DeepSeek TuLAgent "
        stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
        safe_addnstr(stdscr, 0, 0, "╭" + "─" * (width - 3) + "╮", max_x)
        safe_addnstr(stdscr, 1, 0, "│" + title.center(width - 3) + "│", max_x)
        safe_addnstr(stdscr, 2, 0, "╰" + "─" * (width - 3) + "╯", max_x)
        stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

        body_lines = render_messages(self.state.messages, width - 4)
        visible = body_lines[-body_h:]
        for idx in range(body_h):
            y = body_top + idx
            safe_addnstr(stdscr, y, 0, "│", 1, curses.color_pair(1))
            if idx < len(visible):
                safe_addnstr(stdscr, y, 2, visible[idx], width - 5)
            safe_addnstr(stdscr, y, width - 2, "│", 1, curses.color_pair(1))

        composer_top = height - composer_h - status_h
        stdscr.attron(curses.color_pair(2))
        safe_addnstr(stdscr, composer_top, 0, "╭─ message " + "─" * max(width - 13, 0) + "╮", max_x)
        safe_addnstr(stdscr, composer_top + 1, 0, "│ " + self.state.input_text[: width - 5].ljust(width - 5) + "│", max_x)
        safe_addnstr(stdscr, composer_top + 2, 0, "│ " + "Enter send · / commands · Ctrl-D exit".ljust(width - 5) + "│", max_x)
        safe_addnstr(stdscr, composer_top + 3, 0, "╰" + "─" * (width - 3) + "╯", max_x)
        stdscr.attroff(curses.color_pair(2))

        status = f" model {self.state.model} | mode {self.state.mode} | think {self.state.thinking} | {self.state.status}"
        if self.state.session_id:
            status += f" | session {self.state.session_id[:8]}"
        safe_addnstr(stdscr, height - 1, 0, status.ljust(width - 1), max_x, curses.color_pair(5))
        safe_move(stdscr, composer_top + 1, min(2 + len(self.state.input_text), width - 3))
        stdscr.refresh()


def render_messages(messages: list[tuple[str, str]], width: int) -> list[str]:
    lines: list[str] = []
    for role, content in messages:
        prefix = f"{role}> "
        wrapped = textwrap.wrap(content or "", width=max(width - len(prefix), 10)) or [""]
        lines.append(prefix + wrapped[0])
        for extra in wrapped[1:]:
            lines.append(" " * len(prefix) + extra)
        lines.append("")
    return lines or ["Type a message. Press / for commands."]


def safe_addnstr(stdscr, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
    try:
        height, width = stdscr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width - 1:
            return
        limit = max(0, min(n, width - x - 1))
        if limit <= 0:
            return
        if attr:
            stdscr.addnstr(y, x, text, limit, attr)
        else:
            stdscr.addnstr(y, x, text, limit)
    except curses.error:
        return


def safe_move(stdscr, y: int, x: int) -> None:
    try:
        height, width = stdscr.getmaxyx()
        stdscr.move(max(0, min(y, height - 1)), max(0, min(x, width - 2)))
    except curses.error:
        return
