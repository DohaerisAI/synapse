"""Tests for streaming wiring through gateway and runtime."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synapse.streaming.sink import NullSink


class TestAgentLoopStreamingPath:
    """Verify agent loop uses plain-text streaming for simple chat messages."""

    @pytest.mark.asyncio
    async def test_simple_chat_uses_streaming_generation(self) -> None:
        """When stream_sink is set and no active task, agent loop should
        call generate_stream with a plain-text prompt (not JSON)."""
        from synapse.gateway.agent_loop import AgentLoop

        gw = MagicMock()
        gw.AGENT_LOOP_MAX_TURNS = 4
        gw.memory.read_current_task.return_value = None
        gw.context_builder.system_prompt.return_value = "You are a helpful assistant."
        gw.context_builder.attachment_summary.return_value = ""
        gw.context_builder.attachment_list.return_value = []
        gw.context_builder.is_heartbeat.return_value = False
        gw.store.append_run_event = MagicMock()
        gw.model_router.generate_stream = AsyncMock(return_value="Hello there!")
        gw.state_manager.finalize_reply_text.return_value = MagicMock(value="COMPLETED")
        gw._workflow.return_value = MagicMock()

        loop = AgentLoop(gw)
        run = MagicMock(run_id="r1", session_key="s1", user_id="u1")
        event = MagicMock(text="hello")
        sink = NullSink()

        result = await loop.run(run, event, MagicMock(), stream_sink=sink)

        # Should have used generate_stream, not generate
        gw.model_router.generate_stream.assert_called_once()
        gw.model_router.generate.assert_not_called()
        assert result.reply_text == "Hello there!"

    @pytest.mark.asyncio
    async def test_complex_task_uses_json_loop(self) -> None:
        """When there's an active task, agent loop should use the JSON
        generation path even when stream_sink is provided."""
        from synapse.gateway.agent_loop import AgentLoop

        gw = MagicMock()
        gw.AGENT_LOOP_MAX_TURNS = 4
        gw.memory.read_current_task.return_value = {"mode": "act", "intent": "gws.gmail"}
        gw.context_builder.system_prompt.return_value = "System prompt"
        gw.context_builder.attachment_summary.return_value = ""
        gw.context_builder.attachment_list.return_value = []
        gw.store.append_run_event = MagicMock()
        # JSON response from model
        gw.model_router.generate = AsyncMock(return_value='{"status":"reply","reply":"Done with task"}')
        gw._parse_model_json = MagicMock(return_value={"status": "reply", "reply": "Done with task"})
        gw.planner._is_action_follow_up.return_value = False
        gw.state_manager.finalize_reply_text.return_value = MagicMock(value="COMPLETED")
        gw._workflow.return_value = MagicMock()

        loop = AgentLoop(gw)
        run = MagicMock(run_id="r1", session_key="s1", user_id="u1")
        event = MagicMock(text="send that email")
        sink = NullSink()

        result = await loop.run(run, event, MagicMock(), stream_sink=sink)

        # Should have used regular generate (JSON path), not generate_stream
        gw.model_router.generate.assert_called()
        gw.model_router.generate_stream.assert_not_called()


class TestGatewayStreamSink:
    """Verify Gateway.ingest passes stream_sink to agent_loop for chat.respond."""

    @pytest.mark.asyncio
    async def test_ingest_accepts_stream_sink_param(self) -> None:
        """Gateway.ingest() should accept optional stream_sink kwarg."""
        from synapse.gateway.core import Gateway
        import inspect

        sig = inspect.signature(Gateway.ingest)
        assert "stream_sink" in sig.parameters

    @pytest.mark.asyncio
    async def test_agent_loop_run_accepts_stream_sink(self) -> None:
        """AgentLoop.run() should accept optional stream_sink kwarg."""
        from synapse.gateway.agent_loop import AgentLoop
        import inspect

        sig = inspect.signature(AgentLoop.run)
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

        # Mock gateway.ingest to return a result
        mock_result = MagicMock()
        mock_result.queued = False
        mock_result.reply_text = "Hello"
        mock_result.run_id = "run-1"
        mock_result.suppress_delivery = False
        runtime.gateway.ingest = AsyncMock(return_value=mock_result)

        event = MagicMock()
        event.adapter = "telegram"
        event.channel_id = "12345"

        # Call the real method
        await Runtime.async_handle_telegram_event(runtime, event)

        # Should have called ingest with stream_sink
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
        event.adapter = "api"  # Not telegram
        event.channel_id = "12345"

        await Runtime.async_handle_telegram_event(runtime, event)

        call_kwargs = runtime.gateway.ingest.call_args
        # stream_sink should be None for non-telegram
        if "stream_sink" in (call_kwargs.kwargs or {}):
            assert call_kwargs.kwargs["stream_sink"] is None
