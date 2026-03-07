from __future__ import annotations

from typing import Any

from ...adapters import TelegramAdapter
from ...models import NormalizedInboundEvent
from ..types import ChannelHealth, ChannelMeta, ChannelPlugin


class TelegramMessaging:
    def __init__(self, adapter: TelegramAdapter) -> None:
        self._adapter = adapter

    def normalize(self, raw_event: dict[str, Any]) -> NormalizedInboundEvent:
        return self._adapter.normalize_update(raw_event)

    def send(self, channel_id: str, text: str) -> None:
        self._adapter.send_text(channel_id, text)


class TelegramHealth:
    def __init__(self, adapter: TelegramAdapter) -> None:
        self._adapter = adapter

    def health_check(self) -> ChannelHealth:
        snapshot = self._adapter.status_snapshot()
        return ChannelHealth(
            status=snapshot.get("status", "unknown"),
            details=snapshot,
        )


class TelegramPlugin:
    @staticmethod
    def create(adapter: TelegramAdapter) -> ChannelPlugin:
        return ChannelPlugin(
            id="telegram",
            meta=ChannelMeta(
                name="Telegram",
                description="Telegram Bot API adapter with polling and webhook support",
                icon="telegram",
            ),
            messaging=TelegramMessaging(adapter),
            health=TelegramHealth(adapter),
        )
