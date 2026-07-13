from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping


ToolHandler = Callable[[dict[str, Any]], Any]
MAX_TOOL_SCHEMA_BYTES = 64_000


@dataclass(frozen=True)
class ToolContract:
    """Runtime contract shared by built-in and extension-provided tools."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: ToolHandler | None = None
    origin: str = "builtin"
    read_only: bool = False
    trusted_read_only: bool = False

    def provider_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or self.name,
                "parameters": normalize_tool_schema(self.schema),
            },
        }


def normalize_tool_schema(value: Any) -> dict[str, Any]:
    schema = dict(value) if isinstance(value, Mapping) else {}
    if schema.get("type") != "object":
        schema = {
            "type": "object",
            "properties": dict(schema.get("properties") or {})
            if isinstance(schema.get("properties"), Mapping)
            else {},
            "additionalProperties": True,
        }
    schema.setdefault("properties", {})
    schema.setdefault("additionalProperties", True)
    try:
        encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    if len(encoded) > MAX_TOOL_SCHEMA_BYTES:
        return {"type": "object", "properties": {}, "additionalProperties": True}
    return schema


def builtin_tool_contracts(registry: Any) -> dict[str, ToolContract]:
    # Imports stay local so capabilities.py can continue importing ToolRegistry without
    # creating an import cycle.
    from .capabilities import READ_ONLY_TOOLS, TOOL_SCHEMAS, VIRTUAL_TOOL_DESCRIPTIONS
    from .tools import TOOL_DESCRIPTIONS

    names = set(getattr(registry, "names", ())) | set(VIRTUAL_TOOL_DESCRIPTIONS)
    contracts: dict[str, ToolContract] = {}
    for name in sorted(names):
        handler = None
        tools = getattr(registry, "_tools", {})
        if isinstance(tools, dict):
            handler = tools.get(name)
        contracts[name] = ToolContract(
            name=name,
            description=VIRTUAL_TOOL_DESCRIPTIONS.get(name) or TOOL_DESCRIPTIONS.get(name) or name,
            schema=normalize_tool_schema(TOOL_SCHEMAS.get(name)),
            handler=handler,
            origin="builtin",
            read_only=name in READ_ONLY_TOOLS,
            trusted_read_only=name in READ_ONLY_TOOLS,
        )
    return contracts


def coerce_tool_contract(value: Any, *, handler: ToolHandler | None = None) -> ToolContract:
    if isinstance(value, ToolContract):
        return value if handler is None else ToolContract(
            name=value.name,
            description=value.description,
            schema=value.schema,
            handler=handler,
            origin=value.origin,
            read_only=value.read_only,
            trusted_read_only=value.trusted_read_only,
        )
    if isinstance(value, Mapping):
        name = str(value.get("name") or "").strip()
        description = str(value.get("description") or name).strip()
        schema = value.get("schema", value.get("inputSchema", value.get("parameters", {})))
        origin = str(value.get("origin") or "extension")
        read_only = bool(value.get("read_only", value.get("readOnly", False)))
        trusted = bool(value.get("trusted_read_only", value.get("trustedReadOnly", False)))
    else:
        name = str(getattr(value, "name", "")).strip()
        description = str(getattr(value, "description", name)).strip()
        schema = getattr(value, "schema", getattr(value, "input_schema", {}))
        origin = str(getattr(value, "origin", "extension"))
        read_only = bool(getattr(value, "read_only", False))
        trusted = bool(getattr(value, "trusted_read_only", False))
    if not name:
        raise ValueError("tool contract name cannot be empty")
    return ToolContract(
        name=name,
        description=description or name,
        schema=normalize_tool_schema(schema),
        handler=handler,
        origin=origin,
        read_only=read_only,
        trusted_read_only=trusted,
    )
