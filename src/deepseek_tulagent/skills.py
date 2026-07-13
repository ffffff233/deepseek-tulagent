from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


SKILL_CONTEXT_PREFIX = '<runtime-context kind="skills" version="1">'
SKILL_CONTEXT_SUFFIX = "</runtime-context>"
SKILL_PIN_PREFIX = '<skill-pin name="{name}">'
SKILL_PIN_SUFFIX = "</skill-pin>"
MAX_SKILL_BODY_CHARS = 24_000
MAX_SKILL_REFERENCE_CHARS = 16_000
MAX_SKILL_TOTAL_CHARS = 48_000
SCRIPT_EXTENSIONS = {"", ".sh", ".py", ".js", ".ts", ".rb", ".pl", ".php", ".ps1"}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path
    source: str = "user"
    scope: str = "user"
    description_declared: bool = False

    def summary(self) -> str:
        description = " ".join(self.description.split())[:180] or "(description missing)"
        return f"- {self.name}: {description}"


@dataclass(frozen=True)
class SkillRoot:
    path: Path
    scope: str
    priority: int
    status: str


@dataclass(frozen=True)
class SkillCandidate:
    skill: Skill
    status: str
    winner_path: Path | None = None


@dataclass(frozen=True)
class SkillInspection:
    roots: list[SkillRoot]
    candidates: list[SkillCandidate]


class SkillStore:
    def __init__(self, workspace: Path, home: Path | None = None):
        self.workspace = workspace.resolve()
        self.home = (home or Path.home()).resolve()

    @property
    def search_roots(self) -> list[SkillRoot]:
        candidates = [
            (self.workspace / ".deepseek-tulagent" / "skills", "project"),
            (self.workspace / ".agents" / "skills", "project"),
            (self.workspace / ".agent" / "skills", "project"),
            (self.workspace / ".claude" / "skills", "project"),
            (self.workspace / "skills", "project"),
            (self.home / ".deepseek-tulagent" / "skills", "user"),
            (self.home / ".agents" / "skills", "user"),
            (self.home / ".agent" / "skills", "user"),
            (self.home / ".claude" / "skills", "user"),
            (Path(__file__).resolve().parent / "builtin_skills", "official"),
        ]
        roots: list[SkillRoot] = []
        seen: set[Path] = set()
        for path, scope in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            roots.append(SkillRoot(resolved, scope, len(roots), skill_root_status(resolved)))
        return roots

    @property
    def search_dirs(self) -> list[Path]:
        return [root.path for root in self.search_roots]

    @property
    def writable_dir(self) -> Path:
        return self.workspace / ".deepseek-tulagent" / "skills"

    def list(self) -> list[Skill]:
        winners = [candidate.skill for candidate in self.inspect().candidates if candidate.status == "winner"]
        return sorted(winners, key=lambda skill: skill.name)

    def inspect(self) -> SkillInspection:
        roots = self.search_roots
        candidates: list[SkillCandidate] = []
        winners: dict[str, Skill] = {}
        for root in roots:
            if root.status != "ok":
                continue
            try:
                directory_skills = list(root.path.glob("*/SKILL.md"))
                flat_skills = [path for path in root.path.glob("*.md") if path.name.casefold() != "skill.md"]
                skill_files = sorted(directory_skills + flat_skills)
            except OSError:
                continue
            for skill_md in skill_files:
                skill = parse_skill(
                    skill_md,
                    source="official" if root.scope == "official" else "user",
                    scope=root.scope,
                )
                if skill_md.name.casefold() != "skill.md" and not skill.description_declared:
                    continue
                winner = winners.get(skill.name)
                if winner is None:
                    winners[skill.name] = skill
                    candidates.append(SkillCandidate(skill, "winner"))
                else:
                    candidates.append(SkillCandidate(skill, "shadowed", winner.path))
        candidates.sort(key=lambda candidate: (candidate.skill.name, 0 if candidate.status == "winner" else 1, str(candidate.skill.path)))
        return SkillInspection(roots, candidates)

    def get(self, name: str) -> Skill | None:
        requested = clean_skill_reference(name)
        for skill in self.list():
            if skill.name.casefold() == requested.casefold():
                return skill
        return None

    def search(self, query: str = "", limit: int = 100) -> list[Skill]:
        needle = query.strip().casefold()
        matches = [
            skill
            for skill in self.list()
            if not needle or needle in skill.name.casefold() or needle in skill.description.casefold()
        ]
        return matches[: max(1, min(int(limit), 200))]

    def create(self, name: str, description: str, body: str = "") -> Skill:
        safe = safe_skill_name(name)
        root = self.writable_dir / safe
        path = root / "SKILL.md"
        root.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f"name: {safe}\n"
            f"description: {description.strip()}\n"
            "---\n\n"
            f"# {safe}\n\n"
            f"{body.strip()}\n"
        )
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
        return parse_skill(path, scope="project")

    def prompt_context(self, max_chars: int = 4000) -> str:
        prompt, _, _ = self.prompt_context_info(max_chars=max_chars)
        return prompt

    def prompt_context_info(self, max_chars: int = 4000) -> tuple[str, int, bool]:
        skills = self.list()
        if not skills:
            return "", 0, False
        header = "\n".join((
            SKILL_CONTEXT_PREFIX,
            "# Skills available on demand",
            "",
            "Before non-trivial work, scan this index. When a skill is plausibly relevant, call read_skill with its bare name before acting. Use list_skills to search beyond this bounded index. Only names and descriptions are pinned here; full bodies load on demand.",
            "",
        ))
        lines: list[str] = []
        for skill in skills:
            line = skill.summary()
            projected = len(header) + len("\n".join(lines)) + len(line) + len(SKILL_CONTEXT_SUFFIX) + 3
            if projected > max_chars:
                break
            lines.append(line)
        truncated = len(lines) < len(skills)
        if truncated:
            lines.append(f"- ... {len(skills) - len(lines)} more skills; call list_skills to search them")
        return "\n".join((header, *lines, SKILL_CONTEXT_SUFFIX)), len(lines) - int(truncated), truncated


def parse_skill(path: Path, *, source: str = "user", scope: str = "user") -> Skill:
    text = path.read_text(encoding="utf-8", errors="replace")
    name = path.parent.name if path.name.casefold() == "skill.md" else path.stem
    description = ""
    description_declared = False
    body = text
    if text.startswith("---"):
        match = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?(.*)", text, flags=re.DOTALL)
        if match:
            frontmatter, body = match.groups()
            for line in frontmatter.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key == "name" and value:
                    name = value
                elif key == "description":
                    description = value
                    description_declared = bool(value)
    if not description:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                description = stripped[:200]
                break
    return Skill(
        name=name,
        description=description,
        body=body.strip(),
        path=path,
        source=source,
        scope=scope,
        description_declared=description_declared,
    )


def skill_root_status(path: Path) -> str:
    if not path.exists():
        return "missing"
    if not path.is_dir():
        return "not_directory"
    try:
        next(path.iterdir(), None)
    except OSError:
        return "unreadable"
    return "ok"


def safe_skill_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    if not cleaned:
        raise ValueError("skill name cannot be empty")
    return cleaned[:80]


def clean_skill_reference(name: str) -> str:
    cleaned = re.sub(r"\[[^\]]*\]", " ", str(name or "")).strip().lstrip("/")
    if cleaned.lower().startswith("skill "):
        cleaned = cleaned[6:].strip()
    return cleaned.split()[0] if cleaned else ""


def render_skill(skill: Skill, arguments: str = "") -> str:
    body = load_skill_body(skill)
    lines = [
        SKILL_PIN_PREFIX.format(name=skill.name),
        f"# Skill: {skill.name}",
    ]
    if skill.description:
        lines.append(f"> {skill.description}")
    lines.extend((f"(scope: {skill.scope}; source: {skill.path})", "", body))
    if arguments.strip():
        lines.extend(("", f"Arguments: {arguments.strip()}"))
    lines.append(SKILL_PIN_SUFFIX)
    return "\n".join(lines)


def load_skill_body(skill: Skill) -> str:
    body = skill.body[:MAX_SKILL_BODY_CHARS]
    truncated = len(skill.body) > MAX_SKILL_BODY_CHARS
    remaining = MAX_SKILL_TOTAL_CHARS - len(body)
    if skill.path.name.casefold() == "skill.md" and remaining > 0:
        references_dir = skill.path.parent / "references"
        try:
            references = sorted(path for path in references_dir.glob("*.md") if path.is_file())
        except OSError:
            references = []
        for reference in references:
            if remaining <= 0:
                truncated = True
                break
            try:
                content = reference.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            content = content[: min(MAX_SKILL_REFERENCE_CHARS, remaining)]
            addition = f"\n\n## Reference: {reference.stem}\n\n{content}"
            body += addition[:remaining]
            remaining = MAX_SKILL_TOTAL_CHARS - len(body)

        scripts = skill_script_paths(skill)
        if scripts and remaining > 0:
            listing = "\n\n## Scripts\n\n" + "\n".join(f"- `{path}`" for path in scripts)
            body += listing[:remaining]
            remaining = MAX_SKILL_TOTAL_CHARS - len(body)
    if truncated or remaining <= 0:
        body = body.rstrip() + "\n\n[Skill content truncated at the runtime safety limit.]"
    return body


def skill_script_paths(skill: Skill) -> list[str]:
    scripts_dir = skill.path.parent / "scripts"
    try:
        entries = sorted(path for path in scripts_dir.iterdir() if path.is_file())
    except OSError:
        return []
    return [str(path) for path in entries if not path.name.startswith(".") and path.suffix.lower() in SCRIPT_EXTENSIONS]

