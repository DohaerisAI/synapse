"""Tests for streaming wiring through gateway and runtime."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestGatewayStreamSink:
    """Verify Gateway.ingest passes stream_sink to react loop."""

    @pytest.mark.asyncio
    async def test_ingest_accepts_stream_sink_param(self) -> None:
        """Gateway.ingest() should accept optional stream_sink kwarg."""
        from synapse.gateway.core import Gateway
        import inspect

        sig = inspect.signature(Gateway.ingest)
        assert "stream_sink" in sig.parameters


class TestRuntimeStreamWiring:
    """Verify runtime creates TelegramDraftStream and wires it through."""

    @pytest.mark.asyncio
    async def test_async_handle_telegram_event_creates_stream(self) -> None:
        """Runtime should create TelegramDraftStream for telegram events."""
        from synapse.runtime import Runtime

        runtime = MagicMock(spec=Runtime)
        runtime.telegram = MagicMock()
        runtime.telegram.token = "test-token"
        runtime.gateway = MagicMock()

        mock_result = MagicMock()
        mock_result.queued = False
        mock_result.reply_text = "Hello"
        mock_result.run_id = "run-1"
        mock_result.suppress_delivery = False
        runtime.gateway.ingest = AsyncMock(return_value=mock_result)

        event = MagicMock()
        event.adapter = "telegram"
        event.channel_id = "12345"

        await Runtime.async_handle_telegram_event(runtime, event)

        runtime.gateway.ingest.assert_called_once()
        call_kwargs = runtime.gateway.ingest.call_args
        assert "stream_sink" in call_kwargs.kwargs or (
            len(call_kwargs.args) > 1 and call_kwargs.args[1] is not None
        )

    @pytest.mark.asyncio
    async def test_non_telegram_event_no_stream(self) -> None:
        """Runtime should not create stream for non-telegram adapters."""
        from synapse.runtime import Runtime

        runtime = MagicMock(spec=Runtime)
        runtime.telegram = MagicMock()
        runtime.telegram.token = "test-token"
        runtime.gateway = MagicMock()

        mock_result = MagicMock()
        mock_result.queued = False
        mock_result.reply_text = "Hello"
        mock_result.run_id = "run-1"
        mock_result.suppress_delivery = False
        runtime.gateway.ingest = AsyncMock(return_value=mock_result)
        runtime.deliver_result = MagicMock()

        event = MagicMock()
        event.adapter = "api"
        event.channel_id = "12345"

        await Runtime.async_handle_telegram_event(runtime, event)

        call_kwargs = runtime.gateway.ingest.call_args
        if "stream_sink" in (call_kwargs.kwargs or {}):
            assert call_kwargs.kwargs["stream_sink"] is None
