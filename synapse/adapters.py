from __future__ import annotations

import logging
import re
import threading
import time
from html import escape
from typing import Any

import httpx

from .attachments import enrich_attachment
from .models import NormalizedInboundEvent

logger = logging.getLogger(__name__)


def _split_text(text: str, *, max_len: int = 4096) -> list[str]:
    """Split text into chunks that fit within Telegram's message limit.

    Splits at paragraph boundaries (double newline) first, then single
    newlines, then hard-cuts at max_len as a last resort.
    """
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Try splitting at last double newline within limit
        cut = remaining[:max_len].rfind("\n\n")
        if cut > 0:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 2:]
            continue
        # Try splitting at last single newline within limit
        cut = remaining[:max_len].rfind("\n")
        if cut > 0:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 1:]
            continue
        # Hard cut
        chunks.append(remaining[:max_len])
        remaining = remaining[max_len:]
    return chunks


class TelegramAdapter:
    def __init__(
        self,
        token: str | None = None,
        client: httpx.Client | None = None,
        *,
        polling_enabled: bool = False,
        poll_interval: float = 2.0,
    ) -> None:
        self.token = token
        self.client = client or httpx.Client(timeout=30.0)
        self.polling_enabled = polling_enabled
        self.poll_interval = poll_interval
        self.last_error: str | None = None
        self.update_offset: int | None = None
        self._inbound_handler: Any | None = None
        self._health_handler: Any | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def normalize_update(self, payload: dict[str, Any]) -> NormalizedInboundEvent:
        message = payload.get("message") or payload.get("edited_message")
        if not message:
            raise ValueError("telegram update does not contain a supported message payload")
        attachments = self._extract_telegram_attachments(message)
        attachments = self._enrich_telegram_attachments(attachments)
        text = message.get("text") or message.get("caption") or self._attachment_placeholder(attachments)
        if not text:
            raise ValueError("telegram message does not contain supported text or attachments")
        chat = message["chat"]
        sender = message.get("from") or {}
        metadata: dict[str, Any] = {
            "update_id": payload.get("update_id"),
            "attachments": attachments,
            "chat_type": chat.get("type"),  # private | group | supergroup | channel
            "message_thread_id": message.get("message_thread_id"),
            "is_forum": bool(chat.get("is_forum")),
        }
        return NormalizedInboundEvent(
            adapter="telegram",
            channel_id=str(chat["id"]),
            user_id=str(sender.get("id", chat["id"])),
            message_id=str(message["message_id"]),
            text=text,
            metadata=metadata,
        )

    def send_text(self, chat_id: str, text: str) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("telegram bot token is not configured")
        chunks = _split_text(text, max_len=4096)
        last_response: dict[str, Any] = {}
        for chunk in chunks:
            rendered = self._render_telegram_html(chunk)
            response = self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": chat_id, "text": rendered, "parse_mode": "HTML"},
            )
            response.raise_for_status()
            last_response = response.json()
        return last_response

    def edit_text(self, chat_id: str, message_id: int, text: str) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("telegram bot token is not configured")
        rendered = self._render_telegram_html(text)
        response = self.client.post(
            f"https://api.telegram.org/bot{self.token}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": rendered, "parse_mode": "HTML"},
        )
        response.raise_for_status()
        return response.json()

    def send_draft(self, chat_id: str, draft_id: int, text: str) -> dict[str, Any]:
        """Send or update a draft preview via sendMessageDraft (Bot API 9.5+)."""
        if not self.token:
            raise RuntimeError("telegram bot token is not configured")
        rendered = self._render_telegram_html(text)
        response = self.client.post(
            f"https://api.telegram.org/bot{self.token}/sendMessageDraft",
            json={"chat_id": chat_id, "draft_id": draft_id, "text": rendered, "parse_mode": "HTML"},
        )
        response.raise_for_status()
        return response.json()

    def delete_message(self, chat_id: str, message_id: int) -> None:
        """Delete a message. Best-effort, errors are ignored."""
        if not self.token:
            return
        try:
            self.client.post(
                f"https://api.telegram.org/bot{self.token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            )
        except Exception:
            pass

    def send_typing_action(self, chat_id: str) -> None:
        if not self.token:
            return
        try:
            self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )
        except Exception:
            pass  # typing indicator is best-effort

    def _render_telegram_html(self, text: str) -> str:
        escaped = escape(text)
        placeholders: dict[str, str] = {}

        def stash(prefix: str, value: str) -> str:
            key = f"@@{prefix}_{len(placeholders)}@@"
            placeholders[key] = value
            return key

        def replace_block_code(match: re.Match[str]) -> str:
            code = match.group(1).strip("\n")
            return stash("PRE", f"<pre>{code}</pre>")

        def replace_inline_code(match: re.Match[str]) -> str:
            return stash("CODE", f"<code>{match.group(1)}</code>")

        rendered = re.sub(r"```(?:[a-zA-Z0-9_+-]+\n)?(.*?)```", replace_block_code, escaped, flags=re.DOTALL)
        rendered = re.sub(r"`([^`]+)`", replace_inline_code, rendered)
        rendered = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", rendered, flags=re.DOTALL)
        rendered = re.sub(r"__(.+?)__", r"<b>\1</b>", rendered, flags=re.DOTALL)
        for key, value in placeholders.items():
            rendered = rendered.replace(key, value)
        return rendered

    def set_handlers(self, *, inbound_handler: Any, health_handler: Any) -> None:
        self._inbound_handler = inbound_handler
        self._health_handler = health_handler

    def start(self) -> None:
        snapshot = self.status_snapshot()
        self._emit_health(status=snapshot["status"], auth_required=not bool(self.token), last_error=self.last_error)
        if not self.token or not self.polling_enabled or self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="telegram-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread = None

    def status_snapshot(self) -> dict[str, Any]:
        if not self.token:
            status = "auth_required"
        elif self.polling_enabled:
            status = "polling"
        else:
            status = "configured"
        return {
            "configured": bool(self.token),
            "polling_enabled": self.polling_enabled,
            "status": status,
            "update_offset": self.update_offset,
            "last_error": self.last_error,
        }

    def get_updates(self, *, timeout: int = 20) -> list[dict[str, Any]]:
        if not self.token:
            raise RuntimeError("telegram bot token is not configured")
        response = self.client.post(
            f"https://api.telegram.org/bot{self.token}/getUpdates",
            json={
                "timeout": timeout,
                "offset": self.update_offset,
                "allowed_updates": ["message", "edited_message"],
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"telegram getUpdates failed: {payload}")
        return list(payload.get("result", []))

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                updates = self.get_updates(timeout=20)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self.update_offset = update_id + 1
                    try:
                        event = self.normalize_update(update)
                    except ValueError:
                        continue
                    if self._inbound_handler is not None:
                        try:
                            self._inbound_handler(event)
                        except Exception:
                            logger.exception("inbound_handler crashed for update %s", update.get("update_id"))
                self.last_error = None
                self._emit_health(status="healthy", auth_required=False, last_error=None)
            except Exception as error:  # pragma: no cover - depends on network/runtime
                self.last_error = str(error)
                logger.warning("telegram poll error: %s", error)
                self._emit_health(status="error", auth_required=False, last_error=self.last_error)
                time.sleep(self.poll_interval)

    def _emit_health(self, *, status: str, auth_required: bool, last_error: str | None) -> None:
        if self._health_handler is None:
            return
        self._health_handler(
            adapter="telegram",
            status=status,
            auth_required=auth_required,
            last_error=last_error,
        )

    def _extract_telegram_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        photo_sizes = message.get("photo") or []
        if photo_sizes:
            largest = photo_sizes[-1]
            attachments.append(
                {
                    "kind": "photo",
                    "file_id": largest.get("file_id"),
                    "file_unique_id": largest.get("file_unique_id"),
                    "mime_type": "image/jpeg",
                    "width": largest.get("width"),
                    "height": largest.get("height"),
                    "file_size": largest.get("file_size"),
                }
            )
        document = message.get("document")
        if document:
            attachments.append(
                {
                    "kind": "document",
                    "file_id": document.get("file_id"),
                    "file_unique_id": document.get("file_unique_id"),
                    "file_name": document.get("file_name"),
                    "mime_type": document.get("mime_type"),
                    "file_size": document.get("file_size"),
                }
            )
        video = message.get("video")
        if video:
            attachments.append(
                {
                    "kind": "video",
                    "file_id": video.get("file_id"),
                    "file_unique_id": video.get("file_unique_id"),
                    "mime_type": video.get("mime_type"),
                    "duration": video.get("duration"),
                    "width": video.get("width"),
                    "height": video.get("height"),
                    "file_size": video.get("file_size"),
                }
            )
        audio = message.get("audio")
        if audio:
            attachments.append(
                {
                    "kind": "audio",
                    "file_id": audio.get("file_id"),
                    "file_unique_id": audio.get("file_unique_id"),
                    "title": audio.get("title"),
                    "file_name": audio.get("file_name"),
                    "mime_type": audio.get("mime_type"),
                    "duration": audio.get("duration"),
                    "file_size": audio.get("file_size"),
                }
            )
        return attachments

    def _attachment_placeholder(self, attachments: list[dict[str, Any]]) -> str:
        if not attachments:
            return ""
        labels = []
        for attachment in attachments:
            kind = attachment.get("kind", "attachment")
            file_name = attachment.get("file_name")
            labels.append(f"{kind} {file_name}".strip() if file_name else str(kind))
        return "User uploaded: " + ", ".join(labels)

    def _enrich_telegram_attachments(self, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for attachment in attachments:
            file_id = str(attachment.get("file_id", "") or "")
            data = self._download_telegram_file(file_id) if file_id else None
            enriched.append(enrich_attachment(attachment, data))
        return enriched

    def _download_telegram_file(self, file_id: str) -> bytes | None:
        if not self.token:
            return None
        try:
            response = self.client.post(
                f"https://api.telegram.org/bot{self.token}/getFile",
                json={"file_id": file_id},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None
        if not payload.get("ok"):
            return None
        file_path = payload.get("result", {}).get("file_path")
        if not file_path:
            return None
        try:
            file_response = self.client.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}")
            file_response.raise_for_status()
        except Exception:
            return None
        return file_response.content
