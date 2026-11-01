"""Shared fixtures: a fake LLM provider for web/loop tests (no network).

M3 adds the loop surface: `extract_json` (the intent call) and `scripted`
chat responses (the DM tool rounds — one response per loop iteration, the
last one repeats once the script is exhausted).
"""

from __future__ import annotations

from typing import Any

import pytest

from rpg.llm.provider import LLMResponse, ProviderConfig, ProviderError, ToolCall, Usage


def tool_call_response(call_id: str, name: str, arguments: dict[str, Any]) -> LLMResponse:
    """One assistant reply that is a single tool call (the loop's fuel)."""
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        usage=Usage(prompt_tokens=10, completion_tokens=5),
        raw={},
    )


def text_response(content: str) -> LLMResponse:
    """One assistant reply that is narration (the loop's final round)."""
    return LLMResponse(
        content=content,
        tool_calls=[],
        usage=Usage(prompt_tokens=10, completion_tokens=5),
        raw={},
    )


class FakeProvider:
    """Canned-response stand-in for LLMProvider (same surface)."""

    def __init__(
        self,
        content: str = "Hello, adventurer. The dungeon awaits.",
        error: str | None = None,
        tool_calls: list | None = None,
        scripted: list[LLMResponse] | None = None,
        extract_json_result: dict[str, Any] | None = None,
        extract_json_routes: dict[str, dict[str, Any]] | None = None,
        extract_json_queue: list[dict[str, Any]] | None = None,
    ) -> None:
        self.config = ProviderConfig(base_url="fake://test", api_key="x", model="test-model")
        self.calls: list[dict[str, Any]] = []
        self._content = content
        self._error = error
        self._tool_calls = tool_calls or []
        self._scripted = list(scripted) if scripted is not None else []
        self._extract_json_result = extract_json_result or {"check": None, "combat": False}
        # M5: route canned extract_json responses by the exact system prompt
        # (intent / fold / facts / refresh are distinct calls); a queued
        # response, when present, is served first (FIFO) for retry tests.
        self._extract_json_routes = extract_json_routes or {}
        self._extract_json_queue = list(extract_json_queue) if extract_json_queue else []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.8,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools})
        if self._error:
            raise ProviderError(self._error)
        if self._scripted:
            response = self._scripted.pop(0)
            if not self._scripted:  # the last script line repeats (the loop must end)
                self._scripted.append(response)
            return response
        return LLMResponse(
            content=self._content,
            tool_calls=self._tool_calls,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            raw={},
        )

    async def extract_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        self.calls.append({"extract_json": True, "system": system, "user": user})
        if self._error:
            raise ProviderError(self._error)
        raw = None
        if self._extract_json_queue:
            raw = self._extract_json_queue.pop(0)
        elif system in self._extract_json_routes:
            raw = self._extract_json_routes[system]
        if raw is None:
            raw = self._extract_json_result
        # A non-dict canned value passes through (tests the caller's guard).
        return dict(raw) if isinstance(raw, dict) else raw

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()
