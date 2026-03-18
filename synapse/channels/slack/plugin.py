"""Slack channel plugin — wires SlackAdapter into the ChannelPlugin interface."""
from __future__ import annotations

from typing import Any

from ...slack_adapter import SlackAdapter
from ...models import NormalizedInboundEvent
from ..types import ChannelHealth, ChannelMeta, ChannelPlugin


class SlackMessaging:
    def __init__(self, adapter: SlackAdapter) -> None:
        self._adapter = adapter

    def normalize(self, raw_event: dict[str, Any]) -> NormalizedInboundEvent:
        return self._adapter.normalize_event(raw_event)

    def send(self, channel_id: str, text: str) -> None:
        self._adapter.send_text(channel_id, text)


class SlackHealth:
    def __init__(self, adapter: SlackAdapter) -> None:
        self._adapter = adapter

    def health_check(self) -> ChannelHealth:
        snapshot = self._adapter.status_snapshot()
        return ChannelHealth(
            status=snapshot.get("status", "unknown"),
            details=snapshot,
        )


class SlackPlugin:
    @staticmethod
    def create(adapter: SlackAdapter) -> ChannelPlugin:
        return ChannelPlugin(
            id="slack",
            meta=ChannelMeta(
                name="Slack",
                description="Slack Bot API adapter with Socket Mode and HTTP Events API support",
                icon="slack",
            ),
            messaging=SlackMessaging(adapter),
            health=SlackHealth(adapter),
        )
