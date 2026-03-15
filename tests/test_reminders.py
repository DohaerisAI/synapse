from datetime import UTC, datetime

from synapse.models import ReminderStatus
from synapse.runtime import build_runtime


async def test_runtime_dispatches_due_reminder(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    runtime = build_runtime(tmp_path)
    sent: list[tuple[str, str]] = []
    runtime.telegram.send_text = lambda chat_id, text: sent.append((chat_id, text))  # type: ignore[method-assign]

    # Create reminder directly (NL routing now goes through react loop / LLM)
    runtime.store.create_reminder(
        adapter="telegram",
        channel_id="22",
        user_id="44",
        message="stretch",
        due_at=datetime(2026, 3, 5, 22, 5, tzinfo=UTC).isoformat(),
    )

    runtime.background_services_owned = True
    delivered = runtime.maybe_dispatch_due_reminders(now=datetime(2026, 3, 5, 22, 5, tzinfo=UTC))

    reminders = runtime.store.list_reminders()
    assert delivered == 1
    assert sent == [("22", "stretch")]
    assert reminders[0].status is ReminderStatus.DELIVERED
