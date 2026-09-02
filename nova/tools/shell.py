"""Shell execution tool with timeout, sandboxed cwd and output truncation.

Safety: a denylist of catastrophic commands (matched after whitespace
normalization) plus a guard that rejects attempts to WRITE to an absolute
path outside the workspace root. Neither is a true sandbox — read access to
system files and many evasions remain possible — so treat this as the first
line of defense only.
"""

from __future__ import annotations

import asyncio
import os
import shlex

from nova.log import get_logger
from nova.tools.base import tool

log = get_logger("shell")
MAX_OUTPUT = 15000

# Fragments that would be catastrophic if the model ever emits them. Matched
# against a whitespace-normalized copy of the command, so spacing tricks like
# `rm -rf  /` don't bypass it.
_DENYLIST = (
    "rm -rf /",
    "rm -rf /*",
    "rm -fr /",
    "rm -fr /*",
    "mkfs",
    "dd if=/dev/zero",
    "shutdown",
    "reboot",
    "> /dev/sd",
    "> /dev/mem",
    ">/dev/sd",
    ">/dev/mem",
    "chmod -r 777 /",
    "chmod 777 /",
    "chmod -r 777 /*",
    ">" + " /etc/passwd",
    ">" + " /etc/shadow",
    ">" + " /etc/hosts",
    "fdisk",
    "parted",
    "pvcreate",
    "lvremove",
    "mkfs.xfs",
    "mkfs.ext4",
)


def _normalize(command: str) -> str:
    return " ".join(command.split())


_WRITE_VERBS = {
    "rm",
    "mv",
    "cp",
    "touch",
    "mkdir",
    "truncate",
    "dd",
    "unlink",
    "ln",
    "chmod",
    "chown",
    "tee",
}
_WRITE_REDIRECT = {">", ">>", "&>", "&>>"}


def _is_outside(target: str, root: str) -> bool:
    p = os.path.normpath(os.path.abspath(target))
    r = os.path.normpath(os.path.abspath(root))
    if p == r:
        return False  # points at the root itself: inside
    try:
        return os.path.commonpath([p, r]) != r
    except ValueError:  # different prefixes/drives
        return True


def _command_escapes_sandbox(command: str, root: str) -> bool:
    """True when the command writes to an absolute path outside `root`."""
    try:
        toks = shlex.split(command)
    except ValueError:  # unparseable -> let the shell decide
        return False
    for i, t in enumerate(toks):
        if (t in _WRITE_REDIRECT and i + 1 < len(toks)) or (
            t in _WRITE_VERBS and i + 1 < len(toks)
        ):
            nxt = toks[i + 1]
            if nxt.startswith("/") and _is_outside(nxt, root):
                return True
    return False


class ShellTool:
    def __init__(self, cwd: str, timeout_seconds: int = 60, workspace_root: str | None = None):
        self.cwd = cwd
        self.timeout = timeout_seconds
        self.workspace_root = workspace_root or cwd

    def register(self, registry) -> None:
        registry.register(
            tool(
                name="run_shell",
                description="Execute a shell command in the workspace directory and return "
                "combined stdout+stderr. Use for git, grep, ls, builds, tests, etc.\n"
                "Writes to absolute paths outside the workspace are blocked.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "Override timeout (max 300)",
                        },
                    },
                    "required": ["command"],
                },
                danger_level="dangerous",
            )(self.run)
        )

    async def run(self, command: str, timeout_seconds: int | None = None) -> str:
        norm = _normalize(command)
        for frag in _DENYLIST:
            if frag in norm:
                log.warning("blocked dangerous command: %s", command[:100])
                return f"ERROR: command blocked for safety (matched '{frag}')"

        if _command_escapes_sandbox(command, self.workspace_root):
            log.warning("blocked write outside sandbox: %s", command[:100])
            return (
                f"ERROR: command attempts to write outside the workspace "
                f"sandbox ({self.workspace_root})"
            )

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
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return f"ERROR: timed out after {timeout}s"

        text = out.decode("utf-8", "replace")
        head, tail = text[:MAX_OUTPUT], ""
        if len(text) > MAX_OUTPUT:
            tail = f"\n...[{len(text) - MAX_OUTPUT} more chars truncated]..."
            head = text[: MAX_OUTPUT // 2] + "\n...\n" + text[-MAX_OUTPUT // 2 :]
        status = "OK" if proc.returncode == 0 else f"exit {proc.returncode}"
        return (
            f"[{status}] $ {shlex.quote(command)}\n{head}{tail or ''}".rstrip()
            or f"[{status}] (no output)"
        )
