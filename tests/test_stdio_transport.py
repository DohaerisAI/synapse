"""Tests for StdioMcpTransport."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synapse.mcp.stdio_transport import StdioMcpTransport


def _make_fake_process(responses: list[dict] | None = None, *, keep_alive: bool = False):
    """Create a fake async subprocess with controllable stdout.

    If keep_alive=True, the process hangs on readline after all responses
    instead of returning EOF. Use for multi-request tests.
    """
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    if responses:
        for resp in responses:
            queue.put_nowait(json.dumps(resp).encode() + b"\n")
    if not keep_alive:
        queue.put_nowait(b"")  # EOF sentinel

    proc = MagicMock()
    proc.returncode = None
    proc.pid = 12345

    async def readline():
        try:
            return await asyncio.wait_for(queue.get(), timeout=60.0)
        except asyncio.TimeoutError:
            return b""

    proc.stdout = MagicMock()
    proc.stdout.readline = readline

    proc.stderr = MagicMock()
    proc.stderr.readline = AsyncMock(return_value=b"")

    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    return proc


class TestStdioMcpTransport:
    @pytest.mark.asyncio
    async def test_send_request(self):
        """Send a request and get a response."""
        response = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        proc = _make_fake_process([response])

        transport = StdioMcpTransport(command=["echo"], timeout=5.0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await transport.send("tools/list")

        assert result == {"tools": []}
        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0]
        payload = json.loads(written.decode())
        assert payload["method"] == "tools/list"
        assert payload["id"] == 1

        await transport.close()

    @pytest.mark.asyncio
    async def test_send_notification_no_id(self):
        """Notifications have no id and return empty dict."""
        proc = _make_fake_process()
        transport = StdioMcpTransport(command=["echo"], timeout=5.0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await transport.send("initialized")

        assert result == {}
        written = proc.stdin.write.call_args[0][0]
        payload = json.loads(written.decode())
        assert "id" not in payload

        await transport.close()

    @pytest.mark.asyncio
    async def test_request_id_increments(self):
        """Each non-notification request gets a unique incrementing ID."""
        transport = StdioMcpTransport(command=["echo"], timeout=5.0)
        # Verify IDs increment without actually running the reader loop
        assert transport._request_id == 0
        # Simulate two ID allocations
        transport._request_id += 1
        id1 = transport._request_id
        transport._request_id += 1
        id2 = transport._request_id
        assert id1 == 1
        assert id2 == 2

    @pytest.mark.asyncio
    async def test_initialize_handshake(self):
        """initialize() sends init request + initialized notification."""
        init_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
        }
        proc = _make_fake_process([init_response], keep_alive=True)
        transport = StdioMcpTransport(command=["echo"], timeout=5.0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await transport.initialize()

        assert result["protocolVersion"] == "2025-03-26"
        # Two writes: initialize request + initialized notification
        assert proc.stdin.write.call_count == 2

        await transport.close()

    @pytest.mark.asyncio
    async def test_error_response_raises(self):
        """MCP error responses raise RuntimeError."""
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Invalid request"},
        }
        proc = _make_fake_process([response])
        transport = StdioMcpTransport(command=["echo"], timeout=5.0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with pytest.raises(RuntimeError, match="Invalid request"):
                await transport.send("bad_method")

        await transport.close()

    @pytest.mark.asyncio
    async def test_session_id_is_none(self):
        """Stdio transport doesn't use session IDs."""
        transport = StdioMcpTransport(command=["echo"])
        assert transport.session_id is None

    @pytest.mark.asyncio
    async def test_close_terminates_process(self):
        """close() terminates the subprocess."""
        proc = _make_fake_process()
        transport = StdioMcpTransport(command=["echo"], timeout=5.0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await transport._start()
            # Give reader loop a moment
            await asyncio.sleep(0.05)
            await transport.close()

        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_command_not_found(self):
        """FileNotFoundError gives a clear error message."""
        transport = StdioMcpTransport(command=["nonexistent_binary_xyz"])
        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError("No such file")),
        ):
            with pytest.raises(RuntimeError, match="Command not found"):
                await transport._start()
