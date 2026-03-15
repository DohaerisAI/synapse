"""Tests for MCP config loading."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_mcp_config_defaults():
    from synapse.config.schema import MCPConfig
    config = MCPConfig()
    assert config.enabled is False
    assert config.connections == []


def test_mcp_config_on_app_config():
    from synapse.config.schema import AppConfig
    app = AppConfig.from_root(Path("/tmp/test-synapse"))
    assert app.mcp.enabled is False


def test_mcp_connection_config():
    from synapse.config.schema import MCPConnectionConfig, MCPAuthConfig
    conn = MCPConnectionConfig(
        server_id="kite",
        url="https://mcp.kite.trade/mcp",
        auth=MCPAuthConfig(auth_type="oauth", token="tok_123"),
        rate_limit=30,
    )
    assert conn.server_id == "kite"
    assert conn.auth.auth_type == "oauth"
    assert conn.rate_limit == 30
    assert conn.enabled is True


def test_load_mcp_yaml(tmp_path: Path):
    from synapse.config.loader import _load_mcp_config
    yaml_content = """
enabled: true
connections:
  - server_id: kite
    url: https://mcp.kite.trade/mcp
    auth:
      auth_type: oauth
      token: test_token
    rate_limit: 30
  - server_id: mfapi
    url: https://xpack.ai/mcp/mfapi
    auth:
      auth_type: none
"""
    (tmp_path / "mcp.yaml").write_text(yaml_content)
    config = _load_mcp_config(tmp_path)
    assert config.enabled is True
    assert len(config.connections) == 2
    assert config.connections[0].server_id == "kite"
    assert config.connections[0].auth.auth_type == "oauth"
    assert config.connections[1].server_id == "mfapi"


def test_load_mcp_yaml_missing_file(tmp_path: Path):
    from synapse.config.loader import _load_mcp_config
    config = _load_mcp_config(tmp_path)
    assert config.enabled is False
    assert config.connections == []


def test_load_config_includes_mcp(tmp_path: Path):
    from synapse.config.loader import load_config
    yaml_content = """
enabled: true
connections:
  - server_id: kite
    url: https://mcp.kite.trade/mcp
    auth:
      auth_type: oauth
"""
    # load_config expects root dir — mcp.yaml goes in root
    (tmp_path / "mcp.yaml").write_text(yaml_content)
    (tmp_path / "var").mkdir(exist_ok=True)
    config = load_config(tmp_path, {})
    assert config.mcp.enabled is True
    assert len(config.mcp.connections) == 1
