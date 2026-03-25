"""Tests for ChatCompletionsProvider — standard OpenAI-compatible /chat/completions."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from synapse.models import AuthProfile
from synapse.providers_chat import ChatCompletionsProvider, _parse_chat_tool_calls
from synapse.providers import LLMResponse, ProviderToolCall


def _profile(base_url="http://localhost:11434/v1", api_key="test-key", model="gemini-2.0-flash"):
    return AuthProfile(
        provider="custom",
        model=model,
        source="environment",
        settings={"base_url": base_url, "api_key": api_key, "transport": "chat"},
    )


class FakeHttpResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeStreamResponse:
    """Fake httpx streaming response for SSE."""

    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def test_build_request_url_and_headers():
    provider = ChatCompletionsProvider(_profile(), client=MagicMock())
    url, headers, body = provider._build_request(
        [{"role": "user", "content": "hi"}],
        system_prompt="You are helpful.",
        tools=None,
        stream=False,
    )
    assert url == "http://localhost:11434/v1/chat/completions"
    assert headers["Authorization"] == "Bearer test-key"
    assert body["model"] == "gemini-2.0-flash"
    assert body["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert "stream" not in body


def test_build_request_with_tools_and_stream():
    provider = ChatCompletionsProvider(_profile(), client=MagicMock())
    tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
    url, headers, body = provider._build_request(
        [{"role": "user", "content": "hi"}],
        system_prompt=None,
        tools=tools,
        stream=True,
    )
    assert body["tools"] == tools
    assert body["stream"] is True
    assert body["messages"][0]["role"] == "user"  # no system message


def test_build_request_strips_trailing_slash():
    provider = ChatCompletionsProvider(
        _profile(base_url="http://api.example.com/v1/"), client=MagicMock()
    )
    url, _, _ = provider._build_request([], system_prompt=None, tools=None, stream=False)
    assert url == "http://api.example.com/v1/chat/completions"


def test_parse_chat_tool_calls_from_message():
    message = {
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "memory_read", "arguments": '{"key": "foo"}'},
            }
        ]
    }
    result = _parse_chat_tool_calls(message)
    assert len(result) == 1
    assert result[0].id == "call_1"
    assert result[0].name == "memory_read"
    assert result[0].arguments == {"key": "foo"}


def test_parse_chat_tool_calls_none_when_missing():
    assert _parse_chat_tool_calls({}) is None
    assert _parse_chat_tool_calls({"tool_calls": []}) is None


def test_parse_chat_tool_calls_bad_json_arguments():
    message = {
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "not-json"}}
        ]
    }
    result = _parse_chat_tool_calls(message)
    assert result[0].arguments == {}


@pytest.mark.asyncio
async def test_generate_non_streaming():
    """Non-streaming generate returns LLMResponse with text."""
    response_data = {
        "choices": [{"message": {"role": "assistant", "content": "Hello!"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = FakeHttpResponse(response_data)
    provider = ChatCompletionsProvider(_profile(), client=mock_client)
    result = await provider.generate(
        [{"role": "user", "content": "hi"}],
        system_prompt="Be helpful.",
    )
    assert isinstance(result, LLMResponse)
    assert result.text == "Hello!"
    assert result.usage["total_tokens"] == 15


@pytest.mark.asyncio
async def test_generate_with_tool_calls():
    """Non-streaming generate with tool calls."""
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query": "test"}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = FakeHttpResponse(response_data)
    provider = ChatCompletionsProvider(_profile(), client=mock_client)
    result = await provider.generate(
        [{"role": "user", "content": "search for test"}],
        tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
    )
    assert result.tool_calls is not None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "web_search"


@pytest.mark.asyncio
async def test_generate_stream_pushes_to_sink():
    """Streaming pushes deltas to sink and returns complete text."""
    sse_lines = [
        'data: {"choices": [{"delta": {"role": "assistant", "content": ""}}]}',
        'data: {"choices": [{"delta": {"content": "Hello"}}]}',
        'data: {"choices": [{"delta": {"content": " world"}}]}',
        'data: {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}',
        "data: [DONE]",
    ]
    fake_stream = FakeStreamResponse(sse_lines)
    mock_client = MagicMock()
    mock_client.stream.return_value = fake_stream

    sink = AsyncMock()
    sink.push = AsyncMock()

    provider = ChatCompletionsProvider(_profile(), client=mock_client)
    result = await provider.generate_stream(
        [{"role": "user", "content": "hi"}],
        sink=sink,
    )
    assert result.text == "Hello world"
    # sink.push called for each non-empty content delta
    push_calls = [call.args[0] for call in sink.push.call_args_list]
    assert "Hello" in push_calls
    assert " world" in push_calls


@pytest.mark.asyncio
async def test_generate_stream_tool_calls():
    """Streaming with tool call deltas assembled into ProviderToolCall."""
    sse_lines = [
        'data: {"choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "memory_read", "arguments": ""}}]}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\\"key"}}]}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\\": \\"val\\"}"}}]}}]}',
        'data: {"choices": [{"delta": {}}]}',
        "data: [DONE]",
    ]
    fake_stream = FakeStreamResponse(sse_lines)
    mock_client = MagicMock()
    mock_client.stream.return_value = fake_stream
    provider = ChatCompletionsProvider(_profile(), client=mock_client)
    result = await provider.generate_stream(
        [{"role": "user", "content": "read memory"}],
        tools=[{"type": "function", "function": {"name": "memory_read", "parameters": {}}}],
    )
    assert result.tool_calls is not None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "memory_read"
    assert result.tool_calls[0].arguments == {"key": "val"}


@pytest.mark.asyncio
async def test_generate_records_usage():
    """Usage events are recorded on successful calls."""
    response_data = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = FakeHttpResponse(response_data)
    mock_store = MagicMock()
    provider = ChatCompletionsProvider(_profile(), client=mock_client, store=mock_store)
    await provider.generate([{"role": "user", "content": "hi"}])
    mock_store.append_usage_event.assert_called_once()
    call_kwargs = mock_store.append_usage_event.call_args
    assert call_kwargs[1]["provider"] == "custom" or call_kwargs.kwargs.get("provider") == "custom"
