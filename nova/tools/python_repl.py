"""Python REPL tool: executes code in an isolated subprocess with a fresh interpreter."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from nova.tools.base import tool

MAX_OUTPUT = 15000

_BOOTSTRAP = """
import sys, json
class _Capture:
    def __init__(self, stream): self.stream = stream
    def write(self, s): self.stream.write(s); return len(s)
    def flush(self): self.stream.flush()
sys.stdout = _Capture(sys.stdout)
sys.stderr = _Capture(sys.stderr)
"""


class PythonReplTool:
    def __init__(self, cwd: str, timeout_seconds: int = 30):
        self.cwd = cwd
        self.timeout = timeout_seconds

    def register(self, registry) -> None:
        registry.register(tool(
            name="python_repl",
            description="Run Python code in an isolated interpreter and get stdout/stderr. "
                        "Use `print()` to see results. The last expression's value is NOT "
                        "shown automatically. For long-running or blocking work prefer run_shell.",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source code to execute"},
                },
                "required": ["code"],
            },
            danger_level="dangerous",
        )(self.run_code))

    async def run_code(self, code: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write(_BOOTSTRAP + "\n" + code)
            path = f.name
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", path,
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"ERROR: code timed out after {self.timeout}s"
        finally:
            Path(path).unlink(missing_ok=True)

        text = out.decode("utf-8", "replace")[:MAX_OUTPUT]
        status = "OK" if proc.returncode == 0 else f"exit {proc.returncode}"
        return f"[{status}]\n{text or '(no output)'}"
