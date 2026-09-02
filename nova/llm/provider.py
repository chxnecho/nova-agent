"""OpenAI-compatible chat-completions provider (OpenRouter, OpenAI, vLLM, ...).

Features:
- async httpx client with connection pooling
- tool calling
- SSE streaming with incremental callback
- exponential-backoff retry on 429/5xx/network errors
- token usage accounting
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from typing import Any

import httpx

from nova.http_client import create_async_client
from nova.log import get_logger

from .base import LLMResponse, Message, ToolCall, Usage

log = get_logger("llm")

StreamCallback = Callable[[str], None]

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

# module-level sleep handle so tests can swap in a no-op (avoids slow backoff)
_sleep = asyncio.sleep


class LLMError(RuntimeError):
    pass


class OpenAICompatibleProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        model: str = "anthropic/claude-sonnet-4.5",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout_seconds: float = 180.0,
        max_retries: int = 4,
        extra_headers: dict[str, str] | None = None,
        proxy: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **(extra_headers or {}),
        }
        if client is not None:
            # caller-provided client (e.g. httpx.MockTransport in tests)
            self._client = client
        else:
            self._client = create_async_client(
                base_url=base_url.rstrip("/"),
                headers=self._headers,
                timeout=httpx.Timeout(timeout_seconds, connect=30.0),
                proxy=proxy,
            )
        # cumulative usage across the lifetime of this provider instance
        self.total_usage = Usage()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream_callback: StreamCallback | None = None,
        reasoning_callback: StreamCallback | None = None,
    ) -> LLMResponse:
        """Send a chat completion request.

        stream_callback receives content deltas; reasoning_callback receives
        chain-of-thought deltas from reasoning models (e.g. stealth/ox-alpha).
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_api() for m in messages],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            body["tools"] = tools

        if stream_callback is None and reasoning_callback is None:
            return await self._request(body)
        return await self._stream(body, stream_callback, reasoning_callback)

    async def _request(self, body: dict[str, Any]) -> LLMResponse:
        attempt = 0
        while True:
            try:
                resp = await self._client.post("/chat/completions", json=body)
                if resp.status_code in _RETRYABLE_STATUS:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                return self._parse(resp.json())
            except (httpx.TransportError, LLMError) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise LLMError(f"Giving up after {attempt} attempts: {exc}") from exc
                delay = min(2.0**attempt, 20.0) * (0.5 + random.random())
                log.warning(
                    "LLM request failed (%s); retry %d/%d in %.1fs",
                    exc,
                    attempt,
                    self.max_retries,
                    delay,
                )
                await _sleep(delay)

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        raw_msg = choice.get("message", {})
        message = Message(
            role="assistant",
            content=raw_msg.get("content"),
            tool_calls=[ToolCall.from_raw(tc) for tc in raw_msg.get("tool_calls") or []],
            reasoning=raw_msg.get("reasoning"),
        )
        u = data.get("usage") or {}
        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        self.total_usage = self.total_usage + usage
        return LLMResponse(
            message=message,
            usage=usage,
            model=data.get("model", self.model),
            finish_reason=choice.get("finish_reason"),
        )

    async def _stream(
        self, body: dict[str, Any], cb: StreamCallback | None, rcb: StreamCallback | None = None
    ) -> LLMResponse:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        usage = Usage()
        finish_reason = None
        attempt = 0

        while True:
            try:
                async with self._client.stream("POST", "/chat/completions", json=body) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")
                        raise LLMError(f"HTTP {resp.status_code}: {text[:500]}")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        delta, rdelta, chunk_usage, reason = _parse_stream_chunk(payload)
                        if delta and cb:
                            content_parts.append(delta)
                            cb(delta)
                        if rdelta and rcb:
                            reasoning_parts.append(rdelta)
                            rcb(rdelta)
                        for tc in chunk_tool_calls(payload):
                            acc = tool_calls_acc.setdefault(
                                tc["index"], {"id": "", "name": "", "args": ""}
                            )
                            acc["id"] = tc.get("id") or acc["id"]
                            acc["name"] += tc.get("name") or ""
                            acc["args"] += tc.get("arguments") or ""
                        if chunk_usage:
                            usage = usage + chunk_usage
                        if reason:
                            finish_reason = reason
                break  # success

            except (httpx.TransportError, LLMError) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise LLMError(f"Giving up after {attempt} attempts: {exc}") from exc
                delay = min(2.0**attempt, 20.0) * (0.5 + random.random())
                log.warning("LLM stream failed (%s); retry %d in %.1fs", exc, attempt, delay)
                await _sleep(delay)
                content_parts.clear()  # restart accumulation to avoid duplicates
                reasoning_parts.clear()
                tool_calls_acc.clear()

        message = Message(
            role="assistant",
            content="".join(content_parts) or None,
            tool_calls=[
                ToolCall(id=v["id"], name=v["name"], arguments=_safe_json(v["args"]))
                for _, v in sorted(tool_calls_acc.items())
            ],
            reasoning="".join(reasoning_parts) or None,
        )
        if not usage.total_tokens:
            usage = Usage(_approx_tokens(str(body)), _approx_tokens(message.content or ""))
        self.total_usage = self.total_usage + usage
        return LLMResponse(
            message=message, usage=usage, model=self.model, finish_reason=finish_reason
        )


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


def _parse_stream_chunk(payload: str):
    """Return (content_delta, reasoning_delta, usage_or_None, finish_reason_or_None)."""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return "", "", None, None
    choices = obj.get("choices") or []
    delta = ""
    rdelta = ""
    reason = None
    if choices:
        d = choices[0].get("delta") or {}
        delta = d.get("content") or ""
        rdelta = d.get("reasoning") or ""
        reason = choices[0].get("finish_reason")
    u = obj.get("usage")
    usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0)) if u else None
    return delta, rdelta, usage, reason


def chunk_tool_calls(payload: str) -> list[dict]:
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return []
    out = []
    for choice in obj.get("choices") or []:
        for tc in (choice.get("delta") or {}).get("tool_calls") or []:
            fn = tc.get("function") or {}
            out.append(
                {
                    "index": tc.get("index", 0),
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments"),
                }
            )
    return out


def _safe_json(s: str) -> dict:
    try:
        parsed = json.loads(s) if s else {}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    except json.JSONDecodeError:
        return {"_raw": s}


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def create_provider_from_config(cfg, api_key: str):
    """Factory: build the right provider from Config. cfg is nova.config.Config."""
    provider_name = cfg.get("llm.provider", "openrouter")
    if provider_name == "mock":
        from .mock import MockProvider

        return MockProvider()
    extra = None
    if provider_name == "openrouter":
        extra = {"HTTP-Referer": "https://github.com/nova-agent", "X-Title": "NovaAgent"}
    return OpenAICompatibleProvider(
        api_key=api_key,
        base_url=cfg.get("llm.base_url", "https://openrouter.ai/api/v1"),
        model=cfg.get("llm.model", "anthropic/claude-sonnet-4.5"),
        temperature=float(cfg.get("llm.temperature", 0.7)),
        max_tokens=int(cfg.get("llm.max_tokens", 4096)),
        extra_headers=extra,
    )
