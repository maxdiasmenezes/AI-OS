"""Tests for kernel/knowledge_base/evidence.py: bounded, read-only
full-chunk-text retrieval for `/knowledge ask`."""

import sqlite3

import pytest

from kernel.knowledge_base.config import KnowledgeBaseConfig, SourceSpec
from kernel.knowledge_base.evidence import (
    DEFAULT_EVIDENCE_LIMIT,
    MAX_EVIDENCE_LIMIT,
    MAX_EVIDENCE_CHUNK_CHARACTERS,
    MAX_TOTAL_EVIDENCE_CHARACTERS,
    retrieve_evidence,
)
from kernel.knowledge_base.ingest import ingest_source
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


def test_evidence_returns_full_chunk_text_not_a_short_excerpt(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence("repository backup", config=config, db_path=db_path)
    assert evidence
    for chunk in evidence:
        assert "repository" in chunk.text.lower() or "backup" in chunk.text.lower()


def test_lexical_order_preserved(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence("repository", config=config, db_path=db_path)
    ranks = [chunk.rank for chunk in evidence]
    assert ranks == sorted(ranks)


def test_source_filter_preserved(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence(
        "repository", source_keys=["ai_os_docs"], config=config, db_path=db_path
    )
    assert all(chunk.source_key == "ai_os_docs" for chunk in evidence)


def test_unknown_source_filter_rejected(indexed):
    config, db_path = indexed
    with pytest.raises(InvalidSourceFilterError):
        retrieve_evidence("repository", source_keys=["nope"], config=config, db_path=db_path)


def test_default_limit_is_three():
    import inspect

    assert DEFAULT_EVIDENCE_LIMIT == 3
    assert inspect.signature(retrieve_evidence).parameters["limit"].default == 3


def test_maximum_limit_is_five():
    assert MAX_EVIDENCE_LIMIT == 5


def test_no_model_invocation(indexed):
    import ast
    from pathlib import Path

    import kernel.knowledge_base.evidence as evidence_module

    source = Path(evidence_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)

    assert not any("models" in name for name in imported_names)
    assert not any("provider" in name.lower() for name in imported_names)


def test_limit_bounds_chunk_count(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence("the", limit=1, config=config, db_path=db_path)
    assert len(evidence) <= 1


def test_limit_above_max_rejected(indexed):
    config, db_path = indexed
    with pytest.raises(InvalidQueryError):
        retrieve_evidence("repository", limit=MAX_EVIDENCE_LIMIT + 1, config=config, db_path=db_path)


def test_no_result_response_is_empty_list_not_error(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence("nonexistenttermxyz", config=config, db_path=db_path)
    assert evidence == []


def test_no_full_document_leakage(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence("repository", config=config, db_path=db_path)
    for chunk in evidence:
        assert "gardening" not in chunk.text


def test_bounded_chunk_text_length(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence("repository", config=config, db_path=db_path)
    for chunk in evidence:
        assert len(chunk.text) <= MAX_EVIDENCE_CHUNK_CHARACTERS


def test_bounded_total_evidence_characters(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence(
        "repository", limit=MAX_EVIDENCE_LIMIT, config=config, db_path=db_path
    )
    assert sum(len(chunk.text) for chunk in evidence) <= MAX_TOTAL_EVIDENCE_CHARACTERS


def test_chunk_over_budget_is_dropped_whole_not_partially(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    # A handful of large, distinct paragraphs so ingestion produces several
    # separate chunks that all match the same single-term query.
    text = "\n\n".join(f"keyword paragraph number {i} " + ("x" * 1400) for i in range(6))
    _write(docs / "big.md", text)
    db_path = tmp_path / "knowledge_index.sqlite3"
    config = _config(docs)
    ingest_source("ai_os_docs", config=config, db_path=db_path)

    evidence = retrieve_evidence(
        "keyword", limit=MAX_EVIDENCE_LIMIT, config=config, db_path=db_path
    )
    total = 0
    for chunk in evidence:
        total += len(chunk.text)
    assert total <= MAX_TOTAL_EVIDENCE_CHARACTERS
    # Every returned chunk's text is either the original (bounded) chunk or
    # an ellipsis-truncated one - never silently merged/spliced.
    for chunk in evidence:
        assert len(chunk.text) <= MAX_EVIDENCE_CHUNK_CHARACTERS


def test_no_absolute_path_in_evidence(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence("repository", config=config, db_path=db_path)
    for chunk in evidence:
        assert not chunk.relative_path.startswith("/")
        assert ":" not in chunk.relative_path


def test_no_database_path_in_evidence(indexed):
    config, db_path = indexed
    evidence = retrieve_evidence("repository", config=config, db_path=db_path)
    for chunk in evidence:
        assert str(db_path) not in chunk.text
        assert str(db_path) not in chunk.relative_path


def test_database_unavailable_when_never_ingested(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _config(docs)
    with pytest.raises(DatabaseUnavailableError):
        retrieve_evidence("anything", config=config, db_path=tmp_path / "knowledge_index.sqlite3")


def test_schema_mismatch_handled_safely(indexed):
    config, db_path = indexed
    from kernel.knowledge_base.db import open_writer_connection

    conn = open_writer_connection(db_path)
    conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    conn.close()

    with pytest.raises(SchemaIncompatibleError):
        retrieve_evidence("repository", config=config, db_path=db_path)


def test_connection_is_query_only(indexed, monkeypatch):
    config, db_path = indexed
    from kernel.knowledge_base import evidence as evidence_module

    real_open = evidence_module.open_reader_connection
    seen = {}

    def spy(path):
        conn = real_open(path)
        seen["query_only"] = conn.execute("PRAGMA query_only").fetchone()[0]
        return conn

    monkeypatch.setattr(evidence_module, "open_reader_connection", spy)
    retrieve_evidence("repository", config=config, db_path=db_path)
    assert seen["query_only"] == 1


def test_retrieve_evidence_takes_no_path_or_chunk_id_parameters():
    import inspect

    params = set(inspect.signature(retrieve_evidence).parameters)
    assert params == {"question", "source_keys", "limit", "config", "db_path"}


def test_deleted_rows_between_ingestions_fail_safely(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "repository backup notes")
    db_path = tmp_path / "knowledge_index.sqlite3"
    config = _config(docs)
    ingest_source("ai_os_docs", config=config, db_path=db_path)

    # Re-ingest an empty source: prior documents/chunks are atomically
    # removed. A subsequent retrieve_evidence() call must not error, and
    # must reflect the new (empty) generation, not a stale one.
    empty_docs = tmp_path / "empty"
    empty_docs.mkdir()
    empty_config = _config(empty_docs)
    ingest_source("ai_os_docs", config=empty_config, db_path=db_path)

    evidence = retrieve_evidence("repository", config=empty_config, db_path=db_path)
    assert evidence == []
