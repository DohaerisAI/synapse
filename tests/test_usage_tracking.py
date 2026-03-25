"""Tests for cached token tracking in the usage pipeline."""
from __future__ import annotations

from pathlib import Path

import pytest

from synapse.providers import _usage_numbers


def test_usage_numbers_extracts_cached_tokens():
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 80},
    }
    prompt, completion, total, cached = _usage_numbers(usage)
    assert prompt == 100
    assert completion == 20
    assert total == 120
    assert cached == 80


def test_usage_numbers_cached_tokens_top_level():
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "cached_tokens": 50,
    }
    _, _, _, cached = _usage_numbers(usage)
    assert cached == 50


def test_usage_numbers_prefers_details_over_top_level():
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "cached_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 80},
    }
    _, _, _, cached = _usage_numbers(usage)
    assert cached == 80


def test_usage_numbers_no_cached_tokens():
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    _, _, _, cached = _usage_numbers(usage)
    assert cached is None


def test_usage_numbers_none_usage():
    assert _usage_numbers(None) == (None, None, None, None)


def test_store_append_usage_event_stores_cached_tokens(tmp_path: Path):
    from synapse.store import SQLiteStore

    store = SQLiteStore(tmp_path / "test.db")
    store.initialize()
    record = store.append_usage_event(
        run_id="r1",
        session_key="s1",
        provider="custom",
        model="gemini-2.5-flash",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        cached_tokens=80,
        input_chars=500,
        output_chars=100,
        started_at="2026-01-01T00:00:00",
        finished_at="2026-01-01T00:00:01",
        duration_ms=1000,
        status="ok",
    )
    assert record.cached_tokens == 80
    events = store.list_usage_events(run_id="r1")
    assert len(events) == 1
    assert events[0].cached_tokens == 80


def test_store_append_usage_event_cached_tokens_default_none(tmp_path: Path):
    from synapse.store import SQLiteStore

    store = SQLiteStore(tmp_path / "test.db")
    store.initialize()
    record = store.append_usage_event(
        run_id="r1",
        session_key="s1",
        provider="azure-openai",
        model="gpt-4o",
        prompt_tokens=50,
        completion_tokens=10,
        total_tokens=60,
        input_chars=200,
        output_chars=50,
        started_at="2026-01-01T00:00:00",
        finished_at="2026-01-01T00:00:01",
        duration_ms=500,
        status="ok",
    )
    assert record.cached_tokens is None


def test_aggregate_totals_include_cached_tokens(tmp_path: Path):
    from datetime import datetime, timezone
    from synapse.store import SQLiteStore

    store = SQLiteStore(tmp_path / "test.db")
    store.initialize()
    now = datetime.now(timezone.utc).isoformat()
    for i, cached in enumerate([80, 60, None]):
        store.append_usage_event(
            run_id=f"r{i}",
            session_key="s1",
            provider="custom",
            model="gemini-2.5-flash",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cached_tokens=cached,
            input_chars=500,
            output_chars=100,
            started_at=now,
            finished_at=now,
            duration_ms=1000,
            status="ok",
        )
    summary = store.summarize_usage(window_hours=24)
    assert summary["totals"]["cached_tokens"] == 140  # 80 + 60 + 0


def test_context_bundle_stable_ordering(tmp_path: Path):
    """Global memory and skill ops come before transcript for cache-friendly prefix."""
    from synapse.memory import MemoryStore

    store = MemoryStore(tmp_path / "memory")
    store.initialize()
    store.append_global_memory("Global fact one.")
    store.append_user_memory("U1", "User prefers dark mode.")
    store.append_transcript("sess1", {"role": "user", "content": "hello"})
    store.append_transcript("sess1", {"role": "assistant", "content": "hi"})

    bundle = store.context_bundle("sess1", "U1")
    sections = [line for line in bundle.split("\n") if line.startswith("## ")]

    global_idx = next((i for i, s in enumerate(sections) if "Global" in s), -1)
    user_idx = next((i for i, s in enumerate(sections) if "User" in s), -1)
    transcript_idx = next((i for i, s in enumerate(sections) if "Transcript" in s), -1)

    assert global_idx >= 0, "Global Memory section should be present"
    assert transcript_idx >= 0, "Recent Transcript section should be present"
    assert global_idx < user_idx, "Global memory should precede user memory"
    assert user_idx < transcript_idx, "User memory should precede transcript"
