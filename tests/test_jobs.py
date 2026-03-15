from __future__ import annotations

from pathlib import Path

import pytest

from synapse.models import DeliveryTarget, JobStatus
from synapse.store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    active_store = SQLiteStore(tmp_path / "var" / "runtime.sqlite3")
    active_store.initialize()
    return active_store


def test_job_store_create_and_claim(store: SQLiteStore) -> None:
    job = store.create_job(
        job_id="job-1",
        tool_name="shell_exec",
        params={"command": "pwd"},
        artifact_root="/tmp/job-1",
        progress_path="/tmp/job-1/progress.ndjson",
        result_path="/tmp/job-1/result.json",
        parent_run_id="run-1",
        session_key="sess-1",
        delivery_target=DeliveryTarget(adapter="telegram", channel_id="22", user_id="44"),
    )

    claimed = store.claim_next_job("worker-1")

    assert job.status == JobStatus.QUEUED
    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert claimed.status == JobStatus.RUNNING
    assert claimed.worker_id == "worker-1"
    assert claimed.started_at is not None


def test_job_store_update_and_complete(store: SQLiteStore) -> None:
    store.create_job(
        job_id="job-2",
        tool_name="codex_propose",
        params={"repo_path": "/repo", "task": "x"},
        artifact_root="/tmp/job-2",
        progress_path="/tmp/job-2/progress.ndjson",
        result_path="/tmp/job-2/result.json",
    )
    store.claim_next_job("worker-2")

    store.update_job_progress("job-2", progress_current=1, progress_total=3, progress_message="phase 1")
    store.complete_job("job-2", result_summary="proposal written")

    job = store.get_job("job-2")
    assert job is not None
    assert job.progress_current == 1
    assert job.progress_total == 3
    assert job.progress_message == "phase 1"
    assert job.status == JobStatus.SUCCEEDED
    assert job.result_summary == "proposal written"
    assert job.finished_at is not None


def test_job_store_fail_and_cancel(store: SQLiteStore) -> None:
    store.create_job(
        job_id="job-3",
        tool_name="shell_exec",
        params={"command": "pwd"},
        artifact_root="/tmp/job-3",
        progress_path="/tmp/job-3/progress.ndjson",
        result_path="/tmp/job-3/result.json",
    )
    cancelled = store.cancel_queued_job("job-3", result_summary="cancelled", error="cancelled")
    assert cancelled is not None
    assert cancelled.status == JobStatus.CANCELLED

    store.create_job(
        job_id="job-4",
        tool_name="shell_exec",
        params={"command": "pwd"},
        artifact_root="/tmp/job-4",
        progress_path="/tmp/job-4/progress.ndjson",
        result_path="/tmp/job-4/result.json",
    )
    store.claim_next_job("worker-4")
    requested = store.request_job_cancel("job-4")
    assert requested is not None
    assert requested.status == JobStatus.CANCEL_REQUESTED
    store.cancel_running_job("job-4", result_summary="cancelled while running", error="cancelled")
    running = store.get_job("job-4")
    assert running is not None
    assert running.status == JobStatus.CANCELLED

    store.create_job(
        job_id="job-5",
        tool_name="shell_exec",
        params={"command": "pwd"},
        artifact_root="/tmp/job-5",
        progress_path="/tmp/job-5/progress.ndjson",
        result_path="/tmp/job-5/result.json",
    )
    store.claim_next_job("worker-5")
    store.fail_job("job-5", error="boom", result_summary="failed")
    failed = store.get_job("job-5")
    assert failed is not None
    assert failed.status == JobStatus.FAILED
    assert failed.error == "boom"


def test_job_store_marks_orphaned_running_jobs_failed_on_startup(store: SQLiteStore) -> None:
    store.create_job(
        job_id="job-6",
        tool_name="shell_exec",
        params={"command": "pwd"},
        artifact_root="/tmp/job-6",
        progress_path="/tmp/job-6/progress.ndjson",
        result_path="/tmp/job-6/result.json",
    )
    store.claim_next_job("worker-6")
    store.request_job_cancel("job-6")

    interrupted = store.mark_running_jobs_interrupted_on_startup()

    assert interrupted == 1
    job = store.get_job("job-6")
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.error == "interrupted on restart"
