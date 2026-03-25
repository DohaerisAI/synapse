"""ChatCompletionsProvider — standard OpenAI-compatible /chat/completions transport.

Works with Gemini, OpenRouter, Groq, Together, Ollama, and any endpoint
that speaks the OpenAI chat completions format.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .models import AuthProfile, utc_now
from .providers import LLMResponse, ProviderToolCall, _record_usage_event
from .streaming.sink import NullSink, StreamSink
from .usage import estimate_input_chars, estimate_output_chars

logger = logging.getLogger(__name__)


def _parse_chat_tool_calls(message: dict[str, Any]) -> list[ProviderToolCall] | None:
    """Parse tool_calls from a standard chat completions message."""
    raw_calls = message.get("tool_calls")
    if not raw_calls:
        return None
    results: list[ProviderToolCall] = []
    for call in raw_calls:
        fn = call.get("function", {})
        raw_args = fn.get("arguments", "")
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (json.JSONDecodeError, TypeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        results.append(
            ProviderToolCall(
                id=call.get("id", ""),
                name=fn.get("name", ""),
                arguments=arguments,
            )
        )
    return results or None


class ChatCompletionsProvider:
    """OpenAI-compatible /chat/completions provider."""

    def __init__(
        self,
        profile: AuthProfile,
        client: httpx.AsyncClient | None = None,
        *,
        store: Any | None = None,
    ) -> None:
        self.profile = profile
        self.client = client or httpx.AsyncClient(timeout=60.0)
        self.store = store

    def _build_request(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        base_url = self.profile.settings["base_url"].rstrip("/")
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.profile.settings['api_key']}"}
        request_messages: list[dict[str, Any]] = []
        if system_prompt:
            request_messages.append({"role": "system", "content": system_prompt})
        request_messages.extend(messages)
        body: dict[str, Any] = {
            "model": self.profile.model,
            "messages": request_messages,
            "temperature": 0,
        }
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
        return url, headers, body

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        session_key: str | None = None,
    ) -> LLMResponse:
        url, headers, body = self._build_request(
            messages,
            system_prompt=system_prompt,
            tools=tools,
            stream=False,
        )
        started_at = utc_now().isoformat()
        started_perf = time.perf_counter()
        input_chars = estimate_input_chars(messages, system_prompt=system_prompt, tools=tools)
        try:
            resp = await self.client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            message = data.get("choices", [{}])[0].get("message", {})
            text = message.get("content")
            tool_calls = _parse_chat_tool_calls(message)
            usage = data.get("usage")
            result = LLMResponse(text=text, tool_calls=tool_calls, usage=usage)
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
                usage=usage,
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
        effective_sink = sink or NullSink()
        url, headers, body = self._build_request(
            messages,
            system_prompt=system_prompt,
            tools=tools,
            stream=True,
        )
        started_at = utc_now().isoformat()
        started_perf = time.perf_counter()
        input_chars = estimate_input_chars(messages, system_prompt=system_prompt, tools=tools)
        text_parts: list[str] = []
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        try:
            async with self.client.stream("POST", url, json=body, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta", {})
                    content = delta.get("content")
                    if content:
                        text_parts.append(content)
                        await effective_sink.push(content)
                    for tc_delta in delta.get("tool_calls", []):
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_call_buffers:
                            tool_call_buffers[idx] = {
                                "id": tc_delta.get("id", ""),
                                "name": "",
                                "arguments": "",
                            }
                        buf = tool_call_buffers[idx]
                        if tc_delta.get("id"):
                            buf["id"] = tc_delta["id"]
                        fn = tc_delta.get("function", {})
                        if fn.get("name"):
                            buf["name"] = fn["name"]
                        if fn.get("arguments"):
                            buf["arguments"] += fn["arguments"]

            assembled_tool_calls = _assemble_tool_calls(tool_call_buffers)
            text = "".join(text_parts) or None
            result = LLMResponse(
                text=text,
                tool_calls=assembled_tool_calls or None,
                usage=usage,
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
                usage=usage,
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


def _assemble_tool_calls(
    buffers: dict[int, dict[str, Any]],
) -> list[ProviderToolCall] | None:
    """Assemble streaming tool call fragments into ProviderToolCall objects."""
    if not buffers:
        return None
    results: list[ProviderToolCall] = []
    for idx in sorted(buffers):
        buf = buffers[idx]
        try:
            args = json.loads(buf["arguments"]) if buf["arguments"] else {}
        except json.JSONDecodeError:
            args = {}
        results.append(
            ProviderToolCall(
                id=buf["id"],
                name=buf["name"],
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return results or None
