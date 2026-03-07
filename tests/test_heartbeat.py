from datetime import UTC, datetime

from synapse.models import HeartbeatStatus, NormalizedInboundEvent
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
    session_event = NormalizedInboundEvent(
        adapter="telegram",
        channel_id="chat-1",
        user_id="user-1",
        message_id="message-1",
        text="/remember-global needs approval",
    )

    first = await runtime.gateway.ingest(session_event)
    assert first.status == "WAITING_APPROVAL"

    session_key = first.session_key
    runtime.store.enqueue_event(
        session_key,
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-2",
            text="yes go ahead",
        ),
    )

    runtime.start_background_services()
    try:
        run = runtime.store.get_run(first.run_id)
        pending = runtime.store.get_pending_approval_for_session(session_key)
        queued = runtime.store.peek_next_queued_event(session_key)
    finally:
        runtime.stop_background_services()

    assert run is not None
    assert run.state.value == "CANCELLED"
    assert pending is None
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
