"""Shell execution tool with timeout, sandboxed cwd and output truncation."""

from __future__ import annotations

import asyncio
import shlex

from nova.log import get_logger
from nova.tools.base import tool

log = get_logger("shell")
MAX_OUTPUT = 15000

# Commands that would be catastrophic if the model ever emits them.
_DENYLIST_FRAGMENTS = (
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=/dev/zero", ":(){ :|:& };:",
    "shutdown", "reboot", "> /dev/sda",
)


class ShellTool:
    def __init__(self, cwd: str, timeout_seconds: int = 60):
        self.cwd = cwd
        self.timeout = timeout_seconds

    def register(self, registry) -> None:
        registry.register(tool(
            name="run_shell",
            description="Execute a shell command in the workspace directory and return "
                        "combined stdout+stderr. Use for git, grep, ls, builds, tests, etc.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer",
                                        "description": "Override timeout (max 300)"},
                },
                "required": ["command"],
            },
            danger_level="dangerous",
        )(self.run))

    async def run(self, command: str, timeout_seconds: int | None = None) -> str:
        lowered = command.lower()
        for frag in _DENYLIST_FRAGMENTS:
            if frag.lower() in lowered:
                log.warning("blocked dangerous command: %s", command[:100])
                return f"ERROR: command blocked for safety (matched '{frag}')"

        timeout = min(timeout_seconds or self.timeout, 300)
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            return f"ERROR: failed to spawn shell: {exc}"

        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"ERROR: timed out after {timeout}s"

        text = out.decode("utf-8", "replace")
        head, tail = text[:MAX_OUTPUT], ""
        if len(text) > MAX_OUTPUT:
            tail = f"\n...[{len(text) - MAX_OUTPUT} more chars truncated]..."
            head = text[: MAX_OUTPUT // 2] + "\n...\n" + text[-MAX_OUTPUT // 2:]
        status = "OK" if proc.returncode == 0 else f"exit {proc.returncode}"
        return f"[{status}] $ {shlex.quote(command)}\n{head}{tail or ''}".rstrip() \
            or f"[{status}] (no output)"
