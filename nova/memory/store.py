"""Persistent vector memory backed by SQLite + document chunking."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from nova.log import get_logger
from nova.memory.embeddings import HashEmbedder, cosine

log = get_logger("memory")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note',
    source TEXT,
    created_at REAL NOT NULL,
    dim INTEGER NOT NULL,
    embedding TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
"""


def chunk_text(text: str, max_chars: int = 800, overlap: int = 120) -> list[str]:
    """Split text into overlapping chunks on paragraph/sentence boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        while len(para) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:max_chars])
            para = para[max_chars - overlap :]
        if not buf:
            buf = para
        elif len(buf) + len(para) + 2 <= max_chars:
            buf += "\n\n" + para
        else:
            chunks.append(buf)
            tail = buf[-overlap:]
            buf = tail + "\n\n" + para
    if buf.strip():
        chunks.append(buf)
    return chunks


@dataclass
class MemoryRecord:
    id: int
    text: str
    kind: str
    source: str | None
    created_at: float
    score: float = 0.0


class MemoryStore:
    """Thread-safe SQLite-backed vector store using cosine similarity."""

    def __init__(self, db_path: str | Path, embedder=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder or HashEmbedder(512)
        # One persistent connection reused across calls (thread-safe via the
        # lock below) instead of opening/closing a new sqlite handle per call.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            # In-memory vector index: {id: (text, kind, source, created_at, emb)}.
            # Search runs entirely against this index so it never re-reads or
            # re-parses every row's JSON embedding on each query.
            self._index = self._load_index()

    def _acquire(self) -> sqlite3.Connection:
        """Context-style helper: returns the shared connection under the lock."""
        return self._conn

    def _load_index(self) -> dict[int, tuple[str, str, str | None, float, list[float]]]:
        idx: dict[int, tuple[str, str, str | None, float, list[float]]] = {}
        with self._acquire() as c:
            sql = "SELECT id, text, kind, source, created_at, embedding FROM memories"
            for row in c.execute(sql):
                idx[row["id"]] = (
                    row["text"],
                    row["kind"],
                    row["source"],
                    row["created_at"],
                    json.loads(row["embedding"]),
                )
        return idx

    def _count_db(self) -> int:
        """Fast DB-side row count, used as a staleness check for the index."""
        with self._acquire() as c:
            return int(c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])

    # ------------------------------------------------------------------ #

    def add(self, text: str, kind: str = "note", source: str | None = None) -> int:
        text = text.strip()
        if not text:
            raise ValueError("cannot store empty memory")
        emb = self.embedder.embed(text)
        created_at = time.time()
        with self._lock, self._acquire() as c:
            cur = c.execute(
                "INSERT INTO memories (text, kind, source, created_at, dim, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (text, kind, source, created_at, len(emb), json.dumps(emb)),
            )
            mid = int(cur.lastrowid)
            self._index[mid] = (text, kind, source, created_at, emb)
            log.debug("stored memory #%d (%s, %d chars)", mid, kind, len(text))
            return mid

    def add_document(self, text: str, source: str, max_chars: int = 800) -> int:
        """Chunk a document and store every chunk; returns number of chunks stored."""
        chunks = chunk_text(text, max_chars=max_chars)
        for ch in chunks:
            self.add(ch, kind="doc", source=source)
        return len(chunks)

    def search(self, query: str, top_k: int = 5, kind: str | None = None) -> list[MemoryRecord]:
        qemb = self.embedder.embed(query)
        scored: list[MemoryRecord] = []
        with self._lock:
            # cheap consistency guard: if another process/instance wrote to the
            # same DB file, our in-memory index is stale — reload it once.
            if self._count_db() != len(self._index):
                self._index = self._load_index()
            for mid, (text, mkind, source, created_at, emb) in self._index.items():
                if kind and mkind != kind:
                    continue
                score = cosine(qemb, emb)
                scored.append(
                    MemoryRecord(
                        id=mid,
                        text=text,
                        kind=mkind,
                        source=source,
                        created_at=created_at,
                        score=score,
                    )
                )
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    def delete(self, memory_id: int) -> bool:
        with self._lock, self._acquire() as c:
            cur = c.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            if cur.rowcount > 0:
                self._index.pop(memory_id, None)
                return True
            return False

    def count(self, kind: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM memories" + (" WHERE kind = ?" if kind else "")
        params = (kind,) if kind else ()
        with self._lock, self._acquire() as c:
            return int(c.execute(sql, params).fetchone()["n"])
