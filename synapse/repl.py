"""Interactive REPL for Synapse — Claude Code-style terminal chat with streaming."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .models import GatewayResult, NormalizedInboundEvent
from .runtime import Runtime

# ANSI escape codes
_DIM = "\033[2m"
_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_MAGENTA = "\033[35m"


class TerminalStreamSink:
    """Stream sink that prints deltas to stdout in real-time."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._started = False

    async def push(self, delta: str) -> None:
        if not self._started:
            print()
            self._started = True
        self._parts.append(delta)
        print(delta, end="", flush=True)

    async def finalize(self) -> None:
        if self._started:
            print("\n")

    @property
    def accumulated_text(self) -> str:
        return "".join(self._parts)

    @property
    def streamed(self) -> bool:
        return self._started


def run_repl(runtime: Runtime) -> None:
    """Run an interactive chat REPL against the gateway."""
    # Suppress all synapse.* logs during REPL — we show our own trace
    logging.getLogger("synapse").setLevel(logging.WARNING)

    runtime.initialize()

    # Connect MCP servers
    print(f"{_DIM}connecting...{_RESET}", end="", flush=True)
    if runtime.mcp_registry and runtime.config.mcp.enabled:
        asyncio.run(runtime._async_connect_mcp_servers())
    print(f"\r\033[K", end="")  # clear "connecting..." line

    mcp_servers = runtime.mcp_registry.list_connected() if runtime.mcp_registry else []

    print(f"{_BOLD}⚡ Synapse REPL{_RESET}")
    print(f"{_DIM}   {runtime.gateway.agent_name}", end="")
    if mcp_servers:
        names = ", ".join(s.server_id for s in mcp_servers)
        print(f" • MCP: {names}", end="")
    print(f"{_RESET}")
    print(f"{_DIM}   /quit · /mcp · /events <run_id>{_RESET}\n")

    adapter = "repl"
    channel_id = "terminal"
    user_id = "local"
    msg_counter = 0

    while True:
        try:
            text = input(f"{_CYAN}❯{_RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not text:
            continue
        if text in {"/quit", "/exit", "/q"}:
            print("Bye.")
            break
        if text == "/mcp":
            _show_mcp(runtime)
            continue
        if text.startswith("/events"):
            _show_events(runtime, text)
            continue

        msg_counter += 1
        event = NormalizedInboundEvent(
            adapter=adapter,
            channel_id=channel_id,
            user_id=user_id,
            message_id=f"repl-{msg_counter}",
            text=text,
            occurred_at=datetime.now(timezone.utc),
        )

        stream = TerminalStreamSink()
        t0 = time.monotonic()
        try:
            result = asyncio.run(runtime.gateway.ingest(event, stream_sink=stream))
            elapsed = time.monotonic() - t0
            if not stream.streamed:
                reply = (result.reply_text or "").strip()
                if reply:
                    print(f"\n{reply}\n")
                else:
                    print(f"\n{_DIM}[{result.status}]{_RESET}\n")
            _print_trace(runtime, result, elapsed)
        except Exception as e:
            elapsed = time.monotonic() - t0
            print(f"\n{_RED}Error: {e}{_RESET}\n")
            print(f"  {_DIM}{elapsed:.1f}s{_RESET}\n")


def _print_trace(runtime: Runtime, result: GatewayResult, elapsed: float) -> None:
    """Print a collapsed one-line trace of what path the gateway took."""
    events = runtime.store.list_run_events(result.run_id)
    steps: list[str] = []
    for ev in events:
        etype = ev.get("event_type", "")
        payload = ev.get("payload", {})
        if isinstance(payload, str):
            import json
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}

        if etype == "workflow.planned":
            intent = payload.get("intent", "?")
            step_count = payload.get("step_count", 0)
            if step_count:
                steps.append(f"plan({intent}, {step_count} steps)")
            else:
                steps.append(f"plan({intent})")
        elif etype == "workflow.step.completed":
            action = payload.get("action", "?")
            success = payload.get("success", True)
            marker = f"{_GREEN}✓{_RESET}" if success else f"{_RED}✗{_RESET}"
            steps.append(f"{marker} {action}")
        elif etype == "model.streaming.completed":
            steps.append("stream")
        elif etype == "workflow.paused_for_approval":
            steps.append(f"{_YELLOW}⏸ approval{_RESET}")
        elif etype == "workflow.paused_for_input":
            kind = payload.get("kind", "?")
            steps.append(f"{_YELLOW}⏸ input({kind}){_RESET}")
        elif etype == "react.tool_call":
            tool = payload.get("tool", "?")
            error = payload.get("error")
            marker = f"{_RED}✗{_RESET}" if error else f"{_GREEN}✓{_RESET}"
            steps.append(f"{marker} {tool}")

    status = result.status
    status_color = _GREEN if status == "COMPLETED" else _YELLOW if "WAITING" in status else _DIM

    time_str = f"{elapsed:.1f}s" if elapsed >= 1.0 else f"{elapsed * 1000:.0f}ms"

    trail = " → ".join(steps) if steps else status
    print(f"  {_DIM}{trail} • {time_str}{_RESET}")
    print()


def _show_mcp(runtime: Runtime) -> None:
    """Display connected MCP servers and their tools."""
    if not runtime.mcp_registry:
        print(f"  {_DIM}MCP not enabled.{_RESET}\n")
        return
    servers = runtime.mcp_registry.list_connected()
    if not servers:
        print(f"  {_DIM}No MCP servers connected.{_RESET}\n")
        return
    for info in servers:
        print(f"  {_GREEN}●{_RESET} {info.server_id} — {info.tool_count} tools {_DIM}({info.url}){_RESET}")
    print()


def _show_events(runtime: Runtime, text: str) -> None:
    """Show full event log for a run ID."""
    import json
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        # Show last run
        runs = runtime.store.list_runs(limit=1)
        if not runs:
            print(f"  {_DIM}No runs yet.{_RESET}\n")
            return
        run_id = runs[0].run_id
    else:
        run_id = parts[1].strip()

    events = runtime.store.list_run_events(run_id)
    if not events:
        print(f"  {_DIM}No events for run {run_id[:12]}...{_RESET}\n")
        return

    print(f"  {_DIM}Run: {run_id[:12]}...{_RESET}")
    for ev in events:
        etype = ev.get("event_type", "?")
        payload = ev.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                pass
        ts = ev.get("created_at", "")
        summary = _event_summary(etype, payload)
        print(f"  {_DIM}{ts[11:19]}{_RESET}  {etype}  {_DIM}{summary}{_RESET}")
    print()


def _event_summary(etype: str, payload: Any) -> str:
    """One-line summary of an event payload."""
    if not isinstance(payload, dict):
        return ""
    if etype == "workflow.planned":
        return f"intent={payload.get('intent', '?')} steps={payload.get('step_count', 0)}"
    if etype == "workflow.step.completed":
        action = payload.get("action", "?")
        ok = "✓" if payload.get("success") else "✗"
        return f"{ok} {action}"
    if etype == "state.context_built":
        return f"provider={payload.get('provider', '?')}"
    if etype == "run.response":
        reply = str(payload.get("reply_text", ""))
        return reply[:60] + "..." if len(reply) > 60 else reply
    if etype == "react.tool_call":
        tool = payload.get("tool", "?")
        error = payload.get("error")
        return f"{'✗' if error else '✓'} {tool}"
    return ""
