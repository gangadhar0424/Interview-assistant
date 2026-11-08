"""Phase 5: embedding abstraction, fake provider, factory and configuration (no database)."""

import math
import sys
import types
from pathlib import Path

import pytest

from app.core.config import ConfigurationError, Settings, load_settings
from app.embeddings.base import (
    EMBEDDING_DIMENSION,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingUnavailable,
)
from app.embeddings.factory import build_embedding_provider
from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.fastembed_provider import FastEmbedProvider


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # vectors are unit length


@pytest.fixture(autouse=True)
def _no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)


# ---------------------------------------------------------------- fake provider


def test_fake_provider_is_deterministic_normalised_and_correct_width() -> None:
    provider: EmbeddingProvider = FakeEmbeddingProvider()
    a = provider.embed_documents(["hello world", "नमस्ते"])
    b = FakeEmbeddingProvider().embed_documents(["hello world", "नमस्ते"])
    assert a == b
    assert len(a) == 2 and all(len(v) == EMBEDDING_DIMENSION for v in a)
    assert all(math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9) for v in a)
    assert provider.embed_query("hello world") == a[0]
    assert provider.model_id == "fake-hash-v1" and provider.dimension == EMBEDDING_DIMENSION


def test_fake_provider_similarity_reflects_shared_words() -> None:
    p = FakeEmbeddingProvider()
    python_doc, cooking_doc = p.embed_documents(
        ["python backend services with fastapi", "tomato pasta recipe with basil"]
    )
    q = p.embed_query("fastapi python backend")
    assert cosine(q, python_doc) > cosine(q, cooking_doc) + 0.3
    assert cosine(python_doc, python_doc) == pytest.approx(1.0)


def test_fake_provider_handles_empty_and_unicode_text() -> None:
    p = FakeEmbeddingProvider()
    vectors = p.embed_documents(["", "   ", "తెలుగు వచనం"])
    assert all(len(v) == EMBEDDING_DIMENSION for v in vectors)


def test_fake_provider_can_simulate_failures_and_records_calls() -> None:
    p = FakeEmbeddingProvider(error=EmbeddingUnavailable())
    with pytest.raises(EmbeddingUnavailable):
        p.embed_documents(["x"])
    with pytest.raises(EmbeddingUnavailable):
        p.embed_query("x")
    assert p.document_calls == [["x"]] and p.query_calls == ["x"]
    permanent = FakeEmbeddingProvider(error=EmbeddingError("embedding_failed", "bad"))
    with pytest.raises(EmbeddingError) as exc:
        permanent.embed_documents(["x"])
    assert not isinstance(exc.value, EmbeddingUnavailable)  # permanent vs transient is distinct


# -------------------------------------------------------------------- factory


def test_factory_uses_fake_by_default_locally_and_builds_fastembed_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "")
    local = load_settings()
    assert local.embedding_provider_name == "fake"
    assert isinstance(build_embedding_provider(local), FakeEmbeddingProvider)

    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    real = build_embedding_provider(load_settings())
    assert isinstance(real, FastEmbedProvider)
    assert real.model_id == "intfloat/multilingual-e5-large" and real.dimension == 1024
    assert real._model is None  # nothing is downloaded or loaded until first use


def test_real_provider_failures_map_to_safe_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FastEmbedProvider("some/model", None, 8)
    monkeypatch.setitem(sys.modules, "fastembed", None)  # simulates "not installed"
    with pytest.raises(EmbeddingError) as not_installed:
        provider.embed_documents(["x"])
    assert not_installed.value.code == "provider_not_installed"
    assert not isinstance(not_installed.value, EmbeddingUnavailable)

    broken = types.ModuleType("fastembed")

    class Exploding:
        def __init__(self, *_a: object, **_k: object) -> None:
            raise OSError("network down: https://secret.example/token")

    broken.TextEmbedding = Exploding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", broken)
    with pytest.raises(EmbeddingUnavailable) as unavailable:
        FastEmbedProvider("some/model", None, 8).embed_query("x")
    assert "secret" not in unavailable.value.message  # provider details never leak

    class Unknown:
        def __init__(self, *_a: object, **_k: object) -> None:
            raise ValueError("model is not supported")

    broken.TextEmbedding = Unknown  # type: ignore[attr-defined]
    with pytest.raises(EmbeddingError) as unknown:
        FastEmbedProvider("nope/model", None, 8).embed_query("x")
    assert unknown.value.code == "unknown_embedding_model"


def test_real_provider_rejects_wrong_width_output(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("fastembed")

    class Short:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def passage_embed(self, texts: list[str], **_k: object) -> list[list[float]]:
            return [[0.1, 0.2] for _ in texts]

    module.TextEmbedding = Short  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", module)
    with pytest.raises(EmbeddingError) as exc:
        FastEmbedProvider("m", None, 8).embed_documents(["a"])
    assert exc.value.code == "embedding_dimension_mismatch"


# ---------------------------------------------------------------- configuration


def test_defaults_are_documented_values() -> None:
    s = load_settings()
    assert (s.chunk_size_chars, s.chunk_overlap_chars) == (800, 100)
    assert s.embedding_model == "intfloat/multilingual-e5-large"
    assert s.embedding_batch_size == 16 and s.retrieval_max_limit == 20
    assert s.embedding_cache_dir is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CHUNK_SIZE_CHARS", "50"),
        ("CHUNK_SIZE_CHARS", "99999"),
        ("CHUNK_OVERLAP_CHARS", "-1"),
        ("CHUNK_OVERLAP_CHARS", "500"),  # more than half of the default 800
        ("EMBEDDING_BATCH_SIZE", "0"),
        ("EMBEDDING_PROVIDER", "openai"),
        ("RETRIEVAL_MAX_LIMIT", "0"),
    ],
)
def test_invalid_embedding_and_chunk_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert name in str(exc.value) or "CHUNK" in str(exc.value) or "EMBEDDING" in str(exc.value)


@pytest.mark.parametrize("provider", [None, "", "fake"])
def test_deployed_environments_must_use_a_real_embedding_provider(
    monkeypatch: pytest.MonkeyPatch, provider: str | None, tmp_path: Path
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "a-production-looking-secret-" + "x" * 30)
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    if provider is not None:
        monkeypatch.setenv("EMBEDDING_PROVIDER", provider)
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "EMBEDDING_PROVIDER" in str(exc.value)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    assert load_settings().embedding_provider_name == "fastembed"
