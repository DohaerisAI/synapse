from __future__ import annotations

import pytest

from synapse.models import NormalizedInboundEvent
from synapse.runtime import build_runtime


@pytest.mark.asyncio
async def test_planner_does_not_route_live_analyze_when_flag_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rt = build_runtime(tmp_path)
    event = NormalizedInboundEvent(
        adapter="telegram",
        channel_id="1",
        user_id="2",
        message_id="3",
        text="live laurus labs analyse kar",
    )
    plan = await rt.gateway.planner.plan_workflow(event, session_key="telegram__1__2")
    assert plan.intent == "chat.respond"
    assert plan.steps == []


@pytest.mark.asyncio
async def test_planner_routes_live_analyze_only_when_flag_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EXECUTION_ENABLE_LIVE_ANALYZE_NL_ROUTER", "1")
    rt = build_runtime(tmp_path)
    event = NormalizedInboundEvent(
        adapter="telegram",
        channel_id="1",
        user_id="2",
        message_id="3",
        text="live laurus labs analyse kar",
    )
    plan = await rt.gateway.planner.plan_workflow(event, session_key="telegram__1__2")
    assert plan.intent == "trading.live_analyze"
    assert plan.skill_ids == ["swing-trader"]
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.action.action == "shell.exec"
    cmd = str(step.action.payload.get("command"))
    assert "skills/swing-trader/scripts/scanner.py" in cmd
    assert "--symbol LAURUSLABS" in cmd


@pytest.mark.asyncio
async def test_planner_ignores_non_live_trading_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rt = build_runtime(tmp_path)
    event = NormalizedInboundEvent(
        adapter="telegram",
        channel_id="1",
        user_id="2",
        message_id="3",
        text="laurus labs dekh ke bata",
    )
    plan = await rt.gateway.planner.plan_workflow(event, session_key="telegram__1__2")
    # Should defer to react loop
    assert plan.intent == "chat.respond"
    assert plan.steps == []
