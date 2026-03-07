from __future__ import annotations

import hashlib
import re

SAFE_COMPONENT_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def safe_component(value: str, *, max_length: int = 48) -> str:
    cleaned = SAFE_COMPONENT_PATTERN.sub("-", value.strip()).strip("-._")
    if not cleaned:
        cleaned = "item"
    if len(cleaned) <= max_length:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[: max_length - 11]}-{digest}"


def derive_session_key(adapter: str, channel_id: str, user_id: str) -> str:
    base = "__".join(
        (
            safe_component(adapter, max_length=24),
            safe_component(channel_id, max_length=32),
            safe_component(user_id, max_length=32),
        )
    )
    if len(base) <= 100:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return f"{base[:87]}-{digest}"
