from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..models import WorkflowPlan, WorkflowStep

if TYPE_CHECKING:
    from .core import Gateway


class ApprovalHandler:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    def is_approval_confirmation(self, lowered: str) -> bool:
        phrases = {"yes", "y", "approve", "approved", "go ahead", "do it", "yes go ahead", "sure", "okay", "ok"}
        return lowered in phrases or lowered.startswith(("yes ", "approve ", "go ahead "))

    def is_approval_rejection(self, lowered: str) -> bool:
        phrases = {"no", "n", "cancel", "reject", "stop", "don't", "do not"}
        return lowered in phrases or lowered.startswith(("no ", "cancel ", "reject ", "stop "))

    def approval_reply_text(self, workflow: WorkflowPlan) -> str:
        if workflow.steps and workflow.steps[0].action.action.startswith("gws."):
            previews = [
                str(step.action.payload.get("command_preview", "")).strip()
                for step in workflow.steps
                if str(step.action.payload.get("command_preview", "")).strip()
            ]
            if previews:
                joined = "\n".join(f"- {preview}" for preview in previews)
                return "Google Workspace request recorded. Waiting for approval to run:\n" + joined
            return "Google Workspace request recorded and waiting for approval."
        if workflow.steps and workflow.steps[-1].action.action == "integration.apply":
            integration_id = workflow.intent.removeprefix("integration.")
            return f"Integration `{integration_id}` is scaffolded and tested. Waiting for approval to apply."
        return "Request recorded and waiting for approval."

    def apply_decisions_to_workflow(
        self,
        workflow: WorkflowPlan,
        decisions: list[tuple[WorkflowStep, Any]],
    ) -> WorkflowPlan:
        workflow.approval_required = any(decision.requires_approval for _, decision in decisions)
        for step, decision in decisions:
            step.requires_approval = decision.requires_approval
        return workflow

    def split_workflow_for_approval(self, workflow: WorkflowPlan) -> tuple[WorkflowPlan, WorkflowPlan]:
        first_approval_index = next((index for index, step in enumerate(workflow.steps) if step.requires_approval), len(workflow.steps))
        pre_steps = workflow.steps[:first_approval_index]
        post_steps = workflow.steps[first_approval_index:]
        pre = WorkflowPlan(
            workflow_id=workflow.workflow_id,
            intent=workflow.intent,
            steps=pre_steps,
            renderer=workflow.renderer,
            approval_required=False,
            skill_ids=list(workflow.skill_ids),
        )
        post = WorkflowPlan(
            workflow_id=workflow.workflow_id,
            intent=workflow.intent,
            steps=post_steps,
            renderer=workflow.renderer,
            approval_required=bool(post_steps),
            skill_ids=list(workflow.skill_ids),
        )
        return pre, post
