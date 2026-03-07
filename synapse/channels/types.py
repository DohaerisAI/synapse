from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..models import NormalizedInboundEvent


@runtime_checkable
class ChannelMessagingAdapter(Protocol):
    def normalize(self, raw_event: dict[str, Any]) -> NormalizedInboundEvent:
        ...

    def send(self, channel_id: str, text: str) -> None:
        ...


@runtime_checkable
class ChannelHealthAdapter(Protocol):
    def health_check(self) -> ChannelHealth:
        ...


@runtime_checkable
class ChannelSecurityAdapter(Protocol):
    def is_allowed(self, event: NormalizedInboundEvent) -> bool:
        ...


@dataclass(slots=True)
class ChannelMeta:
    name: str
    description: str = ""
    icon: str = ""


@dataclass(slots=True)
class ChannelHealth:
    status: str = "unknown"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChannelPlugin:
    id: str
    meta: ChannelMeta
    messaging: ChannelMessagingAdapter
    health: ChannelHealthAdapter | None = None
    security: ChannelSecurityAdapter | None = None
