"""Tests for provider tool calling support (Step A6)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse.models import AuthProfile
from synapse.providers import (
    AzureOpenAIProvider,
    LLMResponse,
    ModelRouter,
    OpenAICodexResponsesProvider,
    ProviderToolCall,
)
from synapse.streaming.sink import NullSink


SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _codex_profile(**overrides) -> AuthProfile:
    settings = {"access_token": "tok", "endpoint": "https://example.com/responses"}
    settings.update(overrides)
    return AuthProfile(provider="openai-codex", model="gpt-5.4", source="test", settings=settings)


def _azure_profile(**overrides) -> AuthProfile:
    settings = {"endpoint": "https://test.openai.azure.com", "api_key": "key"}
    settings.update(overrides)
    return AuthProfile(provider="azure-openai", model="gpt-4", source="test", settings=settings)


class _AsyncLineIter:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._index]
        self._index += 1
        return line


def _stream_ctx(resp):
    class AsyncCM:
        async def __aenter__(self):
            return resp

        async def __aexit__(self, *args):
            pass

    return AsyncCM()


# ---------------------------------------------------------------------------
# OpenAICodexResponsesProvider — text only (no tools)
# ---------------------------------------------------------------------------


class TestCodexResponsesTextOnly:
    @pytest.mark.asyncio
    async def test_generate_without_tools_returns_llm_response(self) -> None:
        sse = [
            "event: response.completed",
            'data: {"response": {"output_text": "Hello!"}}',
            "",
        ]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aiter_lines = MagicMock(return_value=_AsyncLineIter(sse))
        client = MagicMock()
        client.stream = MagicMock(return_value=_stream_ctx(mock_resp))

        provider = OpenAICodexResponsesProvider(_codex_profile(), client=client)
        result = await provider.generate([{"role": "user", "content": "hi"}])

        assert isinstance(result, LLMResponse)
        assert result.text == "Hello!"
        assert result.tool_calls is None


# ---------------------------------------------------------------------------
# OpenAICodexResponsesProvider — tool calls
# ---------------------------------------------------------------------------


class TestCodexResponsesToolCalls:
    @pytest.mark.asyncio
    async def test_generate_with_tools_parses_function_calls(self) -> None:
        """Test that function_call output items are parsed into ProviderToolCall."""
        completed_payload = {
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_abc",
                        "name": "get_weather",
                        "arguments": '{"city": "London"}',
                    }
                ]
            }
        }
        sse = [
            "event: response.completed",
            f"data: {json.dumps(completed_payload)}",
            "",
        ]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aiter_lines = MagicMock(return_value=_AsyncLineIter(sse))
        client = MagicMock()
        client.stream = MagicMock(return_value=_stream_ctx(mock_resp))

        provider = OpenAICodexResponsesProvider(_codex_profile(), client=client)
        result = await provider.generate(
            [{"role": "user", "content": "weather in London"}],
            tools=SAMPLE_TOOLS,
        )

        assert isinstance(result, LLMResponse)
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc.id == "call_abc"
        assert tc.name == "get_weather"
        assert tc.arguments == {"city": "London"}
        # tools should be in the payload
        call_json = client.stream.call_args[1].get("json") or client.stream.call_args[0][3] if len(client.stream.call_args[0]) > 3 else None
        # Verify tools were sent
        sent_json = client.stream.call_args.kwargs.get("json")
        assert sent_json is not None
        assert "tools" in sent_json
        # Tools should be converted to Responses API format (flat, no nested "function")
        expected_tools = [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get weather for a city",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ]
        assert sent_json["tools"] == expected_tools

    @pytest.mark.asyncio
    async def test_streaming_tool_calls_via_sse_events(self) -> None:
        """Test that streaming function call events are accumulated correctly."""
        sse = [
            "event: response.output_item.added",
            'data: {"item": {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": ""}}',
            "",
            "event: response.function_call_arguments.delta",
            'data: {"call_id": "call_1", "delta": "{\\"city"}',
            "",
            "event: response.function_call_arguments.delta",
            'data: {"call_id": "call_1", "delta": ": \\"Paris\\"}"}',
            "",
            "event: response.function_call_arguments.done",
            'data: {"call_id": "call_1", "arguments": "{\\"city\\": \\"Paris\\"}"}',
            "",
        ]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aiter_lines = MagicMock(return_value=_AsyncLineIter(sse))
        client = MagicMock()
        client.stream = MagicMock(return_value=_stream_ctx(mock_resp))

        provider = OpenAICodexResponsesProvider(_codex_profile(), client=client)
        result = await provider.generate_stream(
            [{"role": "user", "content": "weather in Paris"}],
            tools=SAMPLE_TOOLS,
        )

        assert isinstance(result, LLMResponse)
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_weather"
        assert result.tool_calls[0].arguments == {"city": "Paris"}


# ---------------------------------------------------------------------------
# AzureOpenAIProvider — tool calls
# ---------------------------------------------------------------------------


class TestAzureToolCalls:
    @pytest.mark.asyncio
    async def test_generate_with_tools_parses_response(self) -> None:
        api_response = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_xyz",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "Tokyo"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_response
        mock_resp.raise_for_status = MagicMock()
        client.post = AsyncMock(return_value=mock_resp)

        provider = AzureOpenAIProvider(_azure_profile(), client=client)
        result = await provider.generate(
            [{"role": "user", "content": "weather in Tokyo"}],
            tools=SAMPLE_TOOLS,
        )

        assert isinstance(result, LLMResponse)
        assert result.text is None
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc.id == "call_xyz"
        assert tc.name == "get_weather"
        assert tc.arguments == {"city": "Tokyo"}
        assert result.usage is not None

    @pytest.mark.asyncio
    async def test_generate_without_tools_returns_text(self) -> None:
        api_response = {
            "choices": [{"message": {"content": "Hello!", "tool_calls": None}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_response
        mock_resp.raise_for_status = MagicMock()
        client.post = AsyncMock(return_value=mock_resp)

        provider = AzureOpenAIProvider(_azure_profile(), client=client)
        result = await provider.generate([{"role": "user", "content": "hi"}])

        assert isinstance(result, LLMResponse)
        assert result.text == "Hello!"
        assert result.tool_calls is None


# ---------------------------------------------------------------------------
# ModelRouter backward compat
# ---------------------------------------------------------------------------


class TestModelRouterBackwardCompat:
    @pytest.mark.asyncio
    async def test_generate_still_returns_str(self) -> None:
        """ModelRouter.generate() should still return str | None for old callers."""
        auth_store = MagicMock()
        auth_store.resolve.return_value = _azure_profile()

        api_response = {
            "choices": [{"message": {"content": "world"}}],
        }
        client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_response
        mock_resp.raise_for_status = MagicMock()
        client.post = AsyncMock(return_value=mock_resp)

        router = ModelRouter(auth_store, client=client)
        result = await router.generate([{"role": "user", "content": "hi"}])

        assert isinstance(result, str)
        assert result == "world"

    @pytest.mark.asyncio
    async def test_chat_returns_llm_response(self) -> None:
        """ModelRouter.chat() should return LLMResponse."""
        auth_store = MagicMock()
        auth_store.resolve.return_value = _azure_profile()

        api_response = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "tc1",
                                "type": "function",
                                "function": {"name": "fn1", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ],
        }
        client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_response
        mock_resp.raise_for_status = MagicMock()
        client.post = AsyncMock(return_value=mock_resp)

        router = ModelRouter(auth_store, client=client)
        result = await router.chat(
            [{"role": "user", "content": "hi"}],
            tools=SAMPLE_TOOLS,
        )

        assert isinstance(result, LLMResponse)
        assert result.tool_calls is not None
        assert result.tool_calls[0].name == "fn1"

    @pytest.mark.asyncio
    async def test_chat_returns_none_without_profile(self) -> None:
        auth_store = MagicMock()
        auth_store.resolve.return_value = None

        router = ModelRouter(auth_store)
        result = await router.chat([{"role": "user", "content": "hi"}])

        assert result is None
