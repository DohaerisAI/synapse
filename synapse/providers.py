from __future__ import annotations

import asyncio
import json
import tempfile
from typing import Any, Protocol

import httpx

from .attachments import attachment_prompt_context
from .auth import AuthStore
from .models import AuthProfile


class ModelProvider(Protocol):
    async def generate(self, messages: list[dict[str, Any]], *, system_prompt: str | None = None) -> str:
        ...


class AzureOpenAIProvider:
    def __init__(self, profile: AuthProfile, client: httpx.AsyncClient | None = None) -> None:
        self.profile = profile
        self.client = client or httpx.AsyncClient(timeout=20.0)

    async def generate(self, messages: list[dict[str, Any]], *, system_prompt: str | None = None) -> str:
        endpoint = self.profile.settings["endpoint"].rstrip("/")
        deployment = self.profile.settings.get("deployment", self.profile.model)
        api_version = self.profile.settings.get("api_version", "2024-10-21")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions"
        request_messages = []
        if system_prompt:
            request_messages.append({"role": "system", "content": system_prompt})
        request_messages.extend(messages)
        response = await self.client.post(
            url,
            params={"api-version": api_version},
            headers={"api-key": self.profile.settings["api_key"]},
            json={"messages": request_messages, "temperature": 0},
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"]


class OpenAICodexResponsesProvider:
    def __init__(self, profile: AuthProfile, client: httpx.AsyncClient | None = None) -> None:
        self.profile = profile
        self.client = client or httpx.AsyncClient(timeout=30.0)

    async def generate(self, messages: list[dict[str, Any]], *, system_prompt: str | None = None) -> str:
        token = str(
            self.profile.settings.get("access_token")
            or self.profile.settings.get("token")
            or ""
        ).strip()
        if not token:
            raise RuntimeError("openai-codex responses transport requires an access token")
        endpoint = str(
            self.profile.settings.get("endpoint", "https://chatgpt.com/backend-api/codex/responses")
        ).strip()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        account_id = str(self.profile.settings.get("account_id", "") or "").strip()
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        async with self.client.stream(
            "POST",
            endpoint,
            headers=headers,
            json=self._build_payload(messages, system_prompt=system_prompt),
        ) as response:
            response.raise_for_status()
            return await self._extract_stream_text(response)

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "store": False,
            "stream": True,
            "input": [self._message_to_input(message) for message in messages],
        }
        if system_prompt:
            payload["instructions"] = system_prompt
        return payload

    def _message_to_input(self, message: dict[str, Any]) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        text = str(message.get("content", "") or "").strip()
        if text:
            parts.append({"type": "input_text", "text": text})
        attachments = message.get("attachments")
        if isinstance(attachments, list):
            parts.extend(self._attachment_parts(attachments))
        if not parts:
            parts.append({"type": "input_text", "text": ""})
        return {"role": str(message.get("role", "user") or "user"), "content": parts}

    def _attachment_parts(self, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            data_url = str(attachment.get("inline_data_url", "") or "").strip()
            if not data_url:
                continue
            kind = str(attachment.get("kind", "") or "").strip().lower()
            if kind not in {"image", "photo"}:
                continue
            parts.append({"type": "input_image", "image_url": data_url})
        return parts

    def _extract_text(self, payload: dict[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        texts: list[str] = []
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "message" and isinstance(item.get("content"), list):
                    texts.extend(self._extract_message_content(item["content"]))
                    continue
                if item.get("type") in {"output_text", "text"}:
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())
        if texts:
            return "\n".join(texts).strip()

        raise RuntimeError(f"openai-codex responses payload did not include assistant text: {json.dumps(payload)[:400]}")

    async def _extract_stream_text(self, response: httpx.Response) -> str:
        current_event = ""
        current_data: list[str] = []
        deltas: list[str] = []
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line:
                if current_data:
                    text = self._consume_sse_event(current_event, current_data, deltas)
                    if text is not None:
                        return text
                    current_event = ""
                    current_data = []
                continue
            if line.startswith("event:"):
                current_event = line.partition(":")[2].strip()
                continue
            if line.startswith("data:"):
                current_data.append(line.partition(":")[2].strip())
        if current_data:
            text = self._consume_sse_event(current_event, current_data, deltas)
            if text is not None:
                return text
        if deltas:
            return "".join(deltas).strip()
        raise RuntimeError("openai-codex responses stream ended without assistant text")

    def _consume_sse_event(self, event: str, data_lines: list[str], deltas: list[str]) -> str | None:
        payload_text = "\n".join(data_lines).strip()
        if not payload_text or payload_text == "[DONE]":
            return "".join(deltas).strip() if deltas else None
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return None
        if event == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            return None
        if event == "response.completed":
            try:
                return self._extract_text(payload.get("response", {}))
            except RuntimeError:
                return "".join(deltas).strip() if deltas else None
        if event == "response.failed":
            raise RuntimeError(f"openai-codex responses stream failed: {payload_text[:400]}")
        return None

    def _extract_message_content(self, content: list[dict[str, Any]]) -> list[str]:
        texts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"}:
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        return texts


class CodexCliProvider:
    def __init__(
        self,
        profile: AuthProfile,
        client: httpx.AsyncClient | None = None,
        *,
        workdir: str = ".",
    ) -> None:
        self.profile = profile
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self.workdir = workdir

    async def generate(self, messages: list[dict[str, Any]], *, system_prompt: str | None = None) -> str:
        prompt = self._build_prompt(messages, system_prompt=system_prompt)
        with tempfile.NamedTemporaryFile(mode="r+", encoding="utf-8", suffix=".txt") as output_file:
            command = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "-C",
                self.workdir,
                "-m",
                self.profile.model,
                "-o",
                output_file.name,
                prompt,
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
            if process.returncode != 0:
                detail = stderr.decode().strip() or stdout.decode().strip() or f"exit code {process.returncode}"
                raise RuntimeError(f"codex exec failed: {detail}")
            output_file.seek(0)
            result = output_file.read().strip()
            if not result:
                raise RuntimeError("codex exec returned an empty response")
            return result

    def _build_prompt(self, messages: list[dict[str, Any]], *, system_prompt: str | None) -> str:
        parts = ["Reply as a concise assistant."]
        if system_prompt:
            parts.extend(["", "System instructions:", system_prompt.strip()])
        parts.extend(["", "Conversation:"])
        for message in messages:
            parts.append(f"{message['role']}: {message['content']}")
            attachments = message.get("attachments")
            if isinstance(attachments, list) and attachments:
                parts.extend(["attachments:", attachment_prompt_context([item for item in attachments if isinstance(item, dict)])])
        parts.extend(["", "Return only the assistant reply text."])
        return "\n".join(parts)


class ModelRouter:
    def __init__(self, auth_store: AuthStore, client: httpx.AsyncClient | None = None, *, workdir: str = ".") -> None:
        self.auth_store = auth_store
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self.workdir = workdir

    def resolve_profile(self) -> AuthProfile | None:
        return self.auth_store.resolve()

    async def generate(self, messages: list[dict[str, Any]], *, system_prompt: str | None = None) -> str | None:
        profile = self.resolve_profile()
        if profile is None:
            return None
        provider = self._provider_for(profile)
        try:
            return await provider.generate(messages, system_prompt=system_prompt)
        except Exception:
            fallback = self._fallback_provider_for(profile)
            if fallback is None:
                raise
            return await fallback.generate(messages, system_prompt=system_prompt)

    def _provider_for(self, profile: AuthProfile) -> ModelProvider:
        if profile.provider == "azure-openai":
            return AzureOpenAIProvider(profile, client=self.client)
        if profile.provider == "codex-cli":
            return CodexCliProvider(profile, client=self.client, workdir=self.workdir)
        if profile.provider == "openai-codex":
            if str(profile.settings.get("transport", "responses")).strip().lower() == "cli":
                return CodexCliProvider(profile, client=self.client, workdir=self.workdir)
            return OpenAICodexResponsesProvider(profile, client=self.client)
        raise ValueError(f"unsupported provider: {profile.provider}")

    def _fallback_provider_for(self, profile: AuthProfile) -> ModelProvider | None:
        if profile.provider != "openai-codex":
            return None
        if not profile.settings.get("cli_fallback", True):
            return None
        if str(profile.settings.get("transport", "responses")).strip().lower() == "cli":
            return None
        return CodexCliProvider(profile, client=self.client, workdir=self.workdir)
