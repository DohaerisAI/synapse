"""Approval manager — persistent allowlist with interactive approval flow."""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .store import SQLiteStore
    from .tools.registry import ToolDef


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool = False
    pending: bool = False
    approval_id: str | None = None
    message: str | None = None


class ApprovalManager:
    """Manages tool approval with a persistent allowlist file.

    Supports exact match and glob patterns (e.g. ``gws_*``).
    """

    def __init__(self, allowlist_path: Path) -> None:
        self._path = allowlist_path
        self._allowlist: list[str] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._allowlist = list(data.get("always_allow", []))
            except (json.JSONDecodeError, TypeError):
                self._allowlist = []
        else:
            self._allowlist = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "always_allow": self._allowlist,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def is_allowed(self, tool_name: str) -> bool:
        """Check if a tool is in the allowlist (exact match or glob)."""
        for pattern in self._allowlist:
            if fnmatch.fnmatch(tool_name, pattern):
                return True
        return False

    def add_to_allowlist(self, pattern: str) -> None:
        """Add a pattern to the allowlist and persist."""
        if pattern not in self._allowlist:
            self._allowlist.append(pattern)
            self._save()

    async def check_and_approve(
        self,
        tool: "ToolDef",
        params: dict[str, Any],
        *,
        send_fn: Callable[..., Any] | None = None,
        receive_fn: Callable[..., Any] | None = None,
    ) -> bool:
        """Check allowlist, then interactively approve if needed.

        Returns True if approved, False if denied.
        """
        decision = await self.authorize_tool_call(
            tool,
            params,
            send_fn=send_fn,
            receive_fn=receive_fn,
        )
        return decision.approved

    async def authorize_tool_call(
        self,
        tool: "ToolDef",
        params: dict[str, Any],
        *,
        send_fn: Callable[..., Any] | None = None,
        receive_fn: Callable[..., Any] | None = None,
        store: "SQLiteStore | None" = None,
        run_id: str | None = None,
        session_key: str | None = None,
        event: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        turn: int | None = None,
        tool_calls_made: list[dict[str, Any]] | None = None,
    ) -> ApprovalDecision:
        """Resolve approval for a tool call.

        If interactive callbacks are present, approval is resolved inline.
        Otherwise, if run context is available, a pending approval record is created.
        """
        if self.is_allowed(tool.name):
            return ApprovalDecision(approved=True)

        if send_fn is not None and receive_fn is not None:
            prompt = (
                f"Tool '{tool.name}' requires approval.\n"
                f"Params: {json.dumps(params, default=str)}\n"
                "Approve? (yes/no/always)"
            )
            await send_fn(prompt)
            response = await receive_fn()
            lowered = str(response).strip().lower()
            if lowered in {"always", "yes always"}:
                self.add_to_allowlist(tool.name)
                return ApprovalDecision(approved=True)
            if lowered in {"yes", "y", "approve", "go ahead", "ok", "sure"}:
                return ApprovalDecision(approved=True)
            return ApprovalDecision(approved=False, message="tool call denied by user")

        if all(
            value is not None
            for value in (store, run_id, session_key, event, system_prompt, messages, tool_call_id)
        ):
            approval = store.create_approval(
                run_id,
                session_key,
                tool.name,
                {
                    "kind": "react_tool_call",
                    "tool_name": tool.name,
                    "params": params,
                    "event": event,
                    "system_prompt": system_prompt,
                    "messages": messages,
                    "tool_call_id": tool_call_id,
                    "turn": turn,
                    "tool_calls_made": list(tool_calls_made or []),
                },
            )
            return ApprovalDecision(
                pending=True,
                approval_id=approval.approval_id,
                message=f"Approval required before running '{tool.name}'.",
            )

        return ApprovalDecision(
            approved=False,
            message=f"Approval is required before I can run '{tool.name}'.",
        )
