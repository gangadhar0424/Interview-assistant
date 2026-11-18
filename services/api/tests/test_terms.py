"""Question terms for lexical search and routing (D-66)."""

from app.retrieval.terms import content_terms, coverage, stem, tsquery_or


def test_content_terms_drop_filler_and_plurals() -> None:
    assert content_terms("What enhancements have you done in EWM?") == ["enhancement", "ewm"]
    assert content_terms("Tell me about yourself") == []


def test_terms_are_distinct_ordered_and_script_neutral() -> None:
    assert content_terms("queue queue Queue issues") == ["queue", "issue"]
    assert content_terms("मुझे क्यू के बारे में बताओ") != []  # non-Latin words are content words


def test_stem_strips_simple_plurals_only() -> None:
    assert stem("enhancements") == "enhancement"
    assert stem("queries") == "query"
    assert stem("boxes") == "box"
    assert stem("class") == "class"
    assert stem("is") == "is"


def test_the_tsquery_is_a_safe_prefix_or() -> None:
    assert tsquery_or(["enhancement", "ewm"]) == "enhancement:* | ewm:*"
    hostile = ["a'b", "x & y", "ok", "z); DROP TABLE users;--"]
    assert tsquery_or(hostile) == "ok:*"  # only plain word characters ever reach to_tsquery


def test_coverage_is_the_share_of_terms_found() -> None:
    terms = ["enhancement", "ewm"]
    assert coverage(terms, "40. Enhancement in EWM") == 1.0
    assert coverage(terms, "Enhancement of the system") == 0.5
    assert coverage(terms, "queue messages") == 0.0
    assert coverage([], "anything") == 0.0


def test_short_terms_need_a_whole_word_but_long_ones_match_by_prefix() -> None:
    assert coverage(["cap"], "the pod has capacity") == 0.0  # "cap" is not "capacity"
    assert coverage(["replenish"], "replenishment moves stock") == 1.0


def test_a_word_followed_by_a_number_is_one_numbered_term() -> None:
    assert content_terms("enhancement 1") == ["enhancement1"]
    assert content_terms("Enhancement1") == ["enhancement1"]
    assert content_terms("what is project #2") == ["project2"]
    assert content_terms("top 10 skills") == ["top10", "skill"]
    assert content_terms("it was 2019") == []  # a bare number is dropped


def test_a_numbered_term_matches_its_own_section_exactly() -> None:
    terms = content_terms("enhancement 1")
    assert coverage(terms, "Enhancement1: Receiving Excess Qty") == 1.0
    assert coverage(terms, "Enhancement 1: written with a space") == 1.0
    assert coverage(terms, "Part 1 - Outbound delivery and wave enhancements") == 0.0
    assert coverage(terms, "Enhancement 3: Unloading By ASN") == 0.0
    assert coverage(terms, "Enhancement 10: Another one") == 0.0  # not a prefix match


def test_the_numbered_tsquery_matches_either_spelling_and_stays_safe() -> None:
    assert tsquery_or(["enhancement1"]) == "(enhancement1 | enhancement <-> 1)"
    assert tsquery_or(["x1", "a'b2"]) == "(x1 | x <-> 1)"
