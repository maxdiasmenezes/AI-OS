"""Tests for kernel/knowledge_base/chunking.py: normalization, chunking,
and stable identifier derivation."""

import hashlib

import pytest

from kernel.knowledge_base.chunking import (
    CHUNK_OVERLAP_CHARACTERS,
    MAX_CHUNK_CHARACTERS,
    MAX_CHUNKS_PER_DOCUMENT,
    MAX_DOCUMENT_CHARACTERS,
    InvalidDocumentEncodingError,
    _markdown_heading_starts,
    chunk_normalized_text,
    compute_chunk_id,
    compute_chunk_text_hash,
    compute_content_hash,
    compute_document_key,
    normalize_text,
)
from kernel.knowledge_base.types import SourceLimitExceededError


# --- normalization -----------------------------------------------------


def test_normalize_text_decodes_plain_utf8():
    assert normalize_text("hello".encode("utf-8")) == "hello"


def test_normalize_text_strips_bom():
    assert normalize_text("hello".encode("utf-8-sig")) == "hello"


def test_normalize_text_rejects_invalid_utf8():
    with pytest.raises(InvalidDocumentEncodingError):
        normalize_text(b"\xff\xfe\x00\x01invalid")


def test_normalize_text_rejects_nul_byte():
    with pytest.raises(InvalidDocumentEncodingError):
        normalize_text("hello\x00world".encode("utf-8"))


def test_normalize_text_converts_crlf_to_lf():
    assert normalize_text(b"a\r\nb\r\nc") == "a\nb\nc"


def test_normalize_text_converts_lone_cr_to_lf():
    assert normalize_text(b"a\rb\rc") == "a\nb\nc"


def test_normalize_text_preserves_internal_single_newlines():
    text = normalize_text(b"line one\nline two")
    assert text == "line one\nline two"


# --- chunking: basic behavior --------------------------------------------


def test_no_empty_chunks_ever_emitted():
    text = "\n\n\n   \n\nreal paragraph\n\n\n"
    chunks = chunk_normalized_text(text)
    assert all(c.text.strip() for c in chunks)
    assert all(len(c.text) > 0 for c in chunks)


def test_paragraph_boundaries_preserved_when_small():
    text = "First paragraph.\n\nSecond paragraph."
    chunks = chunk_normalized_text(text)
    assert len(chunks) == 1
    assert chunks[0].text == text


def test_chunk_order_is_stable_and_sequential():
    text = "\n\n".join(f"Paragraph number {i} with some extra padding text." for i in range(20))
    chunks = chunk_normalized_text(text)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_no_chunk_exceeds_max_characters():
    text = "\n\n".join("word " * 100 for _ in range(30))
    chunks = chunk_normalized_text(text)
    assert all(len(c.text) <= MAX_CHUNK_CHARACTERS for c in chunks)


def test_char_offsets_match_original_text():
    text = "First paragraph here.\n\nSecond paragraph, a bit longer than the first one."
    chunks = chunk_normalized_text(text)
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.text


def test_long_paragraph_is_split_into_multiple_chunks():
    long_paragraph = "word " * 500  # ~2500 chars, no blank lines
    chunks = chunk_normalized_text(long_paragraph)
    assert len(chunks) > 1
    assert all(len(c.text) <= MAX_CHUNK_CHARACTERS for c in chunks)


def test_long_paragraph_split_prefers_word_boundary():
    long_paragraph = "word " * 500
    chunks = chunk_normalized_text(long_paragraph)
    # None of the interior split points should have cut a word in half:
    # every chunk (except possibly the very first) should not start
    # mid-word, i.e. should be preceded by whitespace in the original text
    # or start at 0.
    for c in chunks:
        if c.char_start == 0:
            continue
        assert long_paragraph[c.char_start - 1] in (" ", "\n")


def test_overlap_present_between_consecutive_chunks():
    long_paragraph = "abcdefgh " * 400  # forces multiple splits
    chunks = chunk_normalized_text(long_paragraph)
    assert len(chunks) > 1
    for prev, nxt in zip(chunks, chunks[1:]):
        # The next chunk should start before the previous one ends,
        # proving overlap (up to CHUNK_OVERLAP_CHARACTERS, word-boundary
        # adjusted).
        assert nxt.char_start < prev.char_end
        assert prev.char_end - nxt.char_start <= CHUNK_OVERLAP_CHARACTERS + 1


def test_repeated_chunking_is_deterministic():
    text = "\n\n".join(f"Paragraph {i}." * 5 for i in range(10))
    first = chunk_normalized_text(text)
    second = chunk_normalized_text(text)
    assert [c.text for c in first] == [c.text for c in second]
    assert [(c.char_start, c.char_end) for c in first] == [
        (c.char_start, c.char_end) for c in second
    ]


def test_document_character_limit_enforced(monkeypatch):
    import kernel.knowledge_base.chunking as chunking_module

    monkeypatch.setattr(chunking_module, "MAX_DOCUMENT_CHARACTERS", 10)
    with pytest.raises(SourceLimitExceededError):
        chunk_normalized_text("x" * 100)


def test_max_chunks_per_document_enforced(monkeypatch):
    import kernel.knowledge_base.chunking as chunking_module

    monkeypatch.setattr(chunking_module, "MAX_CHUNKS_PER_DOCUMENT", 1)
    text = "\n\n".join(f"Paragraph {i}: " + ("word " * 100) for i in range(5))
    with pytest.raises(SourceLimitExceededError):
        chunk_normalized_text(text)


# --- Markdown heading-aware chunk boundaries (Milestone 38.2B) ------------

# -- ATX heading grammar (private helper, exercised directly for precision) --


def test_atx_heading_one_to_six_levels_detected():
    text = "\n".join(f"{'#' * n} Heading {n}" for n in range(1, 7))
    starts = _markdown_heading_starts(text)
    assert len(starts) == 6


def test_seven_hashes_is_not_a_heading():
    text = "####### Not a heading\nBody text."
    assert _markdown_heading_starts(text) == []


def test_bare_hash_with_no_heading_text_is_not_a_heading():
    text = "#\nBody text."
    assert _markdown_heading_starts(text) == []


def test_heading_with_no_space_after_hash_is_not_a_heading():
    text = "#NoSpace\nBody text."
    assert _markdown_heading_starts(text) == []


def test_four_space_indented_pseudo_heading_is_ignored():
    text = "    # Indented, not a heading\nBody text."
    assert _markdown_heading_starts(text) == []


def test_up_to_three_leading_spaces_is_still_a_heading():
    text = "   # Heading\nBody text."
    assert _markdown_heading_starts(text) == [0]


def test_optional_closing_hashes_are_allowed():
    text = "## Heading ##\nBody text."
    assert _markdown_heading_starts(text) == [0]


def test_unicode_heading_text_is_detected():
    text = "# Título em Português 你好\nBody text."
    assert _markdown_heading_starts(text) == [0]


# -- fenced code blocks suppress heading detection --------------------------


def test_heading_inside_backtick_fence_is_ignored():
    text = "```\n# Not a heading\n```\n# Real heading\nBody."
    starts = _markdown_heading_starts(text)
    assert len(starts) == 1
    assert text[starts[0] :].startswith("# Real heading")


def test_heading_inside_tilde_fence_is_ignored():
    text = "~~~\n# Not a heading\n~~~\n# Real heading\nBody."
    starts = _markdown_heading_starts(text)
    assert len(starts) == 1
    assert text[starts[0] :].startswith("# Real heading")


def test_shorter_closing_fence_does_not_close():
    text = "````\n# Inside\n```\nStill inside\n````\n# Real heading\n"
    starts = _markdown_heading_starts(text)
    assert len(starts) == 1
    assert text[starts[0] :].startswith("# Real heading")


def test_mismatched_fence_character_does_not_close():
    text = "```\n# Inside\n~~~\nStill inside\n```\n# Real heading\n"
    starts = _markdown_heading_starts(text)
    assert len(starts) == 1
    assert text[starts[0] :].startswith("# Real heading")


def test_longer_matching_closing_fence_closes():
    text = "```\n# Inside\n````\n# Real heading\n"
    starts = _markdown_heading_starts(text)
    assert len(starts) == 1
    assert text[starts[0] :].startswith("# Real heading")


def test_heading_after_properly_closed_fence_is_detected():
    text = "```python\ncode here\n```\n# Real heading\nBody."
    starts = _markdown_heading_starts(text)
    assert len(starts) == 1
    assert text[starts[0] :].startswith("# Real heading")


# -- section packing: boundaries, overlap, and section-start alignment ------


def test_no_chunk_straddles_a_heading_and_headings_start_their_section():
    text = (
        "Intro paragraph before any heading, just plain text.\n\n"
        "# First Heading\n"
        "Body text of the first section, enough to read.\n\n"
        "## Second Heading\n"
        "Body text of the second section.\n"
    )
    chunks = chunk_normalized_text(text, is_markdown=True)
    heading_starts = _markdown_heading_starts(text)
    assert len(heading_starts) == 2
    for h in heading_starts:
        assert any(c.char_start == h for c in chunks)
    for c in chunks:
        for h in heading_starts:
            assert not (c.char_start < h < c.char_end)


def test_adjacent_headings_each_start_their_own_chunk():
    text = "# Heading One\n# Heading Two\nBody text under heading two."
    chunks = chunk_normalized_text(text, is_markdown=True)
    assert len(chunks) == 2
    assert chunks[0].text.startswith("# Heading One")
    assert "Heading Two" not in chunks[0].text
    assert chunks[1].text.startswith("# Heading Two")


def test_heading_without_preceding_blank_line_still_starts_new_section():
    text = "Paragraph text right before a heading, no blank line.\n# Heading\nBody after heading."
    chunks = chunk_normalized_text(text, is_markdown=True)
    heading_offset = text.index("# Heading")
    assert any(c.char_start == heading_offset for c in chunks)
    for c in chunks:
        if c.char_start < heading_offset:
            assert c.char_end <= heading_offset


def test_short_sections_each_produce_a_single_chunk():
    text = "# One\nShort body one.\n\n# Two\nShort body two.\n"
    chunks = chunk_normalized_text(text, is_markdown=True)
    assert len(chunks) == 2


def test_long_section_produces_multiple_chunks_with_overlap():
    text = "# Big Section\n" + ("word " * 500)
    chunks = chunk_normalized_text(text, is_markdown=True)
    assert len(chunks) > 1
    assert chunks[0].char_start == 0
    assert chunks[0].text.startswith("# Big Section")
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.char_start < prev.char_end


def test_overlap_never_crosses_into_previous_section():
    section1 = "# First\n\n" + ("alpha " * 300) + "\n\n"
    heading2 = "# Second\n\n"
    long_para = "b" * 1195
    text = section1 + heading2 + long_para
    chunks = chunk_normalized_text(text, is_markdown=True)
    heading2_offset = text.index("# Second")

    # No chunk overlapping the second section may start before its heading.
    for c in chunks:
        if c.char_end > heading2_offset:
            assert c.char_start >= heading2_offset

    section2_chunks = [c for c in chunks if c.char_start >= heading2_offset]
    assert len(section2_chunks) >= 2
    assert section2_chunks[0].char_start == heading2_offset
    # Overlap is present within the section (second chunk starts before the
    # first one ends), even though it can never reach before the heading.
    assert section2_chunks[1].char_start < section2_chunks[0].char_end


def test_all_heading_levels_start_new_sections():
    parts = [f"{'#' * n} Level {n}\nBody {n}." for n in range(1, 7)]
    text = "\n\n".join(parts)
    chunks = chunk_normalized_text(text, is_markdown=True)
    assert len(chunks) == 6
    for n, c in zip(range(1, 7), chunks):
        assert c.text.startswith(f"{'#' * n} Level {n}")


def test_four_space_indented_pseudo_heading_does_not_split_a_section():
    text = "Paragraph one.\n\n    # Indented, not a heading\n\nParagraph two."
    chunks = chunk_normalized_text(text, is_markdown=True)
    assert len(chunks) == 1


def test_bare_hash_does_not_split_a_section():
    text = "Paragraph one.\n\n#\n\nParagraph two."
    chunks = chunk_normalized_text(text, is_markdown=True)
    assert len(chunks) == 1


# -- compatibility: non-Markdown behavior is unchanged -----------------------


def test_plain_text_behavior_unchanged_without_is_markdown_flag():
    text = "Paragraph one.\n\n" + ("word " * 500)
    default_chunks = chunk_normalized_text(text)
    explicit_false_chunks = chunk_normalized_text(text, is_markdown=False)
    assert [(c.char_start, c.char_end, c.text) for c in default_chunks] == [
        (c.char_start, c.char_end, c.text) for c in explicit_false_chunks
    ]


def test_non_markdown_text_with_hash_lines_is_not_boundary_split():
    text = "# This looks like a heading\n\nA short second paragraph."
    chunks = chunk_normalized_text(text)
    assert len(chunks) == 1
    assert chunks[0].text == text


def test_markdown_repeated_chunking_is_deterministic():
    text = "# Heading\n\nBody text here.\n\n## Sub\n\nMore body text."
    first = chunk_normalized_text(text, is_markdown=True)
    second = chunk_normalized_text(text, is_markdown=True)
    assert [(c.char_start, c.char_end, c.text) for c in first] == [
        (c.char_start, c.char_end, c.text) for c in second
    ]


def test_markdown_char_offsets_match_original_text():
    text = "# Heading\n\n" + ("word " * 400) + "\n\n## Sub\n\nA short closing paragraph."
    chunks = chunk_normalized_text(text, is_markdown=True)
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.text


def test_no_markdown_chunk_exceeds_max_characters():
    text = "# Heading\n\n" + "\n\n".join("word " * 100 for _ in range(30))
    chunks = chunk_normalized_text(text, is_markdown=True)
    assert all(len(c.text) <= MAX_CHUNK_CHARACTERS for c in chunks)


def test_markdown_document_character_limit_enforced(monkeypatch):
    import kernel.knowledge_base.chunking as chunking_module

    monkeypatch.setattr(chunking_module, "MAX_DOCUMENT_CHARACTERS", 10)
    with pytest.raises(SourceLimitExceededError):
        chunk_normalized_text("# Heading\nBody text that is too long.", is_markdown=True)


def test_markdown_max_chunks_per_document_enforced(monkeypatch):
    import kernel.knowledge_base.chunking as chunking_module

    monkeypatch.setattr(chunking_module, "MAX_CHUNKS_PER_DOCUMENT", 1)
    text = "# One\nBody.\n\n# Two\nBody.\n\n# Three\nBody."
    with pytest.raises(SourceLimitExceededError):
        chunk_normalized_text(text, is_markdown=True)


# --- hashes and identifiers -------------------------------------------------


def test_content_hash_depends_only_on_normalized_content():
    a = compute_content_hash("same text")
    b = compute_content_hash("same text")
    assert a == b
    assert a == hashlib.sha256("same text".encode("utf-8")).hexdigest()


def test_content_hash_changes_with_content():
    assert compute_content_hash("one") != compute_content_hash("two")


def test_identical_content_at_different_paths_has_same_content_hash():
    text = "identical body text"
    assert compute_content_hash(text) == compute_content_hash(text)


def test_document_key_differs_by_source_or_path():
    a = compute_document_key("source_a", "doc.md")
    b = compute_document_key("source_b", "doc.md")
    c = compute_document_key("source_a", "other.md")
    assert len({a, b, c}) == 3


def test_document_key_is_stable_for_same_inputs():
    assert compute_document_key("s", "p") == compute_document_key("s", "p")


def test_chunk_id_repeats_for_identical_inputs():
    doc_key = compute_document_key("s", "p")
    content_hash = compute_content_hash("body")
    text_hash = compute_chunk_text_hash("chunk text")
    a = compute_chunk_id(doc_key, content_hash, 0, text_hash)
    b = compute_chunk_id(doc_key, content_hash, 0, text_hash)
    assert a == b


def test_chunk_id_changes_when_content_changes():
    doc_key = compute_document_key("s", "p")
    hash_a = compute_content_hash("body a")
    hash_b = compute_content_hash("body b")
    text_hash = compute_chunk_text_hash("chunk text")
    a = compute_chunk_id(doc_key, hash_a, 0, text_hash)
    b = compute_chunk_id(doc_key, hash_b, 0, text_hash)
    assert a != b


def test_chunk_id_changes_with_ordinal():
    doc_key = compute_document_key("s", "p")
    content_hash = compute_content_hash("body")
    text_hash = compute_chunk_text_hash("chunk text")
    a = compute_chunk_id(doc_key, content_hash, 0, text_hash)
    b = compute_chunk_id(doc_key, content_hash, 1, text_hash)
    assert a != b


def test_hashes_are_sha256_hex_digests():
    h = compute_content_hash("anything")
    assert len(h) == 64
    int(h, 16)  # raises if not valid hex
