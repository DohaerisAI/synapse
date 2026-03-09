"""StreamSink protocol and NullSink no-op implementation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StreamSink(Protocol):
    """Receives text deltas from the model during streaming generation."""

    async def push(self, delta: str) -> None:
        """Receive a text delta from the model."""
        ...

    async def finalize(self) -> None:
        """Mark stream as complete. Flush any pending edits."""
        ...

    @property
    def accumulated_text(self) -> str:
        """Full text accumulated so far."""
        ...


class NullSink:
    """No-op sink for non-streaming paths. Accumulates text silently."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    async def push(self, delta: str) -> None:
        self._parts.append(delta)

    async def finalize(self) -> None:
        pass

    @property
    def accumulated_text(self) -> str:
        return "".join(self._parts)
