"""MCP Stdio transport — JSON-RPC 2.0 over subprocess stdin/stdout.

Used for MCP servers that require a bridge process like `npx mcp-remote`.
The bridge handles OAuth, token refresh, and protocol translation while
Synapse communicates via newline-delimited JSON-RPC over stdio.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class StdioMcpTransport:
    """Stdio transport for MCP servers via subprocess bridge.

    Launches a command (e.g. ``npx mcp-remote https://mcp.upstox.com/mcp``)
    and communicates via stdin/stdout using newline-delimited JSON-RPC 2.0.
    """

    def __init__(
        self,
        command: list[str],
        *,
        url: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._command = command
        self._url = url
        self._timeout = timeout
        self._request_id = 0
        self._session_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None

    async def _start(self) -> None:
        """Launch the subprocess and start the reader loop."""
        if self._process is not None and self._process.returncode is None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Command not found: {self._command[0]}. Is it installed and on PATH?"
            )
        logger.info("stdio transport started: pid=%s cmd=%s", self._process.pid, self._command)
        self._reader_task = asyncio.create_task(self._read_loop())
        # Also drain stderr in background so it doesn't block
        asyncio.create_task(self._stderr_drain())

    async def _read_loop(self) -> None:
        """Read JSON-RPC responses from stdout and resolve pending futures."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break  # EOF — process exited
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("stdio: skipping non-JSON line: %s", text[:100])
                    continue
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    self._pending[msg_id].set_result(msg)
                elif msg_id is not None:
                    logger.debug("stdio: unmatched response id=%s", msg_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("stdio reader loop crashed")
        finally:
            # Fail any pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("stdio process exited"))
            self._pending.clear()

    async def _stderr_drain(self) -> None:
        """Log stderr output from the subprocess.

        Surfaces OAuth authorization URLs at INFO level so the user can
        click them when running headless (e.g. Synapse server mode).
        """
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                # Surface auth URLs so users can see them in logs
                if "authorize" in text.lower() or "http" in text.lower():
                    logger.info("stdio [%s]: %s", self._url, text)
                else:
                    logger.debug("stdio stderr: %s", text[:200])
        except (asyncio.CancelledError, Exception):
            pass

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC 2.0 message via stdin, wait for response on stdout."""
        if self._process is None or self._process.returncode is not None:
            await self._start()
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("stdio process not started")

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

        raw = json.dumps(payload, separators=(",", ":")) + "\n"
        proc.stdin.write(raw.encode("utf-8"))
        await proc.stdin.drain()

        if is_notification:
            return {}

        # Wait for the response
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[self._request_id] = future

        try:
            response = await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.TimeoutError:
            self._pending.pop(self._request_id, None)
            raise
        finally:
            self._pending.pop(self._request_id, None)

        if "error" in response:
            error = response["error"]
            msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise RuntimeError(f"MCP error: {msg}")

        return response.get("result", {})

    async def initialize(self) -> Any:
        """Perform the MCP initialization handshake."""
        await self._start()
        result = await self.send("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "synapse", "version": "0.1.0"},
        })
        await self.send("initialized")
        return result

    @property
    def session_id(self) -> str | None:
        """Stdio transport doesn't use session IDs."""
        return self._session_id

    async def close(self) -> None:
        """Terminate the subprocess and clean up."""
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

        proc = self._process
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            logger.info("stdio transport stopped: pid=%s", proc.pid)
        self._process = None
        self._pending.clear()
