"""Tests for self-awareness wiring: SELF.md, context injection, executor actions, API."""
from __future__ import annotations

from pathlib import Path

import pytest

from synapse.diagnosis import DiagnosisEngine
from synapse.executors import HostExecutor
from synapse.introspection import RuntimeIntrospector
from synapse.models import ExecutionResult, NormalizedInboundEvent, PlannedAction, RunState
from synapse.plugins.registry import PluginRegistry
from synapse.skills import SkillRegistry
from synapse.store import SQLiteStore
from synapse.workspace import DEFAULT_WORKSPACE_FILES, WorkspaceStore
from synapse.memory import MemoryStore


# --- SELF.md workspace file ---


def test_self_md_in_default_workspace_files():
    assert "SELF.md" in DEFAULT_WORKSPACE_FILES
    content = DEFAULT_WORKSPACE_FILES["SELF.md"]
    assert "Synapse" in content


def test_workspace_initializes_self_md(tmp_path: Path):
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()
    ws = WorkspaceStore(tmp_path, memory)
    ws.initialize()
    self_path = tmp_path / "SELF.md"
    assert self_path.exists()
    text = self_path.read_text()
    assert "Synapse" in text


def test_workspace_context_includes_self(tmp_path: Path):
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()
    ws = WorkspaceStore(tmp_path, memory)
    ws.initialize()
    bundle = ws.context_bundle("session1", "user1")
    assert "Self" in bundle or "Synapse" in bundle


# --- HostExecutor handles self.* and diagnosis.* actions ---


@pytest.fixture()
def executor_env(tmp_path: Path):
    from synapse.gws import GWSBridge
    from synapse.integrations import IntegrationRegistry

    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()
    skills = SkillRegistry(tmp_path / "skills")
    (tmp_path / "skills").mkdir()
    skills.load()
    store = SQLiteStore(tmp_path / "store.sqlite3")
    store.initialize()
    integrations = IntegrationRegistry(
        tmp_path / "integrations",
        skills_dir=tmp_path / "skills",
        boot_path=tmp_path / "BOOT.md",
        env={},
    )
    gws = GWSBridge(enabled=False, binary="gws", allowed_services=set(), env={})
    introspector = RuntimeIntrospector(
        plugin_registry=PluginRegistry(),
        skill_registry=skills,
    )
    diagnosis = DiagnosisEngine(store=store)
    executor = HostExecutor(
        memory, skills, store, integrations, gws,
        introspector=introspector,
        diagnosis_engine=diagnosis,
    )
    return executor, store


@pytest.mark.asyncio
async def test_executor_self_describe(executor_env):
    executor, _ = executor_env
    action = PlannedAction(action="self.describe", payload={})
    result = await executor.execute(action, session_key="s1", user_id="u1")
    assert result.success
    assert "Synapse" in result.detail or "Synapse" in str(result.artifacts)
    assert "identity" in result.artifacts


@pytest.mark.asyncio
async def test_executor_self_health(executor_env):
    executor, store = executor_env
    event = NormalizedInboundEvent(
        adapter="telegram", channel_id="c1", user_id="u1",
        message_id="m1", text="test",
    )
    run = store.create_run("s1", event)
    store.set_run_state(run.run_id, RunState.COMPLETED)

    action = PlannedAction(action="self.health", payload={})
    result = await executor.execute(action, session_key="s1", user_id="u1")
    assert result.success
    assert "health" in result.artifacts


@pytest.mark.asyncio
async def test_executor_self_capabilities(executor_env):
    executor, _ = executor_env
    action = PlannedAction(action="self.capabilities", payload={})
    result = await executor.execute(action, session_key="s1", user_id="u1")
    assert result.success
    assert "capabilities" in result.artifacts


@pytest.mark.asyncio
async def test_executor_self_gaps(executor_env):
    executor, _ = executor_env
    action = PlannedAction(action="self.gaps", payload={})
    result = await executor.execute(action, session_key="s1", user_id="u1")
    assert result.success
    assert "limitations" in result.artifacts


@pytest.mark.asyncio
async def test_executor_diagnosis_report(executor_env):
    executor, store = executor_env
    for i in range(3):
        event = NormalizedInboundEvent(
            adapter="telegram", channel_id="c1", user_id="u1",
            message_id=f"m{i}", text="test",
        )
        run = store.create_run(f"s{i}", event)
        store.set_run_state(run.run_id, RunState.COMPLETED)

    action = PlannedAction(action="diagnosis.report", payload={})
    result = await executor.execute(action, session_key="s1", user_id="u1")
    assert result.success
    assert "report" in result.artifacts
    assert result.artifacts["report"]["total_runs"] == 3
