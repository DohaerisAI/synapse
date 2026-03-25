from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .attachments import attachment_prompt_context
from .auth import AuthStore
from .models import AuthProfile, utc_now
from .streaming.sink import NullSink, StreamSink
from .usage import estimate_input_chars, estimate_output_chars


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    """A single tool call returned by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Unified response from any LLM provider."""

    text: str | None = None
    tool_calls: list[ProviderToolCall] | None = None
    usage: dict[str, Any] | None = None


class ModelProvider(Protocol):
    async def generate(self, messages: list[dict[str, Any]], *, system_prompt: str | None = None) -> str:
        ...


def _usage_numbers(usage: dict[str, Any] | None) -> tuple[int | None, int | None, int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None, None, None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    cached_tokens = None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached_tokens = details.get("cached_tokens")
    if cached_tokens is None:
        cached_tokens = usage.get("cached_tokens")
    return (
        int(prompt_tokens) if isinstance(prompt_tokens, int) else None,
        int(completion_tokens) if isinstance(completion_tokens, int) else None,
        int(total_tokens) if isinstance(total_tokens, int) else None,
        int(cached_tokens) if isinstance(cached_tokens, int) else None,
    )


def _record_usage_event(
    store: Any,
    *,
    run_id: str | None,
    session_key: str | None,
    provider: str,
    model: str,
    input_chars: int,
    output_chars: int,
    started_at: str,
    duration_ms: int,
    usage: dict[str, Any] | None = None,
    status: str = "ok",
    error: str | None = None,
) -> None:
    if store is None:
        return
    prompt_tokens, completion_tokens, total_tokens, cached_tokens = _usage_numbers(usage)
    store.append_usage_event(
        run_id=run_id,
        session_key=session_key,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        input_chars=input_chars,
        output_chars=output_chars,
        started_at=started_at,
        finished_at=utc_now().isoformat(),
        duration_ms=duration_ms,
        status=status,
        error=error,
    )


class AzureOpenAIProvider:
    def __init__(self, profile: AuthProfile, client: httpx.AsyncClient | None = None, *, store: Any | None = None) -> None:
        self.profile = profile
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self.store = store

    def _build_request(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        endpoint = self.profile.settings["endpoint"].rstrip("/")
        deployment = self.profile.settings.get("deployment", self.profile.model)
        api_version = self.profile.settings.get("api_version", "2024-10-21")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
        headers = {"api-key": self.profile.settings["api_key"]}
        request_messages: list[dict[str, Any]] = []
        if system_prompt:
            request_messages.append({"role": "system", "content": system_prompt})
        request_messages.extend(messages)
        body: dict[str, Any] = {"messages": request_messages, "temperature": 0}
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
        return url, headers, body

    @staticmethod
    def _parse_tool_calls(message: dict[str, Any]) -> list[ProviderToolCall] | None:
        raw = message.get("tool_calls")
        if not isinstance(raw, list) or not raw:
            return None
        calls: list[ProviderToolCall] = []
        for tc in raw:
            fn = tc.get("function", {})
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ProviderToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=args,
            ))
        return calls or None

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> LLMResponse:
        started_at = utc_now().isoformat()
        started_perf = time.perf_counter()
        input_chars = estimate_input_chars(messages, system_prompt=system_prompt, tools=tools)
        url, headers, body = self._build_request(
            messages, system_prompt=system_prompt, tools=tools,
        )
        response_payload: dict[str, Any] | None = None
        try:
            response = await self.client.post(url, headers=headers, json=body)
            response.raise_for_status()
            response_payload = response.json()
            message = response_payload["choices"][0]["message"]
            result = LLMResponse(
                text=message.get("content"),
                tool_calls=self._parse_tool_calls(message),
                usage=response_payload.get("usage"),
            )
            _record_usage_event(
                self.store,
                run_id=run_id,
                session_key=session_key,
                provider=self.profile.provider,
                model=self.profile.model,
                input_chars=input_chars,
                output_chars=estimate_output_chars(result.text, result.tool_calls),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                usage=response_payload.get("usage"),
            )
            return result
        except Exception as exc:
            _record_usage_event(
                self.store,
                run_id=run_id,
                session_key=session_key,
                provider=self.profile.provider,
                model=self.profile.model,
                input_chars=input_chars,
                output_chars=0,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                usage=None if response_payload is None else response_payload.get("usage"),
                status="error",
                error=str(exc),
            )
            raise

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        sink: StreamSink | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> LLMResponse:
        """Generate with streaming SSE. Push deltas to sink. Return LLMResponse."""
        effective_sink = sink or NullSink()
        started_at = utc_now().isoformat()
        started_perf = time.perf_counter()
        input_chars = estimate_input_chars(messages, system_prompt=system_prompt, tools=tools)
        url, headers, body = self._build_request(
            messages, system_prompt=system_prompt, tools=tools, stream=True,
        )
        # Accumulate tool calls from stream deltas
        tc_accum: dict[int, dict[str, Any]] = {}  # index -> {id, name, arguments_parts}
        try:
            async with self.client.stream("POST", url, headers=headers, json=body) as response:
                response.raise_for_status()
                deltas: list[str] = []
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line.partition(":")[2].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if isinstance(content, str):
                        deltas.append(content)
                        await effective_sink.push(content)
                    for tc_delta in delta.get("tool_calls", []):
                        idx = tc_delta.get("index", 0)
                        if idx not in tc_accum:
                            tc_accum[idx] = {"id": "", "name": "", "arguments_parts": []}
                        entry = tc_accum[idx]
                        if "id" in tc_delta:
                            entry["id"] = tc_delta["id"]
                        fn = tc_delta.get("function", {})
                        if "name" in fn:
                            entry["name"] = fn["name"]
                        if "arguments" in fn:
                            entry["arguments_parts"].append(fn["arguments"])
                full_text = "".join(deltas).strip() or None
                tool_calls: list[ProviderToolCall] | None = None
                if tc_accum:
                    calls = []
                    for idx in sorted(tc_accum):
                        entry = tc_accum[idx]
                        args_str = "".join(entry["arguments_parts"])
                        try:
                            args = json.loads(args_str)
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        calls.append(ProviderToolCall(id=entry["id"], name=entry["name"], arguments=args))
                    tool_calls = calls or None
                result = LLMResponse(text=full_text, tool_calls=tool_calls)
                _record_usage_event(
                    self.store,
                    run_id=run_id,
                    session_key=session_key,
                    provider=self.profile.provider,
                    model=self.profile.model,
                    input_chars=input_chars,
                    output_chars=estimate_output_chars(result.text, result.tool_calls),
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started_perf) * 1000),
                )
                return result
        except Exception as exc:
            _record_usage_event(
                self.store,
                run_id=run_id,
                session_key=session_key,
                provider=self.profile.provider,
                model=self.profile.model,
                input_chars=input_chars,
                output_chars=0,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                status="error",
                error=str(exc),
            )
            raise


class OpenAICodexResponsesProvider:
    def __init__(self, profile: AuthProfile, client: httpx.AsyncClient | None = None, *, store: Any | None = None) -> None:
        self.profile = profile
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self.store = store

    def _auth_headers(self) -> tuple[str, dict[str, str]]:
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
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        account_id = str(self.profile.settings.get("account_id", "") or "").strip()
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        return endpoint, headers

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> LLMResponse:
        endpoint, headers = self._auth_headers()
        started_at = utc_now().isoformat()
        started_perf = time.perf_counter()
        input_chars = estimate_input_chars(messages, system_prompt=system_prompt, tools=tools)
        try:
            async with self.client.stream(
                "POST",
                endpoint,
                headers=headers,
                json=self._build_payload(messages, system_prompt=system_prompt, tools=tools),
            ) as response:
                response.raise_for_status()
                result = await self._extract_stream_response(response)
            _record_usage_event(
                self.store,
                run_id=run_id,
                session_key=session_key,
                provider=self.profile.provider,
                model=self.profile.model,
                input_chars=input_chars,
                output_chars=estimate_output_chars(result.text, result.tool_calls),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                usage=result.usage,
            )
            return result
        except Exception as exc:
            _record_usage_event(
                self.store,
                run_id=run_id,
                session_key=session_key,
                provider=self.profile.provider,
                model=self.profile.model,
                input_chars=input_chars,
                output_chars=0,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                status="error",
                error=str(exc),
            )
            raise

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        sink: StreamSink | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> LLMResponse:
        """Generate with streaming. Push deltas to sink. Return LLMResponse."""
        effective_sink = sink or NullSink()
        endpoint, headers = self._auth_headers()
        started_at = utc_now().isoformat()
        started_perf = time.perf_counter()
        input_chars = estimate_input_chars(messages, system_prompt=system_prompt, tools=tools)
        try:
            async with self.client.stream(
                "POST",
                endpoint,
                headers=headers,
                json=self._build_payload(messages, system_prompt=system_prompt, tools=tools),
            ) as response:
                response.raise_for_status()
                result = await self._extract_stream_response(response, sink=effective_sink)
            _record_usage_event(
                self.store,
                run_id=run_id,
                session_key=session_key,
                provider=self.profile.provider,
                model=self.profile.model,
                input_chars=input_chars,
                output_chars=estimate_output_chars(result.text, result.tool_calls),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                usage=result.usage,
            )
            return result
        except Exception as exc:
            _record_usage_event(
                self.store,
                run_id=run_id,
                session_key=session_key,
                provider=self.profile.provider,
                model=self.profile.model,
                input_chars=input_chars,
                output_chars=0,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                status="error",
                error=str(exc),
            )
            raise

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "store": False,
            "stream": True,
            "input": [self._message_to_input(message) for message in messages],
        }
        if system_prompt:
            payload["instructions"] = system_prompt
        if tools:
            payload["tools"] = [self._tool_to_responses_format(t) for t in tools]
        return payload

    @staticmethod
    def _tool_to_responses_format(tool: dict[str, Any]) -> dict[str, Any]:
        """Convert Chat Completions tool schema to Responses API format.

        Chat Completions: {"type": "function", "function": {"name": ..., "parameters": ...}}
        Responses API:    {"type": "function", "name": ..., "parameters": ...}

        Also sanitizes names to match ^[a-zA-Z0-9_-]+$ (dots → underscores).
        """
        fn = tool.get("function")
        if not isinstance(fn, dict):
            return tool
        name = fn.get("name", "").replace(".", "_")
        return {
            "type": "function",
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        }

    def _message_to_input(self, message: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
        msg_type = message.get("type")

        # Pass-through Responses API items (function_call, function_call_output)
        if msg_type in ("function_call", "function_call_output"):
            return message

        role = str(message.get("role", "user") or "user")

        # Tool result → Responses API function_call_output
        if role == "tool":
            return {
                "type": "function_call_output",
                "call_id": message.get("tool_call_id", ""),
                "output": str(message.get("content", "")),
            }

        parts: list[dict[str, Any]] = []
        text = str(message.get("content", "") or "").strip()
        if text:
            parts.append({"type": "input_text", "text": text})
        attachments = message.get("attachments")
        if isinstance(attachments, list):
            parts.extend(self._attachment_parts(attachments))
        if not parts:
            parts.append({"type": "input_text", "text": ""})
        return {"role": role, "content": parts}

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

    def _extract_text_and_calls(self, payload: dict[str, Any]) -> LLMResponse:
        """Parse a completed response payload into LLMResponse."""
        tool_calls: list[ProviderToolCall] = []
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "function_call":
                    args_str = item.get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tool_calls.append(ProviderToolCall(
                        id=item.get("call_id", item.get("id", "")),
                        name=item.get("name", ""),
                        arguments=args if isinstance(args, dict) else {},
                    ))
        # Use output_text (top-level convenience field) as the single text source
        # to avoid double-counting from output[].message.content[]
        direct = payload.get("output_text")
        text = direct.strip() if isinstance(direct, str) and direct.strip() else None
        return LLMResponse(
            text=text,
            tool_calls=tool_calls or None,
            usage=payload.get("usage"),
        )

    async def _extract_stream_response(
        self,
        response: httpx.Response,
        *,
        sink: StreamSink | None = None,
    ) -> LLMResponse:
        current_event = ""
        current_data: list[str] = []
        text_deltas: list[str] = []
        # Accumulate function calls: call_id -> {name, arguments_parts}
        fc_accum: dict[str, dict[str, Any]] = {}
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line:
                if current_data:
                    result = await self._consume_sse_event(
                        current_event, current_data, text_deltas, fc_accum, sink=sink,
                    )
                    if result is not None:
                        return result
                    current_event = ""
                    current_data = []
                continue
            if line.startswith("event:"):
                current_event = line.partition(":")[2].strip()
                continue
            if line.startswith("data:"):
                current_data.append(line.partition(":")[2].strip())
        if current_data:
            result = await self._consume_sse_event(
                current_event, current_data, text_deltas, fc_accum, sink=sink,
            )
            if result is not None:
                return result
        return self._build_stream_result(text_deltas, fc_accum)

    def _build_stream_result(
        self,
        text_deltas: list[str],
        fc_accum: dict[str, dict[str, Any]],
    ) -> LLMResponse:
        text = "".join(text_deltas).strip() or None
        tool_calls: list[ProviderToolCall] | None = None
        if fc_accum:
            calls = []
            for call_id, entry in fc_accum.items():
                args_str = "".join(entry.get("arguments_parts", []))
                try:
                    args = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                calls.append(ProviderToolCall(
                    id=call_id, name=entry.get("name", ""), arguments=args,
                ))
            tool_calls = calls or None
        if text is None and tool_calls is None:
            raise RuntimeError("openai-codex responses stream ended without assistant text or tool calls")
        return LLMResponse(text=text, tool_calls=tool_calls)

    async def _consume_sse_event(
        self,
        event: str,
        data_lines: list[str],
        text_deltas: list[str],
        fc_accum: dict[str, dict[str, Any]],
        *,
        sink: StreamSink | None = None,
    ) -> LLMResponse | None:
        payload_text = "\n".join(data_lines).strip()
        if not payload_text or payload_text == "[DONE]":
            return self._build_stream_result(text_deltas, fc_accum) if (text_deltas or fc_accum) else None
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return None
        if event == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                text_deltas.append(delta)
                if sink is not None:
                    await sink.push(delta)
            return None
        if event == "response.output_item.added":
            item = payload.get("item", payload)
            if isinstance(item, dict) and item.get("type") == "function_call":
                call_id = item.get("call_id", item.get("id", ""))
                fc_accum[call_id] = {"name": item.get("name", ""), "arguments_parts": []}
            return None
        if event == "response.function_call_arguments.delta":
            delta = payload.get("delta", "")
            call_id = payload.get("call_id", payload.get("item_id", ""))
            if call_id in fc_accum and isinstance(delta, str):
                fc_accum[call_id]["arguments_parts"].append(delta)
            return None
        if event == "response.function_call_arguments.done":
            call_id = payload.get("call_id", payload.get("item_id", ""))
            if call_id in fc_accum:
                fc_accum[call_id]["arguments_parts"] = [payload.get("arguments", "")]
            return None
        if event == "response.completed":
            resp = payload.get("response", {})
            try:
                result = self._extract_text_and_calls(resp)
            except RuntimeError:
                return self._build_stream_result(text_deltas, fc_accum) if (text_deltas or fc_accum) else None
            # If output_text was empty/missing but we accumulated deltas, use those
            if not result.text and text_deltas:
                return self._build_stream_result(text_deltas, fc_accum)
            return result
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
        store: Any | None = None,
    ) -> None:
        self.profile = profile
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self.workdir = workdir
        self.store = store

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> str:
        prompt = self._build_prompt(messages, system_prompt=system_prompt)
        started_at = utc_now().isoformat()
        started_perf = time.perf_counter()
        input_chars = len(prompt)
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
            try:
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
                _record_usage_event(
                    self.store,
                    run_id=run_id,
                    session_key=session_key,
                    provider=self.profile.provider,
                    model=self.profile.model,
                    input_chars=input_chars,
                    output_chars=len(result),
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started_perf) * 1000),
                    usage=None,
                )
                return result
            except Exception as exc:
                _record_usage_event(
                    self.store,
                    run_id=run_id,
                    session_key=session_key,
                    provider=self.profile.provider,
                    model=self.profile.model,
                    input_chars=input_chars,
                    output_chars=0,
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started_perf) * 1000),
                    usage=None,
                    status="error",
                    error=str(exc),
                )
                raise

    def _build_prompt(self, messages: list[dict[str, Any]], *, system_prompt: str | None) -> str:
        parts = ["Reply as a concise assistant."]
        if system_prompt:
            parts.extend(["", "System instructions:", system_prompt.strip()])
        parts.extend(["", "Conversation:"])
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content")
            if content is None:
                # Some internal callers may pass non-chat payloads; stringify safely.
                content = str(message)
            parts.append(f"{role}: {content}")
            attachments = message.get("attachments")
            if isinstance(attachments, list) and attachments:
                parts.extend(["attachments:", attachment_prompt_context([item for item in attachments if isinstance(item, dict)])])
        parts.extend(["", "Return only the assistant reply text."])
        return "\n".join(parts)


class ModelRouter:
    def __init__(
        self,
        auth_store: AuthStore,
        client: httpx.AsyncClient | None = None,
        *,
        workdir: str = ".",
        store: Any | None = None,
    ) -> None:
        self.auth_store = auth_store
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self.workdir = workdir
        self.store = store

    def resolve_profile(self) -> AuthProfile | None:
        return self.auth_store.resolve()

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> str | None:
        """Backward-compat: returns text only (str | None). Old callers use this."""
        profile = self.resolve_profile()
        if profile is None:
            return None
        provider = self._provider_for(profile)
        try:
            resp = await provider.generate(messages, system_prompt=system_prompt, run_id=run_id, session_key=session_key)
        except Exception:
            fallback = self._fallback_provider_for(profile)
            if fallback is None:
                raise
            resp = await fallback.generate(messages, system_prompt=system_prompt, run_id=run_id, session_key=session_key)
        if isinstance(resp, str):
            return resp
        return resp.text if resp else None

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        sink: StreamSink | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> str | None:
        """Backward-compat streaming: returns text only (str | None)."""
        profile = self.resolve_profile()
        if profile is None:
            return None
        provider = self._provider_for(profile)
        effective_sink = sink or NullSink()
        if hasattr(provider, "generate_stream"):
            try:
                resp = await provider.generate_stream(
                    messages, system_prompt=system_prompt, sink=effective_sink, run_id=run_id, session_key=session_key,
                )
            except Exception:
                fallback = self._fallback_provider_for(profile)
                if fallback is None:
                    raise
                text = await fallback.generate(messages, system_prompt=system_prompt, run_id=run_id, session_key=session_key)
                if isinstance(text, LLMResponse):
                    text = text.text
                if text:
                    await effective_sink.push(text)
                return text
            if isinstance(resp, str):
                return resp
            return resp.text if resp else None
        resp = await provider.generate(messages, system_prompt=system_prompt, run_id=run_id, session_key=session_key)
        text = resp if isinstance(resp, str) else (resp.text if resp else None)
        if text and effective_sink is not None:
            await effective_sink.push(text)
        return text

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        sink: StreamSink | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> LLMResponse | None:
        """Full-featured method for react loop — returns LLMResponse with tool_calls."""
        profile = self.resolve_profile()
        if profile is None:
            return None
        provider = self._provider_for(profile)
        effective_sink = sink or NullSink()

        if sink is not None and hasattr(provider, "generate_stream"):
            try:
                return await provider.generate_stream(
                    messages,
                    system_prompt=system_prompt,
                    tools=tools,
                    sink=effective_sink,
                    run_id=run_id,
                    session_key=session_key,
                )
            except Exception:
                fallback = self._fallback_provider_for(profile)
                if fallback is None:
                    raise
                # Fallback without streaming but still attempt tools
                try:
                    resp = await provider.generate(
                        messages,
                        system_prompt=system_prompt,
                        tools=tools,
                        run_id=run_id,
                        session_key=session_key,
                    )
                except Exception:
                    text = await fallback.generate(
                        messages,
                        system_prompt=system_prompt,
                        run_id=run_id,
                        session_key=session_key,
                    )
                    resp = LLMResponse(text=text) if isinstance(text, str) else text
                if isinstance(resp, str):
                    resp = LLMResponse(text=resp)
                if resp and resp.text and not resp.tool_calls:
                    await effective_sink.push(resp.text)
                return resp

        try:
            resp = await provider.generate(
                messages,
                system_prompt=system_prompt,
                tools=tools,
                run_id=run_id,
                session_key=session_key,
            )
        except TypeError:
            # Provider doesn't accept tools kwarg (e.g. CodexCliProvider)
            resp = await provider.generate(messages, system_prompt=system_prompt, run_id=run_id, session_key=session_key)
        except Exception:
            fallback = self._fallback_provider_for(profile)
            if fallback is None:
                raise
            text = await fallback.generate(messages, system_prompt=system_prompt, run_id=run_id, session_key=session_key)
            return LLMResponse(text=text) if isinstance(text, str) else text

        # Normalize: old providers may return str, new ones return LLMResponse
        if isinstance(resp, str):
            return LLMResponse(text=resp)
        return resp

    def _provider_for(self, profile: AuthProfile) -> Any:
        if profile.provider == "azure-openai":
            return AzureOpenAIProvider(profile, client=self.client, store=self.store)
        if profile.provider == "codex-cli":
            return CodexCliProvider(profile, client=self.client, workdir=self.workdir, store=self.store)
        if profile.provider == "openai-codex":
            if str(profile.settings.get("transport", "responses")).strip().lower() == "cli":
                return CodexCliProvider(profile, client=self.client, workdir=self.workdir, store=self.store)
            return OpenAICodexResponsesProvider(profile, client=self.client, store=self.store)
        if profile.provider == "custom":
            from .providers_chat import ChatCompletionsProvider
            return ChatCompletionsProvider(profile, client=self.client, store=self.store)
        raise ValueError(f"unsupported provider: {profile.provider}")

    def _fallback_provider_for(self, profile: AuthProfile) -> Any | None:
        if profile.provider != "openai-codex":
            return None
        if not profile.settings.get("cli_fallback", True):
            return None
        if str(profile.settings.get("transport", "responses")).strip().lower() == "cli":
            return None
        return CodexCliProvider(profile, client=self.client, workdir=self.workdir, store=self.store)
