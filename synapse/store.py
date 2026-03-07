from __future__ import annotations

import json
import sqlite3
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
    NormalizedInboundEvent,
    PendingInputRecord,
    ReminderRecord,
    ReminderStatus,
    RunRecord,
    RunState,
    utc_now,
)


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
                """
            )

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
