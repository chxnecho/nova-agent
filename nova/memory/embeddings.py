"""Local deterministic embeddings (feature hashing) — no network, no dependencies.

Quality is below neural embeddings but sufficient for semantic-ish recall of
notes/documents, keeps NovaAgent fully self-contained, and the interface matches
what an API-backed embedder would expose.
"""

from __future__ import annotations

import hashlib
import math
import re


class HashEmbedder:
    """Feature-hashing bag-of-features embedder with L2 normalization.

    Features: word unigrams (w=1.0), adjacent-bigrams (w=0.6),
    CJK char bigrams (w=0.8). Each feature is double-hashed into `dim`
    buckets with signed hashing to reduce collision bias.
    """

    def __init__(self, dim: int = 512):
        if dim < 64:
            raise ValueError("dim must be >= 64")
        self.dim = dim

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", text.lower())

    def _bucket(self, feature: str) -> tuple[int, float]:
        h = hashlib.md5(feature.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "little") % self.dim
        sign = 1.0 if h[4] % 2 == 0 else -1.0
        return idx, sign

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = self.tokenize(text)

        features: list[tuple[str, float]] = []
        for t in tokens:
            features.append((f"u:{t}", 1.0))
        for a, b in zip(tokens, tokens[1:]):
            features.append((f"b:{a}|{b}", 0.6))

        # CJK char bigrams (tokens list already splits CJK into chars)
        cjk = [t for t in tokens if "\u4e00" <= t <= "\u9fff"]
        for a, b in zip(cjk, cjk[1:]):
            features.append((f"c:{a}{b}", 0.8))

        for f, w in features:
            idx, sign = self._bucket(f)
            vec[idx] += sign * w

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return num / (na * nb)
