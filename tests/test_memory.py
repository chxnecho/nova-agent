import pytest

from nova.memory.embeddings import HashEmbedder, cosine
from nova.memory.store import MemoryStore, chunk_text


def test_embedder_deterministic_and_normalized():
    e = HashEmbedder(256)
    v1 = e.embed("hello world of agents")
    v2 = e.embed("hello world of agents")
    assert v1 == v2
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_similarity_ordering():
    e = HashEmbedder(512)
    q = "how to fix a failing unit test"
    good = "when a unit test fails, read the traceback and fix the code"
    bad = "cooking pasta requires boiling water and adding salt"
    assert cosine(e.embed(q), e.embed(good)) > cosine(e.embed(q), e.embed(bad))


def test_chunking():
    text = "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(5))
    chunks = chunk_text(text, max_chars=800, overlap=100)
    assert len(chunks) > 2
    assert all(len(c) <= 800 for c in chunks)
    # no content lost: every paragraph marker appears somewhere
    joined = "".join(chunks)
    for i in range(5):
        assert f"paragraph {i}" in joined


def test_store_roundtrip(tmp_path):
    store = MemoryStore(tmp_path / "mem.sqlite3", embedder=HashEmbedder(256))
    id1 = store.add("the deploy server is 10.0.0.42", kind="fact")
    store.add("agent prefers python3 over python", kind="lesson")
    assert store.count() == 2

    hits = store.search("what is the deploy server address", top_k=1)
    assert hits[0].id == id1
    assert "10.0.0.42" in hits[0].text

    assert store.delete(id1) is True
    assert store.delete(id1) is False
    assert store.count() == 1


def test_add_document(tmp_path):
    store = MemoryStore(tmp_path / "mem.sqlite3", embedder=HashEmbedder(256))
    text = "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(5))
    n = store.add_document(text, source="notes.md")
    assert n > 2
    assert store.count(kind="doc") == n

    with pytest.raises(ValueError):
        store.add("   ")
