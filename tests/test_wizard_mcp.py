"""Tests for MCP wizard step — RED phase."""
from __future__ import annotations

from pathlib import Path

import pytest

from synapse.wizard.prompter import MockPrompter


def test_step_mcp_skip(tmp_path: Path):
    """User declines MCP — no mcp.yaml created."""
    from synapse.wizard.steps import step_mcp
    prompter = MockPrompter([False])  # "Enable financial services?" → No
    env: dict[str, str] = {}
    result = step_mcp(prompter, env, tmp_path)
    assert result.get("_MCP_ENABLED") != "1"
    assert not (tmp_path / "mcp.yaml").exists()


def test_step_mcp_kite_only(tmp_path: Path):
    """User enables MCP with Kite only."""
    from synapse.wizard.steps import step_mcp
    answers = [
        True,           # Enable financial services? → Yes
        ["kite"],       # Which services? → Kite
        "test_token",   # Kite API key
    ]
    prompter = MockPrompter(answers)
    env: dict[str, str] = {}
    result = step_mcp(prompter, env, tmp_path)
    assert result["_MCP_ENABLED"] == "1"
    assert (tmp_path / "mcp.yaml").exists()
    yaml_text = (tmp_path / "mcp.yaml").read_text()
    assert "kite" in yaml_text
    assert "mcp.kite.trade" in yaml_text


def test_step_mcp_all_services(tmp_path: Path):
    """User enables all MCP services."""
    from synapse.wizard.steps import step_mcp
    answers = [
        True,                                    # Enable financial services?
        ["kite", "mfapi", "tradingview"],         # Which services?
        "kite_token",                            # Kite API key
        "rapidapi_key",                          # TradingView API key
    ]
    prompter = MockPrompter(answers)
    env: dict[str, str] = {}
    result = step_mcp(prompter, env, tmp_path)
    assert result["_MCP_ENABLED"] == "1"
    yaml_text = (tmp_path / "mcp.yaml").read_text()
    assert "kite" in yaml_text
    assert "mfapi" in yaml_text
    assert "tradingview" in yaml_text


def test_step_mcp_mfapi_only(tmp_path: Path):
    """User enables only MF API (no auth needed)."""
    from synapse.wizard.steps import step_mcp
    answers = [
        True,           # Enable financial services?
        ["mfapi"],      # Which services? → mfapi only
    ]
    prompter = MockPrompter(answers)
    env: dict[str, str] = {}
    result = step_mcp(prompter, env, tmp_path)
    assert result["_MCP_ENABLED"] == "1"
    yaml_text = (tmp_path / "mcp.yaml").read_text()
    assert "mfapi" in yaml_text
    assert "kite" not in yaml_text


def test_step_mcp_generates_valid_yaml(tmp_path: Path):
    """Generated mcp.yaml should be parseable by the config loader."""
    from synapse.wizard.steps import step_mcp
    from synapse.config.loader import _load_mcp_config
    answers = [
        True,
        ["kite", "mfapi"],
        "kite_token",
    ]
    prompter = MockPrompter(answers)
    step_mcp(prompter, {}, tmp_path)
    config = _load_mcp_config(tmp_path)
    assert config.enabled is True
    server_ids = {c.server_id for c in config.connections}
    assert "kite" in server_ids
    assert "mfapi" in server_ids


def test_wizard_advanced_flow_includes_mcp(tmp_path: Path):
    """The advanced onboarding flow should include the MCP step."""
    from synapse.wizard.onboarding import _advanced_flow
    # Create var dir so from_root doesn't fail
    (tmp_path / "var").mkdir(exist_ok=True)
    answers = [
        # step_agent (section 1)
        "Synapse",      # name
        "",             # extra instructions
        # step_provider (section 2)
        "codex",        # provider type
        "",             # auth file
        "gpt-5.4",      # model
        "responses",    # transport
        # step_telegram (section 3)
        False,          # Enable telegram? → No
        # step_gws (section 4)
        False,          # Enable GWS? → No
        # step_mcp (section 5)
        False,          # Enable financial services? → No
        # step_heartbeat (section 6)
        False,          # Enable heartbeat? → No
        # step_server (section 7)
        "127.0.0.1",    # host
        "8000",         # port
    ]
    prompter = MockPrompter(answers)
    env: dict[str, str] = {}
    result = _advanced_flow(prompter, env, tmp_path)
    assert "AGENT_NAME" in result
