"""Answer router (D-66): resume first, then documents, then partial/uncertain, else general."""

import uuid

from app.qa.answer_router import (
    INTRO_SPAN_CHARS,
    Decision,
    Mode,
    RouterSettings,
    decide,
    is_intro_question,
    strength,
)
from app.retrieval.service import ChunkHit
from app.retrieval.terms import content_terms

CFG = RouterSettings()
RESUME = uuid.uuid4()
DOC_A = uuid.uuid4()
DOC_B = uuid.uuid4()


def hit(
    source: uuid.UUID,
    text: str,
    heading: str | None = None,
    *,
    index: int = 0,
    fused: float = 0.03,
    section: tuple[int, int] | None = None,
    kind: str = "document",
) -> ChunkHit:
    return ChunkHit(
        chunk_id=uuid.uuid4(),
        source_type=kind,
        source_id=source,
        chunk_index=index,
        text=text,
        similarity=0.0,
        heading=heading,
        section_start=section[0] if section else None,
        section_end=section[1] if section else None,
        fused_score=fused,
    )


def resume_hit(text: str, heading: str | None = None, **kw: object) -> ChunkHit:
    return hit(RESUME, text, heading, kind="resume", **kw)  # type: ignore[arg-type]


def test_intro_questions_are_recognised() -> None:
    for question in (
        "tell me about yourself",
        "Tell me about your self",
        "can you talk a bit about yourself",
        "introduce yourself",
        "walk me through your resume",
        "give me a brief introduction",
    ):
        assert is_intro_question(question), question
    assert not is_intro_question("tell me about POSC")


def test_an_introduction_opens_the_resume_regardless_of_search_results() -> None:
    opening = resume_hit("Total Experience IT professional...", index=0)
    decision = decide("tell me about yourself", [], [], CFG, resume_opening=opening)
    assert decision.mode is Mode.RESUME_EXTRACTIVE and decision.reason == "intro"
    assert decision.hit is opening and decision.anchored_start
    assert decision.span == (0, INTRO_SPAN_CHARS)


def test_a_title_match_in_the_resume_is_answered_verbatim_from_the_resume() -> None:
    skills = resume_hit("Expertise in creating functional specifications", "Technical Skills:")
    other = resume_hit("Worked on interfaces", "Project #1 > Responsibilities:", index=1)
    decision = decide("what are your technical skills", [skills, other], [], CFG)
    assert decision.mode is Mode.RESUME_EXTRACTIVE and decision.hit is skills
    assert decision.reason == "title" and decision.anchored_start


def test_a_document_title_match_answers_from_the_document_when_the_resume_has_no_match() -> None:
    section = hit(DOC_A, "POSC defines the external flow", "38. Explain POSC", section=(10, 900))
    decision = decide(
        "what is POSC", [resume_hit("Experience in POSC, LOSC and yard")], [section], CFG
    )
    assert decision.mode is Mode.DOCUMENT_EXTRACTIVE and decision.hit is section


def test_a_strong_document_heading_beats_a_weak_resume_body_mention() -> None:
    weak_resume = resume_hit("Experience in POSC, LOSC, Production Supply Process")  # no heading
    section = hit(DOC_A, "POSC defines the flow", "38. Explain POSC (Process-Oriented)")
    decision = decide("what is POSC", [weak_resume], [section], CFG)
    assert decision.mode is Mode.DOCUMENT_EXTRACTIVE


def test_on_equal_strength_the_resume_wins() -> None:
    resume = resume_hit("Replenishment strategies were set up", "Replenishment")
    section = hit(DOC_A, "Replenishment moves stock", "24. Replenishment")
    decision = decide("replenishment", [resume], [section], CFG)
    assert decision.mode is Mode.RESUME_EXTRACTIVE


def test_the_best_title_wins_among_several_sections_of_one_document() -> None:
    wide = hit(DOC_A, "x", "32. What is the difference between decentralized and embedded EWM?")
    partial = hit(DOC_A, "y", "31. Have you worked on decentralized or embedded EWM?", index=1)
    decision = decide("difference between decentralized and embedded EWM", [], [partial, wide], CFG)
    assert decision.mode is Mode.DOCUMENT_EXTRACTIVE and decision.hit is wide


def test_a_body_that_holds_most_of_a_multi_word_question_is_extractive() -> None:
    chunk = hit(DOC_A, "Receiving excess qty: the operator scans the article and quantity")
    decision = decide("receiving excess quantity article", [], [chunk], CFG)
    assert decision.mode is Mode.DOCUMENT_EXTRACTIVE and decision.reason == "body"
    assert not decision.anchored_start


def test_a_single_word_never_counts_as_a_body_match() -> None:
    chunk = hit(DOC_A, "BAdI is mentioned once in passing here")
    assert decide("BAdI", [], [chunk], CFG).mode is not Mode.DOCUMENT_EXTRACTIVE


def test_an_enclosing_heading_alone_sends_the_question_to_synthesis() -> None:
    leaf = resume_hit("Worked on interfaces", "Project #1 > Responsibilities:")
    decision = decide("which projects", [leaf], [], CFG)
    assert decision.mode is Mode.SYNTHESIS and decision.reason == "path"


def test_partial_evidence_is_uncertain_and_no_evidence_is_general() -> None:
    partial = hit(DOC_A, "Queue monitoring and the monitor transaction", "Monitoring")
    unsure = decide("queue monitoring issues", [], [partial], CFG)
    assert unsure.mode is Mode.UNCERTAIN
    unrelated = hit(DOC_A, "Warehouse bins and storage types", "Bins")
    assert decide("what is a kubernetes pod", [], [unrelated], CFG).mode is Mode.GENERAL
    assert decide("what is a kubernetes pod", [], [], CFG).mode is Mode.GENERAL


def test_partial_evidence_in_a_document_is_seen_even_when_the_resume_hits_come_first() -> None:
    resume_hits = [resume_hit(f"unrelated resume text {i}", index=i) for i in range(6)]
    partial = hit(DOC_A, "Queue monitoring and monitor transaction", "Monitoring")
    decision = decide("queue monitor problems today", resume_hits, [partial], CFG)
    assert decision.mode is Mode.UNCERTAIN


def test_rewritten_terms_replace_the_question_words_on_the_second_pass() -> None:
    section = hit(DOC_A, "Custom development was done with the ABAP team", "34. Custom development")
    first = decide("did you write custom code yourself", [], [section], CFG)
    assert first.mode is not Mode.DOCUMENT_EXTRACTIVE
    second = decide(
        "did you write custom code yourself",
        [],
        [section],
        CFG,
        terms=content_terms("custom development"),
    )
    assert second.mode is Mode.DOCUMENT_EXTRACTIVE and second.hit is section


def test_strength_reports_the_kind_of_anchor() -> None:
    title = hit(DOC_A, "body", "5. Warehouse order creation rule")
    path = hit(DOC_A, "body", "40. Enhancement in EWM > Solution:")
    terms = content_terms("enhancement ewm")
    assert strength(terms, path, CFG) == 1
    assert strength(content_terms("warehouse order creation rule"), title, CFG) == 3


def test_decisions_carry_only_a_fixed_reason_vocabulary() -> None:
    reasons = {
        decide("x y z", [], [], CFG).reason,
        Decision(Mode.GENERAL).reason or "none",
    }
    assert reasons <= {"none", "intro", "title", "body", "path", "partial", ""}


def test_enhancement_one_picks_its_own_section_not_a_heading_that_only_contains_a_one() -> None:
    wrong = hit(
        DOC_A,
        "Outbound delivery",
        "Part 1 - Outbound delivery, picking and wave enhancements",
        fused=0.0328,
    )
    right = hit(
        DOC_A,
        "Receiving excess",
        "40. Enhancement in EWM > Enhancement1: Receiving Excess Qty",
        fused=0.0300,
        index=3,
    )
    other = hit(
        DOC_A,
        "Unloading",
        "40. Enhancement in EWM > Enhancement 3: Unloading By ASN",
        fused=0.0310,
        index=4,
    )
    decision = decide("enhancement 1", [], [wrong, other, right], CFG)
    assert decision.mode is Mode.DOCUMENT_EXTRACTIVE and decision.hit is right
    assert decision.reason == "title"
