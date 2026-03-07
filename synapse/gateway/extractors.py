from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, TYPE_CHECKING

from ..models import NormalizedInboundEvent

if TYPE_CHECKING:
    from .core import Gateway


class RequestExtractors:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    def extract_user_preference(self, text: str) -> str | None:
        lowered = text.lower().strip()
        markers = [
            ("i like to be called ", "User prefers to be called "),
            ("call me ", "User prefers to be called "),
            ("my name is ", "User says their name is "),
            ("i prefer ", "User prefers "),
        ]
        for marker, prefix in markers:
            if marker not in lowered:
                continue
            start = lowered.index(marker)
            original = text[start + len(marker) :].strip()
            candidate = original.rstrip(".!? ").strip()
            if candidate:
                return f"{prefix}{candidate}."
        return None

    def extract_reminder_request(self, event: NormalizedInboundEvent) -> dict[str, Any] | None:
        text = event.text.strip()
        lowered = text.lower()
        for prefix in ("/remind in ", "remind me in ", "message me in "):
            if not lowered.startswith(prefix):
                continue
            parsed = self._parse_reminder_remainder(text[len(prefix) :].strip())
            if parsed is None:
                return None
            delay_seconds, message = parsed
            due_at = event.occurred_at + timedelta(seconds=delay_seconds)
            return {
                "adapter": event.adapter,
                "channel_id": event.channel_id,
                "message": message,
                "due_at": due_at.isoformat(),
            }
        return None

    def _parse_reminder_remainder(self, remainder: str) -> tuple[int, str] | None:
        if not remainder:
            return None
        matches = list(
            re.finditer(r"(?P<value>\d+)\s*(?P<unit>seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|hr|h|days?|day|d)\b", remainder, flags=re.IGNORECASE)
        )
        if not matches or matches[0].start() != 0:
            return None
        total_seconds = 0
        cursor = 0
        for match in matches:
            if match.start() != cursor and remainder[cursor : match.start()].strip() not in {"", ",", "and"}:
                break
            value = int(match.group("value"))
            unit = match.group("unit").lower()
            total_seconds += value * self._unit_seconds(unit)
            cursor = match.end()
            trailing = remainder[cursor:]
            prefix = trailing[:5].lower().strip()
            if prefix.startswith("to"):
                break
        if total_seconds <= 0:
            return None
        message = remainder[cursor:].strip(" ,")
        if message.lower().startswith("to "):
            message = message[3:].strip()
        if not message:
            message = "This is your reminder."
        return total_seconds, message

    def _unit_seconds(self, unit: str) -> int:
        if unit.startswith("s"):
            return 1
        if unit.startswith("m"):
            return 60
        if unit.startswith("h"):
            return 3600
        if unit.startswith("d"):
            return 86400
        return 0

    def extract_integration_request(self, text: str) -> dict[str, Any] | None:
        lowered = text.lower().strip()
        for prefix in ("add integration ", "install integration ", "enable integration ", "install "):
            if lowered.startswith(prefix):
                request = text[len(prefix) :].strip()
                if not request:
                    return None
                integration_id = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-") or "integration"
                return {"request": request, "integration_id": integration_id}
        return None
