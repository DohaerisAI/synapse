"""ReAct loop — single LLM loop with native tool calling."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, TYPE_CHECKING

from .tools.registry import ToolDef, ToolResult

if TYPE_CHECKING:
    from .approvals import ApprovalManager
    from .streaming.sink import StreamSink

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReactResult:
    """Immutable result of a ReAct loop execution."""

    reply: str
    tool_calls_made: list[dict[str, Any]]
    turns: int
    usage: dict[str, Any] | None = None


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
) -> ReactResult:
    """Run a ReAct agent loop.

    The LLM is called with native tool_use. When it returns tool calls,
    each tool is executed and results fed back. When it returns text only,
    the loop terminates with that text as the reply.
    """
    tool_map = {t.name: t for t in tools}
    # Also map sanitized names (dots→underscores) for Responses API compatibility
    tool_map.update({t.name.replace(".", "_"): t for t in tools if "." in t.name})
    tool_schemas = [t.to_llm_schema() for t in tools]
    tool_calls_made: list[dict[str, Any]] = []

    for turn in range(1, max_turns + 1):
        response = await model_router.chat(
            messages,
            system=system_prompt,
            tools=tool_schemas if tool_schemas else None,
            stream_sink=stream_sink,
        )

        if response is None:
            return ReactResult(
                reply="[No model available.]",
                tool_calls_made=tool_calls_made,
                turns=turn,
            )

        # Handle tool calls
        if response.tool_calls:
            # Append the model's function call items so the next turn sees them
            for tc in response.tool_calls:
                messages.append({
                    "type": "function_call",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else str(tc.arguments),
                })
            for tc in response.tool_calls:
                tool = tool_map.get(tc.name)
                record: dict[str, Any] = {
                    "tool": tc.name,
                    "params": tc.arguments,
                    "turn": turn,
                }

                if tool is None:
                    record["error"] = f"tool not found: {tc.name}"
                    record["result"] = None
                    tool_calls_made.append(record)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"error": f"tool not found: {tc.name}"}),
                    })
                    continue

                # Check approval
                if tool.check_approval(tc.arguments) and approval_manager is not None:
                    approved = await approval_manager.check_and_approve(
                        tool, tc.arguments,
                    )
                    if not approved:
                        record["denied"] = True
                        record["result"] = "denied by user"
                        tool_calls_made.append(record)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({"error": "tool call denied by user"}),
                        })
                        continue

                # Execute tool
                try:
                    result = await tool.execute(tc.arguments, ctx=tool_context)
                    record["result"] = result.output
                    if result.error:
                        record["error"] = result.error
                    tool_calls_made.append(record)
                    content = result.output
                    if result.error:
                        content = json.dumps({"error": result.error, "output": result.output})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    })
                except Exception as e:
                    record["error"] = str(e)
                    record["result"] = None
                    tool_calls_made.append(record)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"error": str(e)}),
                    })
            continue  # next turn

        # Handle text-only response
        if response.text:
            text = response.text.strip()
            if stream_sink and not stream_sink.streamed:
                # Text wasn't streamed inline (e.g. non-streaming provider) — push now
                await stream_sink.push(text)
            return ReactResult(
                reply=text,
                tool_calls_made=tool_calls_made,
                turns=turn,
                usage=response.usage,
            )

        # No text and no tool calls — break
        return ReactResult(
            reply="[No response from model.]",
            tool_calls_made=tool_calls_made,
            turns=turn,
        )

    return ReactResult(
        reply="[Max turns reached — could not complete the request.]",
        tool_calls_made=tool_calls_made,
        turns=max_turns,
    )
