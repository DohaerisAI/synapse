"""TelegramDraftStream — Telegram-specific draft-stream implementation.

Dual transport: uses sendMessageDraft (Bot API 9.5+) as primary for DMs,
with sendMessage + editMessageText as fallback. Ported from OpenClaw's
draft-stream pattern.

Draft transport: user sees a live preview in the input area — no
notifications, no permanent messages until materialize() is called.

Message transport (fallback): sends an initial message via send_text
after min_initial_chars accumulate, then edits it in place via
editMessageText as tokens arrive.

Adapter methods are synchronous (httpx.Client), so they are dispatched
to a thread pool via run_in_executor to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .draft_stream import DraftStreamLoop

if TYPE_CHECKING:
    from ..adapters import TelegramAdapter

logger = logging.getLogger(__name__)

# Telegram message size limit
_MAX_CHARS = 4096

# Global draft ID counter (wraps at 2^31-1 per Telegram spec)
_next_draft_id = 0


def _allocate_draft_id() -> int:
    global _next_draft_id
    _next_draft_id = 1 if _next_draft_id >= 2_147_483_647 else _next_draft_id + 1
    return _next_draft_id


# Patterns indicating sendMessageDraft is not supported
_DRAFT_UNAVAILABLE_KEYWORDS = (
    "unknown method",
    "not found",
    "not available",
    "not supported",
    "unsupported",
    "can't be used",
    "can be used only",
)


def _is_draft_unavailable_error(exc: Exception) -> bool:
    """Check if the error means sendMessageDraft is not supported."""
    text = str(exc).lower()
    if "sendmessagedraft" not in text and "send_message_draft" not in text:
        return False
    return any(kw in text for kw in _DRAFT_UNAVAILABLE_KEYWORDS)


class TelegramDraftStream:
    def __init__(
        self,
        adapter: TelegramAdapter,
        chat_id: str,
        *,
        throttle_ms: int = 1000,
        min_initial_chars: int = 20,
        prefer_draft: bool = True,
    ) -> None:
        self._adapter = adapter
        self._chat_id = chat_id
        self._parts: list[str] = []
        self._message_id: int | None = None
        self._typing_sent: bool = False
        self._typing_task: asyncio.Task[None] | None = None
        self._stopped: bool = False

        # Draft transport state
        # sendMessageDraft (Bot API 9.5+) gives the smoothest DM experience
        # (native live preview with no notifications). When unavailable, or when
        # Telegram unexpectedly materializes it into a real message, we fall back
        # to message transport to avoid double-delivery.
        self._prefer_draft = bool(prefer_draft)
        self._transport: str = "draft" if self._prefer_draft else "message"
        self._draft_id: int | None = _allocate_draft_id() if self._prefer_draft else None
        self._draft_sent: bool = False

        self._loop = DraftStreamLoop(
            throttle_ms=throttle_ms,
            min_initial_chars=min_initial_chars,
            send_or_edit=self._send_or_edit,
        )

    async def start(self) -> None:
        """Start typing heartbeat immediately. Call before generation begins."""
        if not self._typing_sent:
            self._typing_sent = True
            self._typing_task = asyncio.create_task(self._typing_heartbeat())

    async def push(self, delta: str) -> None:
        """Receive a text delta from the model."""
        if not self._typing_sent:
            await self.start()
        self._parts.append(delta)
        text = self.accumulated_text
        if len(text) > _MAX_CHARS:
            self._stopped = True
            return
        await self._loop.update(text)

    async def finalize(self) -> None:
        """Mark stream as complete. Flush any pending edits."""
        if self._typing_task is not None:
            self._typing_task.cancel()
            try:
                await self._typing_task
            except asyncio.CancelledError:
                pass
            self._typing_task = None
        await self._loop.stop()

    async def materialize(self) -> int | None:
        """Convert draft preview into a permanent message.

        For draft transport: sends a real sendMessage with the final text.
        For message transport: no-op (message is already permanent).
        Returns the permanent message_id.
        """
        await self.finalize()

        logger.info(
            "materialize: transport=%s message_id=%s draft_sent=%s accumulated=%d",
            self._transport, self._message_id, self._draft_sent,
            len(self.accumulated_text),
        )

        # Message transport — already a real message
        if self._transport == "message" and self._message_id is not None:
            return self._message_id

        # Draft transport — send a real message with the final text
        text = self.accumulated_text.strip()
        if not text:
            return None

        logger.info("materialize: sending permanent message for draft transport")
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: self._adapter.send_text(self._chat_id, text),
            )
            result = response.get("result", {}) if isinstance(response, dict) else {}
            msg_id = result.get("message_id")
            if isinstance(msg_id, int):
                self._message_id = msg_id
                return msg_id
        except Exception as exc:
            logger.warning("telegram draft materialize failed: %s", exc)

        return None

    @property
    def accumulated_text(self) -> str:
        """Full text accumulated so far."""
        return "".join(self._parts)

    @property
    def streamed(self) -> bool:
        """True if any content was pushed via streaming (deltas received)."""
        return len(self._parts) > 0 or self._message_id is not None or self._draft_sent

    @property
    def transport(self) -> str:
        """Current transport mode: 'draft' or 'message'."""
        return self._transport

    async def _send_typing(self) -> None:
        """Send typing indicator — fire and forget, errors are ignored."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self._adapter.send_typing_action(self._chat_id),
            )
        except Exception:
            pass

    async def _typing_heartbeat(self) -> None:
        """Re-send typing every 4s until stream is finalized.

        Telegram's typing indicator expires after 5 seconds, so we
        refresh it every 4 seconds to keep it visible continuously.
        Only used when draft transport is unavailable — draft transport
        shows a native preview without needing typing indicators.
        """
        try:
            while True:
                # Draft transport shows a native preview — skip typing
                if self._transport == "draft" and self._draft_sent:
                    await asyncio.sleep(4.0)
                    continue
                await self._send_typing()
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            pass

    async def _send_or_edit(self, text: str) -> None:
        """Route to draft or message transport.

        Draft transport: sendMessageDraft (live preview, no notification).
        Message transport: sendMessage + editMessageText (fallback).
        """
        if self._transport == "draft":
            try:
                await self._send_draft(text)
                return
            except Exception as exc:
                if _is_draft_unavailable_error(exc):
                    logger.info(
                        "sendMessageDraft not supported, falling back to message transport"
                    )
                else:
                    logger.warning("telegram draft send failed (non-draft error): %s", exc)
                self._transport = "message"
                self._draft_id = None

        logger.debug("_send_or_edit: message transport, message_id=%s, text_len=%d", self._message_id, len(text))
        await self._send_message(text)

    async def _send_draft(self, text: str) -> None:
        """Send or update a draft preview via sendMessageDraft."""
        draft_id = self._draft_id
        if draft_id is None:
            raise RuntimeError("no draft_id allocated")
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._adapter.send_draft(self._chat_id, draft_id, text),
        )
        self._draft_sent = True

        # Some Telegram deployments may respond with a real message payload.
        # If we see a message_id, treat this as message transport from now on
        # to avoid materialize() sending a duplicate.
        try:
            result = response.get("result", {}) if isinstance(response, dict) else {}
            msg_id = result.get("message_id")
            if isinstance(msg_id, int):
                self._message_id = msg_id
                self._transport = "message"
        except Exception:
            pass

    async def _send_message(self, text: str) -> None:
        """Send or edit via sendMessage + editMessageText (fallback)."""
        loop = asyncio.get_running_loop()
        if self._message_id is None:
            response = await loop.run_in_executor(
                None, lambda: self._adapter.send_text(self._chat_id, text),
            )
            result = response.get("result", {}) if isinstance(response, dict) else {}
            self._message_id = result.get("message_id")
        else:
            msg_id = self._message_id
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._adapter.edit_text(self._chat_id, msg_id, text),
                )
            except Exception as exc:
                exc_str = str(exc).lower()
                if "not modified" in exc_str or "message_not_modified" in exc_str:
                    pass
                else:
                    logger.warning("telegram edit_text failed: %s", exc)
