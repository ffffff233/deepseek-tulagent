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

    def summary(self) -> str:
        return f"- {self.name}: {self.description}"


class SkillStore:
    def __init__(self, workspace: Path, home: Path | None = None):
        self.workspace = workspace.resolve()
        self.home = (home or Path.home()).resolve()

    @property
    def search_dirs(self) -> list[Path]:
        return [
            self.workspace / ".deepseek-tulagent" / "skills",
            self.workspace / ".agents" / "skills",
            self.workspace / "skills",
            self.home / ".deepseek-tulagent" / "skills",
            self.home / ".agents" / "skills",
            Path(__file__).resolve().parent / "builtin_skills",
        ]

    @property
    def writable_dir(self) -> Path:
        return self.workspace / ".deepseek-tulagent" / "skills"

    def list(self) -> list[Skill]:
        skills: dict[str, Skill] = {}
        seen_roots: set[Path] = set()
        official_dir = (Path(__file__).resolve().parent / "builtin_skills").resolve()
        for root in self.search_dirs:
            root = root.resolve()
            if root in seen_roots:
                continue
            seen_roots.add(root)
            if not root.exists():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                skill = parse_skill(skill_md, source="official" if root == official_dir else "user")
                skills.setdefault(skill.name, skill)
        return [skills[name] for name in sorted(skills)]

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
        path.write_text(content, encoding="utf-8")
        return parse_skill(path)

    def prompt_context(self, max_skills: int = 12) -> str:
        skills = self.list()[:max_skills]
        if not skills:
            return "Skills: none discovered."
        return "Available skills:\n" + "\n".join(skill.summary() for skill in skills)


def parse_skill(path: Path, *, source: str = "user") -> Skill:
    text = path.read_text(encoding="utf-8", errors="replace")
    name = path.parent.name
    description = ""
    body = text
    if text.startswith("---"):
        match = re.match(r"---\n(.*?)\n---\n?(.*)", text, flags=re.DOTALL)
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
    if not description:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                description = stripped[:200]
                break
    return Skill(name=name, description=description, body=body.strip(), path=path, source=source)


def safe_skill_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    if not cleaned:
        raise ValueError("skill name cannot be empty")
    return cleaned[:80]

