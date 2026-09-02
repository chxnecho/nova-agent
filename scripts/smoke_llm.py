#!/usr/bin/env python3
"""Smoke test for the LLM layer: non-streaming, streaming and tool calling.

Usage: .venv/bin/python scripts/smoke_llm.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova.config import api_key_for, load_config
from nova.llm.base import Message, make_tool_schema
from nova.llm.provider import create_provider_from_config


async def main() -> None:
    cfg = load_config()
    provider = create_provider_from_config(cfg, api_key_for(cfg))
    print(f"[1] model = {provider.model}")

    # --- 1. plain completion ---
    resp = await provider.chat([Message(role="user", content="用一句话回答:1+1等于几?")])
    print(f"[2] plain      -> {resp.message.content!r}  usage={resp.usage.total_tokens}t")
    assert resp.message.content, "empty response"

    # --- 2. streaming ---
    chunks: list[str] = []
    resp = await provider.chat(
        [Message(role="user", content="从1数到5,只用数字和逗号。")],
        stream_callback=chunks.append,
    )
    streamed_text = "".join(chunks)
    print(f"[3] streaming  -> {streamed_text!r} ({len(chunks)} chunks)")
    assert "3" in streamed_text

    # --- 3. tool calling ---
    tools = [
        make_tool_schema(
            name="get_weather",
            description="Get current weather for a city.",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        )
    ]
    resp = await provider.chat(
        [Message(role="user", content="北京今天天气怎么样?请调用工具查询。")],
        tools=tools,
    )
    print(f"[4] tool call  -> {[(tc.name, tc.arguments) for tc in resp.message.tool_calls]}")
    assert resp.message.tool_calls and resp.message.tool_calls[0].name == "get_weather"
    assert "city" in resp.message.tool_calls[0].arguments

    await provider.aclose()
    print(f"\nALL SMOKE TESTS PASSED  | total usage: {provider.total_usage.total_tokens} tokens")


if __name__ == "__main__":
    asyncio.run(main())
