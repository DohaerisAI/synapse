from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunState(StrEnum):
    RECEIVED = "RECEIVED"
    CONTEXT_BUILT = "CONTEXT_BUILT"
    PLANNED = "PLANNED"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESPONDING = "RESPONDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_RUN_STATES = frozenset(
    {
        RunState.RECEIVED,
        RunState.CONTEXT_BUILT,
        RunState.PLANNED,
        RunState.WAITING_INPUT,
        RunState.WAITING_APPROVAL,
        RunState.EXECUTING,
        RunState.VERIFYING,
        RunState.RESPONDING,
    }
)


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class InputStatus(StrEnum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class HeartbeatStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    SKIPPED = "SKIPPED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReminderStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class IntegrationStatus(StrEnum):
    PROPOSED = "PROPOSED"
    SCAFFOLDED = "SCAFFOLDED"
    TESTED = "TESTED"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"


class NormalizedInboundEvent(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    adapter: str
    channel_id: str
    user_id: str
    message_id: str
    text: str
    occurred_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _parse_occurred_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        if isinstance(value, datetime):
            return value
        return utc_now()

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        data["occurred_at"] = self.occurred_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NormalizedInboundEvent:
        return cls.model_validate(payload)


class PlannedAction(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PlannedAction:
        return cls.model_validate(payload)


class WorkflowStep(BaseModel):
    step_id: str
    action: PlannedAction
    requires_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WorkflowStep:
        return cls.model_validate(payload)


class WorkflowPlan(BaseModel):
    workflow_id: str
    intent: str
    steps: list[WorkflowStep] = Field(default_factory=list)
    renderer: str = "default"
    approval_required: bool = False
    skill_ids: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WorkflowPlan:
        return cls.model_validate(payload)


class PendingInputRecord(BaseModel):
    input_id: str
    run_id: str
    session_key: str
    kind: str
    status: InputStatus
    payload: dict[str, Any]
    prompt: str
    created_at: str
    updated_at: str


class CapabilityDecision(BaseModel):
    allowed: bool
    requires_approval: bool
    executor: str
    reason: str


class ExecutionResult(BaseModel):
    action: str
    success: bool
    detail: str
    artifacts: dict[str, Any] = Field(default_factory=dict)


class SkillDefinition(BaseModel):
    skill_id: str
    name: str
    description: str
    instruction_markdown: str
    path: str = ""
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuthProfile(BaseModel):
    provider: str
    model: str
    source: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class UsageEventRecord(BaseModel):
    usage_id: int
    run_id: str | None = None
    session_key: str | None = None
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    input_chars: int
    output_chars: int
    started_at: str
    finished_at: str
    duration_ms: int
    status: str
    error: str | None = None


class ToolEventRecord(BaseModel):
    tool_event_id: int
    run_id: str | None = None
    session_key: str | None = None
    job_id: str | None = None
    tool_name: str
    needs_approval: bool
    started_at: str
    finished_at: str
    duration_ms: int
    status: str
    error: str | None = None


class GatewayResult(BaseModel):
    run_id: str
    session_key: str
    status: str
    reply_text: str
    approval_id: str | None = None
    queued: bool = False
    suppress_delivery: bool = False
    delivery_target: DeliveryTarget | None = None


class RunRecord(BaseModel):
    run_id: str
    session_key: str
    state: RunState
    adapter: str
    channel_id: str
    user_id: str
    created_at: str
    updated_at: str


class ApprovalRecord(BaseModel):
    approval_id: str
    run_id: str
    session_key: str
    status: ApprovalStatus
    action_name: str
    payload: dict[str, Any]
    created_at: str
    updated_at: str


class ProposalRecord(BaseModel):
    proposal_id: str
    repo_path: str
    proposal_path: str
    task: str
    context: str
    files: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    summary: str = ""
    status: str
    created_at: str
    updated_at: str


class AdapterHealth(BaseModel):
    adapter: str
    status: str
    auth_required: bool
    last_inbound_at: str | None = None
    last_outbound_at: str | None = None
    last_error: str | None = None


class DeliveryTarget(BaseModel):
    adapter: str
    channel_id: str
    user_id: str
    # Optional message_id for reactions or message edits.
    message_id: str | None = None


class HeartbeatRecord(BaseModel):
    heartbeat_id: str
    status: HeartbeatStatus
    scheduled_for: str | None
    started_at: str | None
    completed_at: str | None
    delivery_target_adapter: str | None
    delivery_target_channel_id: str | None
    delivery_target_user_id: str | None
    ack_suppressed: bool
    skip_reason: str | None
    last_error: str | None
    run_id: str | None
    created_at: str
    updated_at: str


class ReminderRecord(BaseModel):
    reminder_id: str
    adapter: str
    channel_id: str
    user_id: str
    message: str
    due_at: str
    status: ReminderStatus
    created_at: str
    updated_at: str
    delivered_at: str | None = None
    last_error: str | None = None


class JobRecord(BaseModel):
    job_id: str
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus
    progress_current: int | None = None
    progress_total: int | None = None
    progress_message: str | None = None
    result_summary: str | None = None
    result_path: str | None = None
    progress_path: str | None = None
    artifact_root: str
    error: str | None = None
    parent_run_id: str | None = None
    session_key: str | None = None
    delivery_target_adapter: str | None = None
    delivery_target_channel_id: str | None = None
    delivery_target_user_id: str | None = None
    approval_id: str | None = None
    worker_id: str | None = None
    cancel_requested_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str
    updated_at: str


class IntegrationRecord(BaseModel):
    integration_id: str
    kind: str
    status: IntegrationStatus
    title: str
    summary: str
    required_env: list[str]
    bootstrap_steps: list[str]
    files: list[str]
    test_spec: str
    created_at: str
    updated_at: str
    last_error: str | None = None
