import pytest

from synapse.envfile import load_env_file, write_env_file
from synapse.runtime import build_runtime
from synapse.tui import RuntimeTuiApp, render_tui


def test_tui_renders_auth_and_telegram_sections(tmp_path) -> None:
    runtime = build_runtime(tmp_path)

    output = render_tui(runtime)

    assert "Resolved model:" in output
    assert "Telegram" in output
    assert "GWS:" in output
    assert "Heartbeat:" in output


def test_env_file_round_trip(tmp_path) -> None:
    path = tmp_path / ".env.local"
    write_env_file(path, {"AGENT_NAME": "Claw", "TELEGRAM_BOT_TOKEN": "abc", "HEARTBEAT_ENABLED": "1"})
    values = load_env_file(path)
    assert values["AGENT_NAME"] == "Claw"
    assert values["TELEGRAM_BOT_TOKEN"] == "abc"
    assert values["HEARTBEAT_ENABLED"] == "1"


@pytest.mark.anyio
async def test_textual_tui_mounts_and_shows_overview(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    app = RuntimeTuiApp(runtime, refresh_interval=60.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        summary = app.query_one("#summary-card").render()
        assert "Pending approvals" in str(summary)


def test_only_one_runtime_process_owns_background_services(tmp_path) -> None:
    first = build_runtime(tmp_path)
    second = build_runtime(tmp_path)

    first.start_background_services()
    second.start_background_services()

    assert first.background_services_owned is True
    assert second.background_services_owned is False
    assert second.background_services_note is not None

    first.stop_background_services()
    second.stop_background_services()
