"""ReAct loop — single LLM loop with native tool calling."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .operator import OperatorAction, OperatorLayer
from .tools.registry import ToolDef

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReactResult:
    """Immutable result of a ReAct loop execution."""

    reply: str
    tool_calls_made: list[dict[str, Any]]
    turns: int
    usage: dict[str, Any] | None = None
    pending_approval_id: str | None = None


class _ToolLookup:
    def __init__(self, tools: dict[str, ToolDef]) -> None:
        self._tools = tools

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)


async def run_react_loop(
    messages: list[dict[str, Any]],
    system_prompt: str,
    tools: list[ToolDef],
    *,
    model_router: Any,
    approval_manager: Any | None = None,
    stream_sink: Any | None = None,
    max_turns: int = 10,
    session_key: str | None = None,
    tool_context: Any | None = None,
    run_id: str | None = None,
    approval_event: dict[str, Any] | None = None,
    initial_tool_calls_made: list[dict[str, Any]] | None = None,
    operator_layer: OperatorLayer | None = None,
) -> ReactResult:
    """Run a ReAct agent loop with operator policies."""
    tool_map = {t.name: t for t in tools}
    # Also map sanitized names (dots→underscores) for Responses API compatibility.
    tool_map.update({t.name.replace(".", "_"): t for t in tools if "." in t.name})
    tool_schemas = [t.to_llm_schema() for t in tools]
    tool_calls_made: list[dict[str, Any]] = list(initial_tool_calls_made or [])

    operator = operator_layer or OperatorLayer()
    operator_state: dict[str, Any] = {}
    lookup = _ToolLookup(tool_map)

    async def _execute_operator_tool_call(
        *,
        tool_name: str,
        params: dict[str, Any],
        turn: int,
        call_id: str,
    ) -> None:
        nonlocal messages
        pre_plan, _ = operator.apply(
            None,
            None,
            {
                "kind": "react_pre_tool_call",
                "messages": messages,
                "tool_name": tool_name,
                "params": params,
                "operator_state": operator_state,
            },
            lookup,
        )
        if isinstance(pre_plan, dict):
            tool_name = str(pre_plan.get("tool_name", tool_name))
            params = dict(pre_plan.get("params", params) or {})
            if pre_plan.get("blocked"):
                error = str(pre_plan.get("blocked"))
                tool_calls_made.append(
                    {
                        "tool": tool_name,
                        "params": params,
                        "turn": turn,
                        "denied": True,
                        "error": error,
                        "result": None,
                    }
                )
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"error": error})})
                return

        tool = lookup.get(tool_name)
        if tool is None:
            error = f"tool not found: {tool_name}"
            tool_calls_made.append({"tool": tool_name, "params": params, "turn": turn, "error": error, "result": None})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"error": error})})
            return

        record: dict[str, Any] = {"tool": tool_name, "params": params, "turn": turn, "operator_generated": True}
        try:
            result = await tool.execute(params, ctx=tool_context)
        except Exception as error:
            record["error"] = str(error)
            record["result"] = None
            tool_calls_made.append(record)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"error": str(error)})})
            return

        output = result.output
        if result.error:
            record["error"] = result.error
        followups = operator.on_tool_result(
            tool_name,
            result,
            {"params": params, "messages": messages, "operator_state": operator_state, "tool_calls_made": tool_calls_made},
        ) or []

        system_messages: list[str] = []
        pending_followups: list[OperatorAction] = []
        for action in followups:
            if action.kind == "override_result":
                output = str(action.payload.get("output", output))
            elif action.kind == "system_message":
                content = str(action.payload.get("content", "")).strip()
                if content:
                    system_messages.append(content)
            elif action.kind == "tool_call":
                pending_followups.append(action)

        record["result"] = output
        tool_calls_made.append(record)
        content = output if not result.error else json.dumps({"error": result.error, "output": output})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        for content in system_messages:
            messages.append({"role": "system", "content": content})

        for index, action in enumerate(pending_followups, start=1):
            payload = action.payload
            follow_tool = str(payload.get("tool_name", "")).strip()
            follow_params = payload.get("params", {})
            if not follow_tool or not isinstance(follow_params, dict):
                continue
            await _execute_operator_tool_call(
                tool_name=follow_tool,
                params=follow_params,
                turn=turn,
                call_id=f"{call_id}-op-{index}",
            )

    start_plan, _ = operator.apply(
        None,
        None,
        {"kind": "react_start", "messages": messages, "tool_calls_made": tool_calls_made, "pre_tool_calls": [], "operator_state": operator_state},
        lookup,
    )
    if isinstance(start_plan, dict):
        pre_tool_calls = start_plan.get("pre_tool_calls", [])
        if isinstance(pre_tool_calls, list):
            for index, item in enumerate(pre_tool_calls, start=1):
                if not isinstance(item, dict):
                    continue
                tool_name = str(item.get("tool_name", "")).strip()
                params = item.get("params", {})
                if not tool_name or not isinstance(params, dict):
                    continue
                call_id = f"auto-start-{index}"
                messages.append({"type": "function_call", "call_id": call_id, "name": tool_name, "arguments": json.dumps(params)})
                await _execute_operator_tool_call(tool_name=tool_name, params=params, turn=0, call_id=call_id)

    for turn in range(1, max_turns + 1):
        response = await model_router.chat(
            messages,
            system=system_prompt,
            tools=tool_schemas if tool_schemas else None,
            stream_sink=stream_sink,
        )

        if response is None:
            return ReactResult(reply="[No model available.]", tool_calls_made=tool_calls_made, turns=turn)

        # Handle tool calls
        if response.tool_calls:
            # Append the model's function call items so the next turn sees them.
            for tc in response.tool_calls:
                messages.append(
                    {
                        "type": "function_call",
                        "call_id": tc.id,
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else str(tc.arguments),
                    }
                )
            for tc in response.tool_calls:
                tool_name = tc.name
                params = tc.arguments if isinstance(tc.arguments, dict) else {}
                pre_plan, _ = operator.apply(
                    None,
                    None,
                    {
                        "kind": "react_pre_tool_call",
                        "messages": messages,
                        "tool_name": tool_name,
                        "params": params,
                        "operator_state": operator_state,
                    },
                    lookup,
                )
                if isinstance(pre_plan, dict):
                    tool_name = str(pre_plan.get("tool_name", tool_name))
                    params = dict(pre_plan.get("params", params) or {})
                    blocked = pre_plan.get("blocked")
                    if blocked:
                        record = {"tool": tool_name, "params": params, "turn": turn, "denied": True, "error": str(blocked), "result": None}
                        tool_calls_made.append(record)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": str(blocked)})})
                        continue

                tool = lookup.get(tool_name)
                record: dict[str, Any] = {"tool": tool_name, "params": params, "turn": turn}

                if tool is None:
                    record["error"] = f"tool not found: {tool_name}"
                    record["result"] = None
                    tool_calls_made.append(record)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": f"tool not found: {tool_name}"})})
                    continue

                # Check approval.
                if tool.check_approval(params):
                    if approval_manager is None:
                        record["denied"] = True
                        record["result"] = "approval required"
                        tool_calls_made.append(record)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": "approval required"})})
                        continue
                    authorize_tool_call = getattr(approval_manager, "authorize_tool_call", None)
                    if authorize_tool_call is not None:
                        try:
                            decision = await authorize_tool_call(
                                tool,
                                params,
                                store=getattr(tool_context, "store", None),
                                run_id=run_id,
                                session_key=session_key,
                                event=approval_event,
                                system_prompt=system_prompt,
                                messages=messages,
                                tool_call_id=tc.id,
                                turn=turn,
                                tool_calls_made=tool_calls_made,
                            )
                        except TypeError:
                            decision = None
                    else:
                        decision = None
                    if decision is None:
                        approved = await approval_manager.check_and_approve(tool, params)
                        decision = type(
                            "ApprovalDecisionCompat",
                            (),
                            {
                                "pending": False,
                                "approval_id": None,
                                "approved": approved,
                                "message": None if approved else "tool call denied by user",
                            },
                        )()
                    if decision.pending:
                        record["pending_approval"] = True
                        record["approval_id"] = decision.approval_id
                        record["result"] = "pending approval"
                        tool_calls_made.append(record)
                        return ReactResult(
                            reply=decision.message or f"Approval required before running '{tool.name}'.",
                            tool_calls_made=tool_calls_made,
                            turns=turn,
                            usage=response.usage,
                            pending_approval_id=decision.approval_id,
                        )
                    if not decision.approved:
                        record["denied"] = True
                        record["result"] = decision.message or "denied by user"
                        tool_calls_made.append(record)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": decision.message or "tool call denied by user"})})
                        continue

                # Execute tool.
                try:
                    result = await tool.execute(params, ctx=tool_context)
                    output = result.output
                    if result.error:
                        record["error"] = result.error

                    followups = operator.on_tool_result(
                        tool_name,
                        result,
                        {
                            "params": params,
                            "messages": messages,
                            "operator_state": operator_state,
                            "tool_calls_made": tool_calls_made,
                        },
                    ) or []
                    system_messages: list[str] = []
                    pending_followups: list[OperatorAction] = []
                    for action in followups:
                        if action.kind == "override_result":
                            output = str(action.payload.get("output", output))
                        elif action.kind == "system_message":
                            content = str(action.payload.get("content", "")).strip()
                            if content:
                                system_messages.append(content)
                        elif action.kind == "tool_call":
                            pending_followups.append(action)

                    record["result"] = output
                    tool_calls_made.append(record)
                    content = output
                    if result.error:
                        content = json.dumps({"error": result.error, "output": output})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
                    for content in system_messages:
                        messages.append({"role": "system", "content": content})

                    for index, action in enumerate(pending_followups, start=1):
                        payload = action.payload
                        follow_tool = str(payload.get("tool_name", "")).strip()
                        follow_params = payload.get("params", {})
                        if not follow_tool or not isinstance(follow_params, dict):
                            continue
                        follow_call_id = f"{tc.id}-op-{index}"
                        messages.append(
                            {
                                "type": "function_call",
                                "call_id": follow_call_id,
                                "name": follow_tool,
                                "arguments": json.dumps(follow_params),
                            }
                        )
                        await _execute_operator_tool_call(
                            tool_name=follow_tool,
                            params=follow_params,
                            turn=turn,
                            call_id=follow_call_id,
                        )
                except Exception as e:
                    record["error"] = str(e)
                    record["result"] = None
                    tool_calls_made.append(record)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": str(e)})})
            continue  # next turn

        # Handle text-only response.
        if response.text:
            text = response.text.strip()
            pre_reply_plan, _ = operator.apply(
                None,
                None,
                {
                    "kind": "react_before_reply",
                    "messages": messages,
                    "reply_text": text,
                    "tool_calls_made": tool_calls_made,
                    "operator_state": operator_state,
                },
                lookup,
            )
            if isinstance(pre_reply_plan, dict):
                forced_reply = pre_reply_plan.get("forced_reply")
                if isinstance(forced_reply, str) and forced_reply.strip():
                    return ReactResult(
                        reply=forced_reply.strip(),
                        tool_calls_made=tool_calls_made,
                        turns=turn,
                        usage=response.usage,
                    )
                enforce_tool_calls = pre_reply_plan.get("enforce_tool_calls", [])
                if isinstance(enforce_tool_calls, list) and enforce_tool_calls:
                    for index, item in enumerate(enforce_tool_calls, start=1):
                        if not isinstance(item, dict):
                            continue
                        tool_name = str(item.get("tool_name", "")).strip()
                        params = item.get("params", {})
                        if not tool_name or not isinstance(params, dict):
                            continue
                        call_id = f"auto-enforce-{turn}-{index}"
                        messages.append({"type": "function_call", "call_id": call_id, "name": tool_name, "arguments": json.dumps(params)})
                        await _execute_operator_tool_call(tool_name=tool_name, params=params, turn=turn, call_id=call_id)
                    continue
            if stream_sink and not stream_sink.streamed:
                # Text wasn't streamed inline (e.g. non-streaming provider) — push now.
                await stream_sink.push(text)
            return ReactResult(reply=text, tool_calls_made=tool_calls_made, turns=turn, usage=response.usage)

        # No text and no tool calls — break.
        return ReactResult(reply="[No response from model.]", tool_calls_made=tool_calls_made, turns=turn)

    return ReactResult(reply="[Max turns reached — could not complete the request.]", tool_calls_made=tool_calls_made, turns=max_turns)
