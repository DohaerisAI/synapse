"""Approval manager — persistent allowlist with interactive approval flow."""
from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .tools.registry import ToolDef


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
        if self.is_allowed(tool.name):
            return True

        if send_fn is None or receive_fn is None:
            # No interactive channel — auto-approve with warning
            import logging
            logging.getLogger(__name__).warning(
                "No interactive channel for approval of '%s' — auto-approving", tool.name,
            )
            return True

        # Send approval prompt
        prompt = f"Tool '{tool.name}' requires approval.\nParams: {json.dumps(params, default=str)}\nApprove? (yes/no/always)"
        await send_fn(prompt)

        # Wait for response
        response = await receive_fn()
        lowered = str(response).strip().lower()

        if lowered in {"always", "yes always"}:
            self.add_to_allowlist(tool.name)
            return True
        if lowered in {"yes", "y", "approve", "go ahead", "ok", "sure"}:
            return True
        return False
