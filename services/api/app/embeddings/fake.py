"""Deterministic fake embeddings for tests and local development (no model, no network).

A hashed bag-of-words: each (lower-cased) word contributes to a few fixed buckets, then the vector
is L2-normalised. Texts that share words are closer under cosine similarity, which makes retrieval
tests meaningful and reproducible. It has NO real semantic understanding; never use it in
staging/production (configuration rejects that).
"""

import hashlib
import math
import re
from collections.abc import Sequence

from app.embeddings.base import EMBEDDING_DIMENSION, EmbeddingError

_WORD = re.compile(r"\w+", re.UNICODE)
_BUCKETS_PER_WORD = 3


class FakeEmbeddingProvider:
    def __init__(
        self,
        model_id: str = "fake-hash-v1",
        dimension: int = EMBEDDING_DIMENSION,
        error: EmbeddingError | None = None,
    ) -> None:
        self.model_id = model_id
        self.dimension = dimension
        self.error = error  # set to simulate a provider failure
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        words = _WORD.findall(text.lower()) or [""]
        for word in words:
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            for i in range(_BUCKETS_PER_WORD):
                index = int.from_bytes(digest[i * 4 : i * 4 + 4], "big") % self.dimension
                vector[index] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        if self.error is not None:
            raise self.error
        return self._vector(text)
