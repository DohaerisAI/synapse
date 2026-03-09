from __future__ import annotations

from pathlib import Path

import pytest

from synapse.introspection import (
    RuntimeIntrospector,
)
from synapse.capabilities import DEFAULT_CAPABILITY_REGISTRY
from synapse.plugins.registry import PluginRegistry
from synapse.plugins.types import PluginKind, PluginManifest, PluginRecord
from synapse.skills import SkillRegistry


def test_introspector_discovers_capabilities():
    introspector = RuntimeIntrospector(
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
        plugin_registry=PluginRegistry(),
        skill_registry=SkillRegistry(Path("/nonexistent")),
    )
    caps = introspector.discover_capabilities()
    assert "gws.gmail.send" in caps
    assert "memory.read" in caps
    assert "shell.exec" in caps
    assert len(caps) > 10


def test_introspector_discovers_plugins():
    registry = PluginRegistry()
    registry.register(
        PluginRecord(
            plugin_id="telegram",
            manifest=PluginManifest(id="telegram", name="Telegram", kind=PluginKind.CHANNEL),
            loaded=True,
        )
    )
    introspector = RuntimeIntrospector(
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
        plugin_registry=registry,
        skill_registry=SkillRegistry(Path("/nonexistent")),
    )
    plugins = introspector.discover_plugins()
    assert len(plugins) == 1
    assert plugins[0]["id"] == "telegram"
    assert plugins[0]["kind"] == "channel"
    assert plugins[0]["loaded"] is True


def test_introspector_discovers_skills(tmp_path: Path):
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text('{"id":"test-skill","name":"Test","description":"A test skill","capabilities":["testing"]}')
    (skill_dir / "SKILL.md").write_text("# Test Skill\nDo test things.")

    skill_registry = SkillRegistry(tmp_path)
    skill_registry.load()

    introspector = RuntimeIntrospector(
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
        plugin_registry=PluginRegistry(),
        skill_registry=skill_registry,
    )
    skills = introspector.discover_skills()
    assert len(skills) == 1
    assert skills[0]["id"] == "test-skill"
    assert skills[0]["capabilities"] == ["testing"]


def test_introspector_discover_limitations():
    introspector = RuntimeIntrospector(
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
        plugin_registry=PluginRegistry(),
        skill_registry=SkillRegistry(Path("/nonexistent")),
    )
    limitations = introspector.discover_limitations()
    assert isinstance(limitations, list)
    assert len(limitations) > 0
    assert all(hasattr(lim, "area") for lim in limitations)


def test_introspector_build_architecture():
    introspector = RuntimeIntrospector(
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
        plugin_registry=PluginRegistry(),
        skill_registry=SkillRegistry(Path("/nonexistent")),
    )
    arch = introspector.build_architecture()
    names = arch.component_names()
    assert "Gateway" in names
    assert "Store" in names
    assert "PluginSystem" in names
