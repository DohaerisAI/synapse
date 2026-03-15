from __future__ import annotations

import json

from synapse.operator import OperatorLayer
from synapse.tools.registry import ToolResult


class _FakeRegistry:
    def __init__(self, names: set[str]) -> None:
        self._names = names

    def get(self, name: str):
        return object() if name in self._names else None


def test_operator_repo_request_adds_status_and_diffstat() -> None:
    operator = OperatorLayer()
    draft, notes = operator.apply(
        None,
        None,
        {
            "kind": "react_start",
            "messages": [{"role": "user", "content": "show changes in this repo"}],
            "tool_calls_made": [],
            "pre_tool_calls": [],
            "operator_state": {},
        },
        _FakeRegistry({"repo_status", "repo_diffstat"}),
    )

    calls = draft["pre_tool_calls"]
    assert calls[0]["tool_name"] == "repo_status"
    assert calls[1]["tool_name"] == "repo_diffstat"
    assert any("repo status + diffstat" in note for note in notes)


def test_operator_substitutes_shell_exec_with_repo_tool() -> None:
    operator = OperatorLayer()
    draft, notes = operator.apply(
        None,
        None,
        {
            "kind": "react_pre_tool_call",
            "messages": [{"role": "user", "content": "show repo status"}],
            "tool_name": "shell_exec",
            "params": {"command": "git status -sb"},
            "operator_state": {},
        },
        _FakeRegistry({"repo_status", "shell_readonly"}),
    )

    assert draft["tool_name"] == "repo_status"
    assert "command" not in draft["params"]
    assert any("substituted shell_exec with repo_status" in note for note in notes)


def test_operator_swing_scan_fallback_and_combined_override() -> None:
    operator = OperatorLayer()
    state = {}

    strict_result = ToolResult(
        output=json.dumps(
            {
                "parsed": {
                    "mode": "trade_ready",
                    "setups": [{"symbol": "AAA"}, {"symbol": "BBB"}],
                    "setups_found": 2,
                }
            }
        )
    )
    first = operator.on_tool_result(
        "swing_scan",
        strict_result,
        {"params": {"mode": "trade_ready", "pattern": "all", "watchlist": "nifty50", "top": 10}, "operator_state": state},
    )
    assert first is not None
    tool_followups = [item for item in first if item.kind == "tool_call"]
    assert len(tool_followups) == 1
    assert tool_followups[0].payload["params"]["mode"] == "near_setups"

    relaxed_result = ToolResult(
        output=json.dumps(
            {
                "parsed": {
                    "mode": "near_setups",
                    "setups": [{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "CCC"}],
                    "setups_found": 3,
                }
            }
        )
    )
    second = operator.on_tool_result(
        "swing_scan",
        relaxed_result,
        {"params": {"mode": "near_setups", "pattern": "all", "watchlist": "nifty50", "top": 10}, "operator_state": state},
    )
    assert second is not None
    overrides = [item for item in second if item.kind == "override_result"]
    assert len(overrides) == 1
    combined = json.loads(overrides[0].payload["output"])
    assert combined["parsed"]["label"] == "combined_scan"


def test_operator_defaults_codex_propose_background_true() -> None:
    operator = OperatorLayer()
    draft, _ = operator.apply(
        None,
        None,
        {
            "kind": "react_pre_tool_call",
            "messages": [{"role": "user", "content": "propose patch for this bug"}],
            "tool_name": "codex_propose",
            "params": {"repo_path": ".", "task": "fix bug"},
            "operator_state": {},
        },
        _FakeRegistry({"codex_propose"}),
    )

    assert draft["params"]["background"] is True
