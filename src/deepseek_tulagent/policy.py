from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApprovalPolicy:
    name: str
    allow_read: bool
    allow_write: bool
    allow_shell: bool
    allow_network: bool
    require_confirmation: bool

    @classmethod
    def from_mode(cls, mode: str) -> "ApprovalPolicy":
        policies = {
            "plan": cls("plan", True, False, False, False, False),
            "review": cls("review", True, False, True, False, True),
            "agent": cls("agent", True, True, True, False, True),
            "trusted": cls("trusted", True, True, True, True, True),
            "yolo": cls("yolo", True, True, True, True, False),
            "root": cls("root", True, True, True, True, False),
        }
        try:
            return policies[mode]
        except KeyError as exc:
            names = ", ".join(sorted(policies))
            raise ValueError(f"mode must be one of: {names}") from exc


@dataclass(frozen=True)
class ThinkingMode:
    name: str
    model_hint: str
    max_tokens: int
    system_hint: str
    deliberation_passes: int = 0
    api_thinking: bool = True
    reasoning_effort: str | None = None

    @classmethod
    def resolve(cls, mode: str) -> "ThinkingMode":
        modes = {
            "auto": cls("auto", "deepseek-v4-flash", 384000, "Let the model choose the thinking depth for each prompt.", 0, True, "medium"),
            "off": cls("off", "deepseek-v4-flash", 384000, "Answer directly. Avoid extended reasoning.", 0, False, None),
            "instant": cls("instant", "deepseek-v4-flash", 384000, "Use the fastest practical response. Ask for tools only when needed.", 0, False, None),
            "fast": cls("fast", "deepseek-v4-flash", 384000, "Use quick reasoning and prefer cheap exploratory tool calls.", 0, True, "low"),
            "standard": cls("standard", "deepseek-v4-flash", 384000, "Use normal task reasoning with concise checks.", 0, True, "low"),
            "balanced": cls("balanced", "deepseek-v4-pro", 384000, "Use measured reasoning. Plan briefly before risky edits.", 1, True, "medium"),
            "careful": cls("careful", "deepseek-v4-pro", 384000, "Use careful reasoning and verify assumptions before edits.", 1, True, "high"),
            "deep": cls("deep", "deepseek-v4-pro", 384000, "Think deeply about tradeoffs, hidden state, tests, and failure modes.", 2, True, "high"),
            "deeper": cls("deeper", "deepseek-v4-pro", 384000, "Use deeper multi-step reasoning for complex debugging and architecture.", 2, True, "xhigh"),
            "max": cls("max", "deepseek-v4-pro", 384000, "Use maximum practical reasoning for ambiguous multi-step engineering work.", 3, True, "xhigh"),
            "ultra": cls("ultra", "deepseek-v4-pro", 384000, "Use the largest supported output and internal thinking budget.", 4, True, "xhigh"),
        }
        try:
            return modes[mode]
        except KeyError as exc:
            names = ", ".join(sorted(modes))
            raise ValueError(f"thinking mode must be one of: {names}") from exc

    @classmethod
    def names(cls) -> list[str]:
        return ["auto", "off", "instant", "fast", "standard", "balanced", "careful", "deep", "deeper", "max", "ultra"]
