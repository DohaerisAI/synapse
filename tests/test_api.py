import httpx
import pytest

from synapse.app import create_app
from synapse.jobs import JobService
from synapse.runtime import build_runtime
from synapse.usage import PricingEntry


@pytest.mark.anyio
async def test_telegram_webhook_creates_run_and_health_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/adapters/telegram/webhook",
            json={
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "text": "hello",
                    "chat": {"id": 22},
                    "from": {"id": 44},
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "COMPLETED"

        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["runs_by_state"]["COMPLETED"] >= 1

        runs = await client.get("/api/runs")
        assert runs.status_code == 200
        run_id = runs.json()[0]["run_id"]

        events = await client.get(f"/api/runs/{run_id}/events")
        assert events.status_code == 200
        assert events.json()

        logs = await client.get("/api/logs")
        assert logs.status_code == 200
        assert logs.json()

        auth = await client.get("/api/auth")
        assert auth.status_code == 200
        assert "resolved" in auth.json()

        gws = await client.get("/api/gws")
        assert gws.status_code == 200
        assert "installed" in gws.json()

        telegram = await client.get("/api/adapters/telegram")
        assert telegram.status_code == 200
        assert "status" in telegram.json()

        heartbeat = await client.get("/api/heartbeat")
        assert heartbeat.status_code == 200
        assert "enabled" in heartbeat.json()

        integrations = await client.get("/api/integrations")
        assert integrations.status_code == 200
        assert isinstance(integrations.json(), list)


@pytest.mark.anyio
async def test_slash_command_executes_directly(tmp_path, monkeypatch) -> None:
    """Slash commands execute directly without broker approval in the new pipeline."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/runs/inbound",
            json={
                "adapter": "telegram",
                "channel_id": "22",
                "user_id": "44",
                "message_id": "10",
                "text": "/remember-global ops note",
            },
        )
        assert created.status_code == 200
        assert created.json()["status"] == "COMPLETED"

        approvals = await client.get("/api/approvals")
        assert approvals.status_code == 200
        assert approvals.json() == []


@pytest.mark.anyio
async def test_memory_and_console_pages_are_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/runs/inbound",
            json={
                "adapter": "telegram",
                "channel_id": "22",
                "user_id": "44",
                "message_id": "10",
                "text": "/remember-session keep this local",
            },
        )

        memory = await client.get("/api/memory")
        assert memory.status_code == 200
        snapshot = memory.json()
        assert snapshot["session_files"]

        workspace = await client.get("/api/workspace")
        assert workspace.status_code == 200
        workspace_snapshot = workspace.json()
        assert workspace_snapshot["files"]

        for path, marker in [
            ("/console", "Overview"),
            ("/console/runs", "Recent Runs"),
            ("/console/runs/" + (await client.get("/api/runs")).json()[0]["run_id"], "Events"),
            ("/console/approvals", "Pending Approvals"),
            ("/console/auth", "Auth Sources"),
            ("/console/gws", "Workspace Status"),
            ("/console/memory", "Session Memory"),
            ("/console/workspace", "Workspace Files"),
            ("/console/skills", "Loaded Skills"),
            ("/console/integrations", "Registry"),
            ("/console/adapters", "Adapters"),
            ("/console/heartbeat", "Recent Heartbeats"),
            ("/console/usage", "Usage Totals"),
            ("/console/logs", "Recent Event Trace"),
        ]:
            response = await client.get(path)
            assert response.status_code == 200
            assert marker in response.text


def test_build_runtime_registers_manifest_skill_tools_after_load(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skill_dir = tmp_path / "skills" / "finance"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text(
        """
        {
          "id": "finance",
          "name": "Finance",
          "description": "finance skill",
          "tools": [
            {
              "name": "analyze_portfolio",
              "description": "Analyze a portfolio",
              "parameters": {"type": "object", "properties": {}}
            }
          ]
        }
        """.strip(),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("# Finance Skill\n\nDo finance things.", encoding="utf-8")

    runtime = build_runtime(tmp_path)

    tool = runtime.gateway.tool_registry.get("skill.finance.analyze_portfolio")
    assert tool is not None
    assert tool.category == "skill.finance"


@pytest.mark.anyio
async def test_job_api_create_and_get_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    service = JobService(
        store=runtime.store,
        root=tmp_path / "var" / "jobs",
        execute_job=lambda job, artifacts, cancel_event: None,
    )
    service.start = lambda: None
    service.stop = lambda: None
    runtime.job_service = service
    runtime.gateway._job_service = service
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/jobs",
            json={
                "tool_name": "shell_exec",
                "params": {"command": "pwd"},
            },
        )

        assert created.status_code == 200
        payload = created.json()
        job_id = payload["job_id"]
        assert payload["tool_name"] == "shell_exec"

        job = await client.get(f"/api/jobs/{job_id}")
        assert job.status_code == 200
        assert job.json()["job_id"] == job_id


@pytest.mark.anyio
async def test_job_api_fails_closed_when_approval_is_required(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    service = JobService(
        store=runtime.store,
        root=tmp_path / "var" / "jobs",
        execute_job=lambda job, artifacts, cancel_event: None,
    )
    service.start = lambda: None
    service.stop = lambda: None
    runtime.job_service = service
    runtime.gateway._job_service = service
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/jobs",
            json={
                "tool_name": "shell_exec",
                "params": {"command": "rm -rf /"},
            },
        )

        assert response.status_code == 409


@pytest.mark.anyio
async def test_job_api_cancel_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    service = JobService(
        store=runtime.store,
        root=tmp_path / "var" / "jobs",
        execute_job=lambda job, artifacts, cancel_event: None,
    )
    service.start = lambda: None
    service.stop = lambda: None
    runtime.job_service = service
    runtime.gateway._job_service = service
    job = service.enqueue_job(
        tool_name="shell_exec",
        params={"command": "pwd"},
        session_key="sess-1",
    )
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        cancelled = await client.post(f"/api/jobs/{job.job_id}/cancel")

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"


@pytest.mark.anyio
async def test_usage_api_endpoints(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    runtime.store.append_usage_event(
        run_id="run-1",
        session_key="sess-1",
        provider="azure-openai",
        model="gpt-4",
        prompt_tokens=50,
        completion_tokens=25,
        total_tokens=75,
        input_chars=500,
        output_chars=250,
        started_at="2026-03-15T00:00:00+00:00",
        finished_at="2026-03-15T00:00:01+00:00",
        duration_ms=1000,
        status="ok",
    )
    runtime.store.append_tool_event(
        run_id="run-1",
        session_key="sess-1",
        job_id=None,
        tool_name="shell_exec",
        needs_approval=False,
        started_at="2026-03-15T00:00:01+00:00",
        finished_at="2026-03-15T00:00:02+00:00",
        duration_ms=1000,
        status="ok",
    )
    runtime.store.append_run_event(
        "run-1",
        "sess-1",
        "workflow.planned",
        {"workflow_id": "wf-1", "skill_ids": ["gws-gmail"]},
    )
    runtime.config.pricing = {"gpt-4": PricingEntry(input_per_1m=1.0, output_per_1m=2.0)}
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        summary = await client.get("/api/usage/summary?window_hours=24")
        runs = await client.get("/api/usage/runs?window_hours=24")
        models = await client.get("/api/usage/models?window_hours=24")

    assert summary.status_code == 200
    assert summary.json()["totals"]["total_tokens"] == 75
    assert summary.json()["top_tools"][0]["tool_name"] == "shell_exec"
    assert summary.json()["top_skills"][0]["skill_id"] == "gws-gmail"
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["run_id"] == "run-1"
    assert models.status_code == 200
    assert models.json()["models"][0]["model"] == "gpt-4"
