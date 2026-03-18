"""Slack Bot API adapter — Socket Mode (WebSocket) + HTTP Events API.

Dual-mode transport:
- Socket Mode (default when app_token set): persistent WS connection, no
  public URL needed.
- HTTP Events API: Slack sends signed POST requests to your webhook endpoint.

DM pairing: channel_type=="im" sets metadata["pairing_mode"] = True so the
gateway can treat this as a paired 1:1 conversation.

Slash commands: /command args normalized into a NormalizedInboundEvent with
text="/command args".
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from typing import Any, Callable

import httpx

from .models import NormalizedInboundEvent

logger = logging.getLogger(__name__)

_SLACK_API_BASE = "https://slack.com/api"
_SKIP_SUBTYPES = frozenset(
    {"bot_message", "message_changed", "message_deleted", "message_replied"}
)
_BOT_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_SIGNATURE_MAX_AGE_S = 300


class SlackAdapter:
    """Adapts Slack Bot/App events to NormalizedInboundEvent."""

    def __init__(
        self,
        bot_token: str | None = None,
        app_token: str | None = None,
        signing_secret: str | None = None,
        client: Any | None = None,
        *,
        socket_mode: bool = False,
        bot_user_id: str = "",
    ) -> None:
        self._bot_token = bot_token or None
        self._app_token = app_token or None
        self._signing_secret = signing_secret or None
        self._socket_mode = socket_mode
        self._bot_user_id = bot_user_id
        self._client = client  # injected httpx-like client for testing
        self._inbound_handler: Callable[[NormalizedInboundEvent], None] | None = None
        self._health_handler: Callable[..., None] | None = None
        self._stop_event = threading.Event()
        self._socket_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Handler wiring
    # ------------------------------------------------------------------

    def set_handlers(
        self,
        *,
        inbound_handler: Callable[[NormalizedInboundEvent], None],
        health_handler: Callable[..., None],
    ) -> None:
        self._inbound_handler = inbound_handler
        self._health_handler = health_handler

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start adapter. Socket Mode: launch WS thread. Always emit health."""
        self._stop_event.clear()
        self._emit_health(
            status="healthy" if self._bot_token else "not_configured",
            auth_required=self._bot_token is None,
            last_error=None,
        )
        if self._socket_mode and self._app_token and self._bot_token:
            self._socket_thread = threading.Thread(
                target=self._socket_loop,
                name="slack-socket-mode",
                daemon=True,
            )
            self._socket_thread.start()

    def stop(self) -> None:
        """Signal the socket loop to stop."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Signature verification (Events API security)
    # ------------------------------------------------------------------

    def verify_signature(self, timestamp: str, body: bytes, signature: str) -> bool:
        """Verify Slack request signature.

        Returns True when:
        - No signing secret configured (dev/test mode).
        - HMAC matches and timestamp is fresh (< 300 s).
        """
        if not self._signing_secret:
            return True
        try:
            age = abs(time.time() - float(timestamp))
        except (ValueError, TypeError):
            return False
        if age > _SIGNATURE_MAX_AGE_S:
            return False
        base = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
        expected_mac = hmac.new(
            self._signing_secret.encode(),
            base.encode(),
            hashlib.sha256,
        )
        expected = f"v0={expected_mac.hexdigest()}"
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # Event normalization
    # ------------------------------------------------------------------

    def normalize_event(self, payload: dict[str, Any]) -> NormalizedInboundEvent:
        """Normalize an HTTP Events API callback OR a Socket Mode envelope payload.

        Raises ValueError for events that should be silently ignored (bots,
        skip subtypes, empty text).
        """
        # HTTP: {"type": "event_callback", "event": {...}}
        # Socket Mode: envelope payload is the inner event dict directly
        if payload.get("type") == "event_callback":
            event = payload["event"]
        else:
            event = payload

        subtype = event.get("subtype")
        if subtype in _SKIP_SUBTYPES:
            raise ValueError(f"skipped subtype: {subtype}")

        if event.get("bot_id"):
            raise ValueError("bot message ignored")

        text: str = event.get("text") or ""
        event_type = event.get("type", "")

        # Strip @mention prefix for app_mention events
        if event_type == "app_mention":
            text = _BOT_MENTION_RE.sub("", text).strip()

        if not text.strip():
            raise ValueError("empty text ignored")

        channel: str = str(event.get("channel", ""))
        user: str = str(event.get("user", ""))
        ts: str = str(event.get("ts", ""))
        channel_type: str = event.get("channel_type", "")

        metadata: dict[str, Any] = {}
        if channel_type == "im":
            metadata["pairing_mode"] = True

        return NormalizedInboundEvent(
            adapter="slack",
            channel_id=channel,
            user_id=user,
            message_id=ts,
            text=text,
            metadata=metadata,
        )

    def normalize_slash_command(self, form: dict[str, str]) -> NormalizedInboundEvent:
        """Normalize a Slack slash command form POST into a NormalizedInboundEvent."""
        command = form.get("command", "")
        args = form.get("text", "").strip()
        text = f"{command} {args}".strip() if args else command
        channel_id = form.get("channel_id", "")
        user_id = form.get("user_id", "")
        trigger_id = form.get("trigger_id", "")
        return NormalizedInboundEvent(
            adapter="slack",
            channel_id=channel_id,
            user_id=user_id,
            message_id=trigger_id or f"slash-{int(time.time() * 1000)}",
            text=text,
            metadata={"slash_command": command},
        )

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    def send_text(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Post a message via chat.postMessage. Returns the API response dict."""
        if not self._bot_token:
            raise RuntimeError("Slack bot_token not configured")
        payload: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        return self._post_api("chat.postMessage", payload)

    def edit_text(self, channel_id: str, ts: str, text: str) -> dict[str, Any]:
        """Update an existing message via chat.update."""
        if not self._bot_token:
            raise RuntimeError("Slack bot_token not configured")
        return self._post_api("chat.update", {"channel": channel_id, "ts": ts, "text": text})

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def bot_token(self) -> str | None:
        return self._bot_token

    def status_snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable status dict for health endpoints."""
        if not self._bot_token:
            return {
                "status": "not_configured",
                "auth_required": True,
                "socket_mode": self._socket_mode,
            }
        return {
            "status": "configured",
            "auth_required": False,
            "socket_mode": self._socket_mode,
            "has_app_token": bool(self._app_token),
            "bot_user_id": self._bot_user_id,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_health(
        self,
        *,
        status: str,
        auth_required: bool,
        last_error: str | None,
    ) -> None:
        if self._health_handler is None:
            return
        try:
            self._health_handler(
                adapter="slack",
                status=status,
                auth_required=auth_required,
                last_error=last_error,
            )
        except Exception:
            logger.exception("slack _emit_health failed")

    def _post_api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to a Slack Web API method. Uses injected client or real httpx."""
        url = f"{_SLACK_API_BASE}/{method}"
        headers = {"Authorization": f"Bearer {self._bot_token}"}
        client = self._client
        if client is None:
            with httpx.Client() as http:
                resp = http.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        # Injected test client — pass headers via kwargs if it supports them
        try:
            resp = client.post(url, json=payload, headers=headers)
        except TypeError:
            # Minimal test client that only accepts url + json
            resp = client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Socket Mode internals
    # ------------------------------------------------------------------

    def _socket_loop(self) -> None:
        """Thread target: run asyncio event loop for Socket Mode."""
        asyncio.run(self._socket_run())

    async def _socket_run(self) -> None:
        """Main Socket Mode WS loop with reconnect."""
        try:
            import websockets  # type: ignore[import]
        except ImportError:
            logger.error("websockets package required for Slack Socket Mode: pip install websockets")
            self._emit_health(
                status="error",
                auth_required=False,
                last_error="websockets not installed",
            )
            return

        while not self._stop_event.is_set():
            try:
                url = await self._open_connection_url()
                logger.info("slack socket mode: connecting to %s", url[:60])
                async with websockets.connect(url) as ws:
                    async for raw_msg in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            envelope = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue
                        await self._process_envelope(ws, envelope)
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("slack socket mode error, reconnecting in 2s: %s", exc)
                await asyncio.sleep(2)

    async def _open_connection_url(self) -> str:
        """Call apps.connections.open to get a WSS URL."""
        url = f"{_SLACK_API_BASE}/apps.connections.open"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {self._app_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"apps.connections.open failed: {data.get('error')}")
        return data["url"]

    async def _process_envelope(self, ws: Any, envelope: dict[str, Any]) -> None:
        """ACK and dispatch a Socket Mode envelope."""
        envelope_id = envelope.get("envelope_id")
        if envelope_id:
            await ws.send(json.dumps({"envelope_id": envelope_id}))

        payload = envelope.get("payload") or envelope
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return

        try:
            event = self.normalize_event(payload)
        except ValueError:
            return

        if self._inbound_handler is not None:
            try:
                self._inbound_handler(event)
            except Exception:
                logger.exception("slack inbound_handler raised")
