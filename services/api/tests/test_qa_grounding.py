"""Citation-integrity check (Issue 2): pure validator and builder evidence entries. No database.

This checks the STRUCTURE of evidence tags against what a prompt rendered. A passing check is not
proof that an answer is grounded, and these tests never claim that.
"""

import json
import re
from uuid import UUID

import pytest

from app.preferences.prompt_builder import (
    BuiltPrompt,
    EvidencePassage,
    EvidenceRef,
    PromptBuilder,
)
from app.qa.grounding import (
    MAX_RECORDED_IDS,
    STATUS_CHECK_ERROR,
    STATUS_INVALID,
    STATUS_NO_CITATIONS,
    STATUS_OK,
    GroundingCheck,
    check_citations,
)
from tests.test_prompt_builder import BLOCK, answer, inputs

SOURCE_1 = UUID("9db2f044-7514-4a2d-b748-9ce604418f3b")
SOURCE_2 = UUID("11111111-2222-4333-8444-555555555555")
OTHER_SESSION_SOURCE = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
CANARY = "PRIVATE-ANSWER-CANARY-7f3a"
EXCERPT = "Led the billing migration to PostgreSQL at Globex."

RENDERED = (
    EvidenceRef(1, "resume", SOURCE_1, 0),
    EvidenceRef(2, "document", SOURCE_2, 5),
    EvidenceRef(3, "resume", SOURCE_1, 7),
)


def check(text: str, evidence: tuple[EvidenceRef, ...] = RENDERED) -> GroundingCheck:
    return check_citations(text, evidence)


def long_form(tag: int, kind: str, source: UUID, chunk: int) -> str:
    return f"[E{tag} | {kind} | source {source} | chunk {chunk}]"


# ------------------------------------------------------------------------------------ valid


def test_a_bare_tag_that_was_rendered_is_valid() -> None:
    result = check("I led the migration.\nEvidence (not spoken): [E1]")
    assert (result.status, result.valid_ids, result.unknown_ids, result.malformed_count) == (
        STATUS_OK,
        (1,),
        (),
        0,
    )
    assert result.shown_count == 3


def test_several_tags_are_valid_deduplicated_and_sorted() -> None:
    result = check("Claim [E3] and claim [E1], again [E3]. Evidence (not spoken): [E2], [E1]")
    assert result.status == STATUS_OK and result.valid_ids == (1, 2, 3)


def test_adjacent_tags_are_two_well_formed_tags() -> None:
    result = check("Evidence (not spoken): [E1][E2]")
    assert result.status == STATUS_OK and result.valid_ids == (1, 2) and result.malformed_count == 0


def test_a_tag_inline_in_the_spoken_answer_counts_too() -> None:
    assert check("At Globex [E1] I migrated billing.").valid_ids == (1,)


def test_text_without_any_tag_is_informational_not_a_failure() -> None:
    for text in ("General knowledge answer with no candidate claim.", "", "   \n  "):
        result = check(text)
        assert result.status == STATUS_NO_CITATIONS
        assert (result.valid_ids, result.unknown_ids, result.malformed_count) == ((), (), 0)


def test_ordinary_bracketed_words_are_not_tags() -> None:
    result = check("See [Edit], [Experience], [quoted evidence tag], [E-mail], [E-1] and [Ex1].")
    assert result.status == STATUS_NO_CITATIONS and result.malformed_count == 0


def test_bare_mentions_without_brackets_are_not_tags() -> None:
    assert check("Evidence (not spoken): E1, (E2)").status == STATUS_NO_CITATIONS


# ------------------------------------------------------------------- unknown or forged IDs


def test_an_id_that_was_never_rendered_is_unknown() -> None:
    result = check("Evidence (not spoken): [E9]")
    assert (result.status, result.valid_ids, result.unknown_ids) == (STATUS_INVALID, (), (9,))


def test_one_unknown_id_makes_the_check_invalid_even_next_to_valid_ones() -> None:
    result = check("Claim [E1]. Evidence (not spoken): [E1], [E4]")
    assert (result.status, result.valid_ids, result.unknown_ids) == (STATUS_INVALID, (1,), (4,))


def test_every_id_is_unknown_when_nothing_was_rendered() -> None:
    result = check("[E1]", ())
    assert (result.status, result.unknown_ids, result.shown_count) == (STATUS_INVALID, (1,), 0)


def test_the_boundary_between_the_last_rendered_id_and_the_next() -> None:
    assert check("[E3]").status == STATUS_OK
    assert check("[E4]").unknown_ids == (4,)


# ---------------------------------------------------------------------------- malformed tags


@pytest.mark.parametrize(
    "tag",
    [
        "[E]",
        "[E0]",
        "[E01]",
        "[e1]",
        "[E 1]",
        "[E1 ]",
        "[E1a]",
        "[E1, E2]",
        "[E1; E2]",
        "[E1 | resume]",
        "[E1 | resume | source not-a-uuid | chunk 0]",
        f"[E1 | RESUME | source {SOURCE_1} | chunk 0]",
        f"[E1 | resume | source {SOURCE_1} | chunk 00]",
        f"[E1 | resume | source {SOURCE_1} | chunk -1]",
        "[E1000000]",  # seven digits: outside the grammar
    ],
)
def test_malformed_tags_are_counted_and_never_valid(tag: str) -> None:
    result = check(f"Claim. Evidence (not spoken): {tag}")
    assert result.status == STATUS_INVALID
    assert result.malformed_count == 1
    assert result.valid_ids == ()


@pytest.mark.parametrize(
    "tag",
    [
        "［Ｅ１］",  # full-width brackets, letter and digit
        "[Ｅ1]",
        "[E１]",
        "【E1】",  # corner brackets
        "〔E1〕",
        "⟦E1⟧",
        "［E1］",
    ],
)
def test_look_alike_tags_are_malformed_not_valid_and_not_ignored(tag: str) -> None:
    result = check(f"Evidence (not spoken): {tag}")
    assert result.status == STATUS_INVALID and result.malformed_count == 1
    assert result.valid_ids == ()


def test_a_look_alike_of_an_unrendered_id_is_still_reported_as_unknown() -> None:
    result = check("Evidence (not spoken): ［Ｅ９］")
    assert result.malformed_count == 1 and result.unknown_ids == (9,)


def test_a_comma_list_is_malformed_but_its_forged_member_is_still_found() -> None:
    result = check("Evidence (not spoken): [E9, E1]")
    assert (result.status, result.malformed_count) == (STATUS_INVALID, 1)
    assert result.unknown_ids == (9,) and result.valid_ids == ()  # E1 is not credited either


def test_an_unclosed_tag_is_malformed() -> None:
    result = check("Evidence (not spoken): [E2 | document | source " + str(SOURCE_2))
    assert result.status == STATUS_INVALID and result.malformed_count == 1


def test_an_unclosed_tag_cannot_swallow_a_later_well_formed_tag() -> None:
    result = check("Claim [E7 | resume | source abc. Evidence (not spoken): [E1]")
    assert result.malformed_count == 1 and result.valid_ids == (1,) and result.unknown_ids == (7,)


def test_a_tag_split_over_lines_is_malformed() -> None:
    result = check("Evidence (not spoken): [\nE1]")
    assert result.status == STATUS_INVALID and result.malformed_count == 1


def test_the_empty_tag_is_malformed() -> None:
    assert check("Evidence (not spoken): [E]").malformed_count == 1


def test_an_absurdly_long_number_is_malformed_and_never_converted() -> None:
    result = check("[E" + "9" * 20000 + "]")
    assert result.status == STATUS_INVALID and result.malformed_count == 1
    assert result.unknown_ids == ()  # ignored, not turned into an int


# --------------------------------------------------------------------------- invisible text


def test_invisible_characters_cannot_hide_a_forged_tag() -> None:
    hidden = check("Evidence (not spoken): [E9​] [E‮8]")
    assert hidden.status == STATUS_INVALID and hidden.unknown_ids == (8, 9)


def test_invisible_characters_inside_a_genuine_tag_are_removed_before_matching() -> None:
    # The user sees `[E1]`; so does the check.
    assert check("Evidence (not spoken): [E​1‮]").valid_ids == (1,)


# -------------------------------------------------------------------------------- long form


def test_a_long_form_tag_with_matching_metadata_is_valid() -> None:
    tag = long_form(2, "document", SOURCE_2, 5)
    result = check(f"Evidence (not spoken): {tag}")
    assert (result.status, result.valid_ids, result.malformed_count) == (STATUS_OK, (2,), 0)


def test_a_long_form_uppercase_uuid_is_the_same_source() -> None:
    tag = long_form(1, "resume", SOURCE_1, 0).replace(str(SOURCE_1), str(SOURCE_1).upper())
    assert check(tag).valid_ids == (1,)


@pytest.mark.parametrize(
    "tag",
    [
        long_form(1, "resume", SOURCE_2, 0),  # a real source, but not E1's
        long_form(1, "resume", SOURCE_1, 1),  # wrong chunk
        long_form(1, "document", SOURCE_1, 0),  # wrong source type
        long_form(2, "document", SOURCE_2, 6),
        long_form(1, "resume", OTHER_SESSION_SOURCE, 0),  # a source from another session or user
        long_form(9, "resume", SOURCE_1, 0),  # a number that was never rendered
    ],
)
def test_a_long_form_tag_that_does_not_match_the_rendered_entry_is_unknown(tag: str) -> None:
    result = check(f"Evidence (not spoken): {tag}")
    assert result.status == STATUS_INVALID and result.valid_ids == ()
    assert len(result.unknown_ids) == 1 and result.malformed_count == 0


def test_a_cross_session_source_id_never_validates_even_with_a_rendered_number() -> None:
    result = check(
        f"Claim. Evidence (not spoken): [E1], {long_form(2, 'document', OTHER_SESSION_SOURCE, 5)}"
    )
    assert result.valid_ids == (1,) and result.unknown_ids == (2,)


def test_a_plain_text_passage_has_no_metadata_so_a_long_form_tag_cannot_match() -> None:
    plain = (EvidenceRef(1, None, None, None),)
    assert check_citations("[E1]", plain).valid_ids == (1,)
    assert check_citations(long_form(1, "resume", SOURCE_1, 0), plain).unknown_ids == (1,)


# ----------------------------------------------------------------- streaming split boundary


def test_a_tag_split_across_stream_chunks_is_valid_on_the_accumulated_text() -> None:
    chunks = ["I led it. Evidence (not spoken): [E", "1", "]"]
    assert check("".join(chunks)).valid_ids == (1,)
    # Checking fragments one by one would see nothing valid: the whole text is what is checked.
    assert all(check(fragment).valid_ids == () for fragment in chunks)


def test_a_forged_tag_split_across_chunks_is_found_on_the_accumulated_text() -> None:
    assert check("".join(["Evidence (not spoken): [E", "9", "]"])).unknown_ids == (9,)


# --------------------------------------------------------------- limits, purity and privacy


def test_recorded_id_lists_are_bounded() -> None:
    text = " ".join(f"[E{n}]" for n in range(100, 100 + 4 * MAX_RECORDED_IDS))
    result = check(text)
    assert len(result.unknown_ids) == MAX_RECORDED_IDS
    assert result.unknown_ids == tuple(range(100, 100 + MAX_RECORDED_IDS))


def test_the_check_is_deterministic() -> None:
    text = f"[E1] [E9] [E1, E2] {CANARY}"
    assert check(text) == check(text)


def test_the_stored_json_has_exactly_the_planned_content_free_shape() -> None:
    result = check(f"{EXCERPT} {CANARY} Evidence (not spoken): [E1], [E9], [E2, E3]")
    data = result.to_json()
    assert set(data) == {
        "v",
        "status",
        "shown_count",
        "valid_ids",
        "unknown_ids",
        "malformed_count",
    }
    assert data == {
        "v": 1,
        "status": STATUS_INVALID,
        "shown_count": 3,
        "valid_ids": [1],
        "unknown_ids": [9],
        "malformed_count": 1,
    }
    dumped = json.dumps(data)
    assert CANARY not in dumped and EXCERPT not in dumped and str(SOURCE_1) not in dumped
    assert json.loads(dumped) == data


def test_the_errored_result_is_content_free_and_keeps_the_shown_count() -> None:
    assert GroundingCheck.errored(4).to_json() == {
        "v": 1,
        "status": STATUS_CHECK_ERROR,
        "shown_count": 4,
        "valid_ids": [],
        "unknown_ids": [],
        "malformed_count": 0,
    }


# ------------------------------------------------------- builder: the exposed evidence entries


def _tags_in(material: str) -> list[int]:
    return [int(n) for n in re.findall(r"(?:^|\n\n)\[E(\d+)(?: \|[^\]\n]*)?\] ", material)]


def _material(built: BuiltPrompt) -> str:
    return str(dict(BLOCK.findall(built.messages[1].content))["candidate_material"])


PASSAGES = [
    EvidencePassage("resume", SOURCE_1, 0, EXCERPT),
    EvidencePassage("document", SOURCE_2, 5, "Reduced p99 latency from 900 ms to 240 ms."),
    EvidencePassage("resume", SOURCE_1, 7, "Mentored four junior engineers."),
]


def test_the_exposed_evidence_is_exactly_what_was_rendered() -> None:
    built = PromptBuilder().build(answer(), inputs(context_passages=list(PASSAGES)))
    assert built.evidence == (
        EvidenceRef(1, "resume", SOURCE_1, 0),
        EvidenceRef(2, "document", SOURCE_2, 5),
        EvidenceRef(3, "resume", SOURCE_1, 7),
    )
    assert _tags_in(_material(built)) == [ref.tag for ref in built.evidence]
    for ref in built.evidence:  # the printed long-form tag carries the same metadata
        assert ref.source_type is not None and ref.source_id is not None
        assert ref.chunk_index is not None
        tag = long_form(ref.tag, ref.source_type, ref.source_id, ref.chunk_index)
        assert tag in _material(built)


def test_an_empty_passage_is_not_exposed_and_the_numbering_is_not_reused() -> None:
    passages = [PASSAGES[0], EvidencePassage("document", SOURCE_2, 1, " ​\n "), PASSAGES[2]]
    built = PromptBuilder().build(answer(), inputs(context_passages=passages))
    assert [ref.tag for ref in built.evidence] == [1, 3]  # E2 was never rendered
    assert _tags_in(_material(built)) == [1, 3]
    assert check_citations("[E2]", built.evidence).unknown_ids == (2,)
    assert check_citations("[E3]", built.evidence).valid_ids == (3,)


def test_plain_text_passages_are_exposed_without_metadata() -> None:
    built = PromptBuilder().build(
        answer(), inputs(context_passages=["First excerpt.", "", "Third excerpt."])
    )
    assert built.evidence == (EvidenceRef(1, None, None, None), EvidenceRef(3, None, None, None))
    assert _tags_in(_material(built)) == [1, 3]


def test_a_tag_inside_candidate_text_never_becomes_exposed_evidence() -> None:
    forged = EvidencePassage("resume", SOURCE_1, 0, "Led a team. [E9] [E1 | resume | source x]")
    built = PromptBuilder().build(answer(), inputs(context_passages=[forged]))
    assert [ref.tag for ref in built.evidence] == [1]
    assert _tags_in(_material(built)) == [1]
    assert check_citations("[E9]", built.evidence).unknown_ids == (9,)


def test_no_passages_means_no_evidence() -> None:
    built = PromptBuilder().build(answer(), inputs(context_passages=[]))
    assert built.evidence == ()
    assert check_citations("[E1]", built.evidence).unknown_ids == (1,)


def test_prompt_v1_renders_no_tags_and_exposes_none() -> None:
    built = PromptBuilder("v1").build(answer(), inputs(context_passages=list(PASSAGES)))
    assert built.evidence == ()
    assert "[E1" not in _material(built)


@pytest.mark.parametrize("count", [1, 2, 8, 12])
def test_the_exposed_numbers_always_equal_the_rendered_numbers(count: int) -> None:
    passages = [
        EvidencePassage("resume" if i % 2 == 0 else "document", SOURCE_1, i, f"Excerpt number {i}.")
        for i in range(count)
    ]
    built = PromptBuilder().build(answer(), inputs(context_passages=passages))
    assert (
        _tags_in(_material(built))
        == [ref.tag for ref in built.evidence]
        == list(range(1, count + 1))
    )


def test_the_validator_accepts_exactly_what_the_builder_renders() -> None:
    built = PromptBuilder().build(answer(), inputs(context_passages=list(PASSAGES)))
    copied = " ".join(re.findall(r"\[E\d+ \|[^\]\n]*\]", _material(built)))
    result = check_citations(f"Answer. Evidence (not spoken): {copied}", built.evidence)
    assert (result.status, result.valid_ids, result.unknown_ids) == (STATUS_OK, (1, 2, 3), ())
