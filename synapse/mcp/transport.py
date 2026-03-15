"""MCP Streamable HTTP transport — JSON-RPC 2.0 over MCP HTTP protocol."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# MCP protocol constants
_MCP_ACCEPT = "application/json, text/event-stream"
_MCP_CONTENT_TYPE = "application/json"


class HttpMcpTransport:
    """Streamable HTTP transport for MCP servers.

    Implements the MCP Streamable HTTP transport spec:
    - POST JSON-RPC 2.0 messages to a single endpoint
    - Accept header: application/json, text/event-stream
    - Tracks Mcp-Session-Id from server for session continuity
    - Sends 'initialized' notification after successful init handshake
    """

    def __init__(self, url: str, *, auth_token: str = "", auth_type: str = "none", timeout: float = 30.0) -> None:
        self._url = url
        self._auth_token = auth_token
        self._auth_type = auth_type
        self._timeout = timeout
        self._request_id = 0
        self._session_id: str | None = None
        self._headers: dict[str, str] = {
            "Content-Type": _MCP_CONTENT_TYPE,
            "Accept": _MCP_ACCEPT,
        }
        if auth_type in ("oauth", "jwt") and auth_token:
            self._headers["Authorization"] = f"Bearer {auth_token}"
        elif auth_type == "api_key" and auth_token:
            self._headers["X-API-Key"] = auth_token
        self._client: httpx.AsyncClient | None = None
        self._loop_id: int | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create an httpx client, recreating if the event loop changed.

        Each asyncio.run() creates a new event loop and closes the old one.
        httpx clients hold connections tied to the loop they were created on.
        We track the loop ID so we recreate the client when the loop changes.
        """
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        loop_id = id(loop) if loop is not None else None
        if self._client is not None and not self._client.is_closed and loop_id == self._loop_id:
            return self._client
        # Drop stale client — its connections are tied to the old event loop
        # and cannot be cleanly closed from the new one. Let GC handle it.
        self._client = None
        self._client = httpx.AsyncClient(headers=dict(self._headers), timeout=self._timeout)
        self._loop_id = loop_id
        return self._client

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC 2.0 message to the MCP server.

        Notifications (methods like 'notifications/*' or 'initialized')
        are sent without an 'id' field and expect 202 Accepted.
        """
        is_notification = method.startswith("notifications/") or method == "initialized"

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if not is_notification:
            self._request_id += 1
            payload["id"] = self._request_id
        if params is not None:
            payload["params"] = params

        extra_headers: dict[str, str] = {}
        if self._session_id is not None:
            extra_headers["Mcp-Session-Id"] = self._session_id

        client = self._get_client()
        response = await client.post(self._url, json=payload, headers=extra_headers)
        response.raise_for_status()

        # Capture session ID from server
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        # Notifications get 202 with no body
        if is_notification or response.status_code == 202:
            return {}

        # Handle JSON response
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            # Parse SSE stream — extract last JSON-RPC result
            return self._parse_sse_response(response.text)

        body = response.json()
        if "error" in body:
            error = body["error"]
            msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise RuntimeError(f"MCP error: {msg}")

        return body.get("result", {})

    async def initialize(self) -> Any:
        """Perform the MCP initialization handshake.

        1. Send 'initialize' request with client capabilities
        2. Receive server capabilities + session ID
        3. Send 'initialized' notification to confirm
        """
        result = await self.send("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "synapse", "version": "0.1.0"},
        })
        # Send initialized notification to complete handshake
        await self.send("initialized")
        return result

    def _parse_sse_response(self, text: str) -> Any:
        """Parse SSE stream text and extract the last JSON-RPC result."""
        import json
        last_data: Any = {}
        for line in text.splitlines():
            if line.startswith("data: "):
                raw = line[6:].strip()
                if raw:
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            if "error" in parsed:
                                error = parsed["error"]
                                msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                                raise RuntimeError(f"MCP error: {msg}")
                            last_data = parsed.get("result", parsed)
                    except json.JSONDecodeError:
                        continue
        return last_data

    @property
    def session_id(self) -> str | None:
        """Current MCP session ID assigned by the server."""
        return self._session_id

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
