"""Ranking helpers for the router path (D-66): fair share per document, duplicates, ordering."""

import uuid

from app.qa.context import ContextConfig
from app.qa.ranking import Ranked, context_from_ranked, order_by_fused
from app.retrieval.hybrid import drop_duplicates, limit_per_source, text_fingerprint
from app.retrieval.service import ChunkHit

RESUME = uuid.uuid4()


def make_hit(source: uuid.UUID, index: int, fused: float, text: str | None = None) -> ChunkHit:
    return ChunkHit(
        chunk_id=uuid.uuid4(),
        source_type="resume" if source == RESUME else "document",
        source_id=source,
        chunk_index=index,
        text=text or f"passage {source.hex[:4]} {index}",
        similarity=0.0,
        fused_score=fused,
    )


def test_the_per_source_cap_keeps_the_best_hits_of_each_source() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    hits = [make_hit(a, i, 0.9 - i / 100) for i in range(5)] + [make_hit(b, 0, 0.1)]
    kept = limit_per_source(sorted(hits, key=lambda h: -h.fused_score), 2)
    assert [h.chunk_index for h in kept if h.source_id == a] == [0, 1]
    assert any(h.source_id == b for h in kept)


def test_an_exempt_source_is_never_capped() -> None:
    hits = [make_hit(RESUME, i, 0.9 - i / 100) for i in range(6)]
    assert len(limit_per_source(hits, 2, exempt={RESUME})) == 6


def test_duplicates_across_files_are_dropped_and_the_first_wins() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    first = make_hit(a, 0, 0.9, "SAP professional with 7 years of experience.")
    copy = make_hit(b, 3, 0.8, "  sap professional  with 7 years of EXPERIENCE ")
    other = make_hit(b, 4, 0.7, "A different passage")
    assert drop_duplicates([first, copy, other]) == [first, other]
    assert drop_duplicates([copy, other], {text_fingerprint(first.text)}) == [other]


def test_one_large_document_cannot_take_every_context_slot_among_ten() -> None:
    docs = [uuid.uuid4() for _ in range(10)]
    big = [make_hit(docs[0], i, 0.99 - i / 1000) for i in range(30)]
    small = [make_hit(d, 0, 0.5 - n / 1000) for n, d in enumerate(docs[1:])]
    ranked = Ranked(
        resume=[], documents=sorted(big + small, key=lambda h: -h.fused_score), opening=None
    )
    config = ContextConfig(top_k=12, resume_quota=0, max_chars=100_000, per_document_cap=3)
    context = context_from_ranked(ranked, config)
    assert len(context) == 12
    assert sum(1 for h in context if h.source_id == docs[0]) == 3  # the cap, not 12
    assert len({h.source_id for h in context}) >= 7  # most of the other documents are represented


def test_a_single_document_may_use_all_of_its_fair_share() -> None:
    doc = uuid.uuid4()
    ranked = Ranked(
        resume=[], documents=[make_hit(doc, i, 0.9 - i / 100) for i in range(20)], opening=None
    )
    config = ContextConfig(top_k=12, resume_quota=0, max_chars=100_000, per_document_cap=3)
    assert len(context_from_ranked(ranked, config)) == 12  # one document: its share is every slot


def test_the_resume_quota_comes_first_then_the_best_remaining() -> None:
    doc = uuid.uuid4()
    resume = [make_hit(RESUME, i, 0.1 - i / 1000) for i in range(4)]
    documents = [make_hit(doc, i, 0.9 - i / 100) for i in range(6)]
    ranked = Ranked(resume=resume, documents=documents, opening=None)
    config = ContextConfig(top_k=6, resume_quota=2, max_chars=100_000, per_document_cap=10)
    context = context_from_ranked(ranked, config)
    assert len(context) == 6
    assert {h.chunk_id for h in resume[:2]} <= {h.chunk_id for h in context}  # the quota is kept
    assert [h.fused_score for h in context] == sorted(
        (h.fused_score for h in context), reverse=True
    )


def test_order_by_fused_is_deterministic() -> None:
    doc = uuid.uuid4()
    hits = [make_hit(doc, 2, 0.5), make_hit(doc, 1, 0.5), make_hit(doc, 0, 0.9)]
    assert [h.chunk_index for h in order_by_fused(hits)] == [0, 1, 2]
