"""Tool dataclass and registry."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nova.log import get_logger

log = get_logger("tools")


@dataclass
class Tool:
    """A callable tool exposed to the LLM via function-calling."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments object
    handler: Callable[..., Awaitable[str]]
    danger_level: str = "safe"  # safe | cautious | dangerous

    def schema(self) -> dict[str, Any]:
        from nova.llm.base import make_tool_schema

        return make_tool_schema(self.name, self.description, self.parameters)

    async def run(self, args: dict[str, Any]) -> str:
        try:
            result = (
                await self.handler(**args)
                if _accepts_kwargs(self.handler)
                else await self.handler(args)
            )
            return result
        except Exception as exc:  # surface errors back to the model, never crash the loop
            log.warning("tool %s failed: %s", self.name, exc)
            return f"ERROR: {type(exc).__name__}: {exc}"


def _accepts_kwargs(handler: Callable) -> bool:
    sig = inspect.signature(handler)
    return all(p.kind != p.VAR_POSITIONAL for p in sig.parameters.values())


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    danger_level: str = "safe",
) -> Callable[[Callable[..., Awaitable[str]]], Tool]:
    """Decorator that wraps an async function into a Tool."""

    def deco(fn: Callable[..., Awaitable[str]]) -> Tool:
        return Tool(
            name=name,
            description=description,
            parameters=parameters,
            handler=fn,
            danger_level=danger_level,
        )

    return deco


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, *tools: Tool) -> None:
        for t in tools:
            if t.name in self.tools:
                raise ValueError(f"duplicate tool name: {t.name}")
            self.tools[t.name] = t
            log.debug("registered tool: %s", t.name)

    def unregister(self, *names: str) -> None:
        for n in names:
            self.tools.pop(n, None)

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        selected = [self.tools[n] for n in names] if names else list(self.tools.values())
        return [t.schema() for t in selected]

    def catalog(self) -> str:
        """Human-readable listing used inside system prompts."""
        lines = []
        for t in self.tools.values():
            params = ", ".join(t.parameters.get("properties", {}).keys())
            lines.append(f"- {t.name}({params}): {t.description}")
        return "\n".join(lines)

    async def execute(self, call) -> str:
        """Execute a nova.llm.base.ToolCall. Unknown tools yield an error message."""
        t = self.get(call.name)
        if t is None:
            return f"ERROR: unknown tool '{call.name}'. Available: {', '.join(self.tools)}"
        log.info(
            "executing tool %s(%s)",
            call.name,
            {k: (str(v)[:60]) for k, v in call.arguments.items()},
        )
        return await t.run(call.arguments)
