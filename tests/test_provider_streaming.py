"""Tests for provider streaming support (generate_stream)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from synapse.models import AuthProfile
from synapse.providers import (
    AzureOpenAIProvider,
    CodexCliProvider,
    ModelRouter,
    OpenAICodexResponsesProvider,
)
from synapse.store import SQLiteStore
from synapse.streaming.sink import NullSink


def _auth_profile(provider: str = "azure-openai", **settings: object) -> AuthProfile:
    defaults = {
        "endpoint": "https://test.openai.azure.com",
        "api_key": "test-key",
        "deployment": "gpt-4",
    }
    defaults.update(settings)
    return AuthProfile(provider=provider, model="gpt-4", settings=defaults)


# ---------------------------------------------------------------------------
# AzureOpenAIProvider.generate_stream
# ---------------------------------------------------------------------------


class _AsyncLineIter:
    """Async iterable that yields SSE lines from raw strings."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


def _stream_ctx(response):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestAzureOpenAIProviderStream:
    @pytest.mark.asyncio
    async def test_generate_stream_pushes_deltas_to_sink(self) -> None:
        lines = [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "data: [DONE]",
        ]

        mock_response = MagicMock()
        mock_response.aiter_lines = MagicMock(return_value=_AsyncLineIter(lines))
        mock_response.raise_for_status = MagicMock()

        client = AsyncMock(spec=httpx.AsyncClient)
        client.stream = MagicMock(return_value=_stream_ctx(mock_response))

        profile = _auth_profile()
        provider = AzureOpenAIProvider(profile, client=client)
        sink = NullSink()
        result = await provider.generate_stream([], sink=sink)
        assert "Hello" in result.text
        assert "world" in result.text
        assert sink.accumulated_text == result.text

    @pytest.mark.asyncio
    async def test_generate_stream_without_sink_returns_text(self) -> None:
        lines = [
            'data: {"choices":[{"delta":{"content":"Just text"}}]}',
            "data: [DONE]",
        ]

        mock_response = MagicMock()
        mock_response.aiter_lines = MagicMock(return_value=_AsyncLineIter(lines))
        mock_response.raise_for_status = MagicMock()

        client = AsyncMock(spec=httpx.AsyncClient)
        client.stream = MagicMock(return_value=_stream_ctx(mock_response))

        profile = _auth_profile()
        provider = AzureOpenAIProvider(profile, client=client)
        result = await provider.generate_stream([])
        assert result.text == "Just text"


# ---------------------------------------------------------------------------
# OpenAICodexResponsesProvider.generate_stream
# ---------------------------------------------------------------------------


class TestCodexResponsesProviderStream:
    @pytest.mark.asyncio
    async def test_generate_stream_pushes_deltas(self) -> None:
        sse_lines = [
            'event: response.output_text.delta',
            'data: {"delta":"Hi "}',
            '',
            'event: response.output_text.delta',
            'data: {"delta":"there"}',
            '',
            'event: response.completed',
            'data: {"response":{"output_text":"Hi there"}}',
            '',
        ]

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.aiter_lines = MagicMock(return_value=_AsyncLineIter(sse_lines))

        client = AsyncMock(spec=httpx.AsyncClient)
        client.stream = MagicMock(return_value=_stream_ctx(mock_response))

        profile = _auth_profile(
            provider="openai-codex",
            access_token="test-token",
            endpoint="https://chatgpt.com/backend-api/codex/responses",
        )
        provider = OpenAICodexResponsesProvider(profile, client=client)
        sink = NullSink()
        result = await provider.generate_stream(
            [{"role": "user", "content": "hi"}],
            sink=sink,
        )
        assert "Hi" in result.text
        assert "there" in result.text
        assert sink.accumulated_text == "Hi there"


# ---------------------------------------------------------------------------
# ModelRouter.generate_stream
# ---------------------------------------------------------------------------


class TestModelRouterStream:
    @pytest.mark.asyncio
    async def test_generate_stream_delegates_to_provider(self) -> None:
        auth_store = MagicMock()
        profile = _auth_profile()
        auth_store.resolve.return_value = profile

        router = ModelRouter(auth_store)
        sink = NullSink()

        with patch.object(
            AzureOpenAIProvider,
            "generate_stream",
            new_callable=AsyncMock,
            return_value="streamed text",
        ) as mock_gen:
            result = await router.generate_stream(
                [{"role": "user", "content": "hi"}],
                sink=sink,
            )
            assert result == "streamed text"
            mock_gen.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_stream_returns_none_without_profile(self) -> None:
        auth_store = MagicMock()
        auth_store.resolve.return_value = None
        router = ModelRouter(auth_store)
        result = await router.generate_stream([])
        assert result is None


class TestCodexCliUsageTracking:
    @pytest.mark.asyncio
    async def test_generate_records_usage_with_null_tokens(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "runtime.sqlite3")
        store.initialize()
        profile = AuthProfile(provider="codex-cli", model="gpt-5.4", settings={})
        provider = CodexCliProvider(profile, workdir=str(tmp_path), store=store)

        async def _fake_subprocess(*argv, **kwargs):
            output_index = argv.index("-o") + 1
            output_path = argv[output_index]
            with open(output_path, "w", encoding="utf-8") as handle:
                handle.write("cli response")
            process = MagicMock()
            process.returncode = 0
            process.communicate = AsyncMock(return_value=(b"", b""))
            return process

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_fake_subprocess)):
            result = await provider.generate(
                [{"role": "user", "content": "hello"}],
                run_id="run-1",
                session_key="sess-1",
            )

        assert result == "cli response"
        usage_events = store.list_usage_events(run_id="run-1")
        assert len(usage_events) == 1
        assert usage_events[0].provider == "codex-cli"
        assert usage_events[0].prompt_tokens is None
        assert usage_events[0].completion_tokens is None
        assert usage_events[0].status == "ok"


# ---------------------------------------------------------------------------
# TelegramAdapter.edit_text + send_typing_action
# ---------------------------------------------------------------------------


class TestTelegramAdapterEditing:
    def test_edit_text(self) -> None:
        from synapse.adapters import TelegramAdapter

        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        mock_response.raise_for_status = MagicMock()

        client = MagicMock()
        client.post.return_value = mock_response

        adapter = TelegramAdapter(token="test-token", client=client)
        result = adapter.edit_text("12345", 42, "Updated text")
        assert result == {"ok": True}
        client.post.assert_called_once()
        call_args = client.post.call_args
        assert "editMessageText" in call_args[0][0]
        body = call_args[1]["json"]
        assert body["chat_id"] == "12345"
        assert body["message_id"] == 42

    def test_send_typing_action(self) -> None:
        from synapse.adapters import TelegramAdapter

        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}

        client = MagicMock()
        client.post.return_value = mock_response

        adapter = TelegramAdapter(token="test-token", client=client)
        adapter.send_typing_action("12345")
        client.post.assert_called_once()
        call_args = client.post.call_args
        assert "sendChatAction" in call_args[0][0]
        assert call_args[1]["json"]["action"] == "typing"

    def test_edit_text_requires_token(self) -> None:
        from synapse.adapters import TelegramAdapter

        adapter = TelegramAdapter(token=None)
        with pytest.raises(RuntimeError, match="not configured"):
            adapter.edit_text("12345", 42, "text")
