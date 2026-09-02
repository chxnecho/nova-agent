"""Filesystem tools, sandboxed to a workspace root directory."""

from __future__ import annotations

import os
from pathlib import Path

from nova.tools.base import tool


class FilesystemTools:
    def __init__(self, workspace_root: str | Path):
        self.root = Path(workspace_root).resolve()

    def _resolve(self, path: str) -> Path:
        p = (self.root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        if not (p == self.root or self.root in p.parents):
            raise PermissionError(f"path escapes workspace sandbox: {path}")
        return p

    def register(self, registry) -> None:
        registry.register(
            tool(
                name="read_file",
                description="Read a text file inside the workspace. "
                "Optionally read only a line range (1-based, inclusive).",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path in workspace"},
                        "start_line": {"type": "integer", "description": "First line to read"},
                        "end_line": {"type": "integer", "description": "Last line to read"},
                    },
                    "required": ["path"],
                },
            )(self.read_file),
            tool(
                name="write_file",
                description="Create or overwrite a text file with the given content. "
                "Parent directories are created automatically.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                danger_level="cautious",
            )(self.write_file),
            tool(
                name="edit_file",
                description="Replace an exact old_text snippet with new_text in a file. "
                "Fails if old_text is not found or is not unique.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
                danger_level="cautious",
            )(self.edit_file),
            tool(
                name="list_dir",
                description="List files and directories under a path in the workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Defaults to workspace root"},
                    },
                },
            )(self.list_dir),
        )

    # ------------------------------------------------------------------ #

    async def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        p = self._resolve(path)
        if start_line or end_line:
            # line-range read: load fully only when the requested range is set
            data = p.read_text(encoding="utf-8")
            lines = data.splitlines()
            s = max((start_line or 1) - 1, 0)
            e = end_line or len(lines)
            return "\n".join(lines[s:e])
        # stream up to the cap without loading the whole file into memory
        cap = 50000
        chunks = []
        total = 0
        with p.open(encoding="utf-8", errors="replace") as f:
            while True:
                block = f.read(65536)
                if not block:
                    break
                chunks.append(block)
                total += len(block)
                if total >= cap:
                    break
        return "".join(chunks)[:cap]

    async def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} chars to {p.relative_to(self.root)}"

    async def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self._resolve(path)
        content = p.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            return f"ERROR: old_text not found in {path}"
        if count > 1:
            return f"ERROR: old_text appears {count} times; make it unique"
        p.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"OK: edited {path}"

    async def list_dir(self, path: str = ".") -> str:
        p = self._resolve(path)
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
        out = []
        for e in entries[:200]:
            tag = "d" if e.is_dir() else "-"
            size = e.stat().st_size if e.is_file() else ""
            out.append(f"{tag} {e.name}{'/' if e.is_dir() else ''} {size}")
        return "\n".join(out) or "(empty)"
