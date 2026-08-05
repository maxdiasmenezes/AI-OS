"""Tests for kernel/knowledge_base/query.py: the shared lexical query
mechanics search.py and evidence.py both build on."""

import pytest

from kernel.knowledge_base.config import KnowledgeBaseConfig, SourceSpec
from kernel.knowledge_base.query import (
    MAX_QUERY_CHARACTERS,
    MAX_QUERY_TERMS,
    RANKED_ORDER_BY_SQL,
    build_match_expression,
    extract_query_terms,
    validate_limit,
    validate_query_text,
    validate_source_filter,
)
from kernel.knowledge_base.types import InvalidQueryError, InvalidSourceFilterError


# --- validate_query_text ----------------------------------------------------


def test_valid_query_text_is_stripped():
    assert validate_query_text("  hello  ") == "hello"


def test_blank_query_text_rejected():
    with pytest.raises(InvalidQueryError):
        validate_query_text("   ")


def test_oversized_query_text_rejected():
    with pytest.raises(InvalidQueryError):
        validate_query_text("a" * (MAX_QUERY_CHARACTERS + 1))


def test_control_character_in_query_text_rejected():
    with pytest.raises(InvalidQueryError):
        validate_query_text("hello\x01world")


def test_non_string_query_text_rejected():
    with pytest.raises(InvalidQueryError):
        validate_query_text(12345)


# --- build_match_expression: safe FTS5 transformation -----------------------


def test_single_term_becomes_quoted_literal():
    assert build_match_expression("hello") == '"hello"'


def test_multiple_terms_joined_with_and():
    assert build_match_expression("hello world") == '"hello" AND "world"'


def test_terms_deduplicated_case_insensitively_preserving_order():
    assert build_match_expression("Hello hello WORLD world") == '"Hello" AND "WORLD"'


def test_wildcard_is_neutralized():
    assert build_match_expression("term*") == '"term"'


def test_near_or_not_treated_as_literals():
    assert build_match_expression("NEAR OR NOT") == '"NEAR" AND "OR" AND "NOT"'


def test_column_filter_syntax_neutralized():
    assert build_match_expression("title:term") == '"title" AND "term"'


def test_terms_capped_at_max_query_terms():
    query = " ".join(f"term{i}" for i in range(30))
    expr = build_match_expression(query)
    assert expr.count("AND") == MAX_QUERY_TERMS - 1


def test_query_with_only_punctuation_rejected():
    with pytest.raises(InvalidQueryError):
        build_match_expression("***???")


# --- extract_query_terms: shared first stage (Milestone 38.1) ---------------
# build_match_expression() is now a thin wrapper over extract_query_terms();
# these tests exercise the shared helper directly, and confirm every
# build_match_expression() behavior above still traces back to it, byte for
# byte, with no change to that function's public output.


def test_extract_query_terms_single_term():
    assert extract_query_terms("hello") == ["hello"]


def test_extract_query_terms_preserves_order_and_original_spelling():
    assert extract_query_terms("Hello hello WORLD world") == ["Hello", "WORLD"]


def test_extract_query_terms_deduplicates_case_insensitively():
    assert extract_query_terms("term term TERM Term") == ["term"]


def test_extract_query_terms_capped_at_max_query_terms():
    query = " ".join(f"term{i}" for i in range(30))
    terms = extract_query_terms(query)
    assert len(terms) == MAX_QUERY_TERMS


def test_extract_query_terms_normalizes_unicode_nfc():
    import unicodedata

    decomposed = unicodedata.normalize("NFD", "café")
    terms = extract_query_terms(decomposed)
    assert terms == [unicodedata.normalize("NFC", "café")]


def test_extract_query_terms_blank_rejected():
    with pytest.raises(InvalidQueryError):
        extract_query_terms("   ")


def test_extract_query_terms_oversized_rejected():
    with pytest.raises(InvalidQueryError):
        extract_query_terms("a" * (MAX_QUERY_CHARACTERS + 1))


def test_extract_query_terms_control_character_rejected():
    with pytest.raises(InvalidQueryError):
        extract_query_terms("hello\x01world")


def test_extract_query_terms_only_punctuation_rejected():
    with pytest.raises(InvalidQueryError):
        extract_query_terms("***???")


def test_extract_query_terms_non_string_rejected():
    with pytest.raises(InvalidQueryError):
        extract_query_terms(12345)


def test_build_match_expression_matches_quoted_and_of_extracted_terms():
    # build_match_expression() must remain exactly " AND ".join of quoted
    # extract_query_terms() output - the refactor's core invariant.
    query = "Hello hello WORLD world café"
    terms = extract_query_terms(query)
    expected = " AND ".join(f'"{term}"' for term in terms)
    assert build_match_expression(query) == expected


# --- validate_limit ----------------------------------------------------------


def test_validate_limit_accepts_in_range():
    assert validate_limit(1, 5) == 1
    assert validate_limit(5, 5) == 5


def test_validate_limit_rejects_zero():
    with pytest.raises(InvalidQueryError):
        validate_limit(0, 5)


def test_validate_limit_rejects_above_ceiling():
    with pytest.raises(InvalidQueryError):
        validate_limit(6, 5)


def test_validate_limit_rejects_bool():
    with pytest.raises(InvalidQueryError):
        validate_limit(True, 5)


def test_validate_limit_rejects_non_int():
    with pytest.raises(InvalidQueryError):
        validate_limit("3", 5)


def test_validate_limit_ceiling_is_caller_supplied():
    # search.py's MAX_RESULT_LIMIT (50) and evidence.py's
    # MAX_EVIDENCE_LIMIT (5) are different ceilings over the same function.
    assert validate_limit(50, 50) == 50
    with pytest.raises(InvalidQueryError):
        validate_limit(50, 5)


# --- validate_source_filter --------------------------------------------------


def _config(keys):
    return KnowledgeBaseConfig(
        approved_sources={key: SourceSpec(path=f"/tmp/{key}", recursive=True) for key in keys}
    )


def test_source_filter_none_means_no_filter():
    assert validate_source_filter(None, _config(["a"])) is None


def test_source_filter_accepts_approved_keys():
    assert validate_source_filter(["a"], _config(["a", "b"])) == ["a"]


def test_source_filter_rejects_unknown_key():
    with pytest.raises(InvalidSourceFilterError):
        validate_source_filter(["nope"], _config(["a"]))


def test_source_filter_rejects_empty_list():
    with pytest.raises(InvalidSourceFilterError):
        validate_source_filter([], _config(["a"]))


def test_source_filter_normalizes_case():
    assert validate_source_filter(["A"], _config(["a"])) == ["a"]


# --- shared ranking order -----------------------------------------------------


def test_ranked_order_by_sql_is_a_fixed_deterministic_tie_break():
    assert "rank" in RANKED_ORDER_BY_SQL
    assert "chunks.id" in RANKED_ORDER_BY_SQL
    # Both search.py and evidence.py import this exact constant rather than
    # each spelling out their own ORDER BY clause - see their source. Go
    # through sys.modules directly (not attribute lookup on the
    # kernel.knowledge_base package) since that package's __init__.py
    # deliberately rebinds the `search` attribute to the search() function
    # it re-exports, shadowing the submodule of the same name.
    import sys

    import kernel.knowledge_base.evidence  # noqa: F401 - ensures it's imported
    import kernel.knowledge_base.search  # noqa: F401 - ensures it's imported

    search_module = sys.modules["kernel.knowledge_base.search"]
    evidence_module = sys.modules["kernel.knowledge_base.evidence"]

    assert search_module.RANKED_ORDER_BY_SQL is RANKED_ORDER_BY_SQL
    assert evidence_module.RANKED_ORDER_BY_SQL is RANKED_ORDER_BY_SQL
