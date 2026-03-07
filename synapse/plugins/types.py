from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class PluginKind(StrEnum):
    CHANNEL = "channel"
    SKILL = "skill"
    HOOK = "hook"


class PluginManifest(BaseModel):
    id: str
    name: str
    version: str = "0.1.0"
    description: str = ""
    kind: PluginKind = PluginKind.SKILL
    entry_point: str = ""
    capabilities: list[str] = Field(default_factory=list)


class PluginRecord(BaseModel):
    plugin_id: str
    manifest: PluginManifest
    loaded: bool = False
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
