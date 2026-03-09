from __future__ import annotations

import json
import logging
import re
from typing import Any, TYPE_CHECKING

from ..models import NormalizedInboundEvent, RunRecord, WorkflowPlan

if TYPE_CHECKING:
    from .core import Gateway

logger = logging.getLogger(__name__)

# Noisy keys to strip from GWS output before sending to the LLM
_GWS_STRIP_KEYS = frozenset({
    "body_html", "raw", "labelIds", "label_ids",
    "historyId", "internalDate", "threadId", "thread_id",
    "sizeEstimate", "payload",
})

# Max chars of GWS output to include in LLM context
_GWS_CONTEXT_LIMIT = 3000

# User-facing fields to show in clean GWS output
_GWS_HEADER_KEYS = ("from", "to", "subject", "date", "summary", "title", "name", "status")
# Body fields to clean up and show
_GWS_BODY_KEYS = ("body_text", "body_preview", "snippet")

# Invisible unicode chars
_INVISIBLE_RE = re.compile(r'[\u200c\u200b\u200d\u00ad\ufeff]')

# Tracking/unsubscribe URL patterns to strip
_TRACKING_URL_RE = re.compile(
    r'https?://(?:links\.e\d+\.|click\.|tracking\.|email\.|e\d+\.)[^\s<>]*',
    re.IGNORECASE,
)


def _clean_body(text: str) -> str:
    """Strip tracking URLs, invisible chars, but keep real content URLs."""
    text = _INVISIBLE_RE.sub('', text)
    text = _TRACKING_URL_RE.sub('', text)
    # Remove angle-bracket wrapped URLs that are tracking/unsubscribe links
    text = re.sub(r'<(https?://[^>]+)>', lambda m: m.group(1) if not _is_junk_url(m.group(1)) else '', text)
    # Remove email footer junk (unsubscribe, address blocks)
    text = re.sub(r'(?:To opt out|To customize|Unsubscribe from|©\s*\d{4}).*', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Clean up formatting
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'Read Full Story\s*\n?', '', text)
    text = re.sub(r'Read Story\s*\n?', '', text)
    text = re.sub(r'<>\s*', '', text)  # empty angle brackets left after URL removal
    return text.strip()


def _is_junk_url(url: str) -> bool:
    """Return True for tracking, unsubscribe, and settings URLs to strip."""
    lower = url.lower()
    junk_patterns = (
        'links.e1.', 'links.e2.', 'click.', 'tracking.',
        '/unsubscribe', '/manage-teams', '/settings',
        '/account/settings', 'email.', '/manage',
    )
    return any(p in lower for p in junk_patterns)


def _render_gws_clean(output: Any) -> str | None:
    """Render GWS output cleanly for the user — no LLM needed.

    Returns None if the output format is unrecognized.
    """
    if isinstance(output, dict):
        lines: list[str] = []
        # Header fields
        for key in _GWS_HEADER_KEYS:
            value = str(output.get(key, "")).strip()
            if value:
                lines.append(f"{key}: {value}")
        # Body content — cleaned
        for key in _GWS_BODY_KEYS:
            body = str(output.get(key, "")).strip()
            if body:
                cleaned = _clean_body(body)
                if cleaned:
                    lines.extend(["", cleaned])
                break  # only use first available body
        if lines:
            return "\n".join(lines)
        # No known fields — fallback to non-noisy key dump
        for key, value in output.items():
            if key not in _GWS_STRIP_KEYS:
                val_str = str(value).strip()
                if val_str and len(val_str) < 500:
                    lines.append(f"{key}: {val_str}")
        return "\n".join(lines) if lines else None
    if isinstance(output, list):
        if not output:
            return "No results found."
        items: list[str] = []
        for item in output[:10]:
            if isinstance(item, dict):
                label = str(
                    item.get("subject", "")
                    or item.get("summary", "")
                    or item.get("name", "")
                    or item.get("title", "")
                ).strip()
                items.append(f"- {label}" if label else f"- {item.get('id', 'item')}")
            else:
                items.append(f"- {item}")
        header = f"{len(output)} result(s):" if len(output) <= 10 else f"{len(output)} result(s) (showing first 10):"
        return "\n".join([header] + items)
    return None


def _clean_gws_output(output: Any) -> str:
    """Produce a compact, LLM-friendly representation of GWS output."""
    if isinstance(output, dict):
        cleaned = {k: v for k, v in output.items() if k not in _GWS_STRIP_KEYS}
        return json.dumps(cleaned, indent=2, default=str)[:_GWS_CONTEXT_LIMIT]
    if isinstance(output, list):
        items = output[:15]
        cleaned = [
            {k: v for k, v in item.items() if k not in _GWS_STRIP_KEYS}
            if isinstance(item, dict) else item
            for item in items
        ]
        result = json.dumps(cleaned, indent=2, default=str)[:_GWS_CONTEXT_LIMIT]
        if len(output) > 15:
            result += f"\n... ({len(output)} total items, showing first 15)"
        return result
    return str(output)[:_GWS_CONTEXT_LIMIT]


class ReplyRenderer:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    async def build_reply(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        execution_results: list[dict[str, Any]],
        workflow: WorkflowPlan,
    ) -> str:
        gw = self._gw

        # Errors → tell user exactly what failed (for self-improvement)
        failed = [r for r in execution_results if not r["success"]]
        if failed:
            return self._error_reply(failed)

        # Heartbeat — no LLM call needed
        if gw.context_builder.is_heartbeat(event):
            return "HEARTBEAT_OK"

        # GWS auth status — diagnostic, deterministic
        if execution_results and execution_results[-1]["action"] == "gws.auth.status":
            output = execution_results[-1].get("artifacts", {}).get("output", {})
            if isinstance(output, dict):
                lines = [
                    "Google Workspace status:",
                    f"- auth method: {output.get('auth_method', 'none')}",
                    f"- storage: {output.get('storage', 'none')}",
                    f"- credential source: {output.get('credential_source', 'none')}",
                    f"- encrypted credentials: {'yes' if output.get('encrypted_credentials_exists') else 'no'}",
                    f"- client config: {'yes' if output.get('client_config_exists') else 'no'}",
                ]
                return "\n".join(lines)

        # GWS confirmations (no content to show)
        if execution_results:
            last_action = execution_results[-1]["action"]
            if last_action == "gws.gmail.send":
                return "Gmail sent."
            if last_action == "integration.apply":
                return self._render_integration(execution_results[-1], applied=True)
            if last_action == "integration.test":
                return self._render_integration(execution_results[-1], applied=False)

        # GWS content results → clean render, no LLM needed
        gws_results = [r for r in execution_results if r["action"].startswith("gws.") and r["success"]]
        if gws_results:
            output = gws_results[-1].get("artifacts", {}).get("output")
            if output is not None:
                rendered = _render_gws_clean(output)
                if rendered:
                    return rendered

        # Everything else → LLM summarization
        system_prompt = gw.context_builder.system_prompt(run.session_key, run.user_id, event)
        attachment_summary = gw.context_builder.attachment_summary(event)
        attachments = gw.context_builder.attachment_list(event)

        user_message: dict[str, Any] = {"role": "user", "content": event.text}
        if attachments:
            user_message["attachments"] = attachments
        messages = [user_message]
        if attachment_summary:
            messages.append({"role": "system", "content": attachment_summary})
        if execution_results:
            context = self._build_execution_context(execution_results)
            messages.append({"role": "system", "content": context})

        try:
            generated = await gw.model_router.generate(messages, system_prompt=system_prompt)
        except Exception as error:
            logger.exception("model.generate failed in build_reply")
            return f"Failed: model.generate — {error}"

        if generated:
            return generated.strip()

        # Model returned empty — report what we have
        if execution_results:
            parts = [f"{r['action']}: {r.get('detail', 'ok')}" for r in execution_results]
            return "Completed: " + " | ".join(parts)

        if attachment_summary:
            return f"I received the upload. {attachment_summary.removeprefix('Inbound attachments: ')}"

        return f"Recorded message for session {event.adapter}/{event.channel_id}: {event.text}"

    def _build_execution_context(self, execution_results: list[dict[str, Any]]) -> str:
        """Build a clean context string for the LLM from execution results."""
        parts: list[str] = []
        for result in execution_results:
            action = result["action"]
            detail = result.get("detail", "")
            artifacts = result.get("artifacts", {})

            output = artifacts.get("output")
            if output is not None:
                cleaned = _clean_gws_output(output)
                parts.append(f"[{action}] {cleaned}")
            elif artifacts:
                # Include all artifacts (memory snapshots, search answers, etc.)
                cleaned = _clean_gws_output(artifacts)
                parts.append(f"[{action}] {cleaned}")
            elif detail:
                parts.append(f"[{action}] {detail}")
            else:
                parts.append(f"[{action}] completed successfully")

        header = (
            "The following actions were executed. "
            "Summarize the results naturally for the user. "
            "Be concise and conversational. "
            "Do NOT include raw IDs, tracking URLs, or HTML markup."
        )
        return header + "\n\n" + "\n\n".join(parts)

    def _error_reply(self, failed: list[dict[str, Any]]) -> str:
        """Build a clear error message so the system can learn from failures."""
        if len(failed) == 1:
            return f"Failed: {failed[0]['action']} — {failed[0].get('detail', 'unknown error')}"
        lines = ["Multiple steps failed:"]
        for r in failed:
            lines.append(f"- {r['action']}: {r.get('detail', 'unknown')}")
        return "\n".join(lines)

    def _render_integration(self, result: dict[str, Any], *, applied: bool) -> str:
        record = result.get("artifacts", {}).get("integration", {})
        integration_id = str(record.get("integration_id", "integration")).strip()
        status = str(record.get("status", "ACTIVE" if applied else "TESTED")).strip()
        verb = "applied" if applied else "ready"
        lines = [f"Integration `{integration_id}` is {verb}."]
        lines.append(f"- status: {status}")
        required_env = record.get("required_env", [])
        if required_env:
            lines.append(f"- required env: {', '.join(required_env)}")
        if not applied:
            lines.append("- next: approve to apply and activate")
        error = record.get("last_error")
        if error:
            lines.append(f"- blocked: {error}")
        return "\n".join(lines)
