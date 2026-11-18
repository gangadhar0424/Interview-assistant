"""Section-aware chunking (D-66): headings, hierarchy, fidelity and the fixed-size fallback."""

from app.ingestion.chunking import (
    ChunkConfig,
    _find_headings,
    chunk_sections,
    chunk_text,
)

BODY = "This section explains the topic in some detail so that it is long enough to count. " * 4


def qa_document() -> str:
    return "\n\n".join(
        [
            "1. What is the inbound process?",
            BODY + "ERP creates the purchase order.",
            "2. How do you create bins in mass?",
            BODY + "Use the mass creation transaction.",
            "3. Explain replenishment",
            BODY + "Replenishment moves stock to the pick bin.",
        ]
    )


def test_headings_become_chunk_context_and_section_ranges() -> None:
    text = qa_document()
    chunks = chunk_sections(text)
    assert chunks
    headings = {c.heading for c in chunks}
    assert "1. What is the inbound process?" in headings
    assert "3. Explain replenishment" in headings
    for chunk in chunks:
        assert chunk.heading is not None
        assert chunk.section_start is not None and chunk.section_end is not None
        assert chunk.section_start <= chunk.start < chunk.end <= chunk.section_end
        assert chunk.embed_text.startswith(chunk.heading)


def test_every_chunk_is_an_exact_slice_of_the_source_text() -> None:
    text = qa_document()
    for chunk in chunk_sections(text):
        assert text[chunk.start : chunk.end] == chunk.text  # nothing rewritten, nothing lost


def test_a_text_without_headings_is_chunked_exactly_as_before() -> None:
    text = ("A plain paragraph without any heading at all. " * 40).strip()
    assert chunk_sections(text) == chunk_text(text)
    assert all(c.heading is None and c.section_start is None for c in chunk_sections(text))


def test_a_single_heading_is_not_enough_structure() -> None:
    text = "1. Only one heading\n\n" + BODY
    assert chunk_sections(text) == chunk_text(text)


def test_list_steps_under_a_heading_are_body_text_not_headings() -> None:
    text = "\n\n".join(
        [
            "9. What is the inbound process in EWM?",
            "1. ERP creates the PO and an inbound delivery.",
            "2. EWM creates its own delivery",
            "3. The truck arrives and unloading starts.",
            BODY,
            "10. What are the different ticket types?",
            BODY,
        ]
    )
    titles = [h.title for h in _find_headings(text)]
    assert titles == [
        "9. What is the inbound process in EWM?",
        "10. What are the different ticket types?",
    ]


def test_instruction_style_numbered_lines_are_not_headings() -> None:
    text = "\n\n".join(
        [
            "1. Customer-specific validation before execution",
            "Implementation approach",
            "2. Check whether standard status management, document checks or delivery config",
            "3. Implement the rule in a custom class; read the header and validate it",
            BODY,
            "2. Customer-specific stock removal",
            BODY,
        ]
    )
    titles = [h.title for h in _find_headings(text)]
    assert titles == [
        "1. Customer-specific validation before execution",
        "2. Customer-specific stock removal",
    ]


def test_a_part_heading_restarts_the_numbering() -> None:
    text = "\n\n".join(
        [
            "1. First topic",
            BODY,
            "2. Second topic",
            BODY,
            "Part 2 - More topics",
            "1. Third topic",
            BODY,
        ]
    )
    titles = [h.title for h in _find_headings(text)]
    assert titles == ["1. First topic", "2. Second topic", "Part 2 - More topics", "1. Third topic"]


def test_a_bare_number_counts_only_as_the_very_next_heading() -> None:
    text = "\n\n".join(
        [
            "34. Custom development",
            BODY,
            "35 Transportation Management (TM) integration",
            BODY,
            "7 years of experience in warehouse systems",
            BODY,
        ]
    )
    titles = [h.title for h in _find_headings(text)]
    assert "35 Transportation Management (TM) integration" in titles
    assert not any(t.startswith("7 years") for t in titles)


def test_nested_headings_build_a_path_and_a_wide_parent_section() -> None:
    text = (chr(10) * 2).join(
        [
            "40. Enhancement in EWM",
            "Enhancement1: Receiving Excess Qty",
            BODY + "The operator receives the excess quantity.",
            "Solution:",
            BODY + "A custom RF transaction creates a warehouse task.",
            "Enhancement2: Putaway by Cart",
            BODY + "Totes are assigned to a cart.",
            "41. Next question",
            BODY,
        ]
    )
    chunks = chunk_sections(text)
    # the parent heading keeps its own chunk, owning the whole of its children's text
    parent = next(c for c in chunks if c.heading == "40. Enhancement in EWM")
    whole = text[parent.section_start : parent.section_end]
    assert whole.startswith("40. Enhancement in EWM")
    assert (
        "Enhancement1: Receiving Excess Qty" in whole and "Enhancement2: Putaway by Cart" in whole
    )
    assert "41. Next question" not in whole
    # each sub-section has its own title and owns exactly its own text
    first = next(
        c
        for c in chunks
        if c.heading == "40. Enhancement in EWM > Enhancement1: Receiving Excess Qty"
    )
    own = text[first.section_start : first.section_end]
    assert own.startswith("Enhancement1: Receiving Excess Qty")
    assert "Solution:" in own and "Enhancement2: Putaway by Cart" not in own
    nested = [
        c for c in chunks if c.heading and c.heading.endswith("Enhancement2: Putaway by Cart")
    ]
    assert nested and (nested[0].heading or "").startswith("40. Enhancement in EWM > ")


def test_chunk_size_is_still_respected_inside_a_section() -> None:
    long_section = "\n\n".join(
        ["1. Long one", ("Sentence number one is here. " * 120), "2. Short", BODY]
    )
    config = ChunkConfig(size=400, overlap=40)
    chunks = chunk_sections(long_section, config)
    assert max(len(c.text) for c in chunks) <= 400
    assert [c.index for c in chunks] == list(range(len(chunks)))
