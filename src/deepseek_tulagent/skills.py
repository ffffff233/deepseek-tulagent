from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


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
        return f"- {self.name}: {self.description}"


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
            (self.workspace / "skills", "project"),
            (self.home / ".deepseek-tulagent" / "skills", "user"),
            (self.home / ".agents" / "skills", "user"),
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
                skill_files = sorted(root.path.glob("*/SKILL.md"))
            except OSError:
                continue
            for skill_md in skill_files:
                skill = parse_skill(
                    skill_md,
                    source="official" if root.scope == "official" else "user",
                    scope=root.scope,
                )
                winner = winners.get(skill.name)
                if winner is None:
                    winners[skill.name] = skill
                    candidates.append(SkillCandidate(skill, "winner"))
                else:
                    candidates.append(SkillCandidate(skill, "shadowed", winner.path))
        candidates.sort(key=lambda candidate: (candidate.skill.name, 0 if candidate.status == "winner" else 1, str(candidate.skill.path)))
        return SkillInspection(roots, candidates)

    def get(self, name: str) -> Skill | None:
        for skill in self.list():
            if skill.name == name:
                return skill
        return None

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

    def prompt_context(self, max_skills: int = 12) -> str:
        skills = self.list()[:max_skills]
        if not skills:
            return "Skills: none discovered."
        return "Available skills:\n" + "\n".join(skill.summary() for skill in skills)


def parse_skill(path: Path, *, source: str = "user", scope: str = "user") -> Skill:
    text = path.read_text(encoding="utf-8", errors="replace")
    name = path.parent.name
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

