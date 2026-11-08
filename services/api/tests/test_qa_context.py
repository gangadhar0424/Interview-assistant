"""Context assembly (Phase 8A): quota merge, character budget, ordering, settings."""

import dataclasses
import uuid
from typing import Any

import pytest

from app.core.config import ConfigurationError, Settings, load_settings
from app.embeddings.fake import FakeEmbeddingProvider
from app.qa.context import (
    MIN_PARTIAL_PASSAGE_CHARS,
    ContextConfig,
    _CachedQueryEmbedder,
    apply_budget,
    merge_hits,
    order_for_prompt,
    truncate,
)
from app.retrieval.service import ChunkHit


def hit(
    name: str, similarity: float, text: str = "x", source: uuid.UUID | None = None, index: int = 0
) -> ChunkHit:
    return ChunkHit(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        source_type="document",
        source_id=source or uuid.uuid5(uuid.NAMESPACE_URL, "source-" + name),
        chunk_index=index,
        text=text,
        similarity=similarity,
    )


# ------------------------------------------------------------------- merge


def test_resume_quota_comes_first_even_when_documents_rank_higher() -> None:
    resume = [hit("r1", 0.2), hit("r2", 0.1)]
    docs = [hit(f"d{i}", 0.9 - i / 100) for i in range(10)]
    merged = merge_hits(resume, docs, top_k=8)
    assert [h.chunk_id for h in merged[:2]] == [h.chunk_id for h in resume]
    assert len(merged) == 8


def test_merge_never_exceeds_top_k_and_never_duplicates() -> None:
    shared = hit("shared", 0.8)
    merged = merge_hits(
        [shared, hit("r2", 0.1)], [shared, *[hit(f"d{i}", 0.5) for i in range(20)]], 5
    )
    assert len(merged) == 5
    assert len({h.chunk_id for h in merged}) == 5
    assert merged[0] == shared


def test_merge_with_fewer_hits_than_top_k_returns_them_all() -> None:
    assert len(merge_hits([hit("r", 0.1)], [hit("d", 0.2)], top_k=8)) == 2
    assert merge_hits([], [], top_k=8) == []


def test_quota_larger_than_available_resume_hits_is_fine() -> None:
    docs = [hit(f"d{i}", 0.5) for i in range(3)]
    assert len(merge_hits([hit("r", 0.1)], docs, top_k=8)) == 4


# ------------------------------------------------------------------ budget


def test_budget_keeps_hits_that_fit_and_never_exceeds_the_limit() -> None:
    hits = [hit(f"h{i}", 0.9, text="a" * 300) for i in range(5)]
    kept = apply_budget(hits, max_chars=1000)
    assert sum(len(h.text) for h in kept) <= 1000
    assert [len(h.text) for h in kept] == [300, 300, 300, 100] or len(kept) == 3


def test_budget_boundaries_are_exact() -> None:
    hits = [hit("a", 0.9, text="a" * 500), hit("b", 0.8, text="b" * 500)]
    assert [len(h.text) for h in apply_budget(hits, 1000)] == [500, 500]  # exactly fits
    kept = apply_budget(hits, 999)  # one character short: the second is truncated, not dropped
    assert [len(h.text) for h in kept] == [500, 499]


def test_a_passage_with_too_little_room_is_dropped_and_selection_stops() -> None:
    room = MIN_PARTIAL_PASSAGE_CHARS - 1
    hits = [hit("a", 0.9, text="a" * 500), hit("b", 0.8, text="b" * 500), hit("c", 0.7, text="c")]
    kept = apply_budget(hits, 500 + room)
    assert [h.text[0] for h in kept] == ["a"]  # not even the tiny third one sneaks in


def test_the_first_passage_alone_can_be_truncated_to_the_budget() -> None:
    kept = apply_budget([hit("a", 0.9, text="a" * 5000)], 800)
    assert len(kept) == 1 and len(kept[0].text) == 800


def test_priority_order_decides_who_survives_a_tight_budget() -> None:
    resume = hit("resume", 0.1, text="r" * 400)
    doc = hit("doc", 0.99, text="d" * 400)
    kept = apply_budget(merge_hits([resume], [doc], top_k=8), 450)
    assert kept[0].text[0] == "r"  # the resume quota is served before higher-similarity documents


def test_budget_of_zero_room_returns_nothing() -> None:
    assert apply_budget([hit("a", 0.9, text="abc")], 0) == []


def test_truncated_hits_keep_their_identity() -> None:
    original = hit("a", 0.9, text="a" * 1000)
    (kept,) = apply_budget([original], 300)
    assert kept.chunk_id == original.chunk_id and kept.source_id == original.source_id
    assert dataclasses.replace(kept, text=original.text) == original


# ---------------------------------------------------------------- ordering


def test_prompt_order_is_most_similar_first_with_deterministic_ties() -> None:
    source = uuid.uuid4()
    low, high = hit("low", 0.1), hit("high", 0.9)
    tie_b = hit("tie-b", 0.5, source=source, index=2)
    tie_a = hit("tie-a", 0.5, source=source, index=1)
    ordered = order_for_prompt([low, tie_b, high, tie_a])
    assert [h.chunk_id for h in ordered] == [
        high.chunk_id,
        tie_a.chunk_id,
        tie_b.chunk_id,
        low.chunk_id,
    ]
    assert order_for_prompt(list(reversed(ordered))) == ordered


def test_truncate_is_by_characters_and_idempotent() -> None:
    assert truncate("abcdef", 3) == "abc" and truncate("abc", 3) == "abc"
    text = "తెలుగు " * 10
    assert truncate(truncate(text, 20), 20) == truncate(text, 20)


# --------------------------------------------------------- query embedding


def test_the_query_is_embedded_once_for_both_searches() -> None:
    inner = FakeEmbeddingProvider()
    cached = _CachedQueryEmbedder(inner)
    first, second = cached.embed_query("question"), cached.embed_query("question")
    assert first == second and inner.query_calls == ["question"]
    cached.embed_query("another")
    assert inner.query_calls == ["question", "another"]
    assert cached.model_id == inner.model_id and cached.dimension == inner.dimension


# ---------------------------------------------------------------- settings


@pytest.fixture
def no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.mark.usefixtures("no_env_file")
def test_defaults_match_the_approved_decisions() -> None:
    s = load_settings()
    assert (s.qa_retrieval_top_k, s.qa_resume_quota, s.qa_history_pairs) == (8, 3, 3)
    assert s.qa_context_max_chars == 6000
    cfg = ContextConfig.from_settings(s)
    assert (cfg.top_k, cfg.resume_quota, cfg.history_pairs, cfg.max_chars) == (8, 3, 3, 6000)


@pytest.mark.usefixtures("no_env_file")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("QA_RETRIEVAL_TOP_K", "0"),
        ("QA_RETRIEVAL_TOP_K", "21"),
        ("QA_RESUME_QUOTA", "-1"),
        ("QA_RESUME_QUOTA", "9"),  # quota above the default top-k of 8
        ("QA_CONTEXT_MAX_CHARS", "100"),
        ("QA_HISTORY_PAIRS", "11"),
        ("QA_HISTORY_ITEM_MAX_CHARS", "10"),
    ],
)
def test_invalid_context_settings_fail_clearly(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "QA_" in str(exc.value)


@pytest.mark.usefixtures("no_env_file")
def test_top_k_may_not_exceed_the_retrieval_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL_MAX_LIMIT", "5")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "QA_RETRIEVAL_TOP_K" in str(exc.value)
    monkeypatch.setenv("QA_RETRIEVAL_TOP_K", "5")
    monkeypatch.setenv("QA_RESUME_QUOTA", "2")
    assert load_settings().qa_retrieval_top_k == 5


@pytest.mark.usefixtures("no_env_file")
def test_settings_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_RETRIEVAL_TOP_K", "6")
    monkeypatch.setenv("QA_RESUME_QUOTA", "2")
    monkeypatch.setenv("QA_CONTEXT_MAX_CHARS", "2500")
    monkeypatch.setenv("QA_HISTORY_PAIRS", "1")
    cfg: Any = ContextConfig.from_settings(load_settings())
    assert (cfg.top_k, cfg.resume_quota, cfg.max_chars, cfg.history_pairs) == (6, 2, 2500, 1)


# ---------------------------------------------------- Phase 8B API settings


@pytest.mark.usefixtures("no_env_file")
def test_api_protection_defaults_match_the_approved_values() -> None:
    s = load_settings()
    assert s.qa_rate_limit_per_minute == 20 and s.qa_max_concurrent_generations == 2
    assert s.qa_request_timeout_seconds == 120.0


@pytest.mark.usefixtures("no_env_file")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("QA_RATE_LIMIT_PER_MINUTE", "0"),
        ("QA_RATE_LIMIT_PER_MINUTE", "601"),
        ("QA_MAX_CONCURRENT_GENERATIONS", "0"),
        ("QA_MAX_CONCURRENT_GENERATIONS", "21"),
        ("QA_REQUEST_TIMEOUT_SECONDS", "0.5"),
        ("QA_REQUEST_TIMEOUT_SECONDS", "901"),
    ],
)
def test_invalid_api_protection_settings_fail_clearly(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert name in str(exc.value)


@pytest.mark.usefixtures("no_env_file")
def test_api_protection_settings_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_RATE_LIMIT_PER_MINUTE", "5")
    monkeypatch.setenv("QA_MAX_CONCURRENT_GENERATIONS", "1")
    monkeypatch.setenv("QA_REQUEST_TIMEOUT_SECONDS", "30")
    s = load_settings()
    assert (s.qa_rate_limit_per_minute, s.qa_max_concurrent_generations) == (5, 1)
    assert s.qa_request_timeout_seconds == 30.0
