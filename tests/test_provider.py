"""Offline tests for the OpenAI-compatible provider (httpx.MockTransport).

Covers the network-bound paths that the agent/kernel tests skip via
MockProvider: non-streaming parsing, tool-call parsing, exponential-backoff
retry, SSE streaming accumulation and usage accounting.
"""

import asyncio
import json

import httpx
from nova.llm.base import Message
from nova.llm.provider import OpenAICompatibleProvider


def make_transport(handler):
    return httpx.MockTransport(handler)


def provider_with(handler, **kwargs):
    client = httpx.AsyncClient(
        transport=make_transport(handler), base_url="https://openrouter.ai/api/v1"
    )
    return OpenAICompatibleProvider(api_key="test-key", model="test-model", client=client, **kwargs)


def test_nonstreaming_text_response():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello world"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    provider = provider_with(handler)
    resp = asyncio.run(provider.chat([Message(role="user", content="hi")]))
    assert resp.message.content == "Hello world"
    assert resp.message.tool_calls == []
    assert resp.usage.prompt_tokens == 12
    assert provider.total_usage.completion_tokens == 5


def test_nonstreaming_tool_call_parsing():
    def handler(request):
        args = json.dumps({"city": "beijing"})
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "get_weather", "arguments": args},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    provider = provider_with(handler)
    resp = asyncio.run(provider.chat([Message(role="user", content="weather?")]))
    assert len(resp.message.tool_calls) == 1
    tc = resp.message.tool_calls[0]
    assert tc.name == "get_weather"
    assert tc.arguments == {"city": "beijing"}


def test_retry_on_429_then_success(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    import nova.llm.provider as provider_mod

    async def noop_sleep(_secs):
        return None

    monkeypatch.setattr(provider_mod, "_sleep", noop_sleep)

    provider = provider_with(handler, max_retries=2)
    resp = asyncio.run(provider.chat([Message(role="user", content="hi")]))
    assert resp.message.content == "ok"
    assert calls["n"] == 2


def test_streaming_accumulates_content_and_usage():
    sse = (
        'data: {"id":"x","object":"chat.completion.chunk","model":"m","choices":'
        '[{"index":0,"delta":{"role":"assistant","content":"Hel"},"finish_reason":null}]}\n\n'
        'data: {"id":"x","object":"chat.completion.chunk","model":"m","choices":'
        '[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
        'data: {"id":"x","object":"chat.completion.chunk","model":"m","choices":'
        '[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: {"id":"x","object":"chat.completion.chunk","model":"m","choices":[],'
        '"usage":{"prompt_tokens":9,"completion_tokens":3}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    provider = provider_with(handler)
    chunks = []
    resp = asyncio.run(
        provider.chat([Message(role="user", content="hi")], stream_callback=chunks.append)
    )
    assert "".join(chunks) == "Hello"
    assert resp.message.content == "Hello"
    assert resp.finish_reason == "stop"
    assert provider.total_usage.total_tokens == 12
