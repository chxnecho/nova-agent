"""Mock provider for offline tests and development without an API key."""

from __future__ import annotations

from typing import Any

from .base import LLMResponse, Message, Usage


class MockProvider:
    """Returns scripted responses. Useful for unit tests of the agent kernel.

    You can queue responses:  mock.enqueue(LLMResponse(...)) or let it echo.
    """

    def __init__(self, model: str = "mock-model"):
        self.model = model
        self.total_usage = Usage()
        self._queue: list[LLMResponse] = []
        self.calls: list[list[Message]] = []

    def enqueue(self, *responses: LLMResponse) -> "MockProvider":
        self._queue.extend(responses)
        return self

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> LLMResponse:
        self.calls.append(messages)
        if self._queue:
            resp = self._queue.pop(0)
        else:
            content = f"[mock] Received {len(messages)} messages."
            if messages and messages[-1].content:
                content = f"[mock] You said: {messages[-1].content[:200]}"
            resp = LLMResponse(
                message=Message(role="assistant", content=content),
                usage=Usage(10, 5),
                model=self.model,
                finish_reason="stop",
            )
        self.total_usage = self.total_usage + resp.usage
        return resp
