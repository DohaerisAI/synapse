"""DraftStreamLoop — throttled edit scheduler for live message streaming.

Ported from OpenClaw's draft-stream-loop pattern. Sends an initial message
after min_initial_chars accumulate, then edits every throttle_ms milliseconds.
Only one in-flight API request at a time.

Key scheduling insight (from OpenClaw): after each send completes, calculate
delay = max(0, throttle - time_since_last_send). This absorbs HTTP latency
into the throttle window, producing consistent edit intervals.

CRITICAL: update() must NEVER block. It schedules flushes as background tasks
so the token stream from the LLM provider is never paused.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable


class DraftStreamLoop:
    def __init__(
        self,
        *,
        throttle_ms: int = 1000,
        min_initial_chars: int = 30,
        send_or_edit: Callable[[str], Awaitable[None]],
    ) -> None:
        self._throttle_s = max(throttle_ms, 250) / 1000.0
        self._min_initial_chars = min_initial_chars
        self._send_or_edit = send_or_edit
        self._current_text: str = ""
        self._last_sent_text: str = ""
        self._last_sent_at: float = 0.0
        self._first_sent: bool = False
        self._stopped: bool = False
        self._flush_task: asyncio.Task[None] | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def update(self, text: str) -> None:
        """Called when accumulated text changes. Schedules non-blocking flush.

        NEVER blocks — schedules background tasks for sends/edits so the
        LLM token stream is never paused.
        """
        if self._stopped:
            return
        self._current_text = text
        if not self._first_sent:
            if len(text) >= self._min_initial_chars:
                if self._flush_task is None or self._flush_task.done():
                    self._flush_task = asyncio.create_task(self._do_flush())
            return
        # After first send: ensure the flush loop is running
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def flush(self) -> None:
        """Send/edit the current text immediately."""
        await self._do_flush()

    async def stop(self) -> None:
        """Final flush + prevent further updates."""
        self._stopped = True
        # Wait for any in-flight flush to complete — do NOT cancel it.
        # Cancelling mid-HTTP-request leaves _last_sent_text out of sync,
        # causing the final flush below to double-send the same message.
        if self._flush_task is not None and not self._flush_task.done():
            try:
                await self._flush_task
            except Exception:
                pass
        # Final flush with complete text
        if self._current_text and self._current_text != self._last_sent_text:
            await self._do_flush()

    async def _flush_loop(self) -> None:
        """Continuous flush loop matching OpenClaw's scheduling pattern.

        After each send: delay = max(0, throttle - time_since_last_send).
        Loops until no pending text or stopped.
        """
        while not self._stopped:
            # Calculate delay absorbing HTTP latency into throttle window
            elapsed = time.monotonic() - self._last_sent_at
            delay = max(0.0, self._throttle_s - elapsed)
            if delay > 0:
                await asyncio.sleep(delay)
            if self._stopped:
                break
            if self._current_text == self._last_sent_text:
                break  # No new text — exit loop; update() will restart it
            await self._do_flush()

    async def _do_flush(self) -> None:
        """Execute the actual send/edit. Ensures one in-flight at a time via lock."""
        async with self._lock:
            text = self._current_text
            if not text or text == self._last_sent_text:
                return
            await self._send_or_edit(text)
            self._last_sent_text = text
            self._last_sent_at = time.monotonic()
            self._first_sent = True
