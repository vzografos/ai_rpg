"""Shared fixtures: a fake LLM provider for web/loop tests (no network)."""

from __future__ import annotations

from typing import Any

import pytest

from rpg.llm.provider import LLMResponse, ProviderConfig, ProviderError, Usage


class FakeProvider:
    """Canned-response stand-in for LLMProvider (same surface)."""

    def __init__(
        self,
        content: str = "Hello, adventurer. The dungeon awaits.",
        error: str | None = None,
        tool_calls: list | None = None,
    ) -> None:
        self.config = ProviderConfig(base_url="fake://test", api_key="x", model="test-model")
        self.calls: list[dict[str, Any]] = []
        self._content = content
        self._error = error
        self._tool_calls = tool_calls or []

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
        return LLMResponse(
            content=self._content,
            tool_calls=self._tool_calls,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            raw={},
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()
