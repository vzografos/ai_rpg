"""Provider-agnostic LLM client.

One code path covers any OpenAI-compatible endpoint: OpenAI / OpenRouter
(API) or Ollama / vLLM (local). Configuration comes from environment so the
same image works against both without code changes (the provider layer).

Design goals:
- Thin wrapper: no vendor SDK, just HTTP against /chat/completions.
- Tool calling passthrough for the DM loop; a "JSON in prose" fallback for
  small local models that cannot reliably emit tool calls (dnd-llm-game
  pattern, risk 1).
- Usage metering: every call returns token accounting so the context builder
  can bound the prompt (roadmap M2/M5).
- Injectable httpx client so tests run hermetic (no network).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from rpg import __version__


class ProviderError(RuntimeError):
    """Raised when the LLM endpoint fails or returns an unusable payload."""


@dataclass(frozen=True)
class ProviderConfig:
    """Connection settings, read once from the environment."""

    base_url: str
    api_key: str
    model: str
    timeout: float = 120.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ProviderConfig:
        e = os.environ if env is None else env
        # Fallback (no .env): the in-compose `ollama` sibling service. The
        # documented default for play is an API model — set LLM_* in .env.
        base_url = e.get("LLM_BASE_URL", "http://ollama:11434/v1").rstrip("/")
        api_key = e.get("LLM_API_KEY", "ollama")
        model = e.get("LLM_MODEL", "llama3.1:8b")
        try:
            timeout = float(e.get("LLM_TIMEOUT", "120"))
        except ValueError:
            timeout = 120.0
        return cls(base_url=base_url, api_key=api_key, model=model, timeout=timeout)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider:
    """Async client for any OpenAI-compatible /chat/completions endpoint."""

    def __init__(
        self,
        config: ProviderConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "User-Agent": f"ai-rpg/{__version__}",
            },
            timeout=config.timeout,
        )
        self.total_usage = Usage()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.8,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """One chat completion. `messages` are OpenAI-format dicts."""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        resp = await self._client.post("/chat/completions", json=payload)
        if resp.status_code >= 400:
            raise ProviderError(f"LLM HTTP {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        raw_usage = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
            completion_tokens=int(raw_usage.get("completion_tokens", 0)),
        )
        self.total_usage.prompt_tokens += usage.prompt_tokens
        self.total_usage.completion_tokens += usage.completion_tokens

        tool_calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=(tc.get("function") or {}).get("name", ""),
                arguments=_parse_args((tc.get("function") or {}).get("arguments")),
            )
            for tc in (message.get("tool_calls") or [])
        ]
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            usage=usage,
            raw=data,
        )

    async def ping(self) -> str:
        """Cheap round-trip used by the web UI to prove a model is reachable.

        Returns the model's one-line greeting (the "LLM says hello" signal).
        """
        messages = [
            {"role": "system", "content": "You are a dungeon master. Reply in one short sentence."},
            {"role": "user", "content": "Say hello to the player."},
        ]
        response = await self.chat(messages, temperature=0.3, max_tokens=64)
        content = (response.content or "").strip()
        return content or "ok"

    async def extract_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        """Utility call that must return a JSON object (intent decision, fact
        extraction, spine generation).

        Works for both tool-capable and prose-only models: we ask for a single
        JSON object and parse it, tolerating code fences and surrounding prose
        (the dnd-llm-game "prompt for JSON + validate" pattern).
        """
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": user + "\n\nRespond with a single JSON object only. No prose.",
            },
        ]
        response = await self.chat(messages, temperature=0.0, max_tokens=max_tokens)
        text = response.content or ""
        parsed = _extract_json_object(text)
        if parsed is None:
            raise ProviderError(f"Model returned no parseable JSON: {text[:200]!r}")
        return parsed


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        recovered = _extract_json_object(raw)
        return recovered or {"raw": raw}


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort pull of the first JSON object out of a model's reply."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None
