"""Tests for ReAct loop — RED → GREEN."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse.tools.registry import ToolDef, ToolResult


def _text_response(text: str):
    """Mock LLM response with text only."""
    resp = MagicMock()
    resp.text = text
    resp.tool_calls = None
    resp.usage = None
    return resp


def _tool_response(tool_calls: list[dict]):
    """Mock LLM response with tool calls."""
    resp = MagicMock()
    resp.text = None
    resp.tool_calls = []
    for tc in tool_calls:
        call = MagicMock()
        call.id = tc.get("id", "tc-1")
        call.name = tc["name"]
        call.arguments = tc.get("arguments", {})
        resp.tool_calls.append(call)
    resp.usage = None
    return resp


def _make_tool(name: str, *, needs_approval: bool = False, output: str = "ok") -> ToolDef:
    async def _exec(params, *, ctx):
        return ToolResult(output=output)

    return ToolDef(
        name=name,
        description=f"Tool {name}",
        input_schema={"type": "object"},
        execute=_exec,
        needs_approval=needs_approval,
    )


def _make_failing_tool(name: str, error: str = "boom") -> ToolDef:
    async def _exec(params, *, ctx):
        return ToolResult(output="", error=error)

    return ToolDef(
        name=name,
        description=f"Tool {name}",
        input_schema={"type": "object"},
        execute=_exec,
    )


class TestReactLoop:
    @pytest.mark.asyncio
    async def test_text_only_response(self):
        from synapse.react_loop import run_react_loop

        model_router = MagicMock()
        model_router.chat = AsyncMock(return_value=_text_response("Hello!"))

        result = await run_react_loop(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="You are helpful.",
            tools=[],
            model_router=model_router,
        )
        assert result.reply == "Hello!"
        assert result.turns == 1
        assert result.tool_calls_made == []

    @pytest.mark.asyncio
    async def test_single_tool_call(self):
        from synapse.react_loop import run_react_loop

        tool = _make_tool("self_describe", output="I am Synapse")
        model_router = MagicMock()
        model_router.chat = AsyncMock(
            side_effect=[
                _tool_response([{"name": "self_describe", "arguments": {}}]),
                _text_response("I am Synapse, your assistant."),
            ]
        )

        result = await run_react_loop(
            messages=[{"role": "user", "content": "who are you?"}],
            system_prompt="test",
            tools=[tool],
            model_router=model_router,
        )
        assert result.reply == "I am Synapse, your assistant."
        assert result.turns == 2
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["tool"] == "self_describe"

    @pytest.mark.asyncio
    async def test_multi_turn_tool_calls(self):
        from synapse.react_loop import run_react_loop

        tool_a = _make_tool("calendar", output="Meeting at 10am")
        tool_b = _make_tool("email", output="Draft sent")
        model_router = MagicMock()
        model_router.chat = AsyncMock(
            side_effect=[
                _tool_response([{"name": "calendar", "arguments": {}}]),
                _tool_response([{"name": "email", "arguments": {"to": "a@b.com"}}]),
                _text_response("Done — checked calendar and sent email."),
            ]
        )

        result = await run_react_loop(
            messages=[{"role": "user", "content": "check cal and email"}],
            system_prompt="test",
            tools=[tool_a, tool_b],
            model_router=model_router,
        )
        assert result.turns == 3
        assert len(result.tool_calls_made) == 2

    @pytest.mark.asyncio
    async def test_tool_needs_approval_denied(self):
        from synapse.react_loop import run_react_loop

        tool = _make_tool("gmail_send", needs_approval=True, output="sent")
        approval = MagicMock()
        approval.check_and_approve = AsyncMock(return_value=False)

        model_router = MagicMock()
        model_router.chat = AsyncMock(
            side_effect=[
                _tool_response([{"name": "gmail_send", "arguments": {"to": "a@b.com"}}]),
                _text_response("Okay, I won't send that."),
            ]
        )

        result = await run_react_loop(
            messages=[{"role": "user", "content": "send email"}],
            system_prompt="test",
            tools=[tool],
            model_router=model_router,
            approval_manager=approval,
        )
        assert result.reply == "Okay, I won't send that."
        # Tool call was attempted but denied
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["denied"] is True

    @pytest.mark.asyncio
    async def test_tool_needs_approval_approved(self):
        from synapse.react_loop import run_react_loop

        tool = _make_tool("gmail_send", needs_approval=True, output="message sent")
        approval = MagicMock()
        approval.check_and_approve = AsyncMock(return_value=True)

        model_router = MagicMock()
        model_router.chat = AsyncMock(
            side_effect=[
                _tool_response([{"name": "gmail_send", "arguments": {"to": "a@b.com"}}]),
                _text_response("Email sent!"),
            ]
        )

        result = await run_react_loop(
            messages=[{"role": "user", "content": "send email"}],
            system_prompt="test",
            tools=[tool],
            model_router=model_router,
            approval_manager=approval,
        )
        assert result.reply == "Email sent!"
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0].get("denied") is not True

    @pytest.mark.asyncio
    async def test_max_turns_reached(self):
        from synapse.react_loop import run_react_loop

        tool = _make_tool("looper", output="again")
        model_router = MagicMock()
        # Always returns tool calls, never text
        model_router.chat = AsyncMock(
            return_value=_tool_response([{"name": "looper", "arguments": {}}])
        )

        result = await run_react_loop(
            messages=[{"role": "user", "content": "loop forever"}],
            system_prompt="test",
            tools=[tool],
            model_router=model_router,
            max_turns=3,
        )
        assert result.turns == 3
        assert "max turns" in result.reply.lower()

    @pytest.mark.asyncio
    async def test_no_reply_detection(self):
        from synapse.react_loop import run_react_loop

        model_router = MagicMock()
        model_router.chat = AsyncMock(return_value=_text_response("NO_REPLY"))

        result = await run_react_loop(
            messages=[{"role": "user", "content": "heartbeat check"}],
            system_prompt="test",
            tools=[],
            model_router=model_router,
        )
        assert result.reply == "NO_REPLY"
        assert result.turns == 1

    @pytest.mark.asyncio
    async def test_tool_execution_error(self):
        from synapse.react_loop import run_react_loop

        tool = _make_failing_tool("broken", error="connection refused")
        model_router = MagicMock()
        model_router.chat = AsyncMock(
            side_effect=[
                _tool_response([{"name": "broken", "arguments": {}}]),
                _text_response("Sorry, the tool failed."),
            ]
        )

        result = await run_react_loop(
            messages=[{"role": "user", "content": "use broken tool"}],
            system_prompt="test",
            tools=[tool],
            model_router=model_router,
        )
        assert result.reply == "Sorry, the tool failed."
        assert len(result.tool_calls_made) == 1
        assert "connection refused" in result.tool_calls_made[0]["error"]

    @pytest.mark.asyncio
    async def test_streaming_passes_sink_to_model(self):
        """The stream_sink is passed to model_router.chat() so the provider handles streaming."""
        from synapse.react_loop import run_react_loop

        model_router = MagicMock()
        model_router.chat = AsyncMock(return_value=_text_response("streamed reply"))

        sink = MagicMock()
        sink.push = AsyncMock()
        sink.finalize = AsyncMock()

        result = await run_react_loop(
            messages=[{"role": "user", "content": "hello"}],
            system_prompt="test",
            tools=[],
            model_router=model_router,
            stream_sink=sink,
        )
        assert result.reply == "streamed reply"
        # Verify sink was passed to chat call
        call_kwargs = model_router.chat.call_args[1]
        assert call_kwargs.get("stream_sink") is sink

    @pytest.mark.asyncio
    async def test_streaming_after_tool_calls_pushes_to_sink(self):
        """After tool calls, the final text reply is pushed to sink."""
        from synapse.react_loop import run_react_loop

        tool = _make_tool("lookup", output="data")
        model_router = MagicMock()
        model_router.chat = AsyncMock(
            side_effect=[
                _tool_response([{"name": "lookup", "arguments": {}}]),
                _text_response("Here's the data."),
            ]
        )

        sink = MagicMock()
        sink.push = AsyncMock()
        sink.finalize = AsyncMock()
        sink.streamed = False  # simulate non-streaming provider

        result = await run_react_loop(
            messages=[{"role": "user", "content": "get data"}],
            system_prompt="test",
            tools=[tool],
            model_router=model_router,
            stream_sink=sink,
        )
        assert result.reply == "Here's the data."
        sink.push.assert_awaited_once_with("Here's the data.")

    @pytest.mark.asyncio
    async def test_unknown_tool_in_response(self):
        from synapse.react_loop import run_react_loop

        model_router = MagicMock()
        model_router.chat = AsyncMock(
            side_effect=[
                _tool_response([{"name": "nonexistent", "arguments": {}}]),
                _text_response("Let me try something else."),
            ]
        )

        result = await run_react_loop(
            messages=[{"role": "user", "content": "do something"}],
            system_prompt="test",
            tools=[],
            model_router=model_router,
        )
        assert result.turns == 2
        assert len(result.tool_calls_made) == 1
        assert "not found" in result.tool_calls_made[0]["error"]
