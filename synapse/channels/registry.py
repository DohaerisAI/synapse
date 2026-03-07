from __future__ import annotations

from typing import Any

from ..models import NormalizedInboundEvent
from .types import ChannelHealth, ChannelPlugin


class ChannelRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, ChannelPlugin] = {}

    def register(self, plugin: ChannelPlugin) -> None:
        self._plugins[plugin.id] = plugin

    def get(self, channel_id: str) -> ChannelPlugin | None:
        return self._plugins.get(channel_id)

    def list(self) -> list[ChannelPlugin]:
        return list(self._plugins.values())

    def normalize(self, adapter: str, raw_event: dict[str, Any]) -> NormalizedInboundEvent:
        plugin = self._plugins.get(adapter)
        if plugin is None:
            raise KeyError(f"unknown channel adapter: {adapter}")
        return plugin.messaging.normalize(raw_event)

    def send(self, adapter: str, channel_id: str, text: str) -> None:
        plugin = self._plugins.get(adapter)
        if plugin is None:
            raise KeyError(f"unknown channel adapter: {adapter}")
        plugin.messaging.send(channel_id, text)

    def health(self, adapter: str) -> ChannelHealth:
        plugin = self._plugins.get(adapter)
        if plugin is None:
            return ChannelHealth(status="unknown", details={"error": f"unknown adapter: {adapter}"})
        if plugin.health is None:
            return ChannelHealth(status="no_health_adapter")
        return plugin.health.health_check()
