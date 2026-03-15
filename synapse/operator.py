"""Operator layer for tool defaults, grounding, substitutions, and fallbacks."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Protocol

from .models import WorkflowPlan
from .tools.registry import ToolResult


class _ToolRegistryLike(Protocol):
    def get(self, name: str) -> Any | None:
        ...


@dataclass(frozen=True, slots=True)
class OperatorAction:
    """Optional follow-up action requested by the operator."""

    kind: str
    payload: dict[str, Any]


class OperatorLayer:
    """Central operator layer for deterministic defaults and tool execution policy."""

    _REPO_CHANGE_RE = re.compile(r"\b(diff|changes?|changed|code review|review changes?|show changes?|what changed)\b", re.IGNORECASE)
    _LIVE_MARKET_RE = re.compile(
        r"\b(live|current|latest|now|price|rsi|support|resistance|analy[sz]e|analysis|technical)\b",
        re.IGNORECASE,
    )
    _SCAN_RE = re.compile(r"\b(scan|watchlist|index|nifty\s*50|nifty50|nifty\s*next\s*50)\b", re.IGNORECASE)
    _TOP_RE = re.compile(r"\btop\s+(\d{1,3})\b", re.IGNORECASE)
    _KITE_RE = re.compile(r"\b(kite|zerodha)\b", re.IGNORECASE)
    _READONLY_COMMAND_RE = re.compile(
        r"^\s*(pwd|ls|whoami|git\s+status|git\s+diff|rg\b|grep\b|cat\b|head\b|tail\b|sed\s+-n\b)",
        re.IGNORECASE,
    )
    _LONG_SHELL_RE = re.compile(
        r"\b(pytest|npm\s+test|pnpm\s+test|yarn\s+test|uv\s+run|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|make\s+test)\b",
        re.IGNORECASE,
    )
    _SYMBOL_STOPWORDS = {
        "live",
        "current",
        "latest",
        "now",
        "price",
        "rsi",
        "support",
        "resistance",
        "analyse",
        "analyze",
        "analysis",
        "technical",
        "chart",
        "setup",
        "swing",
        "check",
        "scan",
        "watchlist",
        "index",
        "top",
        "nifty",
        "dekh",
        "dekho",
        "bata",
        "batao",
        "please",
        "pls",
        "kar",
        "kr",
        "karde",
        "krde",
        "bhai",
        "bro",
    }

    def apply(self, run: Any, event: Any, draft_plan: Any, tool_registry: _ToolRegistryLike | None) -> tuple[Any, list[str]]:
        """Apply operator policy to a draft workflow/tool plan."""
        if isinstance(draft_plan, WorkflowPlan):
            return draft_plan, []
        if not isinstance(draft_plan, dict):
            return draft_plan, []

        kind = str(draft_plan.get("kind", "")).strip()
        if kind == "react_start":
            return self._apply_react_start(draft_plan, tool_registry)
        if kind == "react_pre_tool_call":
            return self._apply_react_pre_tool_call(draft_plan, tool_registry)
        if kind == "react_before_reply":
            return self._apply_react_before_reply(draft_plan, tool_registry)
        return draft_plan, []

    def on_tool_result(self, tool_name: str, result: ToolResult, context: dict[str, Any]) -> list[OperatorAction] | None:
        """Inspect completed tool result and optionally request follow-up actions."""
        actions: list[OperatorAction] = []
        state = context.setdefault("operator_state", {})
        params = context.get("params", {}) if isinstance(context.get("params", {}), dict) else {}

        if tool_name == "repo_diffstat":
            if self._diffstat_is_large(result.output):
                actions.append(
                    OperatorAction(
                        kind="system_message",
                        payload={
                            "content": (
                                "Diffstat shows many changes. Offer either `repo_diff` for full patch details "
                                "or a focused file list before deep summary."
                            )
                        },
                    )
                )

        if tool_name == "swing_scan":
            parsed = self._extract_json(result.output)
            parsed_payload = parsed.get("parsed") if isinstance(parsed.get("parsed"), dict) else {}
            mode = str(params.get("mode", "trade_ready")).strip().lower() or "trade_ready"
            setups_found = self._scan_count(parsed_payload)

            if mode == "trade_ready":
                state["swing_scan_strict"] = parsed_payload
                state["swing_scan_strict_raw"] = result.output
                if setups_found < 5:
                    relaxed_params = dict(params)
                    relaxed_params["mode"] = "near_setups"
                    actions.append(
                        OperatorAction(
                            kind="tool_call",
                            payload={
                                "tool_name": "swing_scan",
                                "params": relaxed_params,
                                "meta": {"operator_label": "relaxed_fallback"},
                            },
                        )
                    )
            elif mode == "near_setups" and isinstance(state.get("swing_scan_strict"), dict):
                strict_payload = state.get("swing_scan_strict", {})
                combined_payload = {
                    "label": "combined_scan",
                    "strict_mode": "trade_ready",
                    "strict": strict_payload,
                    "relaxed_mode": "near_setups",
                    "relaxed": parsed_payload,
                    "note": "Strict scan returned fewer than 5 setups, so relaxed near_setups fallback was added.",
                }
                actions.append(
                    OperatorAction(
                        kind="override_result",
                        payload={
                            "output": json.dumps({"parsed": combined_payload, "raw": result.output}),
                        },
                    )
                )

        return actions or None

    def _apply_react_start(
        self,
        draft_plan: dict[str, Any],
        tool_registry: _ToolRegistryLike | None,
    ) -> tuple[dict[str, Any], list[str]]:
        notes: list[str] = []
        out = dict(draft_plan)
        messages = out.get("messages", [])
        tool_calls_made = out.get("tool_calls_made", [])
        pre_tool_calls = list(out.get("pre_tool_calls", []))

        text = self._latest_user_text(messages)
        if not text:
            out["pre_tool_calls"] = pre_tool_calls
            return out, notes

        if self._is_repo_change_request(text):
            if not self._has_repo_inspection(tool_calls_made):
                if self._has_tool(tool_registry, "repo_status"):
                    pre_tool_calls.append({"tool_name": "repo_status", "params": {}})
                if self._has_tool(tool_registry, "repo_diffstat"):
                    pre_tool_calls.append({"tool_name": "repo_diffstat", "params": {}})
                if pre_tool_calls:
                    notes.append("Operator: preflight repo status + diffstat added before change summary.")

        if self._is_scan_request(text) and self._has_tool(tool_registry, "swing_scan"):
            top = self._extract_top_n(text)
            params: dict[str, Any] = {
                "pattern": self._extract_scan_pattern(text),
                "watchlist": self._extract_watchlist(text),
                "mode": "trade_ready",
            }
            if top is not None:
                params["top"] = top
            pre_tool_calls.append({"tool_name": "swing_scan", "params": params})
            notes.append("Operator: strict swing scan added (fallback to near_setups if needed).")
        elif self._is_live_market_request(text) and self._has_tool(tool_registry, "swing_analyze"):
            symbol = self._extract_symbol(text)
            if symbol:
                pre_tool_calls.append({"tool_name": "swing_analyze", "params": {"symbol": symbol, "timeframe": "daily"}})
                notes.append("Operator: live market request grounded with swing_analyze.")

        out["pre_tool_calls"] = pre_tool_calls
        return out, notes

    def _apply_react_pre_tool_call(
        self,
        draft_plan: dict[str, Any],
        tool_registry: _ToolRegistryLike | None,
    ) -> tuple[dict[str, Any], list[str]]:
        notes: list[str] = []
        out = dict(draft_plan)
        tool_name = str(out.get("tool_name", "")).strip()
        params = dict(out.get("params", {}) or {})
        user_text = self._latest_user_text(out.get("messages", []))
        explicit_foreground = self._explicit_foreground_requested(user_text)
        explicit_kite = self._explicit_kite_requested(user_text)

        if self._is_kite_tool(tool_name) and not explicit_kite:
            out["blocked"] = "kite/zerodha tools require explicit user request"
            notes.append("Operator: blocked kite/zerodha tool without explicit user request.")
            return out, notes

        if tool_name == "shell_exec":
            mapped = self._map_shell_exec(tool_registry=tool_registry, params=params)
            if mapped is not None:
                tool_name, params, note = mapped
                if note:
                    notes.append(note)
            if not explicit_foreground and self._is_long_shell_operation(str(params.get("command", ""))):
                params.setdefault("background", True)
                notes.append("Operator: defaulted long shell operation to background=true.")

        if tool_name in {"codex_propose", "codex_run_tests"} and not explicit_foreground:
            params.setdefault("background", True)
            notes.append(f"Operator: defaulted {tool_name} to background=true.")

        if tool_name == "swing_scan":
            params.setdefault("mode", "trade_ready")

        out["tool_name"] = tool_name
        out["params"] = params
        return out, notes

    def _apply_react_before_reply(
        self,
        draft_plan: dict[str, Any],
        tool_registry: _ToolRegistryLike | None,
    ) -> tuple[dict[str, Any], list[str]]:
        notes: list[str] = []
        out = dict(draft_plan)
        state = out.setdefault("operator_state", {})
        messages = out.get("messages", [])
        tool_calls_made = out.get("tool_calls_made", [])
        text = self._latest_user_text(messages)
        if not text or not self._is_live_market_request(text):
            return out, notes

        if self._has_market_data_call(tool_calls_made):
            return out, notes
        if bool(state.get("market_data_enforced")):
            return out, notes

        state["market_data_enforced"] = True
        if not self._has_tool(tool_registry, "swing_analyze"):
            return out, notes
        symbol = self._extract_symbol(text)
        if symbol is None:
            out["forced_reply"] = "Which symbol should I analyze live right now?"
            notes.append("Operator: asked for symbol before returning live analysis.")
            return out, notes

        out["enforce_tool_calls"] = [{"tool_name": "swing_analyze", "params": {"symbol": symbol, "timeframe": "daily"}}]
        notes.append("Operator: enforced swing_analyze before final live-analysis reply.")
        return out, notes

    def _map_shell_exec(
        self,
        *,
        tool_registry: _ToolRegistryLike | None,
        params: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None] | None:
        command = str(params.get("command", "")).strip()
        cwd = params.get("cwd")

        parsed_git = self._parse_simple_git_command(command, cwd if isinstance(cwd, str) else None)
        if parsed_git is not None:
            target_tool, target_params = parsed_git
            if self._has_tool(tool_registry, target_tool):
                return target_tool, target_params, f"Operator: substituted shell_exec with {target_tool}."

        cat_path = self._parse_simple_cat(command)
        if cat_path is not None and self._has_tool(tool_registry, "fs_read"):
            target_params = {"path": cat_path}
            if isinstance(cwd, str) and cwd.strip():
                target_params["cwd"] = cwd
            return "fs_read", target_params, "Operator: substituted shell_exec with fs_read for file read."

        if self._READONLY_COMMAND_RE.search(command) and self._has_tool(tool_registry, "shell_readonly"):
            return "shell_readonly", params, "Operator: substituted shell_exec with shell_readonly."
        return None

    def _parse_simple_git_command(self, command: str, cwd: str | None) -> tuple[str, dict[str, Any]] | None:
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        if not parts or parts[0] != "git":
            return None

        work_cwd = cwd
        idx = 1
        if len(parts) >= 3 and parts[1] == "-C":
            work_cwd = parts[2]
            idx = 3
        tail = parts[idx:]
        if tail == ["status", "-sb"]:
            return "repo_status", {"cwd": work_cwd} if work_cwd else {}
        if len(tail) >= 2 and tail[0] == "diff" and "--stat" in tail[1:]:
            return "repo_diffstat", {"cwd": work_cwd} if work_cwd else {}
        if tail and tail[0] == "diff":
            return "repo_diff", {"cwd": work_cwd} if work_cwd else {}
        return None

    def _parse_simple_cat(self, command: str) -> str | None:
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        if len(parts) == 2 and parts[0] == "cat":
            return parts[1]
        return None

    def _is_long_shell_operation(self, command: str) -> bool:
        return bool(self._LONG_SHELL_RE.search(command) or "&&" in command or len(command) > 120)

    def _is_repo_change_request(self, text: str) -> bool:
        return bool(self._REPO_CHANGE_RE.search(text))

    def _is_live_market_request(self, text: str) -> bool:
        return bool(self._LIVE_MARKET_RE.search(text))

    def _is_scan_request(self, text: str) -> bool:
        return bool(self._SCAN_RE.search(text) and self._TOP_RE.search(text))

    def _extract_top_n(self, text: str) -> int | None:
        match = self._TOP_RE.search(text)
        if match is None:
            return None
        try:
            value = int(match.group(1))
        except ValueError:
            return None
        return value if value > 0 else None

    def _extract_watchlist(self, text: str) -> str:
        lowered = text.lower()
        if "nifty next 50" in lowered or "niftynext50" in lowered:
            return "nifty_next50"
        if "nifty 500" in lowered or "nifty500" in lowered:
            return "nifty500"
        if "fno" in lowered:
            return "fno_stocks"
        return "nifty50"

    def _extract_scan_pattern(self, text: str) -> str:
        lowered = text.lower()
        if "pullback" in lowered:
            return "pullback"
        if "breakout" in lowered:
            return "breakout"
        if "trendline" in lowered:
            return "trendline"
        return "all"

    def _extract_symbol(self, text: str) -> str | None:
        tokens = [token for token in re.split(r"[^A-Za-z0-9]+", text) if token]
        kept = [token for token in tokens if token.lower() not in self._SYMBOL_STOPWORDS]
        if not kept:
            return None
        symbol = re.sub(r"[^A-Za-z0-9]", "", "".join(kept)).upper()
        if len(symbol) < 3:
            return None
        return symbol

    def _latest_user_text(self, messages: Any) -> str:
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if str(message.get("role", "")).strip() != "user":
                continue
            content = str(message.get("content", "")).strip()
            if content:
                return content
        return ""

    def _has_repo_inspection(self, tool_calls_made: Any) -> bool:
        seen = {str(item.get("tool", "")) for item in tool_calls_made if isinstance(item, dict)}
        return "repo_status" in seen and "repo_diffstat" in seen

    def _has_market_data_call(self, tool_calls_made: Any) -> bool:
        seen = {str(item.get("tool", "")) for item in tool_calls_made if isinstance(item, dict)}
        return any(name in seen for name in ("swing_analyze", "swing_scan"))

    def _explicit_kite_requested(self, text: str) -> bool:
        return bool(self._KITE_RE.search(text or ""))

    def _is_kite_tool(self, tool_name: str) -> bool:
        return bool(self._KITE_RE.search(tool_name.replace("_", ".")))

    def _explicit_foreground_requested(self, text: str) -> bool:
        lowered = (text or "").lower()
        return "run now foreground" in lowered or "foreground" in lowered

    def _has_tool(self, tool_registry: _ToolRegistryLike | None, tool_name: str) -> bool:
        return bool(tool_registry is not None and tool_registry.get(tool_name) is not None)

    def _extract_json(self, text: str) -> dict[str, Any]:
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _scan_count(self, payload: dict[str, Any]) -> int:
        if not isinstance(payload, dict):
            return 0
        setups = payload.get("setups")
        if isinstance(setups, list):
            return len(setups)
        count = payload.get("setups_found")
        if isinstance(count, int):
            return count
        return 0

    def _diffstat_is_large(self, text: str) -> bool:
        lines = [line for line in text.splitlines() if line.strip()]
        file_lines = [line for line in lines if "|" in line]
        if len(file_lines) >= 15:
            return True
        summary = next((line for line in reversed(lines) if "files changed" in line), "")
        match = re.search(r"(\d+)\s+files changed", summary)
        if match is not None:
            try:
                return int(match.group(1)) >= 15
            except ValueError:
                return False
        return False
