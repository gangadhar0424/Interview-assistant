"""Exact-text answers (D-66): verbatim by construction, fitted by whole sentences."""

import re

from app.qa.extractive import (
    fit_to_words,
    is_verbatim,
    split_for_streaming,
    tidy,
    word_count,
)

SECTION = (
    "40. Enhancement in EWM\n\n"
    "Enhancement1: Receiving Excess Qty HHT Enhancement. While unloading, the user finds more "
    "stock than the ASN says. The operator receives the excess on the handheld. "
    "A warehouse task is created for the destination bin.\n\n"
    "Enhancement2: Putaway by Cart. Totes arrive at the dropping zone. Each tote has an open "
    "warehouse task. The operator assigns four totes to a trolley.\n\n"
    "Enhancement 3: Unloading by ASN. The driver hands over an ASN barcode. The operator scans "
    "the ASN, then the carton label, then the article and the quantity."
)


def test_tidy_turns_pdf_bullet_glyphs_into_bullets_without_touching_words() -> None:
    raw = "Skills:\n\nConfiguration of strategies\n�\nSupport of RF\n\n\n\nEnd"
    out = tidy(raw)
    assert "" not in out and "�" not in out
    assert "• Configuration of strategies" in out and "• Support of RF" in out
    assert "\n\n\n" not in out
    assert re.sub(r"[•\s]+", " ", out) == re.sub(r"[�\s]+", " ", raw).strip()


def test_a_short_enough_section_is_returned_whole() -> None:
    assert fit_to_words(SECTION, 500) == SECTION.strip()


def test_a_long_section_is_cut_to_whole_sentences_from_the_start_when_anchored() -> None:
    out = fit_to_words(SECTION, 30, ["enhancement"], anchored_start=True)
    assert out.startswith("40. Enhancement in EWM")
    assert word_count(out) <= 40
    assert is_verbatim(out, SECTION)
    assert out.rstrip().endswith((".", "EWM"))  # ends at a sentence/line boundary


def test_without_an_anchor_the_window_with_the_question_words_is_chosen() -> None:
    out = fit_to_words(SECTION, 25, ["trolley", "totes", "assigns"], anchored_start=False)
    assert "trolley" in out and "Receiving Excess" not in out
    assert is_verbatim(out, SECTION)


def test_a_window_always_contains_at_least_one_sentence() -> None:
    out = fit_to_words(SECTION, 1, ["unloading"], anchored_start=True)
    assert out and is_verbatim(out, SECTION)


def test_the_answer_is_always_an_exact_piece_of_the_source() -> None:
    for budget in (5, 15, 40, 80, 200):
        for anchored in (True, False):
            out = fit_to_words(SECTION, budget, ["warehouse", "task"], anchored_start=anchored)
            assert is_verbatim(out, SECTION), (budget, anchored)


def test_text_that_is_not_in_the_source_is_not_verbatim() -> None:
    assert not is_verbatim("A paraphrase of the enhancement", SECTION)
    assert not is_verbatim("", SECTION)


def test_streaming_pieces_join_back_to_the_exact_text() -> None:
    text = "First paragraph.\n\nSecond paragraph.\n\nThird."
    pieces = split_for_streaming(text)
    assert len(pieces) == 3 and "".join(pieces) == text
    assert split_for_streaming("one line") == ["one line"]
