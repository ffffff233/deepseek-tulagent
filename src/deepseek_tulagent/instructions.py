from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


INSTRUCTION_CONTEXT_PREFIX = '<runtime-context kind="instructions" version="1">'
INSTRUCTION_CONTEXT_SUFFIX = "</runtime-context>"
INSTRUCTION_NAMES = ("REASONIX.md", "AGENTS.md", "CLAUDE.md")
LOCAL_INSTRUCTION_NAMES = ("REASONIX.local.md", "AGENTS.local.md", "CLAUDE.local.md")
MAX_INSTRUCTION_FILE_BYTES = 48_000
MAX_INSTRUCTION_TOTAL_BYTES = 96_000


@dataclass(frozen=True)
class InstructionDocument:
    path: Path
    scope: str
    body: str
    source_bytes: int
    truncated: bool = False


@dataclass(frozen=True)
class InstructionContext:
    documents: list[InstructionDocument]
    prompt: str
    source_bytes: int
    truncated: bool


class InstructionStore:
    def __init__(
        self,
        workspace: Path,
        home: Path | None = None,
        *,
        extra_files: Iterable[Path] = (),
    ):
        self.workspace = workspace.resolve()
        self.home = (home or Path.home()).resolve()
        self.extra_files = tuple(Path(path).expanduser().resolve() for path in extra_files)

    def load(self) -> InstructionContext:
        documents: list[InstructionDocument] = []
        seen_files: set[tuple[int, int] | str] = set()
        remaining = MAX_INSTRUCTION_TOTAL_BYTES
        truncated = False

        for path, scope in self.candidates():
            if remaining <= 0:
                truncated = True
                break
            document = self._read(path, scope, remaining)
            if document is None:
                continue
            identity = file_identity(path)
            if identity in seen_files:
                continue
            seen_files.add(identity)
            documents.append(document)
            remaining -= len(document.body.encode("utf-8"))
            truncated = truncated or document.truncated

        prompt = render_instruction_context(documents, self.workspace, self.home)
        return InstructionContext(
            documents=documents,
            prompt=prompt,
            source_bytes=sum(document.source_bytes for document in documents),
            truncated=truncated,
        )

    def candidates(self) -> list[tuple[Path, str]]:
        # Plugin instructions are defaults. User/project/local files appear later and
        # therefore retain the documented precedence over plugin-provided guidance.
        candidates: list[tuple[Path, str]] = [(path, "plugin") for path in self.extra_files]
        user_root = self.home / ".deepseek-tulagent"
        candidates.extend((user_root / name, "user") for name in INSTRUCTION_NAMES)

        chain = project_chain(self.workspace)
        for directory in chain:
            scope = "project" if directory == self.workspace else "ancestor"
            candidates.extend((directory / name, scope) for name in INSTRUCTION_NAMES)
        candidates.extend((self.workspace / name, "local") for name in LOCAL_INSTRUCTION_NAMES)
        return candidates

    @staticmethod
    def _read(path: Path, scope: str, remaining: int) -> InstructionDocument | None:
        try:
            if not path.is_file():
                return None
            limit = min(MAX_INSTRUCTION_FILE_BYTES, remaining)
            with path.open("rb") as handle:
                raw = handle.read(limit + 1)
        except OSError:
            return None
        if not raw:
            return None
        clipped = raw[:limit]
        body = clipped.decode("utf-8-sig", errors="replace").strip()
        if not body:
            return None
        was_truncated = len(raw) > limit
        if was_truncated:
            body += "\n\n[Instruction file truncated at the runtime safety limit.]"
        return InstructionDocument(
            path=path.resolve(),
            scope=scope,
            body=body,
            source_bytes=min(len(raw), limit),
            truncated=was_truncated,
        )


def project_chain(workspace: Path) -> list[Path]:
    root = nearest_git_root(workspace)
    if root is None:
        return [workspace]
    chain: list[Path] = []
    current = workspace
    while True:
        chain.append(current)
        if current == root:
            break
        parent = current.parent
        if parent == current:
            return [workspace]
        current = parent
    chain.reverse()
    return chain


def nearest_git_root(path: Path) -> Path | None:
    current = path.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def file_identity(path: Path) -> tuple[int, int] | str:
    try:
        stat = path.stat()
        if stat.st_ino:
            return stat.st_dev, stat.st_ino
    except OSError:
        pass
    return str(path.resolve()).casefold()


def render_instruction_context(documents: list[InstructionDocument], workspace: Path, home: Path) -> str:
    if not documents:
        return ""
    lines = [
        INSTRUCTION_CONTEXT_PREFIX,
        "# Project instructions",
        "",
        "User-authored guidance discovered for this workspace. Later, more local documents take precedence.",
    ]
    for document in documents:
        lines.extend(("", f"## {display_instruction_path(document.path, workspace, home)} ({document.scope})", "", document.body))
    lines.extend(("", INSTRUCTION_CONTEXT_SUFFIX))
    return "\n".join(lines)


def display_instruction_path(path: Path, workspace: Path, home: Path) -> str:
    try:
        relative = path.relative_to(workspace)
        return "<workspace>/" + relative.as_posix()
    except ValueError:
        pass
    try:
        relative = path.relative_to(home)
        return "~/" + relative.as_posix()
    except ValueError:
        return "<ancestor>/" + path.name
