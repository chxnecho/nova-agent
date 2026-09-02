"""Memory tools: let the agent persist and recall knowledge across sessions."""

from __future__ import annotations

from pathlib import Path

from nova.log import get_logger
from nova.memory.store import MemoryStore
from nova.tools.base import tool

log = get_logger("memory_tools")


class MemoryTools:
    def __init__(self, store: MemoryStore, workspace: str | Path = "."):
        self.store = store
        self.workspace = Path(workspace).resolve()

    def register(self, registry) -> None:
        registry.register(
            tool(
                name="remember",
                description="Persist an important fact, decision or lesson to long-term "
                "memory so you (or a future session) can recall it later.",
                parameters={
                    "type": "object",
                    "properties": {
                        "note": {"type": "string", "description": "The fact to remember"},
                        "kind": {
                            "type": "string",
                            "description": "Category: note | fact | decision | lesson",
                        },
                    },
                    "required": ["note"],
                },
            )(self.remember),
            tool(
                name="recall",
                description="Search long-term memory for previously stored notes/facts "
                "relevant to a query. Returns the most similar entries.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "description": "Max results (default 5)"},
                    },
                    "required": ["query"],
                },
            )(self.recall),
            tool(
                name="ingest_document",
                description="Read a text/markdown/code file in the workspace, chunk it and "
                "index it into long-term memory for later semantic search.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path in workspace"},
                    },
                    "required": ["path"],
                },
            )(self.ingest_document),
            tool(
                name="search_knowledge",
                description="Semantic search over all ingested documents in memory. "
                "Use to answer questions grounded in previously indexed files.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "description": "Max results (default 6)"},
                    },
                    "required": ["query"],
                },
            )(self.search_knowledge),
        )

    # ------------------------------------------------------------------ #

    async def remember(self, note: str, kind: str = "note") -> str:
        mid = self.store.add(note, kind=kind or "note", source="agent")
        return f"OK: stored as memory #{mid} (kind={kind})"

    async def recall(self, query: str, top_k: int | None = None) -> str:
        records = self.store.search(query, top_k=top_k or 5)
        if not records:
            return "(no memories found)"
        lines = [f"[{r.id}] (score={r.score:.3f}, kind={r.kind}) {r.text[:400]}" for r in records]
        return "\n---\n".join(lines)

    async def ingest_document(self, path: str) -> str:
        p = (self.workspace / path).resolve()
        if self.workspace not in p.parents and p != self.workspace:
            return f"ERROR: path escapes workspace sandbox: {path}"
        if not p.is_file():
            return f"ERROR: not a file: {path}"
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"ERROR: {path} is not a UTF-8 text file"
        n = self.store.add_document(text, source=str(path))
        return f"OK: indexed '{path}' into {n} chunks (total memories: {self.store.count()})"

    async def search_knowledge(self, query: str, top_k: int | None = None) -> str:
        records = self.store.search(query, top_k=top_k or 6)
        if not records:
            return "(no matching documents; use ingest_document first)"
        lines = []
        for r in records:
            src = f", from {r.source}" if r.source else ""
            lines.append(f"[score={r.score:.3f}{src}]\n{r.text[:600]}")
        return "\n=====\n".join(lines)
