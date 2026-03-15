from __future__ import annotations

from typing import Any

from .plugins.registry import PluginRegistry
from .self_model import Architecture, ComponentInfo, Limitation
from .skills import SkillRegistry


class RuntimeIntrospector:
    def __init__(
        self,
        *,
        plugin_registry: PluginRegistry,
        skill_registry: SkillRegistry,
        # Legacy kwarg accepted but ignored
        capability_registry: Any = None,
    ) -> None:
        self._plugins = plugin_registry
        self._skills = skill_registry

    def discover_capabilities(self) -> list[str]:
        """Return skill-based capabilities."""
        caps: list[str] = []
        for skill in self._skills.skills.values():
            caps.extend(skill.capabilities)
        return caps

    def discover_plugins(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p.plugin_id,
                "name": p.manifest.name,
                "kind": p.manifest.kind.value,
                "loaded": p.loaded,
                "error": p.error,
            }
            for p in self._plugins.list()
        ]

    def discover_skills(self) -> list[dict[str, Any]]:
        return [
            {
                "id": s.skill_id,
                "name": s.name,
                "description": s.description,
                "capabilities": s.capabilities,
            }
            for s in self._skills.skills.values()
        ]

    def discover_limitations(self) -> list[Limitation]:
        limitations = [
            Limitation(
                area="self-modification",
                description="Cannot apply code patches automatically (code.patch.apply disabled)",
            ),
            Limitation(
                area="channels",
                description="Only Telegram channel adapter is implemented",
            ),
            Limitation(
                area="isolation",
                description="IsolatedExecutor runs on host, no Docker sandbox",
            ),
        ]
        if not any(p.loaded for p in self._plugins.list()):
            limitations.append(
                Limitation(
                    area="plugins",
                    description="No plugins currently loaded",
                )
            )
        return limitations

    def build_architecture(self) -> Architecture:
        return Architecture(
            components=[
                ComponentInfo(
                    name="Gateway",
                    role="Central orchestration with ReAct tool-calling loop",
                    module="synapse.gateway",
                    sub_components=[
                        "context", "planner", "ingest", "extractors", "state",
                    ],
                ),
                ComponentInfo(
                    name="Store",
                    role="SQLite operational state",
                    module="synapse.store",
                ),
                ComponentInfo(
                    name="MemoryStore",
                    role="Markdown-first durable memory",
                    module="synapse.memory",
                ),
                ComponentInfo(
                    name="ToolRegistry",
                    role="Native tool calling for ReAct loop",
                    module="synapse.tools",
                ),
                ComponentInfo(
                    name="PluginSystem",
                    role="Plugin discovery, loading, and registry",
                    module="synapse.plugins",
                    sub_components=["discovery", "loader", "registry"],
                ),
                ComponentInfo(
                    name="SkillRegistry",
                    role="Skill loading and selection",
                    module="synapse.skills",
                ),
                ComponentInfo(
                    name="HookRunner",
                    role="Lifecycle event hooks",
                    module="synapse.hooks",
                ),
                ComponentInfo(
                    name="Executors",
                    role="Host and isolated command execution",
                    module="synapse.executors",
                ),
                ComponentInfo(
                    name="Channels",
                    role="Channel adapters and routing",
                    module="synapse.channels",
                ),
                ComponentInfo(
                    name="GWSBridge",
                    role="Google Workspace CLI integration",
                    module="synapse.gws",
                ),
            ]
        )
