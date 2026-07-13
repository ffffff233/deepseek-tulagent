from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterator, Mapping

import httpx

from .config import Settings
from .messages import Message


@dataclass(frozen=True)
class NativeToolCall:
    """A provider-native tool request after streaming fragments are assembled."""

    name: str
    arguments: dict[str, Any]
    call_id: str = ""


def openai_tool_definitions(
    tool_names: Iterable[str],
    runtime_contracts: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build OpenAI function definitions from the application's canonical schemas."""

    from .capabilities import TOOL_SCHEMAS, VIRTUAL_TOOL_DESCRIPTIONS
    from .tools import TOOL_DESCRIPTIONS

    definitions: list[dict[str, Any]] = []
    for name in dict.fromkeys(tool_names):
        runtime = (runtime_contracts or {}).get(name)
        if runtime is not None and hasattr(runtime, "provider_definition"):
            definitions.append(runtime.provider_definition())
            continue
        if isinstance(runtime, Mapping):
            schema = runtime.get("schema", runtime.get("inputSchema", runtime.get("parameters")))
            description = str(runtime.get("description") or name)
        else:
            schema = TOOL_SCHEMAS.get(name)
            description = VIRTUAL_TOOL_DESCRIPTIONS.get(name) or TOOL_DESCRIPTIONS.get(name) or name
        if schema is None:
            continue
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema,
                },
            }
        )
    return definitions


def parse_openai_tool_calls(message: dict[str, Any]) -> list[NativeToolCall]:
    """Normalize Chat Completions ``tool_calls`` and legacy ``function_call``."""

    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        legacy = message.get("function_call")
        raw_calls = [{"function": legacy}] if isinstance(legacy, dict) else []
    calls: list[NativeToolCall] = []
    for index, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments = decode_tool_arguments(function.get("arguments", {}))
        calls.append(NativeToolCall(name.strip(), arguments, str(item.get("id") or "")))
    return calls


def decode_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def append_openai_tool_call_deltas(
    parts: dict[int, dict[str, str]],
    delta: dict[str, Any],
) -> None:
    raw_calls = delta.get("tool_calls")
    if not isinstance(raw_calls, list):
        legacy = delta.get("function_call")
        raw_calls = [{"index": 0, "function": legacy}] if isinstance(legacy, dict) else []
    for position, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            continue
        raw_index = item.get("index", position)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = position
        target = parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
        call_id = item.get("id")
        if isinstance(call_id, str) and call_id:
            target["id"] = call_id
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = function.get("name")
        arguments = function.get("arguments")
        if isinstance(name, str):
            target["name"] += name
        if isinstance(arguments, str):
            target["arguments"] += arguments
        elif isinstance(arguments, dict):
            target["arguments"] += json.dumps(arguments, ensure_ascii=False)


def finalize_openai_tool_call_deltas(parts: dict[int, dict[str, str]]) -> list[NativeToolCall]:
    calls: list[NativeToolCall] = []
    for index in sorted(parts):
        item = parts[index]
        name = item["name"].strip()
        if not name:
            continue
        calls.append(
            NativeToolCall(
                name=name,
                arguments=decode_tool_arguments(item["arguments"]),
                call_id=item["id"],
            )
        )
    return calls


# Provider format normalization. Users may save any of the aliases on the left;
# they collapse to one of the canonical families on the right.
FORMAT_ALIASES = {
    "deepseek": "deepseek",
    "openai": "openai",
    "openai-compatible": "openai",
    "openai-chat": "openai",
    "openai-responses": "openai-responses",
    "responses": "openai-responses",
    "gemini": "gemini",
    "google": "gemini",
    "google-gemini": "gemini",
    "anthropic": "anthropic",
    "claude": "anthropic",
}

# Default host for each family, used when the saved base_url is empty or still points
# at the generic DeepSeek default while a different family is selected.
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openai-responses": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com",
    "anthropic": "https://api.anthropic.com",
}

_DEEPSEEK_DEFAULT = "https://api.deepseek.com"

# Anthropic / Gemini reject the very large max_tokens the DeepSeek thinking modes
# request (up to 384000). Cap output so those providers don't 400.
_OUTPUT_CAP = 32000


@dataclass
class UsageStats:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0
    source: str = ""
    cache_miss_input_tokens: int = 0
    reasoning_tokens: int = 0

    def merge(self, other: "UsageStats") -> None:
        if not other.source:
            return
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.cache_miss_input_tokens += other.cache_miss_input_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.total_tokens += other.total_tokens or (other.input_tokens + other.output_tokens)
        self.source = other.source

    def absorb_snapshot(self, other: "UsageStats") -> None:
        """Keep the latest non-zero fields for one upstream request.

        Streaming APIs may emit usage across multiple events. Those values are normally
        request snapshots, not increments, so they must be merged into totals only once.
        """
        if not other.source:
            return
        self.input_tokens = other.input_tokens or self.input_tokens
        self.output_tokens = other.output_tokens or self.output_tokens
        self.cached_input_tokens = other.cached_input_tokens or self.cached_input_tokens
        self.cache_miss_input_tokens = other.cache_miss_input_tokens or self.cache_miss_input_tokens
        self.reasoning_tokens = other.reasoning_tokens or self.reasoning_tokens
        self.total_tokens = other.total_tokens or self.total_tokens
        self.source = other.source


def normalize_format(value: str | None) -> str:
    return FORMAT_ALIASES.get((value or "deepseek").strip().lower(), "deepseek")


def parse_usage_stats(data: dict, source: str) -> UsageStats:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        nested = data.get("response") if isinstance(data, dict) else None
        if isinstance(nested, dict):
            usage = nested.get("usage")
    if not isinstance(usage, dict):
        nested = data.get("message") if isinstance(data, dict) else None
        if isinstance(nested, dict):
            usage = nested.get("usage")
    if not isinstance(usage, dict):
        usage = data.get("usageMetadata") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return UsageStats()

    def intval(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    input_tokens = intval("prompt_tokens", "input_tokens", "inputTokens", "promptTokenCount")
    output_tokens = intval("completion_tokens", "output_tokens", "outputTokens", "candidatesTokenCount")
    total_tokens = intval("total_tokens", "totalTokens", "totalTokenCount")
    cached = 0
    cache_miss = 0
    cache_fields_seen = False
    for key in ("prompt_tokens_details", "input_tokens_details", "inputTokenDetails"):
        details = usage.get(key)
        if isinstance(details, dict):
            detail_cached = int(details.get("cached_tokens") or details.get("cachedTokens") or 0)
            if "cached_tokens" in details or "cachedTokens" in details:
                cache_fields_seen = True
                cached += detail_cached
    # Standard OpenAI usage includes cached tokens in input_tokens. A few compatible
    # gateways instead report only the uncached portion there. cached > input proves
    # that shape, so reconstruct the complete input without double-counting compliant
    # responses.
    if cached > input_tokens:
        cache_miss = input_tokens
        input_tokens += cached
    elif cache_fields_seen:
        cache_miss = max(0, input_tokens - cached)
    # DeepSeek and several compatible gateways expose cache hits/misses beside
    # prompt_tokens. Some gateways incorrectly put only the cache miss count in
    # prompt_tokens, so hit + miss is the reliable full request size there.
    cache_hit = intval("prompt_cache_hit_tokens", "cache_hit_tokens", "cached_input_tokens", "cachedContentTokenCount")
    reported_cache_miss = intval("prompt_cache_miss_tokens", "cache_miss_tokens")
    top_cache_fields_seen = any(
        key in usage
        for key in (
            "prompt_cache_hit_tokens",
            "cache_hit_tokens",
            "cached_input_tokens",
            "cachedContentTokenCount",
            "prompt_cache_miss_tokens",
            "cache_miss_tokens",
        )
    )
    if top_cache_fields_seen:
        cache_fields_seen = True
    cached = max(cached, cache_hit)
    if reported_cache_miss:
        cache_miss = reported_cache_miss
    elif top_cache_fields_seen:
        if cached > input_tokens:
            # Some compatible gateways put only the uncached tail in
            # prompt_tokens while reporting the full cached prefix separately.
            cache_miss = input_tokens
        elif input_tokens > cached:
            cache_miss = input_tokens - cached
    if cache_fields_seen:
        input_tokens = max(input_tokens, cached + cache_miss)

    # Anthropic reports cache creation/read separately from uncached input_tokens.
    # All three parts occupy the current context window, while only cache reads are
    # counted as cached input for the hit-rate display.
    cache_read = intval("cache_read_input_tokens")
    cache_creation = intval("cache_creation_input_tokens")
    if cache_read or cache_creation:
        uncached_input = input_tokens
        input_tokens += cache_read + cache_creation
        cached = cache_read
        cache_miss = uncached_input + cache_creation
        cache_fields_seen = True
    reasoning_tokens = intval("reasoning_tokens", "thoughtsTokenCount")
    for key in ("completion_tokens_details", "output_tokens_details", "outputTokenDetails"):
        details = usage.get(key)
        if isinstance(details, dict):
            reasoning_tokens = max(
                reasoning_tokens,
                int(details.get("reasoning_tokens") or details.get("reasoningTokens") or 0),
            )
    if input_tokens or output_tokens:
        total_tokens = max(total_tokens, input_tokens + output_tokens)
    return UsageStats(
        input_tokens,
        output_tokens,
        cached,
        total_tokens,
        source,
        cache_miss if cache_fields_seen else 0,
        reasoning_tokens,
    )


def prompt_cache_key(settings: Settings) -> str:
    """Stable, non-secret cache key for providers/gateways that support prompt cache affinity."""
    seed = "|".join(
        [
            "DeepSeekFathom",
            settings.base_url or "",
            settings.api_key or "",
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def cache_affinity_headers(settings: Settings) -> dict[str, str]:
    return {"Session_id": prompt_cache_key(settings)}


def apply_anthropic_cache_control(payload: dict) -> None:
    """Inject Anthropic-compatible cache breakpoints without changing message meaning.

    Strategy mirrors Codex/OpenCode-style adapters: cache the stable system prefix and
    the second-to-last user turn so multi-turn conversations can reuse the large prefix.
    """
    system = payload.get("system")
    if isinstance(system, str) and system.strip():
        payload["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(system, list) and system:
        last = system[-1]
        if isinstance(last, dict) and not last.get("cache_control"):
            last["cache_control"] = {"type": "ephemeral"}

    user_indices = [i for i, msg in enumerate(payload.get("messages", [])) if isinstance(msg, dict) and msg.get("role") == "user"]
    if len(user_indices) < 2:
        return
    message = payload["messages"][user_indices[-2]]
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        last_block = content[-1]
        if isinstance(last_block, dict) and last_block.get("type") == "text" and not last_block.get("cache_control"):
            last_block["cache_control"] = {"type": "ephemeral"}


def _split_data_url(data_url: str) -> tuple[str, str]:
    """('image/png', '<base64>') from a data: URL; ('image/png', '') if not a data URL."""
    if data_url.startswith("data:") and "," in data_url:
        head, b64 = data_url.split(",", 1)
        media = head[5:].split(";", 1)[0] or "image/png"
        return media, b64
    return "image/png", ""


def openai_message(message: Message) -> dict[str, Any]:
    """OpenAI/DeepSeek chat message; multimodal (content array) when it carries images."""
    images = getattr(message, "images", None) or []
    if not images:
        return message.to_api()
    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"type": "text", "text": message.content})
    for url in images:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    payload: dict[str, Any] = {"role": message.role, "content": parts}
    if message.name:
        payload["name"] = message.name
    return payload


def openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize messages and restore persisted native tool pairs for Chat Completions."""

    payloads: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        native = native_tool_history_record(message)
        if native:
            prose, calls = native
            results = messages[index + 1:index + 1 + len(calls)]
            if len(results) == len(calls) and all(
                not result.images and tool_result_name(result.content) == call.name
                for call, result in zip(calls, results)
            ):
                serialized_calls = []
                call_ids = []
                used_call_ids: set[str] = set()
                for call_index, call in enumerate(calls):
                    call_id = call.call_id.strip()
                    if not call_id or call_id in used_call_ids:
                        call_id = f"call_{index}_{call_index}"
                        while call_id in used_call_ids:
                            call_id += "_"
                    used_call_ids.add(call_id)
                    call_ids.append(call_id)
                    serialized_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":")),
                            },
                        }
                    )
                payloads.append({"role": "assistant", "content": prose or None, "tool_calls": serialized_calls})
                for call_id, result in zip(call_ids, results):
                    _header, _separator, body = result.content.partition("\n")
                    payloads.append({"role": "tool", "tool_call_id": call_id, "content": body or result.content})
                index += 1 + len(results)
                continue
        payloads.append(openai_message(message))
        index += 1
    return payloads


def native_tool_history_record(message: Message) -> tuple[str, list[NativeToolCall]] | None:
    if message.role != "assistant" or not message.content:
        return None
    prose, separator, candidate = message.content.rstrip().rpartition("\n")
    if not separator:
        candidate = message.content.strip()
        prose = ""
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("_native_tool_protocol") != "openai-chat":
        return None
    calls = parse_openai_tool_calls(data)
    return (prose.rstrip(), calls) if calls else None


def tool_result_name(content: str) -> str:
    first_line = content.partition("\n")[0].strip()
    if first_line.startswith("TOOL_RESULT name="):
        return first_line.removeprefix("TOOL_RESULT name=").strip()
    if first_line.startswith("SUBAGENT_RESULT"):
        return "delegate_agent"
    if first_line.startswith("USER_ANSWER"):
        return "ask_user"
    return ""


def anthropic_content(message: Message):
    """Anthropic content: plain string, or blocks when the message carries images."""
    images = getattr(message, "images", None) or []
    if not images:
        return message.content
    blocks: list[dict[str, Any]] = []
    for url in images:
        media, b64 = _split_data_url(url)
        if b64:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    return blocks or message.content


def gemini_parts(message: Message) -> list[dict[str, Any]]:
    """Gemini parts: text + inline_data image blocks."""
    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"text": message.content})
    for url in getattr(message, "images", None) or []:
        media, b64 = _split_data_url(url)
        if b64:
            parts.append({"inline_data": {"mime_type": media, "data": b64}})
    return parts or [{"text": message.content}]


def responses_content(message: Message):
    """OpenAI Responses `input` content: plain string, or a blocks array carrying
    input_image parts when the message has images."""
    images = getattr(message, "images", None) or []
    if not images:
        return message.content
    kind = "output_text" if message.role == "assistant" else "input_text"
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": kind, "text": message.content})
    for url in images:
        blocks.append({"type": "input_image", "image_url": url})
    return blocks or message.content


def _has_path(base_url: str) -> bool:
    """True if the URL has a real path beyond the host (so we shouldn't append /v1)."""
    from urllib.parse import urlparse

    try:
        path = urlparse(base_url).path.strip("/")
    except Exception:
        return True
    return bool(path)


def guard_api_content_type(response: httpx.Response, *, streaming: bool) -> None:
    """Reject non-API responses (e.g. a gateway's HTML homepage returned with 200).

    Without this, an HTML body has no SSE `data:` lines / JSON, so streaming yields
    nothing and the empty result is silently treated as a valid empty answer.
    """
    ctype = response.headers.get("content-type", "").lower()
    if "text/html" in ctype:
        raise RuntimeError(
            "上游返回的是网页而不是 API 响应（Base URL 可能指向了网关首页）。"
            "请检查接口地址是否正确。"
        )


class DeepSeekClient:
    """OpenAI/DeepSeek/Anthropic/Gemini chat client.

    The class name is kept for backwards compatibility (cli.py, desktop/app.py, and the
    test-suite reference it), but requests are dispatched by ``settings.provider_format``.
    """

    supports_native_tools = False

    def __init__(self, settings: Settings, timeout: float | None = None):
        self.settings = settings
        self.timeout = max(1.0, float(timeout or settings.request_timeout))
        self._client: httpx.Client | None = None
        self.format = normalize_format(getattr(settings, "provider_format", "deepseek"))
        # Native schemas below are implemented only for Chat Completions. Other
        # provider families keep using the agent's structured-text fallback.
        self.supports_native_tools = self.format in {"deepseek", "openai"}
        self.usage = UsageStats()
        # Cumulative usage is useful for billing, while the context meter needs the
        # snapshot from only the most recent upstream request.
        self.last_usage = UsageStats()
        self.last_tool_calls: list[NativeToolCall] = []
        # TuLAgent fills this per runtime. It lets MCP and plugin tools use the same
        # provider-native contract path without mutating the process-wide built-ins.
        self.runtime_tool_contracts: dict[str, Any] = {}

    def _record_usage(self, data: dict, source: str = "upstream") -> None:
        snapshot = parse_usage_stats(data, source)
        self.last_usage = snapshot
        self.usage.merge(snapshot)

    def _usage_snapshot(self, data: dict, source: str = "upstream") -> UsageStats:
        return parse_usage_stats(data, source)

    # ---- shared http ----
    def _http(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=min(10.0, self.timeout),
                    read=self.timeout,
                    write=min(30.0, self.timeout),
                    pool=min(10.0, self.timeout),
                )
            )
        return self._client

    def close(self) -> None:
        """Interrupt an in-flight request and release its connection pool."""
        client = self._client
        self._client = None
        if client is not None and not client.is_closed:
            client.close()

    def _require_key(self) -> str:
        if not self.settings.api_key:
            raise RuntimeError("API key is not set")
        return self.settings.api_key

    def _base_url(self) -> str:
        base = (self.settings.base_url or "").rstrip("/")
        # Fall back to the family default when the base is empty or still the DeepSeek
        # default but the selected family is something else.
        if self.format in DEFAULT_BASE_URLS and (not base or base == _DEEPSEEK_DEFAULT):
            base = DEFAULT_BASE_URLS[self.format]
        elif not base:
            base = _DEEPSEEK_DEFAULT
        # OpenAI-compatible gateways (incl. DeepSeek) serve the API under /v1. If the user
        # gave a bare host with no path, auto-append /v1 so we don't hit the gateway's
        # website (which returns HTML → silent empty responses). Anthropic/Gemini build
        # their own version paths, so leave those alone.
        if self.format in {"openai", "openai-responses", "deepseek"} and not _has_path(base):
            base = base + "/v1"
        return base

    def _output_tokens(self) -> int:
        tokens = int(self.settings.max_tokens or 8192)
        # Only DeepSeek accepts the very large thinking budgets (up to 384000). OpenAI /
        # Gemini / Anthropic reject them, so cap output for every other family.
        if self.format != "deepseek":
            return max(1, min(tokens, _OUTPUT_CAP))
        return tokens

    # ---- public API ----
    def chat(self, messages: Iterable[Message], *, tool_names: Iterable[str] | None = None) -> str:
        messages = list(messages)
        self.last_usage = UsageStats()
        self.last_tool_calls = []
        if self.format == "anthropic":
            return self._anthropic_chat(messages, stream=False)  # type: ignore[return-value]
        if self.format == "gemini":
            return self._gemini_chat(messages, stream=False)  # type: ignore[return-value]
        if self.format == "openai-responses":
            return self._responses_chat(messages, stream=False)  # type: ignore[return-value]
        return self._openai_chat(messages, stream=False, tool_names=tool_names)  # type: ignore[return-value]

    def stream_chat(
        self,
        messages: Iterable[Message],
        *,
        tool_names: Iterable[str] | None = None,
    ) -> Iterator[str]:
        messages = list(messages)
        self.last_usage = UsageStats()
        self.last_tool_calls = []
        if self.format == "anthropic":
            return self._anthropic_chat(messages, stream=True)  # type: ignore[return-value]
        if self.format == "gemini":
            return self._gemini_chat(messages, stream=True)  # type: ignore[return-value]
        if self.format == "openai-responses":
            return self._responses_chat(messages, stream=True)  # type: ignore[return-value]
        return self._openai_chat(messages, stream=True, tool_names=tool_names)  # type: ignore[return-value]

    def models(self) -> list[str]:
        if self.format == "anthropic":
            return self._anthropic_models()
        if self.format == "gemini":
            return self._gemini_models()
        # openai + openai-responses share GET /models
        return self._openai_models()

    def ping(self) -> dict[str, object]:
        models = self.models()
        return {
            "base_url": self._base_url(),
            "model": self.settings.model,
            "provider_format": self.format,
            "model_available": self.settings.model in models,
            "models": models,
        }

    # ---- OpenAI / DeepSeek ----
    def _openai_chat(
        self,
        messages: list[Message],
        *,
        stream: bool,
        tool_names: Iterable[str] | None = None,
    ):
        self._require_key()
        payload = {
            "model": self.settings.model,
            "messages": openai_messages(messages),
            "temperature": 0.2,
            "max_tokens": self._output_tokens(),
            "stream": stream,
        }
        tools = openai_tool_definitions(tool_names or (), self.runtime_tool_contracts)
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream_options"] = {"include_usage": True}
        apply_thinking_payload(payload, self.settings)
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(cache_affinity_headers(self.settings))
        url = f"{self._base_url()}/chat/completions"
        if not stream:
            response = self._http().post(url, headers=headers, json=payload)
            raise_for_status_with_body(response)
            guard_api_content_type(response, streaming=False)
            data = response.json()
            self._record_usage(data)
            try:
                message = data["choices"][0]["message"]
                self.last_tool_calls = parse_openai_tool_calls(message)
                # some gateways omit "content" when the text went to reasoning_content
                return message.get("content") or message.get("reasoning_content") or ""
            except (KeyError, IndexError, TypeError) as exc:
                compact = json.dumps(data, ensure_ascii=False)[:1000]
                raise RuntimeError(f"Unexpected response: {compact}") from exc
        return self._openai_stream(url, headers, payload)

    def _openai_stream(self, url: str, headers: dict, payload: dict) -> Iterator[str]:
        usage = UsageStats()
        tool_call_parts: dict[int, dict[str, str]] = {}
        with self._http().stream("POST", url, headers=headers, json=payload) as response:
            raise_for_status_with_body(response)
            guard_api_content_type(response, streaming=True)
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                chunk = line.removeprefix("data: ").strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                    usage.absorb_snapshot(self._usage_snapshot(data))
                    delta = data["choices"][0].get("delta", {})
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                content = delta.get("content")
                if content:
                    yield content
                append_openai_tool_call_deltas(tool_call_parts, delta)
        self.last_tool_calls = finalize_openai_tool_call_deltas(tool_call_parts)
        self.last_usage = usage
        self.usage.merge(usage)

    def _openai_models(self) -> list[str]:
        self._require_key()
        headers = {"Authorization": f"Bearer {self.settings.api_key}"}
        url = f"{self._base_url()}/models"
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            guard_api_content_type(response, streaming=False)
        data = response.json()
        return [item["id"] for item in data.get("data", []) if isinstance(item, dict) and "id" in item]

    # ---- OpenAI Responses API (newest format) ----
    def _responses_chat(self, messages: list[Message], *, stream: bool):
        self._require_key()
        system, turns = split_system(messages)
        payload: dict = {
            "model": self.settings.model,
            "input": [{"role": m.role, "content": responses_content(m)} for m in turns],
            "max_output_tokens": self._output_tokens(),
            "stream": stream,
            "prompt_cache_key": prompt_cache_key(self.settings),
        }
        if system:
            payload["instructions"] = system
        apply_thinking_payload(payload, self.settings)
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(cache_affinity_headers(self.settings))
        url = f"{self._base_url()}/responses"
        if not stream:
            response = self._http().post(url, headers=headers, json=payload)
            raise_for_status_with_body(response)
            guard_api_content_type(response, streaming=False)
            data = response.json()
            self._record_usage(data)
            text = data.get("output_text")
            if isinstance(text, str) and text:
                return text
            parts: list[str] = []
            for item in data.get("output", []) if isinstance(data, dict) else []:
                if isinstance(item, dict) and item.get("type") == "message":
                    for block in item.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "output_text":
                            parts.append(block.get("text", ""))
            if parts:
                return "".join(parts)
            compact = json.dumps(data, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Unexpected Responses payload: {compact}")
        return self._responses_stream(url, headers, payload)

    def _responses_stream(self, url: str, headers: dict, payload: dict) -> Iterator[str]:
        usage = UsageStats()
        with self._http().stream("POST", url, headers=headers, json=payload) as response:
            raise_for_status_with_body(response)
            guard_api_content_type(response, streaming=True)
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                usage.absorb_snapshot(self._usage_snapshot(data))
                if data.get("type") == "response.output_text.delta":
                    delta = data.get("delta")
                    if delta:
                        yield delta
        self.last_usage = usage
        self.usage.merge(usage)

    # ---- Anthropic / Claude ----
    def _anthropic_chat(self, messages: list[Message], *, stream: bool):
        self._require_key()
        system, turns = split_system(messages)
        payload: dict = {
            "model": self.settings.model,
            "max_tokens": self._output_tokens(),
            "messages": [{"role": m.role, "content": anthropic_content(m)} for m in turns],
            "stream": stream,
        }
        if system:
            payload["system"] = system
        apply_thinking_payload(payload, self.settings)
        apply_anthropic_cache_control(payload)
        headers = {
            "x-api-key": self.settings.api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = f"{self._base_url()}/v1/messages"
        if not stream:
            response = self._http().post(url, headers=headers, json=payload)
            raise_for_status_with_body(response)
            data = response.json()
            self._record_usage(data)
            try:
                parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
                return "".join(parts)
            except (AttributeError, TypeError) as exc:
                compact = json.dumps(data, ensure_ascii=False)[:1000]
                raise RuntimeError(f"Unexpected Anthropic response: {compact}") from exc
        return self._anthropic_stream(url, headers, payload)

    def _anthropic_stream(self, url: str, headers: dict, payload: dict) -> Iterator[str]:
        usage = UsageStats()
        with self._http().stream("POST", url, headers=headers, json=payload) as response:
            raise_for_status_with_body(response)
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                usage.absorb_snapshot(self._usage_snapshot(data))
                if data.get("type") != "content_block_delta":
                    continue
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield delta["text"]
        self.last_usage = usage
        self.usage.merge(usage)

    def _anthropic_models(self) -> list[str]:
        self._require_key()
        headers = {"x-api-key": self.settings.api_key or "", "anthropic-version": "2023-06-01"}
        url = f"{self._base_url()}/v1/models"
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            guard_api_content_type(response, streaming=False)
        data = response.json()
        return [item["id"] for item in data.get("data", []) if isinstance(item, dict) and "id" in item]

    # ---- Google Gemini ----
    def _gemini_chat(self, messages: list[Message], *, stream: bool):
        key = self._require_key()
        system, turns = split_system(messages)
        payload: dict = {
            "contents": [
                {"role": "model" if m.role == "assistant" else "user", "parts": gemini_parts(m)}
                for m in turns
            ],
            "generationConfig": {"maxOutputTokens": self._output_tokens(), "temperature": 0.2},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        apply_thinking_payload(payload, self.settings)
        base = f"{self._base_url()}/v1beta/models/{self.settings.model}"
        headers = {"Content-Type": "application/json"}
        if not stream:
            url = f"{base}:generateContent?key={key}"
            response = self._http().post(url, headers=headers, json=payload)
            raise_for_status_with_body(response)
            data = response.json()
            self._record_usage(data)
            try:
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts)
            except (KeyError, IndexError, TypeError) as exc:
                compact = json.dumps(data, ensure_ascii=False)[:1000]
                raise RuntimeError(f"Unexpected Gemini response: {compact}") from exc
        url = f"{base}:streamGenerateContent?alt=sse&key={key}"
        return self._gemini_stream(url, headers, payload)

    def _gemini_stream(self, url: str, headers: dict, payload: dict) -> Iterator[str]:
        usage = UsageStats()
        with self._http().stream("POST", url, headers=headers, json=payload) as response:
            raise_for_status_with_body(response)
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if not chunk:
                    continue
                try:
                    data = json.loads(chunk)
                    usage.absorb_snapshot(self._usage_snapshot(data))
                    parts = data["candidates"][0]["content"]["parts"]
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                for part in parts:
                    text = part.get("text")
                    if text:
                        yield text
        self.last_usage = usage
        self.usage.merge(usage)

    def _gemini_models(self) -> list[str]:
        key = self._require_key()
        url = f"{self._base_url()}/v1beta/models?key={key}"
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url)
            response.raise_for_status()
        data = response.json()
        names = []
        for item in data.get("models", []):
            name = item.get("name", "") if isinstance(item, dict) else ""
            if name:
                names.append(name.removeprefix("models/"))
        return names


def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    """Pull system messages into a single top-level string; keep the rest as turns.

    Anthropic and Gemini take the system prompt out-of-band rather than as a message
    with role ``system``.
    """
    system_parts: list[str] = []
    turns: list[Message] = []
    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
        else:
            turns.append(message)
    return "\n\n".join(system_parts), turns


def raise_for_status_with_body(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Streaming responses haven't read the body yet — reading .text would raise
        # httpx.ResponseNotRead and mask the real upstream error. Read it first.
        try:
            body = response.text
        except httpx.ResponseNotRead:
            try:
                response.read()
                body = response.text
            except Exception:
                body = "<unreadable body>"
        body = (body or "").strip()[:1000]
        detail = extract_error_message(body)
        raise RuntimeError(f"API error {response.status_code}: {detail or body or response.reason_phrase}") from exc


def extract_error_message(body: str) -> str:
    """Pull the human-readable message out of provider error JSON if present.

    Handles OpenAI/DeepSeek ({"error":{"message":...}}), Anthropic
    ({"error":{"message":...}} / {"type":"error",...}) and Gemini
    ({"error":{"message":...,"status":...}}) shapes.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        message = err.get("message") or err.get("msg") or ""
        code = err.get("code") or err.get("status") or err.get("type") or ""
        return f"{message}" + (f" ({code})" if code and message else "")
    if isinstance(err, str):
        return err
    detail = data.get("detail")
    if isinstance(detail, list):
        messages = []
        for item in detail:
            if isinstance(item, dict):
                message = item.get("message") or item.get("msg") or item.get("detail")
                if message:
                    messages.append(str(message))
            elif item:
                messages.append(str(item))
        return "; ".join(messages[:3])
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("msg") or detail.get("detail") or "")
    if isinstance(detail, str):
        return detail
    for key in ("message", "msg", "error_description"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def _effort_budget_tokens(effort: str | None) -> int:
    """Map a reasoning-effort label to a thinking-token budget for providers that take
    an explicit budget (Anthropic, Gemini) instead of an effort string."""
    return {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 24576}.get((effort or "").lower(), 8192)


def apply_thinking_payload(payload: dict, settings: Settings) -> None:
    """Insert the upstream reasoning/thinking parameter in each provider's native shape.

    Codex-style: reasoning is an upstream API parameter, not a separate local turn. Each
    provider spells it differently, so this must run for every format — not just chat."""
    fmt = normalize_format(getattr(settings, "provider_format", "deepseek"))
    enabled = bool(settings.thinking_enabled)
    effort = settings.reasoning_effort

    if fmt == "deepseek":
        payload["thinking"] = {"type": "enabled" if enabled else "disabled"}
    elif fmt == "openai":
        # OpenAI chat completions (o-series / gpt-5): top-level reasoning_effort.
        if enabled and effort:
            payload["reasoning_effort"] = effort
    elif fmt == "openai-responses":
        # OpenAI Responses API wants the nested shape reasoning:{effort}. This is what
        # Codex sends; top-level reasoning_effort is silently ignored here.
        if enabled and effort:
            payload["reasoning"] = {"effort": effort}
    elif fmt == "anthropic":
        # Anthropic extended thinking: thinking:{type:enabled, budget_tokens:N}, budget
        # must be >=1024 and strictly less than max_tokens.
        if enabled and effort:
            max_tokens = int(payload.get("max_tokens") or 0)
            budget = _effort_budget_tokens(effort)
            if max_tokens:
                budget = min(budget, max_tokens - 1)
            if budget >= 1024:
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif fmt == "gemini":
        # Gemini 2.5 thinking: generationConfig.thinkingConfig.thinkingBudget.
        gen = payload.setdefault("generationConfig", {})
        if enabled and effort:
            gen["thinkingConfig"] = {"thinkingBudget": _effort_budget_tokens(effort), "includeThoughts": False}
