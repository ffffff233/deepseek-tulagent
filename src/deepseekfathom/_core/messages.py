from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class Message:
    role: Role
    content: str
    name: str | None = None
    # data-URL images ("data:image/png;base64,…") for vision. Persisted to the
    # session log so a reloaded conversation (and every follow-up turn, which reloads
    # the session) can still send them to the model.
    images: list[str] = field(default_factory=list)
    # Optional presentation metadata. These fields are persisted with the transcript,
    # but provider adapters decide separately which messages are model-visible.
    ui_kind: str | None = None
    display_content: str | None = None
    model_visible: bool = True
    turn_id: str | None = None

    def to_api(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        return payload


def clone_message(message: Message, **changes: Any) -> Message:
    """Clone a message while preserving all metadata and isolating mutable images."""

    updates = dict(changes)
    updates["images"] = list(updates.get("images", message.images))
    return replace(message, **updates)

