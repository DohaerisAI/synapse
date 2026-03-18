"""Tests for SlackAdapter — written FIRST per TDD (RED phase).

Covers: event normalization, slash commands, send/edit, signature
verification, status snapshot, socket-mode start, and channel plugin.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from synapse.slack_adapter import SlackAdapter
from synapse.channels.slack import SlackPlugin
from synapse.models import NormalizedInboundEvent


# ---------------------------------------------------------------------------
# Helpers — DummyResponse / DummyClient (same pattern as Telegram tests)
# ---------------------------------------------------------------------------


class DummyResponse:
    def __init__(self, payload: dict[str, Any] | None = None, *, status_code: int = 200) -> None:
        self._payload = payload or {"ok": True}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class DummyClient:
    def __init__(self, response: DummyResponse | None = None) -> None:
        self._response = response or DummyResponse()
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any] | None = None, **kwargs: Any) -> DummyResponse:
        self.calls.append({"url": url, "json": json, "kwargs": kwargs})
        return self._response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_message_event(
    *,
    text: str = "hello world",
    channel: str = "C123",
    user: str = "U456",
    ts: str = "1234567890.123456",
    channel_type: str = "channel",
    subtype: str | None = None,
    bot_id: str | None = None,
) -> dict[str, Any]:
    """Build a raw HTTP Events API event_callback payload."""
    event: dict[str, Any] = {
        "type": "message",
        "text": text,
        "channel": channel,
        "user": user,
        "ts": ts,
        "channel_type": channel_type,
    }
    if subtype is not None:
        event["subtype"] = subtype
    if bot_id is not None:
        event["bot_id"] = bot_id
    return {
        "type": "event_callback",
        "event": event,
    }


def _make_mention_event(
    *,
    text: str = "<@U123> hello bot",
    channel: str = "C123",
    user: str = "U456",
    ts: str = "1234567890.123456",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "app_mention",
        "text": text,
        "channel": channel,
        "user": user,
        "ts": ts,
    }
    return {"type": "event_callback", "event": event}


# ---------------------------------------------------------------------------
# normalize_event tests
# ---------------------------------------------------------------------------


def test_normalize_event_message() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    payload = _make_message_event(text="hello world", channel="C123", user="U456", ts="100.1")

    result = adapter.normalize_event(payload)

    assert isinstance(result, NormalizedInboundEvent)
    assert result.adapter == "slack"
    assert result.channel_id == "C123"
    assert result.user_id == "U456"
    assert result.text == "hello world"
    assert result.message_id == "100.1"


def test_normalize_event_app_mention_strips_bot_id() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test", bot_user_id="U123")
    payload = _make_mention_event(text="<@U123> what is 2+2?", channel="C999", user="U777")

    result = adapter.normalize_event(payload)

    assert result.text == "what is 2+2?"
    assert result.channel_id == "C999"
    assert result.user_id == "U777"


def test_normalize_event_dm_sets_pairing_mode() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    payload = _make_message_event(text="hi", channel_type="im", channel="D111", user="U222")

    result = adapter.normalize_event(payload)

    assert result.metadata.get("pairing_mode") is True


def test_normalize_event_channel_does_not_set_pairing_mode() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    payload = _make_message_event(text="hi", channel_type="channel")

    result = adapter.normalize_event(payload)

    assert result.metadata.get("pairing_mode") is not True


def test_normalize_event_rejects_bot_message() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    payload = _make_message_event(text="I am a bot", bot_id="B999")

    with pytest.raises(ValueError, match="bot"):
        adapter.normalize_event(payload)


@pytest.mark.parametrize(
    "subtype",
    ["bot_message", "message_changed", "message_deleted", "message_replied"],
)
def test_normalize_event_rejects_skip_subtypes(subtype: str) -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    payload = _make_message_event(text="some text", subtype=subtype)

    with pytest.raises(ValueError):
        adapter.normalize_event(payload)


def test_normalize_event_rejects_empty_text() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    payload = _make_message_event(text="   ")

    with pytest.raises(ValueError, match="empty"):
        adapter.normalize_event(payload)


# ---------------------------------------------------------------------------
# normalize_slash_command tests
# ---------------------------------------------------------------------------


def test_normalize_slash_command() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    form = {
        "command": "/deploy",
        "text": "prod v1.2.3",
        "channel_id": "C100",
        "user_id": "U200",
        "trigger_id": "trig.123",
    }

    result = adapter.normalize_slash_command(form)

    assert result.adapter == "slack"
    assert result.text == "/deploy prod v1.2.3"
    assert result.channel_id == "C100"
    assert result.user_id == "U200"
    assert result.metadata.get("slash_command") == "/deploy"


def test_normalize_slash_command_no_args() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    form = {
        "command": "/status",
        "text": "",
        "channel_id": "C100",
        "user_id": "U200",
    }

    result = adapter.normalize_slash_command(form)

    assert result.text == "/status"


# ---------------------------------------------------------------------------
# send_text / edit_text tests
# ---------------------------------------------------------------------------


def test_send_text_posts_to_chat_post_message() -> None:
    client = DummyClient(DummyResponse({"ok": True, "ts": "111.222"}))
    adapter = SlackAdapter(bot_token="xoxb-test", client=client)

    response = adapter.send_text("C100", "Hello, Slack!")

    assert len(client.calls) == 1
    call = client.calls[0]
    assert "chat.postMessage" in call["url"]
    assert call["json"]["channel"] == "C100"
    assert call["json"]["text"] == "Hello, Slack!"
    assert response == {"ok": True, "ts": "111.222"}


def test_send_text_with_thread_ts() -> None:
    client = DummyClient(DummyResponse({"ok": True, "ts": "111.222"}))
    adapter = SlackAdapter(bot_token="xoxb-test", client=client)

    adapter.send_text("C100", "reply", thread_ts="111.000")

    assert client.calls[0]["json"]["thread_ts"] == "111.000"


def test_send_text_no_token_raises() -> None:
    adapter = SlackAdapter()

    with pytest.raises(RuntimeError, match="token"):
        adapter.send_text("C100", "oops")


def test_edit_text_calls_chat_update() -> None:
    client = DummyClient(DummyResponse({"ok": True, "ts": "111.222"}))
    adapter = SlackAdapter(bot_token="xoxb-test", client=client)

    adapter.edit_text("C100", "111.222", "Updated text")

    assert len(client.calls) == 1
    call = client.calls[0]
    assert "chat.update" in call["url"]
    assert call["json"]["channel"] == "C100"
    assert call["json"]["ts"] == "111.222"
    assert call["json"]["text"] == "Updated text"


# ---------------------------------------------------------------------------
# verify_signature tests
# ---------------------------------------------------------------------------


def _make_signature(secret: str, timestamp: str, body: bytes) -> str:
    base = f"v0:{timestamp}:{body.decode()}"
    mac = hmac.new(secret.encode(), base.encode(), hashlib.sha256)
    return f"v0={mac.hexdigest()}"


def test_verify_signature_valid() -> None:
    secret = "mysecret"
    body = b"channel_id=C100&text=hello"
    timestamp = str(int(time.time()))
    sig = _make_signature(secret, timestamp, body)

    adapter = SlackAdapter(signing_secret=secret)

    assert adapter.verify_signature(timestamp, body, sig) is True


def test_verify_signature_wrong_sig() -> None:
    adapter = SlackAdapter(signing_secret="mysecret")
    body = b"channel_id=C100&text=hello"
    timestamp = str(int(time.time()))

    assert adapter.verify_signature(timestamp, body, "v0=wrongsig") is False


def test_verify_signature_replay() -> None:
    secret = "mysecret"
    body = b"channel_id=C100&text=hello"
    old_timestamp = str(int(time.time()) - 400)  # > 300s ago
    sig = _make_signature(secret, old_timestamp, body)

    adapter = SlackAdapter(signing_secret=secret)

    assert adapter.verify_signature(old_timestamp, body, sig) is False


def test_verify_signature_no_secret() -> None:
    adapter = SlackAdapter()
    body = b"whatever"
    timestamp = str(int(time.time()))

    # No secret configured — always passes through (dev mode)
    assert adapter.verify_signature(timestamp, body, "v0=anything") is True


# ---------------------------------------------------------------------------
# status_snapshot tests
# ---------------------------------------------------------------------------


def test_status_snapshot_no_token() -> None:
    adapter = SlackAdapter()

    snapshot = adapter.status_snapshot()

    assert snapshot["status"] == "not_configured"
    assert snapshot.get("auth_required") is True


def test_status_snapshot_socket_mode() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test", app_token="xapp-test", socket_mode=True)

    snapshot = adapter.status_snapshot()

    assert snapshot["status"] == "configured"
    assert snapshot.get("socket_mode") is True


def test_status_snapshot_http_mode() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")

    snapshot = adapter.status_snapshot()

    assert snapshot["status"] == "configured"
    assert snapshot.get("socket_mode") is False


# ---------------------------------------------------------------------------
# start emits health
# ---------------------------------------------------------------------------


def test_start_emits_health() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    health_calls: list[dict[str, Any]] = []

    def health_handler(**kwargs: Any) -> None:
        health_calls.append(kwargs)

    adapter.set_handlers(
        inbound_handler=lambda event: None,
        health_handler=health_handler,
    )
    adapter.start()
    adapter.stop()

    assert len(health_calls) >= 1
    assert health_calls[0]["adapter"] == "slack"


# ---------------------------------------------------------------------------
# Channel plugin tests
# ---------------------------------------------------------------------------


def test_slack_plugin_creates_channel_plugin() -> None:
    from synapse.channels.slack.plugin import SlackHealth, SlackMessaging

    adapter = SlackAdapter(bot_token="xoxb-test")
    plugin = SlackPlugin.create(adapter)

    assert plugin.id == "slack"
    assert plugin.meta.name == "Slack"
    assert isinstance(plugin.messaging, SlackMessaging)
    assert isinstance(plugin.health, SlackHealth)


def test_slack_plugin_messaging_normalizes() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    plugin = SlackPlugin.create(adapter)

    payload = _make_message_event(text="test", channel="C1", user="U1", ts="1.0")
    result = plugin.messaging.normalize(payload)

    assert result.text == "test"
    assert result.adapter == "slack"


def test_slack_health_check_configured() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    plugin = SlackPlugin.create(adapter)

    health = plugin.health.health_check()

    assert health.status == "configured"


def test_slack_health_check_not_configured() -> None:
    adapter = SlackAdapter()
    plugin = SlackPlugin.create(adapter)

    health = plugin.health.health_check()

    assert health.status == "not_configured"


# ---------------------------------------------------------------------------
# _emit_health with no handler (no-op)
# ---------------------------------------------------------------------------


def test_emit_health_no_handler_noop() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test")
    # No handler set — should not raise
    adapter._emit_health(status="healthy", auth_required=False, last_error=None)


# ---------------------------------------------------------------------------
# _post_api with injected client (headers via kwargs)
# ---------------------------------------------------------------------------


def test_post_api_uses_authorization_header() -> None:
    captured: list[dict[str, Any]] = []

    class HeaderCapturingClient:
        def post(self, url: str, *, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None, **kwargs: Any) -> DummyResponse:
            captured.append({"url": url, "json": json, "headers": headers})
            return DummyResponse({"ok": True})

    adapter = SlackAdapter(bot_token="xoxb-test", client=HeaderCapturingClient())
    adapter.send_text("C1", "hello")

    assert captured[0]["headers"]["Authorization"] == "Bearer xoxb-test"


# ---------------------------------------------------------------------------
# normalize_event from raw Socket Mode payload (no event_callback wrapper)
# ---------------------------------------------------------------------------


def test_normalize_event_raw_socket_mode_payload() -> None:
    """Socket Mode envelopes deliver the inner event directly without wrapper."""
    adapter = SlackAdapter(bot_token="xoxb-test")
    raw_event = {
        "type": "message",
        "text": "direct payload",
        "channel": "C555",
        "user": "U888",
        "ts": "999.1",
        "channel_type": "channel",
    }

    result = adapter.normalize_event(raw_event)

    assert result.text == "direct payload"
    assert result.channel_id == "C555"


# ---------------------------------------------------------------------------
# _process_envelope tests
# ---------------------------------------------------------------------------


async def test_process_envelope_acks_and_dispatches() -> None:
    received_events: list[Any] = []
    sent_messages: list[str] = []

    class FakeWs:
        async def send(self, msg: str) -> None:
            sent_messages.append(msg)

    adapter = SlackAdapter(bot_token="xoxb-test")
    adapter.set_handlers(
        inbound_handler=lambda e: received_events.append(e),
        health_handler=lambda **kw: None,
    )
    envelope = {
        "envelope_id": "env-1",
        "payload": {
            "type": "event_callback",
            "event": {
                "type": "message",
                "text": "hello from socket",
                "channel": "C111",
                "user": "U222",
                "ts": "1.0",
                "channel_type": "channel",
            },
        },
    }

    await adapter._process_envelope(FakeWs(), envelope)

    assert len(sent_messages) == 1
    import json as _json
    ack = _json.loads(sent_messages[0])
    assert ack["envelope_id"] == "env-1"
    assert len(received_events) == 1
    assert received_events[0].text == "hello from socket"


async def test_process_envelope_skips_bot_message() -> None:
    received_events: list[Any] = []

    class FakeWs:
        async def send(self, msg: str) -> None:
            pass

    adapter = SlackAdapter(bot_token="xoxb-test")
    adapter.set_handlers(
        inbound_handler=lambda e: received_events.append(e),
        health_handler=lambda **kw: None,
    )
    envelope = {
        "envelope_id": "env-2",
        "payload": {
            "type": "event_callback",
            "event": {
                "type": "message",
                "text": "I am a bot",
                "channel": "C111",
                "user": "U222",
                "ts": "1.0",
                "channel_type": "channel",
                "bot_id": "B123",
            },
        },
    }

    await adapter._process_envelope(FakeWs(), envelope)

    assert len(received_events) == 0


async def test_process_envelope_with_string_payload() -> None:
    """Payload may arrive as a JSON string in some Socket Mode variants."""
    import json as _json
    received_events: list[Any] = []

    class FakeWs:
        async def send(self, msg: str) -> None:
            pass

    adapter = SlackAdapter(bot_token="xoxb-test")
    adapter.set_handlers(
        inbound_handler=lambda e: received_events.append(e),
        health_handler=lambda **kw: None,
    )
    inner = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "text": "from string payload",
            "channel": "C222",
            "user": "U333",
            "ts": "2.0",
            "channel_type": "channel",
        },
    }
    envelope = {"envelope_id": "env-3", "payload": _json.dumps(inner)}

    await adapter._process_envelope(FakeWs(), envelope)

    assert len(received_events) == 1
    assert received_events[0].text == "from string payload"


async def test_process_envelope_no_envelope_id_does_not_raise() -> None:
    """Envelopes without envelope_id (e.g., disconnect) should be handled gracefully."""

    class FakeWs:
        async def send(self, msg: str) -> None:
            pass

    adapter = SlackAdapter(bot_token="xoxb-test")
    adapter.set_handlers(
        inbound_handler=lambda e: None,
        health_handler=lambda **kw: None,
    )
    # Envelope with no event data and no envelope_id
    envelope: dict[str, Any] = {"type": "disconnect"}

    # Should not raise — normalize_event will raise ValueError (no text), caught internally
    await adapter._process_envelope(FakeWs(), envelope)


# ---------------------------------------------------------------------------
# socket mode start skips thread when socket_mode=False
# ---------------------------------------------------------------------------


def test_start_no_socket_thread_when_disabled() -> None:
    adapter = SlackAdapter(bot_token="xoxb-test", socket_mode=False)
    adapter.set_handlers(
        inbound_handler=lambda e: None,
        health_handler=lambda **kw: None,
    )
    adapter.start()
    adapter.stop()

    assert adapter._socket_thread is None


# ---------------------------------------------------------------------------
# plugin send delegates to adapter.send_text
# ---------------------------------------------------------------------------


def test_slack_plugin_send_delegates_to_adapter() -> None:
    client = DummyClient(DummyResponse({"ok": True, "ts": "10.1"}))
    adapter = SlackAdapter(bot_token="xoxb-test", client=client)
    plugin = SlackPlugin.create(adapter)

    plugin.messaging.send("C999", "hello from plugin")

    assert len(client.calls) == 1
    assert client.calls[0]["json"]["text"] == "hello from plugin"


# ---------------------------------------------------------------------------
# _socket_run exits gracefully when websockets is missing
# ---------------------------------------------------------------------------


async def test_socket_run_exits_if_websockets_missing() -> None:
    """When websockets package is not installed, _socket_run should emit error health and return."""
    import sys
    import importlib

    adapter = SlackAdapter(bot_token="xoxb-test", app_token="xapp-test", socket_mode=True)
    health_calls: list[dict[str, Any]] = []
    adapter.set_handlers(
        inbound_handler=lambda e: None,
        health_handler=lambda **kw: health_calls.append(kw),
    )

    # Temporarily hide the websockets module
    saved = sys.modules.pop("websockets", None)
    sys.modules["websockets"] = None  # type: ignore[assignment]
    try:
        await adapter._socket_run()
    finally:
        if saved is not None:
            sys.modules["websockets"] = saved
        else:
            sys.modules.pop("websockets", None)

    assert any(c.get("status") == "error" for c in health_calls)


# ---------------------------------------------------------------------------
# Inbound handler exception is caught in _process_envelope
# ---------------------------------------------------------------------------


async def test_process_envelope_handler_exception_caught() -> None:
    """If the inbound handler raises, _process_envelope should not re-raise."""

    class FakeWs:
        async def send(self, msg: str) -> None:
            pass

    def bad_handler(event: Any) -> None:
        raise RuntimeError("handler blew up")

    adapter = SlackAdapter(bot_token="xoxb-test")
    adapter.set_handlers(
        inbound_handler=bad_handler,
        health_handler=lambda **kw: None,
    )
    envelope = {
        "envelope_id": "env-x",
        "payload": {
            "type": "event_callback",
            "event": {
                "type": "message",
                "text": "trigger handler",
                "channel": "C1",
                "user": "U1",
                "ts": "1.0",
                "channel_type": "channel",
            },
        },
    }

    # Should not raise
    await adapter._process_envelope(FakeWs(), envelope)


async def test_process_envelope_invalid_json_string_payload_skipped() -> None:
    """If payload is an invalid JSON string, the envelope should be silently skipped."""

    class FakeWs:
        async def send(self, msg: str) -> None:
            pass

    received: list[Any] = []
    adapter = SlackAdapter(bot_token="xoxb-test")
    adapter.set_handlers(
        inbound_handler=lambda e: received.append(e),
        health_handler=lambda **kw: None,
    )
    envelope = {
        "envelope_id": "env-bad",
        "payload": "{not valid json!!!",  # Invalid JSON string
    }

    await adapter._process_envelope(FakeWs(), envelope)

    assert len(received) == 0


# ---------------------------------------------------------------------------
# socket mode start creates thread when tokens present
# ---------------------------------------------------------------------------


def test_start_creates_socket_thread_when_socket_mode_enabled() -> None:
    """With socket_mode=True, bot_token, and app_token, start() should launch a thread."""
    import threading

    adapter = SlackAdapter(
        bot_token="xoxb-test",
        app_token="xapp-test",
        socket_mode=True,
    )
    adapter.set_handlers(
        inbound_handler=lambda e: None,
        health_handler=lambda **kw: None,
    )
    adapter.start()
    # Immediately signal stop so the thread terminates
    adapter.stop()

    assert adapter._socket_thread is not None
    assert isinstance(adapter._socket_thread, threading.Thread)
    # Allow thread to finish (it will fail to connect, but shouldn't matter)
    adapter._socket_thread.join(timeout=3)


# ---------------------------------------------------------------------------
# edit_text no-token raises RuntimeError
# ---------------------------------------------------------------------------


def test_edit_text_no_token_raises() -> None:
    adapter = SlackAdapter()

    with pytest.raises(RuntimeError, match="token"):
        adapter.edit_text("C100", "111.222", "updated text")


# ---------------------------------------------------------------------------
# verify_signature with invalid timestamp raises False
# ---------------------------------------------------------------------------


def test_verify_signature_invalid_timestamp() -> None:
    adapter = SlackAdapter(signing_secret="mysecret")
    body = b"payload=data"

    assert adapter.verify_signature("not-a-number", body, "v0=whatever") is False
