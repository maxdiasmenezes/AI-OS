"""Tests for kernel/knowledge_base/search.py: query sanitization, safe FTS5
transformation, ranking, filtering, and result shape."""

import pytest

from kernel.knowledge_base.config import KnowledgeBaseConfig, SourceSpec
from kernel.knowledge_base.ingest import ingest_source
from kernel.knowledge_base.search import (
    MAX_QUERY_CHARACTERS,
    MAX_RESULT_LIMIT,
    build_match_expression,
    search,
)
from kernel.knowledge_base.types import (
    DatabaseUnavailableError,
    InvalidQueryError,
    InvalidSourceFilterError,
    SchemaIncompatibleError,
)


def _config(source_path, key="ai_os_docs"):
    return KnowledgeBaseConfig(approved_sources={key: SourceSpec(path=str(source_path), recursive=True)})


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def indexed(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "The quick brown fox jumps over the lazy dog. Repository backup notes.")
    _write(docs / "b.md", "A second document about repository health and backup strategy.")
    _write(docs / "c.md", "Completely unrelated content about gardening and cooking.")
    db_path = tmp_path / "knowledge_index.sqlite3"
    config = _config(docs)
    ingest_source("ai_os_docs", config=config, db_path=db_path)
    return config, db_path


# --- build_match_expression: safe transformation --------------------------


def test_single_term_becomes_quoted_literal():
    assert build_match_expression("hello") == '"hello"'


def test_multiple_terms_joined_with_and():
    assert build_match_expression("hello world") == '"hello" AND "world"'


def test_terms_deduplicated_case_insensitively_preserving_order():
    assert build_match_expression("Hello hello WORLD world") == '"Hello" AND "WORLD"'


def test_quotes_are_neutralized():
    expr = build_match_expression('"unterminated quote term')
    assert '"' not in expr.replace('"unterminated"', "").replace('"quote"', "").replace('"term"', "")
    assert "AND" in expr


def test_wildcard_is_neutralized():
    expr = build_match_expression("term*")
    assert expr == '"term"'


def test_near_or_not_treated_as_literals():
    expr = build_match_expression("NEAR OR NOT")
    assert expr == '"NEAR" AND "OR" AND "NOT"'


def test_column_filter_syntax_neutralized():
    expr = build_match_expression("title:term")
    assert expr == '"title" AND "term"'


def test_parentheses_and_punctuation_stripped():
    expr = build_match_expression("(hello) [world]")
    assert expr == '"hello" AND "world"'


def test_terms_capped_at_max_query_terms():
    query = " ".join(f"term{i}" for i in range(30))
    expr = build_match_expression(query)
    assert expr.count("AND") == 19  # 20 terms joined by AND = 19 ANDs


def test_blank_query_rejected():
    with pytest.raises(InvalidQueryError):
        build_match_expression("   ")


def test_query_with_only_punctuation_rejected():
    with pytest.raises(InvalidQueryError):
        build_match_expression("***???")


def test_oversized_query_rejected():
    with pytest.raises(InvalidQueryError):
        build_match_expression("a" * (MAX_QUERY_CHARACTERS + 1))


def test_embedded_control_character_rejected():
    with pytest.raises(InvalidQueryError):
        build_match_expression("hello\x01world")


def test_trailing_newline_is_stripped_not_rejected():
    assert build_match_expression("hello\n") == '"hello"'


def test_non_string_query_rejected():
    with pytest.raises(InvalidQueryError):
        build_match_expression(12345)


# --- search(): end-to-end behavior -----------------------------------------


def test_exact_term_search_returns_matching_documents(indexed):
    config, db_path = indexed
    hits = search("gardening", config=config, db_path=db_path)
    assert len(hits) == 1
    assert hits[0].relative_path == "c.md"


def test_multi_term_search_uses_and_semantics(indexed):
    config, db_path = indexed
    hits = search("repository backup", config=config, db_path=db_path)
    paths = {h.relative_path for h in hits}
    assert paths == {"a.md", "b.md"}


def test_multi_term_search_excludes_partial_matches(indexed):
    config, db_path = indexed
    hits = search("repository gardening", config=config, db_path=db_path)
    assert hits == []  # no single chunk contains both terms


def test_no_results_returns_empty_list_not_error(indexed):
    config, db_path = indexed
    hits = search("nonexistenttermxyz", config=config, db_path=db_path)
    assert hits == []


def test_bm25_ranking_orders_results(indexed):
    config, db_path = indexed
    hits = search("repository", config=config, db_path=db_path)
    ranks = [h.rank for h in hits]
    assert ranks == sorted(ranks)


def test_deterministic_tie_breaking(indexed):
    config, db_path = indexed
    first = search("repository", config=config, db_path=db_path)
    second = search("repository", config=config, db_path=db_path)
    assert [h.chunk_id for h in first] == [h.chunk_id for h in second]


def test_source_filter_restricts_results(indexed):
    config, db_path = indexed
    hits = search("repository", source_keys=["ai_os_docs"], config=config, db_path=db_path)
    assert all(h.source_key == "ai_os_docs" for h in hits)


def test_unknown_source_filter_rejected(indexed):
    config, db_path = indexed
    with pytest.raises(InvalidSourceFilterError):
        search("repository", source_keys=["nope"], config=config, db_path=db_path)


def test_empty_source_filter_list_rejected(indexed):
    config, db_path = indexed
    with pytest.raises(InvalidSourceFilterError):
        search("repository", source_keys=[], config=config, db_path=db_path)


def test_result_count_bounded_by_limit(indexed):
    config, db_path = indexed
    hits = search("the", limit=1, config=config, db_path=db_path)
    assert len(hits) <= 1


def test_limit_out_of_range_rejected(indexed):
    config, db_path = indexed
    with pytest.raises(InvalidQueryError):
        search("repository", limit=0, config=config, db_path=db_path)
    with pytest.raises(InvalidQueryError):
        search("repository", limit=MAX_RESULT_LIMIT + 1, config=config, db_path=db_path)


def test_no_absolute_path_in_results(indexed):
    config, db_path = indexed
    hits = search("repository", config=config, db_path=db_path)
    for h in hits:
        assert not h.relative_path.startswith("/")
        assert ":" not in h.relative_path  # no Windows drive letter either


def test_excerpts_are_bounded(indexed):
    config, db_path = indexed
    hits = search("repository", config=config, db_path=db_path)
    for h in hits:
        assert len(h.excerpt) <= 300


def test_no_full_document_leakage(indexed):
    config, db_path = indexed
    hits = search("repository", config=config, db_path=db_path)
    for h in hits:
        assert "gardening" not in h.excerpt  # content from an unrelated doc


def test_database_unavailable_when_never_ingested(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _config(docs)
    with pytest.raises(DatabaseUnavailableError):
        search("anything", config=config, db_path=tmp_path / "knowledge_index.sqlite3")


def test_schema_mismatch_behavior(indexed):
    config, db_path = indexed
    from kernel.knowledge_base.db import open_writer_connection

    conn = open_writer_connection(db_path)
    conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    conn.close()

    with pytest.raises(SchemaIncompatibleError):
        search("repository", config=config, db_path=db_path)


def test_malformed_syntax_cannot_produce_raw_sqlite_error(indexed):
    config, db_path = indexed
    # Every one of these would be a syntax error if passed raw to FTS5's
    # MATCH. After sanitization each must either return normally or raise
    # the typed InvalidQueryError (e.g. "((()))" extracts no terms at
    # all) - never a raw sqlite3.Error/OperationalError.
    for query in ['"unterminated', "a OR b", "a -b", "col:term", "term*", "((()))"]:
        try:
            search(query, config=config, db_path=db_path)
        except InvalidQueryError:
            pass
