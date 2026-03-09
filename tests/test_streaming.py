"""Tests for synapse.streaming — draft-stream pattern for live message editing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse.streaming.sink import NullSink, StreamSink
from synapse.streaming.draft_stream import DraftStreamLoop
from synapse.streaming.telegram_stream import TelegramDraftStream


# ---------------------------------------------------------------------------
# StreamSink protocol + NullSink
# ---------------------------------------------------------------------------


class TestNullSink:
    @pytest.mark.asyncio
    async def test_push_accumulates_text(self) -> None:
        sink = NullSink()
        await sink.push("Hello ")
        await sink.push("world")
        assert sink.accumulated_text == "Hello world"

    @pytest.mark.asyncio
    async def test_finalize_is_noop(self) -> None:
        sink = NullSink()
        await sink.push("data")
        await sink.finalize()
        assert sink.accumulated_text == "data"

    @pytest.mark.asyncio
    async def test_empty_by_default(self) -> None:
        sink = NullSink()
        assert sink.accumulated_text == ""


# ---------------------------------------------------------------------------
# DraftStreamLoop
# ---------------------------------------------------------------------------


class TestDraftStreamLoop:
    @pytest.mark.asyncio
    async def test_first_send_waits_for_min_chars(self) -> None:
        send_or_edit = AsyncMock()
        loop = DraftStreamLoop(
            throttle_ms=100,
            min_initial_chars=10,
            send_or_edit=send_or_edit,
        )
        await loop.update("Hi")  # 2 chars — below threshold
        await asyncio.sleep(0.05)
        send_or_edit.assert_not_called()

        await loop.update("Hi, how are you?")  # 16 chars — above threshold
        await asyncio.sleep(0.15)  # background task needs time to run
        send_or_edit.assert_called()
        await loop.stop()

    @pytest.mark.asyncio
    async def test_throttles_edits(self) -> None:
        send_or_edit = AsyncMock()
        loop = DraftStreamLoop(
            throttle_ms=200,
            min_initial_chars=1,
            send_or_edit=send_or_edit,
        )
        await loop.update("a")
        await asyncio.sleep(0.1)  # background task runs first send
        assert send_or_edit.call_count >= 1
        first_count = send_or_edit.call_count

        await loop.update("ab")
        await loop.update("abc")
        await asyncio.sleep(0.05)
        # Throttled — should not have sent again yet
        assert send_or_edit.call_count == first_count

        await asyncio.sleep(0.25)
        # Now throttle window passed
        assert send_or_edit.call_count > first_count
        await loop.stop()

    @pytest.mark.asyncio
    async def test_stop_flushes_final_text(self) -> None:
        send_or_edit = AsyncMock()
        loop = DraftStreamLoop(
            throttle_ms=5000,  # very long throttle
            min_initial_chars=1,
            send_or_edit=send_or_edit,
        )
        await loop.update("Hello final")
        await asyncio.sleep(0.05)  # let background first-send task run
        await loop.stop()
        # stop() should force a final flush
        last_call = send_or_edit.call_args_list[-1]
        assert last_call[0][0] == "Hello final"

    @pytest.mark.asyncio
    async def test_no_send_when_no_update(self) -> None:
        send_or_edit = AsyncMock()
        loop = DraftStreamLoop(
            throttle_ms=100,
            min_initial_chars=10,
            send_or_edit=send_or_edit,
        )
        await loop.stop()
        send_or_edit.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_inflight_request(self) -> None:
        """Ensure only one send_or_edit is in flight at a time."""
        call_count = 0
        max_concurrent = 0
        current_concurrent = 0

        async def slow_send(text: str) -> None:
            nonlocal call_count, max_concurrent, current_concurrent
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            call_count += 1
            try:
                await asyncio.sleep(0.05)
            finally:
                current_concurrent -= 1

        loop = DraftStreamLoop(
            throttle_ms=10,
            min_initial_chars=1,
            send_or_edit=slow_send,
        )
        for i in range(5):
            await loop.update(f"text-{i}")
            await asyncio.sleep(0.02)

        await loop.stop()
        assert max_concurrent <= 1

    @pytest.mark.asyncio
    async def test_update_does_not_block(self) -> None:
        """update() must return immediately, never blocking the caller."""
        call_times: list[float] = []

        async def slow_send(text: str) -> None:
            call_times.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.2)  # simulate slow Telegram API

        loop = DraftStreamLoop(
            throttle_ms=50,
            min_initial_chars=1,
            send_or_edit=slow_send,
        )
        start = asyncio.get_event_loop().time()
        await loop.update("hello world enough chars")
        elapsed = asyncio.get_event_loop().time() - start
        # update must return in <10ms, NOT wait 200ms for slow_send
        assert elapsed < 0.05, f"update() blocked for {elapsed:.3f}s"
        await asyncio.sleep(0.3)  # let background task finish
        assert len(call_times) >= 1
        await loop.stop()


# ---------------------------------------------------------------------------
# TelegramDraftStream
# ---------------------------------------------------------------------------


class TestTelegramDraftStream:
    def _make_adapter(self, *, draft_supported: bool = True) -> MagicMock:
        adapter = MagicMock()
        adapter.token = "test-token"
        # send_text returns a telegram response with message_id
        adapter.send_text.return_value = {
            "ok": True,
            "result": {"message_id": 42},
        }
        adapter.edit_text.return_value = {"ok": True}
        adapter.send_typing_action.return_value = None
        if draft_supported:
            adapter.send_draft.return_value = {"ok": True}
        else:
            adapter.send_draft.side_effect = Exception(
                "Bad Request: sendMessageDraft: unknown method"
            )
        adapter.delete_message.return_value = None
        return adapter

    @pytest.mark.asyncio
    async def test_implements_stream_sink_protocol(self) -> None:
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345")
        # duck-type check
        assert hasattr(stream, "push")
        assert hasattr(stream, "finalize")
        assert hasattr(stream, "accumulated_text")

    @pytest.mark.asyncio
    async def test_push_accumulates_and_streams(self) -> None:
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=5, throttle_ms=50, prefer_draft=False)
        await stream.push("Hello ")
        await stream.push("World!")
        await asyncio.sleep(0.15)
        assert stream.accumulated_text == "Hello World!"
        # Should have sent at least once
        assert adapter.send_text.called or adapter.edit_text.called
        await stream.finalize()

    @pytest.mark.asyncio
    async def test_first_call_sends_then_edits(self) -> None:
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=50, prefer_draft=False)
        await stream.push("First chunk is long enough")
        await asyncio.sleep(0.15)
        # First call should use send_text
        adapter.send_text.assert_called_once()

        await stream.push(" more text appended here")
        await asyncio.sleep(0.15)
        # Subsequent calls use edit_text
        assert adapter.edit_text.called
        await stream.finalize()

    @pytest.mark.asyncio
    async def test_finalize_sends_complete_text(self) -> None:
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=50, prefer_draft=False)
        await stream.push("Complete response text")
        await asyncio.sleep(0.15)
        await stream.finalize()
        # Last edit should contain the full text
        if adapter.edit_text.called:
            last_edit = adapter.edit_text.call_args_list[-1]
            assert "Complete response text" in last_edit[1].get("text", "") or last_edit[0][-1] == "Complete response text"

    @pytest.mark.asyncio
    async def test_streamed_property(self) -> None:
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=50, prefer_draft=False)
        assert stream.streamed is False
        await stream.push("Some text that is long enough to trigger send")
        await asyncio.sleep(0.15)
        assert stream.streamed is True
        await stream.finalize()

    @pytest.mark.asyncio
    async def test_no_send_for_empty_stream(self) -> None:
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345")
        await stream.finalize()
        assert stream.streamed is False
        adapter.send_text.assert_not_called()
        adapter.edit_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_typing_indicator_sent_on_first_push(self) -> None:
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=100)
        await stream.push("a")
        await asyncio.sleep(0.05)  # let typing task fire
        adapter.send_typing_action.assert_called_once_with("12345")
        await stream.finalize()

    @pytest.mark.asyncio
    async def test_edit_text_not_modified_ignored(self) -> None:
        """Telegram 'message is not modified' error should not crash the stream."""
        adapter = self._make_adapter()
        adapter.edit_text.side_effect = Exception("Bad Request: message is not modified")
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=50, prefer_draft=False)
        await stream.push("first message long enough")
        await asyncio.sleep(0.15)
        await stream.push(" more text")
        await asyncio.sleep(0.15)
        # Should not crash, stream should still work
        await stream.finalize()
        assert stream.accumulated_text == "first message long enough more text"

    @pytest.mark.asyncio
    async def test_start_begins_typing_heartbeat(self) -> None:
        """start() should send typing immediately and start the heartbeat."""
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=100)
        await stream.start()
        await asyncio.sleep(0.05)  # let first typing fire
        adapter.send_typing_action.assert_called_with("12345")
        assert stream._typing_task is not None
        assert not stream._typing_task.done()
        await stream.finalize()

    @pytest.mark.asyncio
    async def test_typing_heartbeat_repeats(self) -> None:
        """Typing should be re-sent periodically while stream is active."""
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=100)
        await stream.start()
        await asyncio.sleep(0.05)
        first_count = adapter.send_typing_action.call_count
        assert first_count >= 1
        # Monkey-patch the heartbeat interval for faster test
        # Cancel existing task and start a fast one
        stream._typing_task.cancel()
        try:
            await stream._typing_task
        except asyncio.CancelledError:
            pass

        async def _fast_heartbeat() -> None:
            try:
                while True:
                    await stream._send_typing()
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass

        stream._typing_task = asyncio.create_task(_fast_heartbeat())
        await asyncio.sleep(0.18)  # ~3 more typing calls
        assert adapter.send_typing_action.call_count > first_count
        await stream.finalize()

    @pytest.mark.asyncio
    async def test_finalize_cancels_typing(self) -> None:
        """finalize() should cancel the typing heartbeat task."""
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=100)
        await stream.start()
        await asyncio.sleep(0.05)
        assert stream._typing_task is not None
        await stream.finalize()
        assert stream._typing_task is None

    @pytest.mark.asyncio
    async def test_fallback_push_after_json_loop(self) -> None:
        """Pushing full text through sink after non-streaming generation works."""
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=50, prefer_draft=False)
        await stream.start()
        assert stream.streamed is False
        # Simulate JSON loop fallback: push entire reply at once
        await stream.push("Full reply from JSON loop")
        await asyncio.sleep(0.15)
        await stream.finalize()
        assert stream.streamed is True
        assert stream.accumulated_text == "Full reply from JSON loop"
        adapter.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        """Calling start() multiple times should not create multiple heartbeats."""
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=100)
        await stream.start()
        task1 = stream._typing_task
        await stream.start()  # second call
        assert stream._typing_task is task1  # same task
        await stream.finalize()

    @pytest.mark.asyncio
    async def test_streamed_true_after_finalize_not_before(self) -> None:
        """Push schedules a background send — streamed may be False until finalize flushes."""
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=5000, prefer_draft=False)
        await stream.push("Full reply pushed at once")
        await stream.finalize()
        assert stream.streamed is True
        adapter.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_finalize_idempotent(self) -> None:
        """Calling finalize() twice should not crash or double-send."""
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=50, prefer_draft=False)
        await stream.start()
        await stream.push("Some text for idempotent test")
        await asyncio.sleep(0.15)
        await stream.finalize()
        send_count = adapter.send_text.call_count
        await stream.finalize()  # second finalize — should be a noop
        assert adapter.send_text.call_count == send_count

    # -----------------------------------------------------------------------
    # Draft transport tests
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_prefer_draft_ignored_uses_message_transport(self) -> None:
        """Draft transport is disabled — prefer_draft=True still uses message transport."""
        adapter = self._make_adapter(draft_supported=True)
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=50, prefer_draft=True)
        await stream.push("Hello from message transport")
        await asyncio.sleep(0.15)
        assert stream.transport == "message"
        assert stream.streamed is True
        adapter.send_draft.assert_not_called()
        adapter.send_text.assert_called()
        await stream.finalize()

    @pytest.mark.asyncio
    async def test_materialize_noop_for_message_transport(self) -> None:
        """materialize() on message transport returns existing message_id (no duplicate)."""
        adapter = self._make_adapter()
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=50, prefer_draft=False)
        await stream.push("Already a real message")
        await asyncio.sleep(0.15)
        msg_id = await stream.materialize()
        assert msg_id == 42
        adapter.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_materialize_noop_even_with_prefer_draft(self) -> None:
        """materialize() is a no-op even when prefer_draft=True (draft disabled)."""
        adapter = self._make_adapter(draft_supported=True)
        stream = TelegramDraftStream(adapter, "12345", min_initial_chars=1, throttle_ms=50, prefer_draft=True)
        await stream.push("Text streamed via message transport")
        await asyncio.sleep(0.15)
        msg_id = await stream.materialize()
        assert msg_id == 42
        adapter.send_text.assert_called_once()
