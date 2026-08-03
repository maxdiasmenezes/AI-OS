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
