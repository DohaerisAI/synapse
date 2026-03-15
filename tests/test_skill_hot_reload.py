import json
import shutil
import time

import httpx
import pytest

from synapse.app import create_app
from synapse.runtime import build_runtime
from synapse.skills import SkillHotReloader


def _write_skill(
    root,
    skill_id: str,
    *,
    description: str,
    tool_name: str,
    instruction: str,
) -> None:
    skill_dir = root / "skills" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": skill_id,
                "name": skill_id.title(),
                "description": description,
                "tools": [
                    {
                        "name": tool_name,
                        "description": f"{tool_name} tool",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(instruction, encoding="utf-8")


def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


def test_skill_hot_reloader_detects_add_change_and_remove(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(SkillHotReloader, "DEFAULT_POLL_INTERVAL_SECONDS", 0.05)
    runtime = build_runtime(tmp_path)

    try:
        _write_skill(
            tmp_path,
            "finance",
            description="first version",
            tool_name="analyze_portfolio",
            instruction="# Finance\n\nAnalyze positions.",
        )

        _wait_for(
            lambda: runtime.skills.get("finance") is not None
            and runtime.gateway.tool_registry.get("skill.finance.analyze_portfolio") is not None
        )

        assert runtime.skills.get("finance").description == "first version"

        _write_skill(
            tmp_path,
            "finance",
            description="second version",
            tool_name="rebalance_portfolio",
            instruction="# Finance\n\nRebalance positions.",
        )

        _wait_for(
            lambda: runtime.skills.get("finance") is not None
            and runtime.skills.get("finance").description == "second version"
            and runtime.gateway.tool_registry.get("skill.finance.rebalance_portfolio") is not None
            and runtime.gateway.tool_registry.get("skill.finance.analyze_portfolio") is None
        )

        shutil.rmtree(tmp_path / "skills" / "finance")

        _wait_for(
            lambda: runtime.skills.get("finance") is None
            and runtime.gateway.tool_registry.get("skill.finance.rebalance_portfolio") is None
        )
    finally:
        runtime.shutdown()


@pytest.mark.anyio
async def test_api_skills_reload_endpoint_uses_runtime_reload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    try:
        _write_skill(
            tmp_path,
            "ops",
            description="ops workflows",
            tool_name="triage_incident",
            instruction="# Ops\n\nTriage incidents.",
        )

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/skills/reload")

            assert response.status_code == 200
            payload = response.json()
            assert payload["skills"] == ["ops"]
            assert payload["skill_count"] == 1
            assert payload["tools"] == ["skill.ops.triage_incident"]
            assert payload["tool_count"] == 1

            skills = await client.get("/api/skills")
            assert skills.status_code == 200
            assert skills.json()[0]["skill_id"] == "ops"
    finally:
        runtime.shutdown()
