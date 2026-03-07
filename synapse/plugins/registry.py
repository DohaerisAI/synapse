from __future__ import annotations

from .types import PluginRecord


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, PluginRecord] = {}

    def register(self, record: PluginRecord) -> None:
        self._plugins[record.plugin_id] = record

    def get(self, plugin_id: str) -> PluginRecord | None:
        return self._plugins.get(plugin_id)

    def list(self) -> list[PluginRecord]:
        return list(self._plugins.values())

    def is_loaded(self, plugin_id: str) -> bool:
        record = self._plugins.get(plugin_id)
        return record is not None and record.loaded
