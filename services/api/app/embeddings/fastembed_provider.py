"""Local multilingual embeddings via fastembed (ONNX runtime, no PyTorch, no external API).

Optional: install with `uv sync --extra embeddings`. The model is downloaded from Hugging Face on
first use (intfloat/multilingual-e5-large is ~2.2 GB) into `EMBEDDING_CACHE_DIR` (or fastembed's
default temp cache). User text never leaves the server. E5 models need different prefixes for
queries and passages; fastembed's `query_embed` / `passage_embed` apply them.
"""

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.embeddings.base import EMBEDDING_DIMENSION, EmbeddingError, EmbeddingUnavailable

logger = logging.getLogger(__name__)


class FastEmbedProvider:
    def __init__(self, model_name: str, cache_dir: Path | None, batch_size: int) -> None:
        self.model_id = model_name
        self.dimension = EMBEDDING_DIMENSION
        self._cache_dir = cache_dir
        self._batch_size = batch_size
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                try:
                    from fastembed import TextEmbedding
                except ImportError:
                    raise EmbeddingError(
                        "provider_not_installed",
                        "The embedding library is not installed on the worker.",
                    ) from None
                try:
                    self._model = TextEmbedding(
                        self.model_id,
                        cache_dir=str(self._cache_dir) if self._cache_dir else None,
                    )
                except ValueError:
                    raise EmbeddingError(
                        "unknown_embedding_model",
                        "The configured embedding model is not supported.",
                    ) from None
                except Exception:
                    logger.exception("loading embedding model failed")
                    raise EmbeddingUnavailable() from None  # e.g. download/network problem
            return self._model

    def _check(self, vectors: list[list[float]], expected: int) -> list[list[float]]:
        if len(vectors) != expected or any(len(v) != self.dimension for v in vectors):
            raise EmbeddingError(
                "embedding_dimension_mismatch", "The embedding model returned unexpected output."
            )
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        try:
            vectors = [
                [float(x) for x in v]
                for v in model.passage_embed(list(texts), batch_size=self._batch_size)
            ]
        except Exception:
            logger.exception("embedding documents failed")
            raise EmbeddingUnavailable() from None
        return self._check(vectors, len(texts))

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        try:
            vectors = [[float(x) for x in v] for v in model.query_embed([text])]
        except Exception:
            logger.exception("embedding query failed")
            raise EmbeddingUnavailable() from None
        return self._check(vectors, 1)[0]
