"""Tests for SlackMessageStream — written FIRST per TDD (RED phase).

Tests streaming LLM output to Slack via chat.postMessage + chat.update.
All adapter methods mocked with AsyncMock to avoid real HTTP calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synapse.streaming.slack_stream import SlackMessageStream
from synapse.streaming.sink import StreamSink


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    *,
    send_ts: str = "111.222",
    edit_ok: bool = True,
) -> MagicMock:
    """Return a MagicMock SlackAdapter with async-compatible send/edit."""
    adapter = MagicMock()
    adapter.send_text = MagicMock(return_value={"ok": True, "ts": send_ts})
    adapter.edit_text = MagicMock(return_value={"ok": True, "ts": send_ts})
    return adapter


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_implements_stream_sink_protocol() -> None:
    adapter = _make_adapter()
    stream = SlackMessageStream(adapter, "C100")

    # Duck-type check against StreamSink protocol
    assert isinstance(stream, StreamSink)


# ---------------------------------------------------------------------------
# push() accumulation
# ---------------------------------------------------------------------------


async def test_push_accumulates_text() -> None:
    adapter = _make_adapter()
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=9999)

    await stream.push("Hello")
    await stream.push(", world")

    assert stream.accumulated_text == "Hello, world"


async def test_push_below_min_chars_does_not_send() -> None:
    adapter = _make_adapter()
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=50)

    await stream.push("hi")
    await stream.finalize()

    adapter.send_text.assert_not_called()


# ---------------------------------------------------------------------------
# First push above threshold — sendMessage
# ---------------------------------------------------------------------------


async def test_first_push_above_threshold_sends_message() -> None:
    adapter = _make_adapter(send_ts="100.1")
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=3, throttle_ms=0)

    await stream.push("hello world!")
    # Force immediate flush
    await stream.finalize()

    adapter.send_text.assert_called_once()
    call_args = adapter.send_text.call_args
    assert call_args[0][0] == "C100"
    assert "hello world!" in call_args[0][1]


# ---------------------------------------------------------------------------
# Subsequent push — editMessage
# ---------------------------------------------------------------------------


async def test_subsequent_push_edits_message() -> None:
    """After the initial send, further pushes should trigger edit_text."""
    adapter = _make_adapter(send_ts="100.1")
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=3, throttle_ms=0)

    # First push crosses threshold — triggers send_text
    await stream.push("hello world!")
    # Force the loop to flush now by calling the internal loop flush
    await stream._loop.flush()

    assert adapter.send_text.call_count == 1
    assert stream._message_ts == "100.1"

    # Second push: message_ts is now set, so next flush calls edit_text
    await stream.push(" more text here")
    await stream._loop.flush()

    assert adapter.edit_text.call_count >= 1


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------


async def test_materialize_sends_final_text() -> None:
    adapter = _make_adapter(send_ts="200.3")
    stream = SlackMessageStream(adapter, "C200", min_initial_chars=3, throttle_ms=0)

    await stream.push("final content here")
    ts = await stream.materialize()

    assert adapter.send_text.call_count >= 1


async def test_materialize_no_content_noop() -> None:
    adapter = _make_adapter()
    stream = SlackMessageStream(adapter, "C200", min_initial_chars=3)

    ts = await stream.materialize()

    adapter.send_text.assert_not_called()
    adapter.edit_text.assert_not_called()


# ---------------------------------------------------------------------------
# finalize stops further sends
# ---------------------------------------------------------------------------


async def test_finalize_stops_loop() -> None:
    adapter = _make_adapter(send_ts="100.1")
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=3, throttle_ms=0)

    await stream.push("initial text here")
    await stream.finalize()

    call_count_after_finalize = adapter.send_text.call_count + adapter.edit_text.call_count

    # Push more after finalize — should NOT trigger additional sends
    await stream.push(" extra after finalize")

    assert adapter.send_text.call_count + adapter.edit_text.call_count == call_count_after_finalize


# ---------------------------------------------------------------------------
# streamed property
# ---------------------------------------------------------------------------


async def test_streamed_false_before_push() -> None:
    adapter = _make_adapter()
    stream = SlackMessageStream(adapter, "C100")

    assert stream.streamed is False


async def test_streamed_true_after_send() -> None:
    adapter = _make_adapter(send_ts="100.1")
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=3, throttle_ms=0)

    await stream.push("hello world!")
    await stream.finalize()

    assert stream.streamed is True


# ---------------------------------------------------------------------------
# thread_ts forwarded
# ---------------------------------------------------------------------------


async def test_thread_ts_forwarded_to_send() -> None:
    adapter = _make_adapter(send_ts="100.1")
    stream = SlackMessageStream(
        adapter, "C100", thread_ts="99.0", min_initial_chars=3, throttle_ms=0
    )

    await stream.push("reply in thread!")
    await stream.finalize()

    adapter.send_text.assert_called_once()
    call_kwargs = adapter.send_text.call_args[1]
    assert call_kwargs.get("thread_ts") == "99.0"


# ---------------------------------------------------------------------------
# materialize via run_in_executor (no prior stream, sends directly)
# ---------------------------------------------------------------------------


async def test_materialize_without_prior_stream_sends_and_returns_ts() -> None:
    """When nothing was streamed yet, materialize sends the full text."""
    adapter = _make_adapter(send_ts="55.5")
    stream = SlackMessageStream(adapter, "C300", min_initial_chars=9999, throttle_ms=9999)

    await stream.push("complete response text that was never streamed")
    ts = await stream.materialize()

    adapter.send_text.assert_called_once()
    assert ts == "55.5"


async def test_materialize_with_thread_ts_no_prior_stream() -> None:
    adapter = _make_adapter(send_ts="77.7")
    stream = SlackMessageStream(
        adapter, "C300", thread_ts="66.6", min_initial_chars=9999, throttle_ms=9999
    )

    await stream.push("threaded response")
    await stream.materialize()

    call_kwargs = adapter.send_text.call_args[1]
    assert call_kwargs.get("thread_ts") == "66.6"


async def test_materialize_after_prior_send_returns_existing_ts() -> None:
    """If a message was already sent via streaming, materialize returns that ts."""
    adapter = _make_adapter(send_ts="33.3")
    stream = SlackMessageStream(adapter, "C400", min_initial_chars=3, throttle_ms=0)

    await stream.push("initial chunk!")
    await stream._loop.flush()

    # message_ts is now set from send_text
    ts = await stream.materialize()

    # send_text called exactly once (the initial stream send)
    assert adapter.send_text.call_count == 1
    assert ts == "33.3"


# ---------------------------------------------------------------------------
# push truncation (> MAX_CHARS stops further sends)
# ---------------------------------------------------------------------------


async def test_push_stops_after_max_chars() -> None:
    """After text exceeds _MAX_CHARS, further pushes should not trigger sends."""
    from synapse.streaming.slack_stream import _MAX_CHARS

    adapter = _make_adapter(send_ts="100.1")
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=3, throttle_ms=9999)

    # Push text that exceeds the limit
    big_text = "x" * (_MAX_CHARS + 1)
    await stream.push(big_text)

    # After exceeding the limit, _stopped should be True
    assert stream._stopped is True

    # Reset call count and push more — should be ignored
    adapter.send_text.reset_mock()
    await stream.push("more after limit")
    assert adapter.send_text.call_count == 0


# ---------------------------------------------------------------------------
# finalize idempotent
# ---------------------------------------------------------------------------


async def test_finalize_idempotent() -> None:
    """Calling finalize twice should not raise."""
    adapter = _make_adapter(send_ts="100.1")
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=3, throttle_ms=0)

    await stream.push("some text here")
    await stream.finalize()
    await stream.finalize()  # Second call — should be safe


# ---------------------------------------------------------------------------
# edit_text exception is swallowed gracefully
# ---------------------------------------------------------------------------


async def test_edit_text_exception_is_swallowed() -> None:
    """If edit_text raises, _do_send_or_edit should catch it and not re-raise."""
    adapter = _make_adapter(send_ts="100.1")
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=3, throttle_ms=0)

    # Force first send so _message_ts is set
    await stream.push("hello world!")
    await stream._loop.flush()

    assert stream._message_ts == "100.1"

    # Make edit_text raise
    adapter.edit_text.side_effect = RuntimeError("network error")

    # Should not raise
    await stream._do_send_or_edit("updated text")
    adapter.edit_text.assert_called_once()


# ---------------------------------------------------------------------------
# send_text exception in _do_send_or_edit is swallowed
# ---------------------------------------------------------------------------


async def test_send_text_exception_in_do_send_or_edit_is_swallowed() -> None:
    """If send_text raises in _do_send_or_edit, it should be caught silently."""
    adapter = _make_adapter(send_ts="100.1")
    adapter.send_text.side_effect = RuntimeError("connection refused")

    stream = SlackMessageStream(adapter, "C100", min_initial_chars=3, throttle_ms=0)

    # Should not raise even though send_text raises
    await stream._do_send_or_edit("some text")


# ---------------------------------------------------------------------------
# materialize exception path — send_text raises during materialize
# ---------------------------------------------------------------------------


async def test_materialize_send_exception_returns_none() -> None:
    """If send_text raises during materialize, it should be caught and return None."""
    adapter = _make_adapter(send_ts="100.1")
    adapter.send_text.side_effect = RuntimeError("API error during materialize")

    stream = SlackMessageStream(adapter, "C100", min_initial_chars=9999, throttle_ms=9999)
    await stream.push("some text here")

    result = await stream.materialize()

    assert result is None


# ---------------------------------------------------------------------------
# finalize flush task exception path
# ---------------------------------------------------------------------------


async def test_finalize_handles_flush_task_exception() -> None:
    """If an in-flight flush task raises during finalize (no prior send), it is caught."""
    import asyncio as _asyncio

    adapter = _make_adapter(send_ts="100.1")
    stream = SlackMessageStream(adapter, "C100", min_initial_chars=9999, throttle_ms=0)
    stream._parts.append("some text")

    # Inject a failing flush task that hasn't started yet (not done)
    async def _failing_task() -> None:
        raise RuntimeError("flush failed mid-flight")

    task = _asyncio.create_task(_failing_task())
    stream._loop._flush_task = task
    # Do NOT yield; task is scheduled but not yet done

    # Should not raise even though the flush task will fail when awaited
    await stream.finalize()
    # Allow the task cleanup
    await _asyncio.sleep(0)
