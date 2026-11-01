"""Hermetic tests for the provider client (httpx.MockTransport, no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from rpg.llm.provider import (
    LLMProvider,
    ProviderConfig,
    ProviderError,
    _extract_json_object,
)


def make_provider(handler) -> LLMProvider:
    """Provider over a MockTransport, with the same headers the real client sets."""
    transport = httpx.MockTransport(handler)
    config = ProviderConfig(base_url="http://fake", api_key="sekrit", model="test-model")
    client = httpx.AsyncClient(
        transport=transport,
        base_url=config.base_url,
        headers={"Authorization": f"Bearer {config.api_key}"},
        timeout=config.timeout,
    )
    return LLMProvider(config, client=client)


def ok_handler(body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        assert request.headers["authorization"] == "Bearer sekrit"
        sent = json.loads(request.content)
        assert sent["model"] == "test-model"
        return httpx.Response(200, json=body)

    return handler


@pytest.mark.asyncio
async def test_chat_returns_content_and_usage():
    provider = make_provider(
        ok_handler(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello there."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }
        )
    )
    response = await provider.chat([{"role": "user", "content": "hi"}])
    assert response.content == "Hello there."
    assert response.usage.total_tokens == 10
    assert provider.total_usage.total_tokens == 10
    await provider.aclose()


@pytest.mark.asyncio
async def test_chat_parses_tool_calls():
    provider = make_provider(
        ok_handler(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "roll_check",
                                        "arguments": json.dumps({"ability": "strength", "dc": 15}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )
    )
    response = await provider.chat(
        [{"role": "user", "content": "check strength"}],
        tools=[{"type": "function", "function": {"name": "roll_check", "parameters": {}}}],
    )
    assert response.content is None
    assert [tc.name for tc in response.tool_calls] == ["roll_check"]
    assert response.tool_calls[0].arguments == {"ability": "strength", "dc": 15}
    await provider.aclose()


@pytest.mark.asyncio
async def test_http_error_raises_provider_error():
    provider = make_provider(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(ProviderError, match="500"):
        await provider.chat([{"role": "user", "content": "hi"}])
    await provider.aclose()


@pytest.mark.asyncio
async def test_transport_errors_map_to_provider_error():
    # A read timeout mid-generation is a provider failure (the pipeline's
    # "the turn is never lost" handling takes over), not an uncaught crash.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("the model took too long")

    provider = make_provider(handler)
    with pytest.raises(ProviderError, match="transport error"):
        await provider.chat([{"role": "user", "content": "hi"}])
    await provider.aclose()


@pytest.mark.asyncio
async def test_ping_round_trip():
    provider = make_provider(
        ok_handler(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "Welcome to the dungeon."}}
                ],
                "usage": {},
            }
        )
    )
    assert await provider.ping() == "Welcome to the dungeon."
    await provider.aclose()


@pytest.mark.asyncio
async def test_extract_json_tolerates_prose_and_fences():
    provider = make_provider(
        ok_handler(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                'Sure! Here you go:\n```json\n'
                                '{"needs_check": true, "ability": "dexterity"}\n'
                                "```"
                            ),
                        }
                    }
                ],
                "usage": {},
            }
        )
    )
    result = await provider.extract_json("You are a JSON emitter.", "Decide.")
    assert result == {"needs_check": True, "ability": "dexterity"}
    await provider.aclose()


@pytest.mark.asyncio
async def test_extract_json_rejects_garbage():
    body = {"choices": [{"message": {"role": "assistant", "content": "no json here"}}], "usage": {}}
    provider = make_provider(ok_handler(body))
    with pytest.raises(ProviderError, match="no parseable JSON"):
        await provider.extract_json("sys", "user")
    await provider.aclose()


def test_config_from_env_defaults_to_ollama_sibling(monkeypatch):
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    config = ProviderConfig.from_env()
    assert config.base_url == "http://ollama:11434/v1"
    assert config.api_key == "ollama"
    assert config.model == "llama3.1:8b"


def test_config_from_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1/")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    config = ProviderConfig.from_env()
    assert config.base_url == "https://api.openai.com/v1"
    assert config.api_key == "sk-test"
    assert config.model == "gpt-4o-mini"


def test_extract_json_object_pulls_first_object_from_prose():
    text = 'The decision is {"needs_check": false}. That is all.'
    assert _extract_json_object(text) == {"needs_check": False}
    assert _extract_json_object("nothing at all") is None
