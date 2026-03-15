from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PricingEntry:
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0


def normalize_pricing(raw: Any) -> dict[str, PricingEntry]:
    if not isinstance(raw, dict):
        return {}
    pricing: dict[str, PricingEntry] = {}
    for model, value in raw.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(value, dict):
            continue
        pricing[model.strip()] = PricingEntry(
            input_per_1m=_floatish(value.get("input_per_1m")),
            output_per_1m=_floatish(value.get("output_per_1m")),
        )
    return pricing


def parse_pricing_json(raw: str) -> dict[str, PricingEntry]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return normalize_pricing(payload)


def compute_cost(
    *,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    pricing: dict[str, PricingEntry],
) -> tuple[float, bool]:
    spec = pricing.get(model)
    if spec is None:
        return 0.0, True
    if prompt_tokens is None or completion_tokens is None:
        return 0.0, True
    cost = (
        (prompt_tokens / 1_000_000.0) * spec.input_per_1m
        + (completion_tokens / 1_000_000.0) * spec.output_per_1m
    )
    return round(cost, 8), False


def estimate_input_chars(
    messages: list[dict[str, Any]],
    *,
    system_prompt: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> int:
    payload: dict[str, Any] = {"messages": messages}
    if system_prompt is not None:
        payload["system_prompt"] = system_prompt
    if tools:
        payload["tools"] = tools
    return len(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def estimate_output_chars(text: str | None, tool_calls: list[Any] | None = None) -> int:
    payload: dict[str, Any] = {}
    if text:
        payload["text"] = text
    if tool_calls:
        payload["tool_calls"] = [
            {
                "id": getattr(call, "id", ""),
                "name": getattr(call, "name", ""),
                "arguments": getattr(call, "arguments", {}),
            }
            for call in tool_calls
        ]
    if not payload:
        return 0
    return len(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def format_cost(cost: float, *, unknown: bool) -> str:
    if unknown:
        return "unknown"
    return f"${cost:.4f}"


def render_telegram_usage_summary(summary: dict[str, Any]) -> str:
    totals = summary.get("totals", {})
    tool_rows = summary.get("top_tools", [])
    skill_rows = summary.get("top_skills", [])
    job_counts = summary.get("job_counts", {})
    tool_text = ", ".join(f"{row['tool_name']} {row['count']}" for row in tool_rows[:3]) or "none"
    skill_text = ", ".join(f"{row['skill_id']} {row['count']}" for row in skill_rows[:3]) or "none"
    jobs_text = ", ".join(f"{key} {value}" for key, value in sorted(job_counts.items()) if value) or "none"
    return "\n".join(
        [
            f"**Window:** {summary.get('window_hours', 24)}h",
            f"**Calls:** {totals.get('usage_event_count', 0)} model, {totals.get('tool_event_count', 0)} tools",
            (
                f"**Tokens:** in {totals.get('prompt_tokens', 0)} / "
                f"out {totals.get('completion_tokens', 0)} / total {totals.get('total_tokens', 0)}"
            ),
            f"**Chars:** in {totals.get('input_chars', 0)} / out {totals.get('output_chars', 0)}",
            f"**Cost:** {format_cost(float(totals.get('cost', 0.0) or 0.0), unknown=bool(totals.get('cost_unknown', False)))}",
            f"**Top tools:** {tool_text}",
            f"**Top skills:** {skill_text}",
            f"**Jobs:** {jobs_text}",
        ]
    )


def _floatish(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
