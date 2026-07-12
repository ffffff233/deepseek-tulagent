from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .policy import ApprovalPolicy
from .skills import SkillStore
from .tools import TOOL_DESCRIPTIONS, ToolRegistry


VIRTUAL_TOOL_DESCRIPTIONS = {
    "ask_user": "session: ask the user for a structured choice or manual answer",
    "delegate_agent": "agent: run one or more isolated subagents and return summaries",
}

READ_ONLY_TOOLS = {
    "ask_user",
    "git_status",
    "inspect_media",
    "list_files",
    "read_file",
    "search_text",
    "service_status",
    "todo_write",
    "web_search",
}

APPROVAL_TOOLS = {
    "apply_patch",
    "clone_repo",
    "download_url",
    "run_shell",
    "start_service",
    "stop_service",
    "write_file",
}

WRITE_TOOLS = {"apply_patch", "clone_repo", "download_url", "write_file"}
SHELL_TOOLS = {"run_shell", "start_service", "stop_service"}
NETWORK_TOOLS = {"clone_repo", "download_url", "web_search"}


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _integer(description: str, *, minimum: int | None = None, maximum: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "integer", "description": description}
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    return result


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": True,
    }


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "apply_patch": _object({"patch": _string("Unified diff text."), "timeout": _integer("Timeout in seconds.", minimum=1)}, ("patch",)),
    "ask_user": _object({
        "question": _string("Question shown to the user."),
        "options": {"type": "array", "items": {"type": "object"}},
        "allow_manual": {"type": "boolean"},
        "placeholder": _string("Optional manual-answer hint."),
    }, ("question",)),
    "clone_repo": _object({
        "repo": _string("GitHub repository or Git URL."),
        "path": _string("Destination path."),
        "branch": _string("Optional branch."),
        "timeout": _integer("Timeout in seconds.", minimum=1),
    }, ("repo", "path")),
    "delegate_agent": _object({
        "name": _string("Subagent name."),
        "task": _string("Bounded task."),
        "mode": _string("Permission mode."),
        "thinking": _string("Reasoning effort."),
        "max_rounds": _integer("Maximum tool rounds.", minimum=1, maximum=16),
        "agents": {"type": "array", "items": {"type": "object"}, "maxItems": 8},
    }),
    "download_url": _object({
        "url": _string("HTTP or HTTPS URL."),
        "path": _string("Destination path."),
        "max_bytes": _integer("Maximum download size.", minimum=1),
        "timeout": _integer("Timeout in seconds.", minimum=1),
    }, ("url", "path")),
    "git_status": _object({"timeout": _integer("Timeout in seconds.", minimum=1)}),
    "inspect_media": _object({
        "path": _string("Image or video path."),
        "max_frames": _integer("Maximum extracted video frames.", minimum=1, maximum=12),
    }, ("path",)),
    "list_files": _object({
        "path": _string("Directory or file path."),
        "max_entries": _integer("Maximum returned entries.", minimum=1),
    }),
    "read_file": _object({
        "path": _string("Text file path."),
        "max_bytes": _integer("Maximum bytes to read.", minimum=1),
    }, ("path",)),
    "run_shell": _object({
        "command": _string("Shell command."),
        "timeout": _integer("Timeout in seconds.", minimum=1),
    }, ("command",)),
    "search_text": _object({
        "query": _string("Literal search text."),
        "path": _string("Search root."),
        "max_matches": _integer("Maximum matches.", minimum=1),
        "timeout": _integer("Timeout in seconds.", minimum=1),
        "max_filesize": _string("ripgrep file-size limit."),
    }, ("query",)),
    "service_status": _object({"name": _string("Tracked service name.")}, ("name",)),
    "start_service": _object({
        "name": _string("Tracked service name."),
        "command": _string("Background command."),
    }, ("name", "command")),
    "stop_service": _object({"name": _string("Tracked service name.")}, ("name",)),
    "todo_write": _object({
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"content": _string("Task text."), "status": _string("Task state.")},
                "required": ["content", "status"],
            },
        },
    }, ("todos",)),
    "web_search": _object({
        "query": _string("Search query or direct URL."),
        "max_results": _integer("Maximum result count.", minimum=1),
        "timeout": _integer("Timeout in seconds.", minimum=1),
        "engines": _string("Comma-separated search engine override."),
        "language": _string("Preferred language."),
        "fetch_pages": _integer("Pages to fetch for snippets.", minimum=0, maximum=5),
        "page_chars": _integer("Maximum characters per fetched page.", minimum=200, maximum=1600),
    }, ("query",)),
    "write_file": _object({"path": _string("Destination path."), "content": _string("Complete UTF-8 file content.")}, ("path", "content")),
}


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def display_path(path: Path, workspace: Path, home: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(workspace)
        return "<workspace>" + ("/" + relative.as_posix() if relative.parts else "")
    except ValueError:
        pass
    try:
        relative = resolved.relative_to(home)
        return "~" + ("/" + relative.as_posix() if relative.parts else "")
    except ValueError:
        pass
    return f"<external>/{resolved.name}" if resolved.is_absolute() else resolved.as_posix()


def collect_capability_report(workspace: Path, *, mode: str = "root", home: Path | None = None) -> dict[str, Any]:
    from .agent import KNOWN_TOOL_NAMES, SYSTEM_PROMPT

    root = workspace.resolve()
    user_home = (home or Path.home()).resolve()
    issues: list[dict[str, Any]] = []
    skill_store = SkillStore(root, home=user_home)
    inspection = skill_store.inspect()

    skill_roots = [
        {
            "path": display_path(item.path, root, user_home),
            "scope": item.scope,
            "priority": item.priority,
            "status": item.status,
        }
        for item in inspection.roots
    ]
    skill_entries: list[dict[str, Any]] = []
    for candidate in inspection.candidates:
        skill = candidate.skill
        entry = {
            "name": skill.name,
            "description": skill.description,
            "scope": skill.scope,
            "source": skill.source,
            "status": candidate.status,
            "path": display_path(skill.path, root, user_home),
            "winnerPath": display_path(candidate.winner_path, root, user_home) if candidate.winner_path else None,
            "descriptionDeclared": skill.description_declared,
        }
        skill_entries.append(entry)
        if candidate.status == "shadowed":
            issues.append(issue(
                "info",
                "skill.shadowed",
                "skills",
                skill.name,
                entry["path"],
                f"同名技能由更高优先级路径 {entry['winnerPath']} 生效。",
                "修改技能名称，或编辑当前生效的技能文件。",
                "skills",
            ))
        elif not skill.description_declared:
            issues.append(issue(
                "warning",
                "skill.missing_description",
                "skills",
                skill.name,
                entry["path"],
                "技能缺少 frontmatter description，当前描述由正文首行推导。",
                "在 SKILL.md frontmatter 中添加简短 description。",
                "skills",
            ))

    winners = [entry for entry in skill_entries if entry["status"] == "winner"]
    shadowed = [entry for entry in skill_entries if entry["status"] == "shadowed"]
    skill_prompt = skill_store.prompt_context()
    if len(winners) > 12:
        issues.append(issue(
            "warning",
            "skill.prompt_truncated",
            "skills",
            "skills-index",
            "<provider-prompt>",
            f"发现 {len(winners)} 个生效技能，但固定索引只发送前 12 个。",
            "缩短技能列表或后续改用按需技能索引。",
            "skills",
        ))

    policy = ApprovalPolicy.from_mode(mode)
    registry_names = set(ToolRegistry(root, policy=policy).names)
    runtime_names = registry_names | set(VIRTUAL_TOOL_DESCRIPTIONS)
    known_names = set(KNOWN_TOOL_NAMES)
    prompt_lines: dict[str, str] = {}
    for line in SYSTEM_PROMPT.splitlines():
        match = re.match(r"- ([a-z_]+)\(", line.strip())
        if match:
            prompt_lines[match.group(1)] = line.strip()

    tool_entries: list[dict[str, Any]] = []
    all_tool_names = sorted(known_names | runtime_names | set(TOOL_SCHEMAS))
    for name in all_tool_names:
        description = VIRTUAL_TOOL_DESCRIPTIONS.get(name) or TOOL_DESCRIPTIONS.get(name, "")
        schema = TOOL_SCHEMAS.get(name)
        schema_json = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if schema else ""
        prompt_line = prompt_lines.get(name, "")
        enabled = tool_enabled(name, policy)
        if name not in runtime_names:
            enabled = False
        gate = "none"
        if not enabled:
            gate = "disabled"
        elif name in APPROVAL_TOOLS and policy.require_confirmation:
            gate = "approval"
        entry = {
            "name": name,
            "description": description,
            "readOnly": name in READ_ONLY_TOOLS,
            "enabled": enabled,
            "gate": gate,
            "runtimeRegistered": name in runtime_names,
            "providerPrompted": name in known_names and bool(prompt_line),
            "schema": schema,
            "schemaBytes": len(schema_json.encode("utf-8")),
            "schemaTokenEstimate": estimate_tokens(schema_json),
            "promptBytes": len(prompt_line.encode("utf-8")),
            "promptTokenEstimate": estimate_tokens(prompt_line),
        }
        tool_entries.append(entry)
        if name in known_names and name not in runtime_names:
            issues.append(issue("error", "tool.runtime_missing", "tools", name, "<runtime>", "提示词声明了工具，但运行时未注册。", "修复工具注册表或移除过期提示。", "tools"))
        if name in runtime_names and name not in known_names:
            issues.append(issue("warning", "tool.prompt_missing", "tools", name, "<provider-prompt>", "运行时存在工具，但固定提示词没有声明。", "补充工具提示契约。", "tools"))
        if name in runtime_names and schema is None:
            issues.append(issue("warning", "tool.schema_missing", "tools", name, "<tool-contract>", "工具缺少可检查的参数契约。", "补充确定性的工具参数 schema。", "tools"))

    instruction_entries: list[dict[str, Any]] = []
    for name in ("AGENTS.md", "REASONIX.md", "CLAUDE.md"):
        path = root / name
        if path.is_file():
            instruction_entries.append({"name": name, "path": display_path(path, root, user_home), "loaded": False})
            issues.append(issue(
                "warning",
                "instruction.not_loaded",
                "instructions",
                name,
                display_path(path, root, user_home),
                "检测到项目指令文件，但当前运行时尚未自动加载它。",
                "在接入指令加载前，将关键规则放入用户请求或技能。",
                "diagnostics",
            ))

    issues.append(issue(
        "info",
        "extensions.not_integrated",
        "extensions",
        "mcp-plugins-hooks",
        "<runtime>",
        "当前版本尚未集成 MCP、插件包和 Hooks，诊断页不会伪报为健康。",
        "后续版本接入后再增加静态配置与运行态检查。",
        "diagnostics",
    ))
    issues.sort(key=lambda item: ({"error": 0, "warning": 1, "info": 2}[item["severity"]], item["code"], item["name"], item["source"]))

    errors = sum(item["severity"] == "error" for item in issues)
    warnings = sum(item["severity"] == "warning" for item in issues)
    infos = sum(item["severity"] == "info" for item in issues)
    return {
        "schemaVersion": 1,
        "root": "<workspace>",
        "mode": mode,
        "static": True,
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "infos": infos,
            "skillRoots": len(skill_roots),
            "skills": len(winners),
            "shadowedSkills": len(shadowed),
            "tools": len(tool_entries),
            "enabledTools": sum(bool(item["enabled"]) for item in tool_entries),
            "toolSchemaBytes": sum(int(item["schemaBytes"]) for item in tool_entries),
            "toolSchemaTokenEstimate": sum(int(item["schemaTokenEstimate"]) for item in tool_entries),
            "fixedSystemPromptBytes": len(SYSTEM_PROMPT.encode("utf-8")),
            "fixedSystemPromptTokenEstimate": estimate_tokens(SYSTEM_PROMPT),
            "skillPromptBytes": len(skill_prompt.encode("utf-8")),
            "skillPromptTokenEstimate": estimate_tokens(skill_prompt),
        },
        "instructions": {"entries": instruction_entries},
        "skills": {
            "roots": skill_roots,
            "entries": skill_entries,
            "promptLimit": 12,
            "prompted": min(12, len(winners)),
        },
        "tools": {"protocol": "json-in-text", "entries": tool_entries},
        "extensions": {
            "mcp": {"supported": False, "entries": []},
            "plugins": {"supported": False, "entries": []},
            "hooks": {"supported": False, "entries": []},
        },
        "issues": issues,
    }


def tool_enabled(name: str, policy: ApprovalPolicy) -> bool:
    if name in WRITE_TOOLS and not policy.allow_write:
        return False
    if name in SHELL_TOOLS and not policy.allow_shell:
        return False
    if name in NETWORK_TOOLS and not policy.allow_network:
        return False
    return True


def issue(
    severity: str,
    code: str,
    subsystem: str,
    name: str,
    source: str,
    message: str,
    remediation: str,
    settings_tab: str,
) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "subsystem": subsystem,
        "name": name,
        "source": source,
        "message": message,
        "remediation": remediation,
        "settingsTab": settings_tab,
    }
