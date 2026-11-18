"""Role guard (D-66): passages that limit the candidate's role are found and ranked."""

import uuid

from app.qa.caveats import CAVEAT_PATTERN, MAX_CAVEATS, rank_caveats, role_word_count
from app.retrieval.service import ChunkHit

DOC = uuid.uuid4()


def chunk(text: str, heading: str | None = None, index: int = 0) -> ChunkHit:
    return ChunkHit(
        chunk_id=uuid.uuid4(),
        source_type="document",
        source_id=DOC,
        chunk_index=index,
        text=text,
        similarity=0.0,
        heading=heading,
    )


def test_a_passage_about_the_same_kind_of_work_is_kept() -> None:
    code = chunk(
        "Confirm before using. I have not written the code myself. I gathered the requirements "
        "and worked with the ABAP team on development.",
        "34. Custom development and custom enhancements",
    )
    tools = chunk(
        "[I have not worked on non-SAP WMS tools like Manhattan or Blue Yonder.]", "15. Other"
    )
    floor = chunk("I haven't worked on a warehouse floor.", "16. Domain", index=2)
    ranked = rank_caveats([tools, code, floor], ["enhancement1", "receiving"])
    assert ranked == [code]  # unrelated limits are left out


def test_generic_do_not_claim_guidance_is_kept() -> None:
    guidance = chunk(
        "Describe your actual project role accurately; do not claim hands-on implementation."
    )
    assert rank_caveats([guidance], ["enhancement1"]) == [guidance]


def test_a_passage_about_the_same_topic_is_kept_even_without_role_words() -> None:
    topical = chunk("I have not used queue monitoring in production.", "Queue notes")
    assert rank_caveats([topical], ["queue", "monitoring"]) == [topical]


def test_the_best_matches_come_first_and_the_list_is_capped() -> None:
    hits = [
        chunk(f"I have not written code number {i} and did not develop it.", index=i)
        for i in range(10)
    ]
    ranked = rank_caveats(hits, [])
    assert len(ranked) == MAX_CAVEATS
    assert [h.chunk_index for h in ranked] == [0, 1, 2]  # deterministic on ties


def test_passages_already_in_the_prompt_are_not_repeated() -> None:
    code = chunk("I have not written the code myself.")
    assert rank_caveats([code], [], exclude=[code.chunk_id]) == []


def test_role_words_are_counted_by_stem() -> None:
    assert role_word_count("I developed and implemented the build") >= 3
    assert role_word_count("The weather was sunny") == 0


def test_the_pattern_is_a_word_bounded_alternation_without_user_input() -> None:
    assert CAVEAT_PATTERN.count(r"\m") >= 5 and "$" not in CAVEAT_PATTERN
