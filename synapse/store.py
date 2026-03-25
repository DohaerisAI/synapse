from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    ACTIVE_RUN_STATES,
    ApprovalRecord,
    ApprovalStatus,
    DeliveryTarget,
    HeartbeatRecord,
    HeartbeatStatus,
    InputStatus,
    JobRecord,
    JobStatus,
    NormalizedInboundEvent,
    PendingInputRecord,
    ProposalRecord,
    ReminderRecord,
    ReminderStatus,
    RunRecord,
    RunState,
    ToolEventRecord,
    UsageEventRecord,
    utc_now,
)
from .usage import PricingEntry, compute_cost


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_session_key ON runs(session_key);
                CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);

                CREATE TABLE IF NOT EXISTS run_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id);

                CREATE TABLE IF NOT EXISTS usage_events (
                    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    session_key TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    input_chars INTEGER NOT NULL,
                    output_chars INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    cached_tokens INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_usage_events_started_at ON usage_events(started_at);
                CREATE INDEX IF NOT EXISTS idx_usage_events_run_id ON usage_events(run_id);
                CREATE INDEX IF NOT EXISTS idx_usage_events_model ON usage_events(model);

                CREATE TABLE IF NOT EXISTS tool_events (
                    tool_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    session_key TEXT,
                    job_id TEXT,
                    tool_name TEXT NOT NULL,
                    needs_approval INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tool_events_started_at ON tool_events(started_at);
                CREATE INDEX IF NOT EXISTS idx_tool_events_run_id ON tool_events(run_id);
                CREATE INDEX IF NOT EXISTS idx_tool_events_tool_name ON tool_events(tool_name);

                CREATE TABLE IF NOT EXISTS queued_events (
                    queued_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_queued_events_session_key ON queued_events(session_key);

                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    action_name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    params TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress_current INTEGER,
                    progress_total INTEGER,
                    progress_message TEXT,
                    result_summary TEXT,
                    result_path TEXT,
                    progress_path TEXT,
                    artifact_root TEXT NOT NULL,
                    error TEXT,
                    parent_run_id TEXT,
                    session_key TEXT,
                    delivery_target_adapter TEXT,
                    delivery_target_channel_id TEXT,
                    delivery_target_user_id TEXT,
                    approval_id TEXT,
                    worker_id TEXT,
                    cancel_requested_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_parent_run_id ON jobs(parent_run_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_delivery_target ON jobs(
                    delivery_target_adapter,
                    delivery_target_channel_id,
                    delivery_target_user_id
                );

                CREATE TABLE IF NOT EXISTS codex_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    repo_path TEXT NOT NULL,
                    proposal_path TEXT NOT NULL,
                    task TEXT NOT NULL,
                    context TEXT NOT NULL,
                    files TEXT NOT NULL,
                    test_commands TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_codex_proposals_status ON codex_proposals(status);

                CREATE TABLE IF NOT EXISTS input_requests (
                    input_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_input_requests_status ON input_requests(status);

                CREATE TABLE IF NOT EXISTS adapter_health (
                    adapter TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    auth_required INTEGER NOT NULL,
                    last_inbound_at TEXT,
                    last_outbound_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS heartbeat_state (
                    heartbeat_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    scheduled_for TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    delivery_target_adapter TEXT,
                    delivery_target_channel_id TEXT,
                    delivery_target_user_id TEXT,
                    ack_suppressed INTEGER NOT NULL DEFAULT 0,
                    skip_reason TEXT,
                    last_error TEXT,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_heartbeat_state_updated_at ON heartbeat_state(updated_at);

                CREATE TABLE IF NOT EXISTS heartbeat_dedupe (
                    dedupe_id TEXT PRIMARY KEY,
                    last_digest TEXT,
                    last_delivered_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    reminder_id TEXT PRIMARY KEY,
                    adapter TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    delivered_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_due_at ON reminders(due_at);
                CREATE INDEX IF NOT EXISTS idx_reminders_status_due_at ON reminders(status, due_at);

                CREATE TABLE IF NOT EXISTS mcp_connections (
                    server_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    auth_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tool_count INTEGER NOT NULL DEFAULT 0,
                    last_health_check TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mcp_call_log (
                    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms REAL NOT NULL DEFAULT 0.0,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mcp_call_log_server ON mcp_call_log(server_id);
                CREATE INDEX IF NOT EXISTS idx_mcp_call_log_created ON mcp_call_log(created_at);
                """
            )
            # Migrations for existing databases
            try:
                connection.execute("ALTER TABLE usage_events ADD COLUMN cached_tokens INTEGER")
            except Exception:
                pass  # Column already exists

    def create_run(self, session_key: str, event: NormalizedInboundEvent) -> RunRecord:
        run_id = uuid4().hex
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (run_id, session_key, state, adapter, channel_id, user_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_key,
                    RunState.RECEIVED.value,
                    event.adapter,
                    event.channel_id,
                    event.user_id,
                    now,
                    now,
                ),
            )
        return RunRecord(
            run_id=run_id,
            session_key=session_key,
            state=RunState.RECEIVED,
            adapter=event.adapter,
            channel_id=event.channel_id,
            user_id=event.user_id,
            created_at=now,
            updated_at=now,
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_from_row(row) if row is not None else None

    def list_runs(self, *, limit: int = 50) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def get_active_run(self, session_key: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE session_key = ? AND state IN (?, ?, ?, ?, ?, ?, ?, ?)
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (
                    session_key,
                    RunState.RECEIVED.value,
                    RunState.CONTEXT_BUILT.value,
                    RunState.PLANNED.value,
                    RunState.WAITING_INPUT.value,
                    RunState.WAITING_APPROVAL.value,
                    RunState.EXECUTING.value,
                    RunState.VERIFYING.value,
                    RunState.RESPONDING.value,
                ),
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def list_active_runs(self) -> list[RunRecord]:
        active_states = tuple(state.value for state in ACTIVE_RUN_STATES)
        placeholders = ", ".join("?" for _ in active_states)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs WHERE state IN ({placeholders}) ORDER BY updated_at ASC",
                active_states,
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def set_run_state(self, run_id: str, state: RunState) -> None:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (state.value, now, run_id),
            )

    def append_run_event(self, run_id: str, session_key: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO run_events (run_id, session_key, event_type, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, session_key, event_type, json.dumps(payload, ensure_ascii=True), utc_now().isoformat()),
            )

    def list_run_events(self, run_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, session_key, event_type, payload, created_at
                FROM run_events
                WHERE run_id = ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._run_event_from_row(row) for row in rows]

    def list_recent_run_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, session_key, event_type, payload, created_at
                FROM run_events
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._run_event_from_row(row) for row in rows]

    def append_usage_event(
        self,
        *,
        run_id: str | None,
        session_key: str | None,
        provider: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        input_chars: int,
        output_chars: int,
        started_at: str,
        finished_at: str,
        duration_ms: int,
        status: str,
        error: str | None = None,
        cached_tokens: int | None = None,
    ) -> UsageEventRecord:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO usage_events (
                    run_id, session_key, provider, model, prompt_tokens, completion_tokens,
                    total_tokens, input_chars, output_chars, started_at, finished_at,
                    duration_ms, status, error, cached_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_key,
                    provider,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    input_chars,
                    output_chars,
                    started_at,
                    finished_at,
                    duration_ms,
                    status,
                    error,
                    cached_tokens,
                ),
            )
            usage_id = int(cursor.lastrowid)
        return UsageEventRecord(
            usage_id=usage_id,
            run_id=run_id,
            session_key=session_key,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            input_chars=input_chars,
            output_chars=output_chars,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status=status,
            error=error,
            cached_tokens=cached_tokens,
        )

    def list_usage_events(
        self,
        *,
        run_id: str | None = None,
        window_hours: int | None = None,
        limit: int = 500,
    ) -> list[UsageEventRecord]:
        with self._connect() as connection:
            if run_id is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM usage_events
                    WHERE run_id = ?
                    ORDER BY usage_id DESC
                    LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
            elif window_hours is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM usage_events
                    WHERE started_at >= ?
                    ORDER BY usage_id DESC
                    LIMIT ?
                    """,
                    (self._window_start(window_hours), limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM usage_events ORDER BY usage_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._usage_event_from_row(row) for row in rows]

    def append_tool_event(
        self,
        *,
        run_id: str | None,
        session_key: str | None,
        job_id: str | None,
        tool_name: str,
        needs_approval: bool,
        started_at: str,
        finished_at: str,
        duration_ms: int,
        status: str,
        error: str | None = None,
    ) -> ToolEventRecord:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tool_events (
                    run_id, session_key, job_id, tool_name, needs_approval, started_at,
                    finished_at, duration_ms, status, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_key,
                    job_id,
                    tool_name,
                    int(needs_approval),
                    started_at,
                    finished_at,
                    duration_ms,
                    status,
                    error,
                ),
            )
            tool_event_id = int(cursor.lastrowid)
        return ToolEventRecord(
            tool_event_id=tool_event_id,
            run_id=run_id,
            session_key=session_key,
            job_id=job_id,
            tool_name=tool_name,
            needs_approval=needs_approval,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status=status,
            error=error,
        )

    def list_tool_events(
        self,
        *,
        run_id: str | None = None,
        window_hours: int | None = None,
        limit: int = 500,
    ) -> list[ToolEventRecord]:
        with self._connect() as connection:
            if run_id is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM tool_events
                    WHERE run_id = ?
                    ORDER BY tool_event_id DESC
                    LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
            elif window_hours is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM tool_events
                    WHERE started_at >= ?
                    ORDER BY tool_event_id DESC
                    LIMIT ?
                    """,
                    (self._window_start(window_hours), limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tool_events ORDER BY tool_event_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._tool_event_from_row(row) for row in rows]

    def enqueue_event(self, session_key: str, event: NormalizedInboundEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO queued_events (session_key, payload, created_at) VALUES (?, ?, ?)",
                (session_key, json.dumps(event.to_dict(), ensure_ascii=True), utc_now().isoformat()),
            )

    def pop_next_queued_event(self, session_key: str) -> NormalizedInboundEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT queued_event_id, payload FROM queued_events
                WHERE session_key = ?
                ORDER BY queued_event_id ASC
                LIMIT 1
                """,
                (session_key,),
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM queued_events WHERE queued_event_id = ?", (row["queued_event_id"],))
        return NormalizedInboundEvent.from_dict(json.loads(row["payload"]))

    def peek_next_queued_event(self, session_key: str) -> NormalizedInboundEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM queued_events
                WHERE session_key = ?
                ORDER BY queued_event_id ASC
                LIMIT 1
                """,
                (session_key,),
            ).fetchone()
        if row is None:
            return None
        return NormalizedInboundEvent.from_dict(json.loads(row["payload"]))

    def clear_queued_events(self, session_key: str | None = None) -> int:
        with self._connect() as connection:
            if session_key is None:
                count = connection.execute("SELECT COUNT(*) AS count FROM queued_events").fetchone()["count"]
                connection.execute("DELETE FROM queued_events")
                return int(count)
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM queued_events WHERE session_key = ?",
                (session_key,),
            ).fetchone()["count"]
            connection.execute("DELETE FROM queued_events WHERE session_key = ?", (session_key,))
            return int(count)

    def create_approval(self, run_id: str, session_key: str, action_name: str, payload: dict[str, Any]) -> ApprovalRecord:
        approval_id = uuid4().hex
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals (approval_id, run_id, session_key, status, action_name, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    run_id,
                    session_key,
                    ApprovalStatus.PENDING.value,
                    action_name,
                    json.dumps(payload, ensure_ascii=True),
                    now,
                    now,
                ),
            )
        return ApprovalRecord(
            approval_id=approval_id,
            run_id=run_id,
            session_key=session_key,
            status=ApprovalStatus.PENDING,
            action_name=action_name,
            payload=payload,
            created_at=now,
            updated_at=now,
        )

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        return self._approval_from_row(row) if row is not None else None

    def list_pending_approvals(self) -> list[ApprovalRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approvals
                WHERE status = ?
                ORDER BY created_at ASC
                """,
                (ApprovalStatus.PENDING.value,),
            ).fetchall()
        return [self._approval_from_row(row) for row in rows]

    def get_pending_approval_for_session(self, session_key: str) -> ApprovalRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM approvals
                WHERE session_key = ? AND status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (session_key, ApprovalStatus.PENDING.value),
            ).fetchone()
        return self._approval_from_row(row) if row is not None else None

    def update_approval_status(self, approval_id: str, status: ApprovalStatus) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE approvals SET status = ?, updated_at = ? WHERE approval_id = ?",
                (status.value, utc_now().isoformat(), approval_id),
            )

    def create_job(
        self,
        *,
        job_id: str,
        tool_name: str,
        params: dict[str, Any],
        artifact_root: str,
        progress_path: str,
        result_path: str,
        parent_run_id: str | None = None,
        session_key: str | None = None,
        delivery_target: DeliveryTarget | None = None,
        approval_id: str | None = None,
    ) -> JobRecord:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, tool_name, params, status, progress_current, progress_total,
                    progress_message, result_summary, result_path, progress_path, artifact_root,
                    error, parent_run_id, session_key, delivery_target_adapter,
                    delivery_target_channel_id, delivery_target_user_id, approval_id, worker_id,
                    cancel_requested_at, started_at, finished_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    job_id,
                    tool_name,
                    json.dumps(params, ensure_ascii=True),
                    JobStatus.QUEUED.value,
                    result_path,
                    progress_path,
                    artifact_root,
                    parent_run_id,
                    session_key,
                    None if delivery_target is None else delivery_target.adapter,
                    None if delivery_target is None else delivery_target.channel_id,
                    None if delivery_target is None else delivery_target.user_id,
                    approval_id,
                    now,
                    now,
                ),
            )
        return JobRecord(
            job_id=job_id,
            tool_name=tool_name,
            params=dict(params),
            status=JobStatus.QUEUED,
            result_path=result_path,
            progress_path=progress_path,
            artifact_root=artifact_root,
            parent_run_id=parent_run_id,
            session_key=session_key,
            delivery_target_adapter=None if delivery_target is None else delivery_target.adapter,
            delivery_target_channel_id=None if delivery_target is None else delivery_target.channel_id,
            delivery_target_user_id=None if delivery_target is None else delivery_target.user_id,
            approval_id=approval_id,
            created_at=now,
            updated_at=now,
        )

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job_from_row(row) if row is not None else None

    def list_jobs(self, *, limit: int = 50) -> list[JobRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def claim_next_job(self, worker_id: str) -> JobRecord | None:
        started_at = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = ?, worker_id = ?, started_at = ?, updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    JobStatus.RUNNING.value,
                    worker_id,
                    started_at,
                    started_at,
                    row["job_id"],
                    JobStatus.QUEUED.value,
                ),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
        return None if claimed is None else self._job_from_row(claimed)

    def update_job_progress(
        self,
        job_id: str,
        *,
        progress_current: int | None = None,
        progress_total: int | None = None,
        progress_message: str | None = None,
        status: JobStatus | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET progress_current = COALESCE(?, progress_current),
                    progress_total = COALESCE(?, progress_total),
                    progress_message = COALESCE(?, progress_message),
                    status = COALESCE(?, status),
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    progress_current,
                    progress_total,
                    progress_message,
                    None if status is None else status.value,
                    utc_now().isoformat(),
                    job_id,
                ),
            )

    def complete_job(
        self,
        job_id: str,
        *,
        result_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        finished_at = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    result_summary = COALESCE(?, result_summary),
                    error = COALESCE(?, error),
                    finished_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.SUCCEEDED.value,
                    result_summary,
                    error,
                    finished_at,
                    finished_at,
                    job_id,
                ),
            )

    def fail_job(
        self,
        job_id: str,
        *,
        error: str,
        result_summary: str | None = None,
    ) -> None:
        finished_at = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    result_summary = COALESCE(?, result_summary),
                    error = ?,
                    finished_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.FAILED.value,
                    result_summary,
                    error,
                    finished_at,
                    finished_at,
                    job_id,
                ),
            )

    def request_job_cancel(self, job_id: str) -> JobRecord | None:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = CASE
                        WHEN status = ? THEN ?
                        WHEN status = ? THEN ?
                        ELSE status
                    END,
                    cancel_requested_at = CASE
                        WHEN status IN (?, ?) THEN ?
                        ELSE cancel_requested_at
                    END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.RUNNING.value,
                    JobStatus.CANCEL_REQUESTED.value,
                    JobStatus.CANCEL_REQUESTED.value,
                    JobStatus.CANCEL_REQUESTED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.CANCEL_REQUESTED.value,
                    now,
                    now,
                    job_id,
                ),
            )
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else self._job_from_row(row)

    def cancel_queued_job(
        self,
        job_id: str,
        *,
        result_summary: str | None = None,
        error: str | None = None,
    ) -> JobRecord | None:
        finished_at = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    result_summary = COALESCE(?, result_summary),
                    error = COALESCE(?, error),
                    cancel_requested_at = COALESCE(cancel_requested_at, ?),
                    finished_at = ?,
                    updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    JobStatus.CANCELLED.value,
                    result_summary,
                    error,
                    finished_at,
                    finished_at,
                    finished_at,
                    job_id,
                    JobStatus.QUEUED.value,
                ),
            )
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else self._job_from_row(row)

    def cancel_running_job(
        self,
        job_id: str,
        *,
        result_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        finished_at = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    result_summary = COALESCE(?, result_summary),
                    error = COALESCE(?, error),
                    finished_at = ?,
                    updated_at = ?
                WHERE job_id = ? AND status IN (?, ?)
                """,
                (
                    JobStatus.CANCELLED.value,
                    result_summary,
                    error,
                    finished_at,
                    finished_at,
                    job_id,
                    JobStatus.RUNNING.value,
                    JobStatus.CANCEL_REQUESTED.value,
                ),
            )

    def mark_running_jobs_interrupted_on_startup(self) -> int:
        finished_at = utc_now().isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN (?, ?)",
                (JobStatus.RUNNING.value, JobStatus.CANCEL_REQUESTED.value),
            ).fetchone()
            connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error = ?,
                    finished_at = ?,
                    updated_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    JobStatus.FAILED.value,
                    "interrupted on restart",
                    finished_at,
                    finished_at,
                    JobStatus.RUNNING.value,
                    JobStatus.CANCEL_REQUESTED.value,
                ),
            )
        return int(rows["count"])

    def create_codex_proposal(
        self,
        *,
        proposal_id: str,
        repo_path: str,
        proposal_path: str,
        task: str,
        context: str,
        files: list[str],
        test_commands: list[str],
        summary: str,
        status: str,
    ) -> ProposalRecord:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO codex_proposals (
                    proposal_id,
                    repo_path,
                    proposal_path,
                    task,
                    context,
                    files,
                    test_commands,
                    summary,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    repo_path,
                    proposal_path,
                    task,
                    context,
                    json.dumps(files, ensure_ascii=True),
                    json.dumps(test_commands, ensure_ascii=True),
                    summary,
                    status,
                    now,
                    now,
                ),
            )
        return ProposalRecord(
            proposal_id=proposal_id,
            repo_path=repo_path,
            proposal_path=proposal_path,
            task=task,
            context=context,
            files=list(files),
            test_commands=list(test_commands),
            summary=summary,
            status=status,
            created_at=now,
            updated_at=now,
        )

    def get_codex_proposal(self, proposal_id: str) -> ProposalRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM codex_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return self._codex_proposal_from_row(row) if row is not None else None

    def update_codex_proposal(
        self,
        proposal_id: str,
        *,
        test_commands: list[str] | None = None,
        summary: str | None = None,
        status: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE codex_proposals
                SET test_commands = COALESCE(?, test_commands),
                    summary = COALESCE(?, summary),
                    status = COALESCE(?, status),
                    updated_at = ?
                WHERE proposal_id = ?
                """,
                (
                    None if test_commands is None else json.dumps(test_commands, ensure_ascii=True),
                    summary,
                    status,
                    utc_now().isoformat(),
                    proposal_id,
                ),
            )

    def create_input_request(
        self,
        run_id: str,
        session_key: str,
        *,
        kind: str,
        payload: dict[str, Any],
        prompt: str,
    ) -> PendingInputRecord:
        input_id = uuid4().hex
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO input_requests (input_id, run_id, session_key, kind, status, payload, prompt, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    input_id,
                    run_id,
                    session_key,
                    kind,
                    InputStatus.PENDING.value,
                    json.dumps(payload, ensure_ascii=True),
                    prompt,
                    now,
                    now,
                ),
            )
        return PendingInputRecord(
            input_id=input_id,
            run_id=run_id,
            session_key=session_key,
            kind=kind,
            status=InputStatus.PENDING,
            payload=payload,
            prompt=prompt,
            created_at=now,
            updated_at=now,
        )

    def get_input_request(self, input_id: str) -> PendingInputRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM input_requests WHERE input_id = ?", (input_id,)).fetchone()
        return self._input_request_from_row(row) if row is not None else None

    def get_pending_input_for_session(self, session_key: str) -> PendingInputRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM input_requests
                WHERE session_key = ? AND status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (session_key, InputStatus.PENDING.value),
            ).fetchone()
        return self._input_request_from_row(row) if row is not None else None

    def list_pending_inputs(self) -> list[PendingInputRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM input_requests
                WHERE status = ?
                ORDER BY created_at ASC
                """,
                (InputStatus.PENDING.value,),
            ).fetchall()
        return [self._input_request_from_row(row) for row in rows]

    def update_input_request(
        self,
        input_id: str,
        *,
        status: InputStatus | None = None,
        payload: dict[str, Any] | None = None,
        prompt: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE input_requests
                SET status = COALESCE(?, status),
                    payload = COALESCE(?, payload),
                    prompt = COALESCE(?, prompt),
                    updated_at = ?
                WHERE input_id = ?
                """,
                (
                    None if status is None else status.value,
                    None if payload is None else json.dumps(payload, ensure_ascii=True),
                    prompt,
                    utc_now().isoformat(),
                    input_id,
                ),
            )

    def upsert_adapter_health(
        self,
        *,
        adapter: str,
        status: str,
        auth_required: bool,
        last_inbound_at: str | None = None,
        last_outbound_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO adapter_health (adapter, status, auth_required, last_inbound_at, last_outbound_at, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(adapter) DO UPDATE SET
                    status = excluded.status,
                    auth_required = excluded.auth_required,
                    last_inbound_at = COALESCE(excluded.last_inbound_at, adapter_health.last_inbound_at),
                    last_outbound_at = COALESCE(excluded.last_outbound_at, adapter_health.last_outbound_at),
                    last_error = COALESCE(excluded.last_error, adapter_health.last_error),
                    updated_at = excluded.updated_at
                """,
                (
                    adapter,
                    status,
                    int(auth_required),
                    last_inbound_at,
                    last_outbound_at,
                    last_error,
                    utc_now().isoformat(),
                ),
            )

    def health_snapshot(self) -> dict[str, Any]:
        with self._connect() as connection:
            run_counts = connection.execute(
                "SELECT state, COUNT(*) AS count FROM runs GROUP BY state ORDER BY state"
            ).fetchall()
            queued_count = connection.execute("SELECT COUNT(*) AS count FROM queued_events").fetchone()["count"]
            approval_count = connection.execute(
                "SELECT COUNT(*) AS count FROM approvals WHERE status = ?",
                (ApprovalStatus.PENDING.value,),
            ).fetchone()["count"]
            input_count = connection.execute(
                "SELECT COUNT(*) AS count FROM input_requests WHERE status = ?",
                (InputStatus.PENDING.value,),
            ).fetchone()["count"]
            adapters = connection.execute("SELECT * FROM adapter_health ORDER BY adapter").fetchall()
            heartbeat_row = connection.execute(
                "SELECT * FROM heartbeat_state ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return {
            "runs_by_state": {row["state"]: row["count"] for row in run_counts},
            "queued_events": queued_count,
            "pending_approvals": approval_count,
            "pending_inputs": input_count,
            "adapters": [dict(row) for row in adapters],
            "heartbeat": None if heartbeat_row is None else dict(heartbeat_row),
        }

    def list_adapter_health(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM adapter_health ORDER BY adapter").fetchall()
        return [dict(row) for row in rows]

    def create_heartbeat(
        self,
        *,
        status: HeartbeatStatus,
        scheduled_for: str | None,
        delivery_target: DeliveryTarget | None,
        skip_reason: str | None = None,
        last_error: str | None = None,
        ack_suppressed: bool = False,
        run_id: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> HeartbeatRecord:
        heartbeat_id = uuid4().hex
        now = utc_now().isoformat()
        target_adapter = None if delivery_target is None else delivery_target.adapter
        target_channel_id = None if delivery_target is None else delivery_target.channel_id
        target_user_id = None if delivery_target is None else delivery_target.user_id
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO heartbeat_state (
                    heartbeat_id, status, scheduled_for, started_at, completed_at,
                    delivery_target_adapter, delivery_target_channel_id, delivery_target_user_id,
                    ack_suppressed, skip_reason, last_error, run_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    heartbeat_id,
                    status.value,
                    scheduled_for,
                    started_at,
                    completed_at,
                    target_adapter,
                    target_channel_id,
                    target_user_id,
                    int(ack_suppressed),
                    skip_reason,
                    last_error,
                    run_id,
                    now,
                    now,
                ),
            )
        return HeartbeatRecord(
            heartbeat_id=heartbeat_id,
            status=status,
            scheduled_for=scheduled_for,
            started_at=started_at,
            completed_at=completed_at,
            delivery_target_adapter=target_adapter,
            delivery_target_channel_id=target_channel_id,
            delivery_target_user_id=target_user_id,
            ack_suppressed=ack_suppressed,
            skip_reason=skip_reason,
            last_error=last_error,
            run_id=run_id,
            created_at=now,
            updated_at=now,
        )

    def update_heartbeat(
        self,
        heartbeat_id: str,
        *,
        status: HeartbeatStatus,
        scheduled_for: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        delivery_target: DeliveryTarget | None = None,
        ack_suppressed: bool | None = None,
        skip_reason: str | None = None,
        last_error: str | None = None,
        run_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE heartbeat_state
                SET status = ?,
                    scheduled_for = COALESCE(?, scheduled_for),
                    started_at = COALESCE(?, started_at),
                    completed_at = COALESCE(?, completed_at),
                    delivery_target_adapter = COALESCE(?, delivery_target_adapter),
                    delivery_target_channel_id = COALESCE(?, delivery_target_channel_id),
                    delivery_target_user_id = COALESCE(?, delivery_target_user_id),
                    ack_suppressed = COALESCE(?, ack_suppressed),
                    skip_reason = COALESCE(?, skip_reason),
                    last_error = COALESCE(?, last_error),
                    run_id = COALESCE(?, run_id),
                    updated_at = ?
                WHERE heartbeat_id = ?
                """,
                (
                    status.value,
                    scheduled_for,
                    started_at,
                    completed_at,
                    None if delivery_target is None else delivery_target.adapter,
                    None if delivery_target is None else delivery_target.channel_id,
                    None if delivery_target is None else delivery_target.user_id,
                    None if ack_suppressed is None else int(ack_suppressed),
                    skip_reason,
                    last_error,
                    run_id,
                    utc_now().isoformat(),
                    heartbeat_id,
                ),
            )

    def get_latest_heartbeat(self) -> HeartbeatRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM heartbeat_state ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._heartbeat_from_row(row)

    def list_heartbeats(self, *, limit: int = 20) -> list[HeartbeatRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM heartbeat_state ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._heartbeat_from_row(row) for row in rows]

    def get_heartbeat_last_digest(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_digest FROM heartbeat_dedupe WHERE dedupe_id = 'global'",
            ).fetchone()
        return None if row is None else row["last_digest"]

    def set_heartbeat_last_digest(self, digest: str, *, delivered_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO heartbeat_dedupe (dedupe_id, last_digest, last_delivered_at, updated_at)
                VALUES ('global', ?, ?, ?)
                ON CONFLICT(dedupe_id) DO UPDATE SET
                    last_digest = excluded.last_digest,
                    last_delivered_at = excluded.last_delivered_at,
                    updated_at = excluded.updated_at
                """,
                (digest, delivered_at, utc_now().isoformat()),
            )

    def has_any_active_run(self) -> bool:
        active_states = tuple(state.value for state in ACTIVE_RUN_STATES)
        placeholders = ", ".join("?" for _ in active_states)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM runs WHERE state IN ({placeholders})",
                active_states,
            ).fetchone()
        return bool(row["count"])

    def get_last_delivery_target(self) -> DeliveryTarget | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT adapter, channel_id, user_id
                FROM runs
                WHERE adapter = 'telegram'
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return DeliveryTarget(
            adapter=row["adapter"],
            channel_id=row["channel_id"],
            user_id=row["user_id"],
        )

    def create_reminder(
        self,
        *,
        adapter: str,
        channel_id: str,
        user_id: str,
        message: str,
        due_at: str,
    ) -> ReminderRecord:
        reminder_id = uuid4().hex
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reminders (
                    reminder_id, adapter, channel_id, user_id, message, due_at,
                    status, delivered_at, last_error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    reminder_id,
                    adapter,
                    channel_id,
                    user_id,
                    message,
                    due_at,
                    ReminderStatus.PENDING.value,
                    now,
                    now,
                ),
            )
        return ReminderRecord(
            reminder_id=reminder_id,
            adapter=adapter,
            channel_id=channel_id,
            user_id=user_id,
            message=message,
            due_at=due_at,
            status=ReminderStatus.PENDING,
            created_at=now,
            updated_at=now,
        )

    def claim_due_reminders(self, current_time: str, *, limit: int = 25) -> list[ReminderRecord]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM reminders
                WHERE status = ? AND due_at <= ?
                ORDER BY due_at ASC
                LIMIT ?
                """,
                (ReminderStatus.PENDING.value, current_time, limit),
            ).fetchall()
            if rows:
                connection.executemany(
                    "UPDATE reminders SET status = ?, updated_at = ? WHERE reminder_id = ? AND status = ?",
                    [
                        (
                            ReminderStatus.SENDING.value,
                            utc_now().isoformat(),
                            row["reminder_id"],
                            ReminderStatus.PENDING.value,
                        )
                        for row in rows
                    ],
                )
        claimed: list[ReminderRecord] = []
        for row in rows:
            if row["status"] != ReminderStatus.PENDING.value:
                continue
            claimed.append(
                ReminderRecord(
                    reminder_id=row["reminder_id"],
                    adapter=row["adapter"],
                    channel_id=row["channel_id"],
                    user_id=row["user_id"],
                    message=row["message"],
                    due_at=row["due_at"],
                    status=ReminderStatus.SENDING,
                    delivered_at=row["delivered_at"],
                    last_error=row["last_error"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
        return claimed

    def update_reminder(
        self,
        reminder_id: str,
        *,
        status: ReminderStatus,
        delivered_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE reminders
                SET status = ?,
                    delivered_at = COALESCE(?, delivered_at),
                    last_error = COALESCE(?, last_error),
                    updated_at = ?
                WHERE reminder_id = ?
                """,
                (
                    status.value,
                    delivered_at,
                    last_error,
                    utc_now().isoformat(),
                    reminder_id,
                ),
            )

    def list_reminders(self, *, limit: int = 50) -> list[ReminderRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reminders ORDER BY due_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._reminder_from_row(row) for row in rows]

    def upsert_mcp_connection(
        self,
        *,
        server_id: str,
        url: str,
        auth_type: str,
        status: str,
        tool_count: int = 0,
        last_health_check: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO mcp_connections (server_id, url, auth_type, status, tool_count, last_health_check, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET
                    url = excluded.url,
                    auth_type = excluded.auth_type,
                    status = excluded.status,
                    tool_count = excluded.tool_count,
                    last_health_check = COALESCE(excluded.last_health_check, mcp_connections.last_health_check),
                    updated_at = excluded.updated_at
                """,
                (server_id, url, auth_type, status, tool_count, last_health_check, now, now),
            )

    def list_mcp_connections(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM mcp_connections ORDER BY server_id").fetchall()
        return [dict(row) for row in rows]

    def delete_mcp_connection(self, server_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM mcp_connections WHERE server_id = ?", (server_id,))

    def log_mcp_call(
        self,
        *,
        server_id: str,
        tool_name: str,
        success: bool,
        latency_ms: float,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO mcp_call_log (server_id, tool_name, success, latency_ms, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (server_id, tool_name, int(success), latency_ms, error, utc_now().isoformat()),
            )

    def list_mcp_calls(
        self,
        *,
        server_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if server_id is not None:
                rows = connection.execute(
                    "SELECT * FROM mcp_call_log WHERE server_id = ? ORDER BY call_id DESC LIMIT ?",
                    (server_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM mcp_call_log ORDER BY call_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def summarize_usage(
        self,
        *,
        window_hours: int = 24,
        pricing: dict[str, PricingEntry] | None = None,
        top_n: int = 5,
    ) -> dict[str, Any]:
        pricing_map = pricing or {}
        window_start = self._window_start(window_hours)
        usage_events = self.list_usage_events(window_hours=window_hours, limit=5000)
        tool_events = self.list_tool_events(window_hours=window_hours, limit=5000)
        totals = self._aggregate_usage_totals(usage_events, pricing_map)
        top_tools = self._top_tools(tool_events, limit=top_n)
        top_skills = self._top_skills(window_start, limit=top_n)
        job_counts = self._job_counts(window_start)
        return {
            "window_hours": window_hours,
            "window_start": window_start,
            "generated_at": utc_now().isoformat(),
            "totals": {
                **totals,
                "tool_event_count": len(tool_events),
                "job_count": sum(job_counts.values()),
            },
            "top_tools": top_tools,
            "top_skills": top_skills,
            "job_counts": job_counts,
        }

    def usage_by_run(
        self,
        *,
        window_hours: int = 24,
        pricing: dict[str, PricingEntry] | None = None,
    ) -> list[dict[str, Any]]:
        pricing_map = pricing or {}
        usage_events = self.list_usage_events(window_hours=window_hours, limit=5000)
        tool_events = self.list_tool_events(window_hours=window_hours, limit=5000)
        by_run: dict[str, dict[str, Any]] = {}

        def ensure(run_id: str, session_key: str | None) -> dict[str, Any]:
            if run_id not in by_run:
                by_run[run_id] = {
                    "run_id": run_id,
                    "session_key": session_key,
                    "usage_event_count": 0,
                    "tool_event_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cached_tokens": 0,
                    "input_chars": 0,
                    "output_chars": 0,
                    "cost": 0.0,
                    "cost_unknown": False,
                    "models": Counter(),
                    "tools": Counter(),
                    "last_seen_at": "",
                }
            return by_run[run_id]

        for event in usage_events:
            if not event.run_id:
                continue
            row = ensure(event.run_id, event.session_key)
            row["usage_event_count"] += 1
            row["prompt_tokens"] += event.prompt_tokens or 0
            row["completion_tokens"] += event.completion_tokens or 0
            row["total_tokens"] += event.total_tokens or ((event.prompt_tokens or 0) + (event.completion_tokens or 0))
            row["cached_tokens"] += event.cached_tokens or 0
            row["input_chars"] += event.input_chars
            row["output_chars"] += event.output_chars
            cost, unknown = compute_cost(
                model=event.model,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                pricing=pricing_map,
            )
            row["cost"] += cost
            row["cost_unknown"] = row["cost_unknown"] or unknown
            row["models"][f"{event.provider}/{event.model}"] += 1
            row["last_seen_at"] = max(str(row["last_seen_at"]), event.finished_at)

        for event in tool_events:
            if not event.run_id:
                continue
            row = ensure(event.run_id, event.session_key)
            row["tool_event_count"] += 1
            row["tools"][event.tool_name] += 1
            row["last_seen_at"] = max(str(row["last_seen_at"]), event.finished_at)

        rows = []
        for row in by_run.values():
            rows.append(
                {
                    **row,
                    "cost": round(float(row["cost"]), 8),
                    "models": [name for name, _ in row["models"].most_common()],
                    "tools": [name for name, _ in row["tools"].most_common()],
                }
            )
        rows.sort(key=lambda item: item["last_seen_at"], reverse=True)
        return rows

    def usage_by_model(
        self,
        *,
        window_hours: int = 24,
        pricing: dict[str, PricingEntry] | None = None,
    ) -> list[dict[str, Any]]:
        pricing_map = pricing or {}
        events = self.list_usage_events(window_hours=window_hours, limit=5000)
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for event in events:
            key = (event.provider, event.model)
            if key not in grouped:
                grouped[key] = {
                    "provider": event.provider,
                    "model": event.model,
                    "call_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cached_tokens": 0,
                    "input_chars": 0,
                    "output_chars": 0,
                    "cost": 0.0,
                    "cost_unknown": False,
                    "error_count": 0,
                }
            row = grouped[key]
            row["call_count"] += 1
            row["prompt_tokens"] += event.prompt_tokens or 0
            row["completion_tokens"] += event.completion_tokens or 0
            row["total_tokens"] += event.total_tokens or ((event.prompt_tokens or 0) + (event.completion_tokens or 0))
            row["cached_tokens"] += event.cached_tokens or 0
            row["input_chars"] += event.input_chars
            row["output_chars"] += event.output_chars
            row["error_count"] += 1 if event.status != "ok" else 0
            cost, unknown = compute_cost(
                model=event.model,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                pricing=pricing_map,
            )
            row["cost"] += cost
            row["cost_unknown"] = row["cost_unknown"] or unknown
        rows = [{**row, "cost": round(float(row["cost"]), 8)} for row in grouped.values()]
        rows.sort(key=lambda item: (item["cost"], item["call_count"], item["model"]), reverse=True)
        return rows

    def _window_start(self, window_hours: int) -> str:
        hours = max(1, int(window_hours))
        return (utc_now() - timedelta(hours=hours)).isoformat()

    def _aggregate_usage_totals(
        self,
        usage_events: list[UsageEventRecord],
        pricing: dict[str, PricingEntry],
    ) -> dict[str, Any]:
        totals = {
            "usage_event_count": len(usage_events),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "input_chars": 0,
            "output_chars": 0,
            "duration_ms": 0,
            "cost": 0.0,
            "cost_unknown": False,
            "error_count": 0,
        }
        for event in usage_events:
            totals["prompt_tokens"] += event.prompt_tokens or 0
            totals["completion_tokens"] += event.completion_tokens or 0
            totals["total_tokens"] += event.total_tokens or ((event.prompt_tokens or 0) + (event.completion_tokens or 0))
            totals["cached_tokens"] += event.cached_tokens or 0
            totals["input_chars"] += event.input_chars
            totals["output_chars"] += event.output_chars
            totals["duration_ms"] += event.duration_ms
            totals["error_count"] += 1 if event.status != "ok" else 0
            cost, unknown = compute_cost(
                model=event.model,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                pricing=pricing,
            )
            totals["cost"] += cost
            totals["cost_unknown"] = totals["cost_unknown"] or unknown
        totals["cost"] = round(float(totals["cost"]), 8)
        return totals

    def _top_tools(self, tool_events: list[ToolEventRecord], *, limit: int) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for event in tool_events:
            if event.tool_name not in grouped:
                grouped[event.tool_name] = {
                    "tool_name": event.tool_name,
                    "count": 0,
                    "error_count": 0,
                    "duration_ms": 0,
                    "approval_count": 0,
                }
            row = grouped[event.tool_name]
            row["count"] += 1
            row["error_count"] += 1 if event.status != "ok" else 0
            row["duration_ms"] += event.duration_ms
            row["approval_count"] += 1 if event.needs_approval else 0
        rows = list(grouped.values())
        rows.sort(key=lambda item: (item["count"], item["duration_ms"], item["tool_name"]), reverse=True)
        return rows[:limit]

    def _top_skills(self, window_start: str, *, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM run_events
                WHERE event_type = 'workflow.planned' AND created_at >= ?
                ORDER BY event_id DESC
                LIMIT 2000
                """,
                (window_start,),
            ).fetchall()
        counts: Counter[str] = Counter()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                continue
            skill_ids = payload.get("skill_ids", [])
            if not isinstance(skill_ids, list):
                continue
            for item in skill_ids:
                skill_id = str(item).strip()
                if skill_id:
                    counts[skill_id] += 1
        return [{"skill_id": skill_id, "count": count} for skill_id, count in counts.most_common(limit)]

    def _job_counts(self, window_start: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM jobs
                WHERE created_at >= ?
                GROUP BY status
                ORDER BY status
                """,
                (window_start,),
            ).fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _run_from_row(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            session_key=row["session_key"],
            state=RunState(row["state"]),
            adapter=row["adapter"],
            channel_id=row["channel_id"],
            user_id=row["user_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _approval_from_row(self, row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=row["approval_id"],
            run_id=row["run_id"],
            session_key=row["session_key"],
            status=ApprovalStatus(row["status"]),
            action_name=row["action_name"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _job_from_row(self, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            tool_name=row["tool_name"],
            params=json.loads(row["params"]),
            status=JobStatus(row["status"]),
            progress_current=row["progress_current"],
            progress_total=row["progress_total"],
            progress_message=row["progress_message"],
            result_summary=row["result_summary"],
            result_path=row["result_path"],
            progress_path=row["progress_path"],
            artifact_root=row["artifact_root"],
            error=row["error"],
            parent_run_id=row["parent_run_id"],
            session_key=row["session_key"],
            delivery_target_adapter=row["delivery_target_adapter"],
            delivery_target_channel_id=row["delivery_target_channel_id"],
            delivery_target_user_id=row["delivery_target_user_id"],
            approval_id=row["approval_id"],
            worker_id=row["worker_id"],
            cancel_requested_at=row["cancel_requested_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _codex_proposal_from_row(self, row: sqlite3.Row) -> ProposalRecord:
        return ProposalRecord(
            proposal_id=row["proposal_id"],
            repo_path=row["repo_path"],
            proposal_path=row["proposal_path"],
            task=row["task"],
            context=row["context"],
            files=json.loads(row["files"]),
            test_commands=json.loads(row["test_commands"]),
            summary=row["summary"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _input_request_from_row(self, row: sqlite3.Row) -> PendingInputRecord:
        return PendingInputRecord(
            input_id=row["input_id"],
            run_id=row["run_id"],
            session_key=row["session_key"],
            kind=row["kind"],
            status=InputStatus(row["status"]),
            payload=json.loads(row["payload"]),
            prompt=row["prompt"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _heartbeat_from_row(self, row: sqlite3.Row) -> HeartbeatRecord:
        return HeartbeatRecord(
            heartbeat_id=row["heartbeat_id"],
            status=HeartbeatStatus(row["status"]),
            scheduled_for=row["scheduled_for"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            delivery_target_adapter=row["delivery_target_adapter"],
            delivery_target_channel_id=row["delivery_target_channel_id"],
            delivery_target_user_id=row["delivery_target_user_id"],
            ack_suppressed=bool(row["ack_suppressed"]),
            skip_reason=row["skip_reason"],
            last_error=row["last_error"],
            run_id=row["run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _usage_event_from_row(self, row: sqlite3.Row) -> UsageEventRecord:
        return UsageEventRecord(
            usage_id=int(row["usage_id"]),
            run_id=row["run_id"],
            session_key=row["session_key"],
            provider=row["provider"],
            model=row["model"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            input_chars=int(row["input_chars"]),
            output_chars=int(row["output_chars"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_ms=int(row["duration_ms"]),
            status=row["status"],
            error=row["error"],
            cached_tokens=row["cached_tokens"] if "cached_tokens" in row.keys() else None,
        )

    def _tool_event_from_row(self, row: sqlite3.Row) -> ToolEventRecord:
        return ToolEventRecord(
            tool_event_id=int(row["tool_event_id"]),
            run_id=row["run_id"],
            session_key=row["session_key"],
            job_id=row["job_id"],
            tool_name=row["tool_name"],
            needs_approval=bool(row["needs_approval"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_ms=int(row["duration_ms"]),
            status=row["status"],
            error=row["error"],
        )

    def _run_event_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload_raw = row["payload"]
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = payload_raw
        return {
            "event_id": row["event_id"],
            "run_id": row["run_id"],
            "session_key": row["session_key"],
            "event_type": row["event_type"],
            "payload": payload,
            "created_at": row["created_at"],
        }

    def _reminder_from_row(self, row: sqlite3.Row) -> ReminderRecord:
        return ReminderRecord(
            reminder_id=row["reminder_id"],
            adapter=row["adapter"],
            channel_id=row["channel_id"],
            user_id=row["user_id"],
            message=row["message"],
            due_at=row["due_at"],
            status=ReminderStatus(row["status"]),
            delivered_at=row["delivered_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
