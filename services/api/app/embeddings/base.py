"""Embedding provider abstraction (docs/DECISIONS.md D-30).

Business logic and the database depend only on this interface, never on a specific provider or
library. Providers are synchronous (callers run them in a worker thread). Every stored vector
records `model_id`, so models can be replaced or added later without ambiguity.
"""

from collections.abc import Sequence
from typing import Protocol

# Width of the pgvector column (`chunk_embeddings.embedding`). It matches the selected model
# (intfloat/multilingual-e5-large). A model with a different width needs its own embeddings
# table (D-16); providers must return exactly this many dimensions or the job fails safely.
EMBEDDING_DIMENSION = 1024


class EmbeddingError(Exception):
    """Permanent failure. `code`/`message` are fixed, user-safe strings (no provider details)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class EmbeddingUnavailable(EmbeddingError):
    """Transient failure (provider/model unreachable, timeout). The job is retried later."""

    def __init__(self) -> None:
        super().__init__("embedding_unavailable", "The embedding service is unavailable.")


class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed passages (document chunks). One vector per text, in order."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (some models use a different prefix for queries)."""
        ...
