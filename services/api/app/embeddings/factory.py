from functools import lru_cache
from pathlib import Path

from app.core.config import Settings
from app.embeddings.base import EmbeddingProvider
from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.fastembed_provider import FastEmbedProvider


@lru_cache
def _fastembed(model: str, cache_dir: Path | None, batch_size: int) -> FastEmbedProvider:
    return FastEmbedProvider(model, cache_dir, batch_size)  # one loaded model per process


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Create the configured provider. Credentials/keys never exist client-side."""
    if settings.embedding_provider_name == "fastembed":
        return _fastembed(
            settings.embedding_model, settings.embedding_cache_dir, settings.embedding_batch_size
        )
    return FakeEmbeddingProvider()
