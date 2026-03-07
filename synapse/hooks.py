from __future__ import annotations

from enum import StrEnum
from typing import Any, Callable, Coroutine


HookHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class HookEventType(StrEnum):
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_SENT = "message.sent"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    GATEWAY_STARTED = "gateway.started"
    GATEWAY_STOPPED = "gateway.stopped"


class HookRunner:
    def __init__(self) -> None:
        self._handlers: dict[HookEventType, list[HookHandler]] = {}

    def register(self, event_type: HookEventType, handler: HookHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unregister(self, event_type: HookEventType, handler: HookHandler) -> None:
        handlers = self._handlers.get(event_type, [])
        self._handlers[event_type] = [h for h in handlers if h is not handler]

    async def fire(self, event_type: HookEventType, context: dict[str, Any] | None = None) -> None:
        handlers = self._handlers.get(event_type, [])
        for handler in handlers:
            try:
                await handler(context or {})
            except Exception:
                pass

    def has_handlers(self, event_type: HookEventType) -> bool:
        return bool(self._handlers.get(event_type))

    def clear(self) -> None:
        self._handlers.clear()
