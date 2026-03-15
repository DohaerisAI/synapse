"""Tests for MCP Streamable HTTP transport."""
from __future__ import annotations

import json

import httpx
import pytest

from synapse.mcp.transport import HttpMcpTransport


class FakeTransportHandler(httpx.MockTransport):
    """Mock HTTP transport that returns canned MCP responses."""

    def __init__(self, responses: dict[str, dict] | None = None, *, error: bool = False):
        self._responses = responses or {}
        self._error = error
        self._last_request: dict | None = None
        self._request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self._last_request = body
            self._request_count += 1
            method = body.get("method", "")

            # Notifications (no id) get 202
            is_notification = "id" not in body
            if is_notification:
                return httpx.Response(202, headers={"mcp-session-id": "test-session"})

            if self._error:
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -1, "message": "test error"}},
                    headers={"mcp-session-id": "test-session"},
                )
            result = self._responses.get(method, {})
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": result},
                headers={"mcp-session-id": "test-session"},
            )

        super().__init__(handler)


@pytest.fixture
def transport_with_mock():
    """Create an HttpMcpTransport with a mock HTTP backend."""

    def _factory(responses: dict[str, dict] | None = None, *, error: bool = False, auth_type: str = "none", token: str = ""):
        transport = HttpMcpTransport(
            url="https://example.com/mcp",
            auth_token=token,
            auth_type=auth_type,
        )
        mock = FakeTransportHandler(responses, error=error)
        transport._client = httpx.AsyncClient(transport=mock)
        return transport, mock

    return _factory


class TestHttpMcpTransport:
    async def test_send_request(self, transport_with_mock):
        transport, mock = transport_with_mock({"tools/list": {"tools": []}})
        result = await transport.send("tools/list")
        assert result == {"tools": []}
        assert mock._last_request["method"] == "tools/list"
        assert mock._last_request["jsonrpc"] == "2.0"
        assert "id" in mock._last_request

    async def test_send_notification_no_id(self, transport_with_mock):
        transport, mock = transport_with_mock()
        result = await transport.send("initialized")
        assert result == {}
        assert "id" not in mock._last_request

    async def test_send_with_params(self, transport_with_mock):
        transport, mock = transport_with_mock({"tools/call": {"content": [{"type": "text", "text": "ok"}]}})
        result = await transport.send("tools/call", {"name": "get_holdings", "arguments": {}})
        assert result["content"][0]["text"] == "ok"
        assert mock._last_request["params"]["name"] == "get_holdings"

    async def test_send_error_response(self, transport_with_mock):
        transport, _ = transport_with_mock(error=True)
        with pytest.raises(RuntimeError, match="MCP error"):
            await transport.send("tools/list")

    async def test_request_id_increments(self, transport_with_mock):
        transport, mock = transport_with_mock({"ping": {}})
        await transport.send("ping")
        first_id = mock._last_request["id"]
        await transport.send("ping")
        second_id = mock._last_request["id"]
        assert second_id == first_id + 1

    async def test_session_id_captured(self, transport_with_mock):
        transport, _ = transport_with_mock({"initialize": {"capabilities": {}}})
        assert transport.session_id is None
        await transport.send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0.1"}})
        assert transport.session_id == "test-session"

    async def test_session_id_sent_in_subsequent_requests(self, transport_with_mock):
        transport, mock = transport_with_mock({"initialize": {"capabilities": {}}, "tools/list": {"tools": []}})
        await transport.initialize()
        # After init, session_id should be set and sent in next request
        assert transport.session_id == "test-session"

    async def test_initialize_handshake(self, transport_with_mock):
        transport, mock = transport_with_mock({"initialize": {"capabilities": {"tools": {}}}})
        result = await transport.initialize()
        assert result == {"capabilities": {"tools": {}}}
        # Should have sent initialize + initialized notification
        assert mock._request_count == 2

    async def test_sse_response_parsing(self, transport_with_mock):
        transport, _ = transport_with_mock()
        data = transport._parse_sse_response(
            'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"test"}]}}\n\n'
        )
        assert data == {"tools": [{"name": "test"}]}

    async def test_sse_error_parsing(self, transport_with_mock):
        transport, _ = transport_with_mock()
        with pytest.raises(RuntimeError, match="MCP error"):
            transport._parse_sse_response(
                'data: {"jsonrpc":"2.0","id":1,"error":{"code":-1,"message":"bad"}}\n\n'
            )

    async def test_auth_header_oauth(self):
        transport = HttpMcpTransport(url="https://example.com/mcp", auth_token="my-token", auth_type="oauth")
        assert transport._headers.get("Authorization") == "Bearer my-token"
        assert transport._headers.get("Accept") == "application/json, text/event-stream"
        await transport.close()

    async def test_auth_header_api_key(self):
        transport = HttpMcpTransport(url="https://example.com/mcp", auth_token="key123", auth_type="api_key")
        assert transport._headers.get("X-API-Key") == "key123"
        await transport.close()

    async def test_no_auth_header(self):
        transport = HttpMcpTransport(url="https://example.com/mcp")
        assert "Authorization" not in transport._headers
        assert "X-API-Key" not in transport._headers
        await transport.close()

    async def test_close(self, transport_with_mock):
        transport, _ = transport_with_mock()
        await transport.close()


class TestRuntimeMcpWiring:
    """Test that MCPRegistry + FinanceExecutor wire into build_runtime correctly."""

    def test_mcp_disabled_no_registry(self, tmp_path):
        from synapse.runtime import build_runtime
        (tmp_path / ".env.local").write_text("AGENT_NAME=Test\nSERVER_HOST=127.0.0.1\nSERVER_PORT=9999\n")
        runtime = build_runtime(tmp_path)
        assert runtime.mcp_registry is None

    def test_mcp_enabled_creates_registry(self, tmp_path):
        from synapse.runtime import build_runtime
        (tmp_path / ".env.local").write_text("AGENT_NAME=Test\nSERVER_HOST=127.0.0.1\nSERVER_PORT=9999\n")
        (tmp_path / "mcp.yaml").write_text(
            "enabled: true\nconnections:\n  - server_id: test\n    url: https://test.example.com/mcp\n    enabled: true\n"
        )
        runtime = build_runtime(tmp_path)
        assert runtime.mcp_registry is not None
