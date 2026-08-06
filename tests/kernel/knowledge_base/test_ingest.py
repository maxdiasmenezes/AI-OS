"""Tests for kernel/knowledge_base/ingest.py: atomic, per-source ingestion,
diffing, rollback, and isolation. Every test uses tmp_path sources and a
tmp_path database."""

import os
import sqlite3

import pytest

from kernel.knowledge_base.config import KnowledgeBaseConfig, SourceSpec
from kernel.knowledge_base.db import open_reader_connection, open_writer_connection
from kernel.knowledge_base.ingest import _relative_path_key, ingest_source
from kernel.knowledge_base.search import search
from kernel.knowledge_base.traversal import CandidateFile
from kernel.knowledge_base.types import (
    DatabaseLockedError,
    IngestionFailedError,
    InvalidSourceContentError,
    SourceLimitExceededError,
    UnknownSourceError,
)

pytestmark_windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only behavior")


def _config(source_path, recursive=True, key="ai_os_docs"):
    return KnowledgeBaseConfig(
        approved_sources={key: SourceSpec(path=str(source_path), recursive=recursive)}
    )


def _write(path, text="content"):
    path.write_text(text, encoding="utf-8")
    return path


def _db_path(tmp_path):
    return tmp_path / "knowledge_index.sqlite3"


# --- basic ingestion ---------------------------------------------------


def test_unknown_source_key_raises(tmp_path):
    config = KnowledgeBaseConfig(approved_sources={})
    with pytest.raises(UnknownSourceError):
        ingest_source("nope", config=config, db_path=_db_path(tmp_path))


def test_ingest_new_source_indexes_documents(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "Hello world, this is a test document.")

    result = ingest_source("ai_os_docs", config=_config(docs), db_path=_db_path(tmp_path))

    assert result.source_key == "ai_os_docs"
    assert result.documents_indexed == 1
    assert result.unchanged_documents == 0
    assert result.removed_documents == 0
    assert result.chunks_indexed == 1
    assert result.generation == 1


def test_empty_source_is_valid(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()

    result = ingest_source("ai_os_docs", config=_config(docs), db_path=_db_path(tmp_path))

    assert result.documents_indexed == 0
    assert result.chunks_indexed == 0


def test_idempotent_reingestion_of_unchanged_content(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "Stable content that never changes.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    first = ingest_source("ai_os_docs", config=config, db_path=db_path)
    second = ingest_source("ai_os_docs", config=config, db_path=db_path)

    assert second.documents_indexed == 0
    assert second.unchanged_documents == 1
    assert second.removed_documents == 0
    assert second.chunks_indexed == first.chunks_indexed
    assert second.generation == first.generation + 1


def test_unchanged_documents_keep_same_row_and_chunk_ids(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "Stable content that never changes.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)
    conn = open_reader_connection(db_path)
    first_chunk_ids = {
        row[0] for row in conn.execute("SELECT chunk_id FROM chunks").fetchall()
    }
    first_doc_ids = {row[0] for row in conn.execute("SELECT id FROM documents").fetchall()}
    conn.close()

    ingest_source("ai_os_docs", config=config, db_path=db_path)
    conn = open_reader_connection(db_path)
    second_chunk_ids = {
        row[0] for row in conn.execute("SELECT chunk_id FROM chunks").fetchall()
    }
    second_doc_ids = {row[0] for row in conn.execute("SELECT id FROM documents").fetchall()}
    conn.close()

    assert first_chunk_ids == second_chunk_ids
    assert first_doc_ids == second_doc_ids


def test_changed_document_is_replaced(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    f = _write(docs / "a.md", "Original content.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)
    _write(f, "Completely different content now.")
    result = ingest_source("ai_os_docs", config=config, db_path=db_path)

    assert result.documents_indexed == 1
    assert result.unchanged_documents == 0

    hits = search("Completely different", config=config, db_path=db_path)
    assert len(hits) == 1
    old_hits = search("Original content", config=config, db_path=db_path)
    assert old_hits == []


def test_deleted_file_is_removed_after_successful_ingestion(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    f = _write(docs / "a.md", "Content to be removed later.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)
    f.unlink()
    result = ingest_source("ai_os_docs", config=config, db_path=db_path)

    assert result.removed_documents == 1
    assert result.documents_indexed == 0

    conn = open_reader_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM documents WHERE source_key = 'ai_os_docs'").fetchone()[0]
    conn.close()
    assert count == 0


def test_empty_source_after_prior_ingestion_removes_all_documents(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    f = _write(docs / "a.md", "Some content.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)
    f.unlink()
    result = ingest_source("ai_os_docs", config=config, db_path=db_path)

    assert result.documents_indexed == 0
    assert result.removed_documents == 1
    assert result.chunks_indexed == 0


def test_same_filename_in_different_directories_stays_distinct(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "sub1").mkdir()
    (docs / "sub2").mkdir()
    _write(docs / "sub1" / "same.md", "Content in sub one.")
    _write(docs / "sub2" / "same.md", "Content in sub two.")

    result = ingest_source("ai_os_docs", config=_config(docs), db_path=_db_path(tmp_path))

    assert result.documents_indexed == 2


def test_relative_path_key_is_case_insensitive_on_windows_only():
    if os.name == "nt":
        assert _relative_path_key("Notes.md") == _relative_path_key("NOTES.MD")
    else:
        assert _relative_path_key("Notes.md") != _relative_path_key("NOTES.MD")


@pytestmark_windows_only
def test_windows_case_variation_collapses_to_single_document_preserving_display_casing(
    tmp_path, monkeypatch
):
    # Real NTFS traversal can never surface two case-variant spellings of
    # the same file in one scan (it is case-insensitive at the filesystem
    # level), so the two candidates below are constructed directly to
    # simulate what traversal reports across two separate ingestions of
    # the same physical file after an external case-only rename.
    import kernel.knowledge_base.ingest as ingest_module

    docs = tmp_path / "docs"
    docs.mkdir()
    f = _write(docs / "Notes.md", "Same content both times.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    first_candidate = CandidateFile(canonical_path=f, relative_path="Notes.md", lstat=os.lstat(f))
    monkeypatch.setattr(
        ingest_module, "list_source_candidates", lambda root, recursive: [first_candidate]
    )
    first_result = ingest_source("ai_os_docs", config=config, db_path=db_path)
    assert first_result.documents_indexed == 1

    second_candidate = CandidateFile(canonical_path=f, relative_path="NOTES.MD", lstat=os.lstat(f))
    monkeypatch.setattr(
        ingest_module, "list_source_candidates", lambda root, recursive: [second_candidate]
    )
    second_result = ingest_source("ai_os_docs", config=config, db_path=db_path)

    assert second_result.documents_indexed == 0
    assert second_result.unchanged_documents == 1
    assert second_result.removed_documents == 0

    conn = open_reader_connection(db_path)
    rows = conn.execute(
        "SELECT relative_path FROM documents WHERE source_key = 'ai_os_docs'"
    ).fetchall()
    conn.close()
    assert [row[0] for row in rows] == ["Notes.md"]  # original display casing preserved


def test_unique_constraint_prevents_duplicate_relative_path_key(tmp_path):
    # Schema-level guarantee (portable across platforms): two document
    # rows for the same source with the same relative_path_key can never
    # coexist, regardless of what ingest.py's own diffing logic does.
    db_path = _db_path(tmp_path)
    conn = open_writer_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO sources(source_key) VALUES ('ai_os_docs')")
        conn.execute(
            """
            INSERT INTO documents(
                source_key, relative_path, relative_path_key, content_hash,
                byte_size, mtime_ns, chunk_count, ingested_at
            ) VALUES ('ai_os_docs', 'Notes.md', 'notes.md', 'hash1', 1, 1, 1, 'now')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO documents(
                    source_key, relative_path, relative_path_key, content_hash,
                    byte_size, mtime_ns, chunk_count, ingested_at
                ) VALUES ('ai_os_docs', 'NOTES.MD', 'notes.md', 'hash2', 1, 1, 1, 'now')
                """
            )
        conn.execute("ROLLBACK")
    finally:
        conn.close()


# --- Markdown heading-aware chunking on ingest (Milestone 38.2B) ----------


def test_md_files_use_markdown_aware_chunking(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    text = (
        "# First\n\n"
        + ("alpha " * 300)
        + "\n\n# Second\n\nShort body under the second heading."
    )
    _write(docs / "a.md", text)
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)

    conn = open_reader_connection(db_path)
    rows = conn.execute(
        "SELECT char_start, char_end, text FROM chunks ORDER BY chunk_ordinal"
    ).fetchall()
    conn.close()

    second_heading_offset = text.index("# Second")
    assert any(row[0] == second_heading_offset for row in rows)
    for start, end, _chunk_text in rows:
        assert not (start < second_heading_offset < end)


def test_uppercase_md_suffix_uses_markdown_aware_chunking(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    text = (
        "# First\n\n"
        + ("alpha " * 300)
        + "\n\n# Second\n\nShort body under the second heading."
    )
    _write(docs / "A.MD", text)
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)

    conn = open_reader_connection(db_path)
    rows = conn.execute("SELECT char_start, char_end FROM chunks").fetchall()
    conn.close()

    second_heading_offset = text.index("# Second")
    assert any(row[0] == second_heading_offset for row in rows)
    for start, end in rows:
        assert not (start < second_heading_offset < end)


def test_txt_files_retain_paragraph_only_chunking(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    text = "# This looks like a heading\n\nA short second paragraph that should merge."
    _write(docs / "a.txt", text)
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)

    conn = open_reader_connection(db_path)
    rows = conn.execute("SELECT text FROM chunks").fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0][0] == text


def test_markdown_ingestion_is_deterministic(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    text = "# Heading\n\n" + ("word " * 400) + "\n\n## Sub\n\nMore content here."
    _write(docs / "a.md", text)
    config = _config(docs)

    db_path_1 = tmp_path / "one.sqlite3"
    db_path_2 = tmp_path / "two.sqlite3"
    ingest_source("ai_os_docs", config=config, db_path=db_path_1)
    ingest_source("ai_os_docs", config=config, db_path=db_path_2)

    conn1 = open_reader_connection(db_path_1)
    rows1 = conn1.execute(
        "SELECT char_start, char_end, text FROM chunks ORDER BY chunk_ordinal"
    ).fetchall()
    conn1.close()
    conn2 = open_reader_connection(db_path_2)
    rows2 = conn2.execute(
        "SELECT char_start, char_end, text FROM chunks ORDER BY chunk_ordinal"
    ).fetchall()
    conn2.close()

    assert rows1 == rows2


def test_changed_markdown_content_replaces_chunks_transactionally(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    f = _write(docs / "a.md", "# Original\n\nOriginal body text only.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)
    _write(f, "# Replaced\n\nAll new content.\n\n## Second\n\nMore new content.")
    result = ingest_source("ai_os_docs", config=config, db_path=db_path)

    assert result.documents_indexed == 1
    assert result.unchanged_documents == 0

    conn = open_reader_connection(db_path)
    rows = conn.execute("SELECT text FROM chunks").fetchall()
    conn.close()

    texts = [r[0] for r in rows]
    assert not any("Original" in t for t in texts)
    assert any(t.startswith("# Replaced") for t in texts)
    assert any(t.startswith("## Second") for t in texts)


def test_ingestion_does_not_change_schema_version(tmp_path):
    from kernel.knowledge_base.db import SCHEMA_VERSION

    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "# Heading\n\nBody text.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)

    conn = open_reader_connection(db_path)
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    conn.close()

    assert SCHEMA_VERSION == 1
    assert row[0] == str(SCHEMA_VERSION)


# --- atomicity and rollback ----------------------------------------------


def test_invalid_utf8_rolls_back_entire_source(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "good.md", "Valid content here.")
    (docs / "bad.md").write_bytes(b"\xff\xfe\x00\x01broken")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    with pytest.raises(InvalidSourceContentError):
        ingest_source("ai_os_docs", config=config, db_path=db_path)

    # Nothing from the failed ingestion should be searchable/present.
    conn = open_writer_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM documents WHERE source_key = 'ai_os_docs'").fetchone()[0]
    conn.close()
    assert count == 0


def test_nul_byte_content_rolls_back(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "bad.md").write_bytes("hello\x00world".encode("utf-8"))
    db_path = _db_path(tmp_path)
    config = _config(docs)

    with pytest.raises(InvalidSourceContentError):
        ingest_source("ai_os_docs", config=config, db_path=db_path)


def test_second_invalid_file_rolls_back_first_files_changes_too(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a_good.md", "Good content in the first file.")
    (docs / "b_bad.md").write_bytes(b"\xff\xfe\x00\x01broken")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    with pytest.raises(InvalidSourceContentError):
        ingest_source("ai_os_docs", config=config, db_path=db_path)

    conn = open_writer_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    assert count == 0


def test_source_remains_searchable_after_failed_replacement(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "Original searchable content.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)

    # Introduce a second, invalid file - the whole re-ingestion must fail
    # and roll back, leaving the prior generation intact.
    (docs / "bad.md").write_bytes(b"\xff\xfe\x00\x01broken")
    with pytest.raises(InvalidSourceContentError):
        ingest_source("ai_os_docs", config=config, db_path=db_path)

    hits = search("Original searchable", config=config, db_path=db_path)
    assert len(hits) == 1


def test_file_count_limit_rolls_back(tmp_path, monkeypatch):
    import kernel.knowledge_base.traversal as traversal_module

    monkeypatch.setattr(traversal_module, "MAX_FILES_PER_SOURCE", 1)
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "one")
    _write(docs / "b.md", "two")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    with pytest.raises(SourceLimitExceededError):
        ingest_source("ai_os_docs", config=config, db_path=db_path)

    conn = open_writer_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    assert count == 0


def test_total_byte_limit_rolls_back(tmp_path, monkeypatch):
    import kernel.knowledge_base.traversal as traversal_module

    monkeypatch.setattr(traversal_module, "MAX_TOTAL_SOURCE_BYTES", 5)
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "this is more than five bytes")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    with pytest.raises(SourceLimitExceededError):
        ingest_source("ai_os_docs", config=config, db_path=db_path)


def test_total_chunk_limit_rolls_back(tmp_path, monkeypatch):
    import kernel.knowledge_base.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "MAX_TOTAL_CHUNKS_PER_INGESTION", 1)
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "\n\n".join(f"Paragraph {i}: " + ("word " * 100) for i in range(5)))
    db_path = _db_path(tmp_path)
    config = _config(docs)

    with pytest.raises(SourceLimitExceededError):
        ingest_source("ai_os_docs", config=config, db_path=db_path)

    conn = open_writer_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    assert count == 0


def test_injected_sqlite_write_failure_rolls_back(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "content one")
    _write(docs / "b.md", "content two")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    import kernel.knowledge_base.ingest as ingest_module

    original = ingest_module._run_ingestion_transaction
    call_count = {"n": 0}

    def flaky(conn, source_key, canonical_root, candidates):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise sqlite3.OperationalError("injected failure")
        return original(conn, source_key, canonical_root, candidates)

    monkeypatch.setattr(ingest_module, "_run_ingestion_transaction", flaky)

    with pytest.raises(IngestionFailedError):
        ingest_source("ai_os_docs", config=config, db_path=db_path)

    conn = open_writer_connection(db_path)
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    assert count == 0


def test_database_locked_maps_to_fixed_error(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "content")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    # Prime the schema first so the held transaction below has something
    # to lock against.
    conn = open_writer_connection(db_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR IGNORE INTO sources(source_key) VALUES ('holder')")

    try:
        with pytest.raises(DatabaseLockedError):
            ingest_source("ai_os_docs", config=config, db_path=db_path)
    finally:
        conn.execute("ROLLBACK")
        conn.close()


# --- isolation between sources -------------------------------------------


def test_multiple_sources_remain_isolated(tmp_path):
    docs_a = tmp_path / "a"
    docs_a.mkdir()
    _write(docs_a / "one.md", "Alpha source content.")
    docs_b = tmp_path / "b"
    docs_b.mkdir()
    _write(docs_b / "one.md", "Beta source content.")

    config = KnowledgeBaseConfig(
        approved_sources={
            "source_a": SourceSpec(path=str(docs_a), recursive=True),
            "source_b": SourceSpec(path=str(docs_b), recursive=True),
        }
    )
    db_path = _db_path(tmp_path)

    ingest_source("source_a", config=config, db_path=db_path)
    ingest_source("source_b", config=config, db_path=db_path)

    hits_a = search("Alpha", config=config, db_path=db_path)
    hits_b = search("Beta", config=config, db_path=db_path)
    assert {h.source_key for h in hits_a} == {"source_a"}
    assert {h.source_key for h in hits_b} == {"source_b"}


def test_ingesting_one_source_does_not_damage_another(tmp_path):
    docs_a = tmp_path / "a"
    docs_a.mkdir()
    _write(docs_a / "one.md", "Alpha content that should survive.")
    docs_b = tmp_path / "b"
    docs_b.mkdir()
    _write(docs_b / "one.md", "Beta content, changes soon.")

    config = KnowledgeBaseConfig(
        approved_sources={
            "source_a": SourceSpec(path=str(docs_a), recursive=True),
            "source_b": SourceSpec(path=str(docs_b), recursive=True),
        }
    )
    db_path = _db_path(tmp_path)

    ingest_source("source_a", config=config, db_path=db_path)
    ingest_source("source_b", config=config, db_path=db_path)

    (docs_b / "one.md").write_text("Beta content, now changed.", encoding="utf-8")
    ingest_source("source_b", config=config, db_path=db_path)

    hits_a = search("Alpha content", config=config, db_path=db_path)
    assert len(hits_a) == 1


def test_search_during_ingestion_sees_prior_committed_generation(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "First generation content.")
    db_path = _db_path(tmp_path)
    config = _config(docs)

    ingest_source("ai_os_docs", config=config, db_path=db_path)

    # Open a writer transaction and hold it uncommitted - a reader
    # connection opened now must still see the prior committed generation,
    # not a half-written one (WAL snapshot isolation).
    writer = open_writer_connection(db_path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "UPDATE documents SET content_hash = 'placeholder' WHERE source_key = 'ai_os_docs'"
    )

    try:
        hits = search("First generation", config=config, db_path=db_path)
        assert len(hits) == 1
    finally:
        writer.execute("ROLLBACK")
        writer.close()
