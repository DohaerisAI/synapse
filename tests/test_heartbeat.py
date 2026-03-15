from datetime import UTC, datetime

from synapse.models import HeartbeatStatus, NormalizedInboundEvent, RunState
from synapse.runtime import build_runtime


async def test_runtime_heartbeat_runs_and_suppresses_ok(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HEARTBEAT_ENABLED", "1")
    monkeypatch.setenv("HEARTBEAT_EVERY_MINUTES", "10")
    runtime = build_runtime(tmp_path)

    await runtime.gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="22",
            user_id="44",
            message_id="1",
            text="hello",
        )
    )
    runtime.start_background_services()
    try:
        result = await runtime.maybe_run_heartbeat(now=datetime(2026, 3, 5, 22, 0, tzinfo=UTC))
    finally:
        runtime.stop_background_services()

    latest = runtime.store.get_latest_heartbeat()
    assert result is not None
    assert result.reply_text == "HEARTBEAT_OK"
    assert result.suppress_delivery is True
    assert latest is not None
    assert latest.status is HeartbeatStatus.COMPLETED
    assert latest.ack_suppressed is True


async def test_runtime_heartbeat_respects_active_hours(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HEARTBEAT_ENABLED", "1")
    monkeypatch.setenv("HEARTBEAT_ACTIVE_HOURS", "09:00-17:00")
    runtime = build_runtime(tmp_path)
    runtime.start_background_services()
    try:
        result = await runtime.maybe_run_heartbeat(now=datetime(2026, 3, 5, 22, 0, tzinfo=UTC))
    finally:
        runtime.stop_background_services()

    latest = runtime.store.get_latest_heartbeat()
    assert result is None
    assert latest is not None
    assert latest.status is HeartbeatStatus.SKIPPED
    assert latest.skip_reason == "outside_active_hours"


async def test_runtime_clears_queue_and_cancels_active_runs_on_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    session_key = "telegram__chat-1__user-1"

    # Manually create an active run stuck in EXECUTING state
    event = NormalizedInboundEvent(
        adapter="telegram",
        channel_id="chat-1",
        user_id="user-1",
        message_id="message-1",
        text="stuck request",
    )
    run = runtime.store.create_run(session_key, event)
    runtime.store.set_run_state(run.run_id, RunState.EXECUTING)

    runtime.store.enqueue_event(
        session_key,
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-2",
            text="follow up",
        ),
    )

    runtime.start_background_services()
    try:
        run_after = runtime.store.get_run(run.run_id)
        queued = runtime.store.peek_next_queued_event(session_key)
    finally:
        runtime.stop_background_services()

    assert run_after is not None
    assert run_after.state.value == "CANCELLED"
    assert queued is None


async def test_runtime_clears_queue_on_shutdown(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = build_runtime(tmp_path)
    session_key = "telegram__chat-1__user-1"
    runtime.store.enqueue_event(
        session_key,
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="hi",
        ),
    )

    runtime.start_background_services()
    runtime.stop_background_services()

    assert runtime.store.peek_next_queued_event(session_key) is None
