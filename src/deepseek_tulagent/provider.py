from __future__ import annotations

from collections.abc import Iterable
import json
from typing import Iterator

import httpx

from .config import Settings
from .messages import Message


class DeepSeekClient:
    def __init__(self, settings: Settings, timeout: float | None = None):
        self.settings = settings
        self.timeout = timeout or settings.request_timeout
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def chat(self, messages: Iterable[Message]) -> str:
        if not self.settings.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")
        payload = {
            "model": self.settings.model,
            "messages": [message.to_api() for message in messages],
            "temperature": 0.2,
            "max_tokens": self.settings.max_tokens,
            "stream": False,
        }
        apply_thinking_payload(payload, self.settings)
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.settings.base_url}/chat/completions"
        response = self._http().post(url, headers=headers, json=payload)
        raise_for_status_with_body(response)
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            compact = json.dumps(data, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Unexpected DeepSeek response: {compact}") from exc

    def stream_chat(self, messages: Iterable[Message]) -> Iterator[str]:
        if not self.settings.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")
        payload = {
            "model": self.settings.model,
            "messages": [message.to_api() for message in messages],
            "temperature": 0.2,
            "max_tokens": self.settings.max_tokens,
            "stream": True,
        }
        apply_thinking_payload(payload, self.settings)
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.settings.base_url}/chat/completions"
        with self._http().stream("POST", url, headers=headers, json=payload) as response:
            raise_for_status_with_body(response)
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                chunk = line.removeprefix("data: ").strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                    delta = data["choices"][0].get("delta", {})
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                content = delta.get("content")
                if content:
                    yield content

    def models(self) -> list[str]:
        if not self.settings.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")
        headers = {"Authorization": f"Bearer {self.settings.api_key}"}
        url = f"{self.settings.base_url}/models"
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
        data = response.json()
        return [item["id"] for item in data.get("data", []) if isinstance(item, dict) and "id" in item]

    def ping(self) -> dict[str, object]:
        models = self.models()
        return {
            "base_url": self.settings.base_url,
            "model": self.settings.model,
            "model_available": self.settings.model in models,
            "models": models,
        }


def raise_for_status_with_body(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = response.text[:1000]
        raise RuntimeError(f"DeepSeek API error {response.status_code}: {body}") from exc


def apply_thinking_payload(payload: dict, settings: Settings) -> None:
    payload["thinking"] = {"type": "enabled" if settings.thinking_enabled else "disabled"}
    if settings.thinking_enabled and settings.reasoning_effort:
        payload["reasoning_effort"] = settings.reasoning_effort
