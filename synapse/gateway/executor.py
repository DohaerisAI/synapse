from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ..models import PlannedAction, RunRecord, RunState, WorkflowPlan

if TYPE_CHECKING:
    from .core import Gateway


class WorkflowExecutor:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    async def execute_workflow(
        self,
        run: RunRecord,
        current: RunState,
        workflow: WorkflowPlan,
    ) -> tuple[RunState, list[dict[str, Any]]]:
        gw = self._gw
        execution_results: list[dict[str, Any]] = []
        if not workflow.steps:
            return current, execution_results
        current = gw.state_manager.transition(
            run,
            current,
            RunState.EXECUTING,
            {"workflow_id": workflow.workflow_id, "step_count": len(workflow.steps), "intent": workflow.intent},
        )
        for index, step in enumerate(workflow.steps, start=1):
            action = self._resolve_step_action(step.action, execution_results)
            gw.store.append_run_event(
                run.run_id,
                run.session_key,
                "workflow.step.started",
                {"workflow_id": workflow.workflow_id, "step_id": step.step_id, "index": index, "action": action.to_dict()},
            )
            decision = gw.broker.decide(action)
            if decision.executor == "host":
                result = await gw.host_executor.execute(action, session_key=run.session_key, user_id=run.user_id)
            else:
                result = await gw.isolated_executor.execute(action)
            result_payload = {"action": result.action, "success": result.success, "detail": result.detail, "artifacts": result.artifacts}
            execution_results.append(result_payload)
            gw.store.append_run_event(
                run.run_id,
                run.session_key,
                "workflow.step.completed",
                {"workflow_id": workflow.workflow_id, "step_id": step.step_id, "index": index, "result": result_payload},
            )
            if not result.success:
                break
        current = gw.state_manager.transition(run, current, RunState.VERIFYING, {"results": execution_results})
        return current, execution_results

    def _resolve_step_action(self, action: PlannedAction, execution_results: list[dict[str, Any]]) -> PlannedAction:
        if not execution_results:
            return action
        payload = json.loads(json.dumps(action.payload, ensure_ascii=True))
        last_artifacts = execution_results[-1].get("artifacts", {})
        last_output = last_artifacts.get("output", {})

        def resolve(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("$last."):
                key = value.removeprefix("$last.")
                if isinstance(last_output, dict) and key in last_output:
                    return last_output[key]
                return last_artifacts.get(key, value)
            if isinstance(value, list):
                return [resolve(item) for item in value]
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            return value

        return PlannedAction(action=action.action, payload=resolve(payload))
