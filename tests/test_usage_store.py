from __future__ import annotations

from synapse.store import SQLiteStore
from synapse.usage import PricingEntry


def test_usage_store_inserts_and_summarizes_events(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "runtime.sqlite3")
    store.initialize()

    store.append_usage_event(
        run_id="run-1",
        session_key="sess-1",
        provider="azure-openai",
        model="gpt-4",
        prompt_tokens=120,
        completion_tokens=30,
        total_tokens=150,
        input_chars=600,
        output_chars=200,
        started_at="2026-03-15T00:00:00+00:00",
        finished_at="2026-03-15T00:00:01+00:00",
        duration_ms=1000,
        status="ok",
    )
    store.append_tool_event(
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
    store.append_run_event(
        "run-1",
        "sess-1",
        "workflow.planned",
        {"workflow_id": "wf-1", "skill_ids": ["gws-gmail", "gws-shared"]},
    )

    usage_events = store.list_usage_events(run_id="run-1")
    tool_events = store.list_tool_events(run_id="run-1")
    summary = store.summarize_usage(
        window_hours=24,
        pricing={"gpt-4": PricingEntry(input_per_1m=2.0, output_per_1m=4.0)},
    )

    assert len(usage_events) == 1
    assert usage_events[0].total_tokens == 150
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "shell_exec"
    assert summary["totals"]["total_tokens"] == 150
    assert summary["totals"]["tool_event_count"] == 1
    assert summary["top_tools"][0]["tool_name"] == "shell_exec"
    assert {item["skill_id"] for item in summary["top_skills"]} == {"gws-gmail", "gws-shared"}
