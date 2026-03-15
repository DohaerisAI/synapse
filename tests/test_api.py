import httpx
import pytest

from synapse.app import create_app
from synapse.runtime import build_runtime


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
            ("/console/logs", "Recent Event Trace"),
        ]:
            response = await client.get(path)
            assert response.status_code == 200
            assert marker in response.text
