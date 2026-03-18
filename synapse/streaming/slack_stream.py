"""SlackMessageStream — streaming LLM output to Slack.

Strategy:
  1. Accumulate text deltas from the model via push().
  2. After min_initial_chars chars accumulate, call adapter.send_text()
     (sync, dispatched to executor) to post the initial message.
  3. For subsequent chunks, call adapter.edit_text() with the full text
     so far, throttled at throttle_ms intervals via DraftStreamLoop.
  4. materialize() sends a final flush if content exists and returns the ts.

Adapter methods (send_text/edit_text) are synchronous (httpx.Client) so
they are dispatched to a thread pool via run_in_executor.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .draft_stream import DraftStreamLoop

if TYPE_CHECKING:
    from ..slack_adapter import SlackAdapter

logger = logging.getLogger(__name__)

_MAX_CHARS = 4000


class SlackMessageStream:
    """Streams LLM output to Slack with throttled send/edit cycle."""

    def __init__(
        self,
        adapter: Any,
        channel_id: str,
        *,
        thread_ts: str | None = None,
        throttle_ms: int = 800,
        min_initial_chars: int = 10,
    ) -> None:
        self._adapter = adapter
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self._parts: list[str] = []
        self._message_ts: str | None = None
        self._stopped: bool = False

        self._loop = DraftStreamLoop(
            throttle_ms=throttle_ms,
            min_initial_chars=min_initial_chars,
            send_or_edit=self._do_send_or_edit,
        )

    # ------------------------------------------------------------------
    # StreamSink protocol
    # ------------------------------------------------------------------

    async def push(self, delta: str) -> None:
        """Receive a text delta from the model."""
        if self._stopped:
            return
        self._parts.append(delta)
        text = self.accumulated_text
        if len(text) > _MAX_CHARS:
            self._stopped = True
            return
        await self._loop.update(text)

    async def finalize(self) -> None:
        """Mark stream as complete. Flush any pending edits.

        Only triggers a final flush if a message has already been posted
        (i.e., the min_initial_chars threshold was crossed). If nothing was
        sent yet we just mark the loop stopped without forcing a send — the
        caller should use materialize() to send the complete text.
        """
        self._stopped = True
        if self._message_ts is not None:
            # A message is already live — flush final text via edit
            await self._loop.stop()
        else:
            # Nothing sent yet; mark loop stopped to prevent further updates
            self._loop._stopped = True
            if self._loop._flush_task is not None and not self._loop._flush_task.done():
                try:
                    await self._loop._flush_task
                except Exception:
                    pass

    @property
    def accumulated_text(self) -> str:
        """Full text accumulated so far."""
        return "".join(self._parts)

    # ------------------------------------------------------------------
    # materialize
    # ------------------------------------------------------------------

    async def materialize(self) -> str | None:
        """Flush the final text and return the Slack message ts.

        For the message-transport flow (we always use this):
        - If a message was already sent, edit it to the final text.
        - If no message was sent yet (empty/short stream), do nothing.
        Returns the Slack ts of the posted message, or None.
        """
        await self.finalize()

        text = self.accumulated_text.strip()
        if not text:
            return None

        if self._message_ts is not None:
            # Already posted; a final edit is handled by finalize/stop above.
            return self._message_ts

        # Nothing was sent yet — send the full text now.
        loop = asyncio.get_running_loop()
        try:
            if self._thread_ts is not None:
                response = await loop.run_in_executor(
                    None,
                    lambda: self._adapter.send_text(
                        self._channel_id, text, thread_ts=self._thread_ts
                    ),
                )
            else:
                response = await loop.run_in_executor(
                    None,
                    lambda: self._adapter.send_text(self._channel_id, text),
                )
            if isinstance(response, dict):
                self._message_ts = response.get("ts")
        except Exception as exc:
            logger.warning("slack materialize send failed: %s", exc)

        return self._message_ts

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def streamed(self) -> bool:
        """True if any content was pushed via streaming."""
        return self._message_ts is not None

    # ------------------------------------------------------------------
    # Internal send/edit callback
    # ------------------------------------------------------------------

    async def _do_send_or_edit(self, text: str) -> None:
        """Perform the actual Slack API call (send or edit)."""
        loop = asyncio.get_running_loop()
        if self._message_ts is None:
            # First send
            try:
                if self._thread_ts is not None:
                    response = await loop.run_in_executor(
                        None,
                        lambda: self._adapter.send_text(
                            self._channel_id, text, thread_ts=self._thread_ts
                        ),
                    )
                else:
                    response = await loop.run_in_executor(
                        None,
                        lambda: self._adapter.send_text(self._channel_id, text),
                    )
                if isinstance(response, dict):
                    self._message_ts = response.get("ts")
            except Exception as exc:
                logger.warning("slack send_text failed: %s", exc)
        else:
            # Edit existing message
            ts = self._message_ts
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._adapter.edit_text(self._channel_id, ts, text),
                )
            except Exception as exc:
                logger.warning("slack edit_text failed: %s", exc)
