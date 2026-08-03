"""
Atomic, per-source ingestion orchestration for kernel/knowledge_base/
(Milestone 36).

A caller supplies only a symbolic source key - never a path, glob, or
extension list. The entire ingestion for one source runs inside a single
SQLite write transaction (BEGIN IMMEDIATE ... COMMIT): any failure at any
point - an invalid file, a safety-limit violation, or an unexpected
database error - rolls the whole transaction back, leaving the prior
searchable generation for that source completely untouched. Nothing here
ever partially commits.

Unchanged files (same relative path, same content hash as the previous
generation) are left untouched - their document and chunk rows, and
therefore their chunk_id values, are never rewritten. Changed files are
deleted (cascading to their chunks and FTS rows) and reinserted. Files no
longer present in the source are deleted once every candidate has been
processed successfully. A source with zero supported files is a valid,
empty ingestion that atomically removes any previously indexed documents
for that source.
"""

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from kernel.knowledge_base.chunking import (
    InvalidDocumentEncodingError,
    chunk_normalized_text,
    compute_chunk_id,
    compute_chunk_text_hash,
    compute_content_hash,
    compute_document_key,
    normalize_text,
)
from kernel.knowledge_base.config import KnowledgeBaseConfig, load_knowledge_base_config
from kernel.knowledge_base.db import open_writer_connection, resolve_database_path
from kernel.knowledge_base.traversal import (
    list_source_candidates,
    read_source_file,
    resolve_canonical_root,
)
from kernel.knowledge_base.types import (
    DatabaseLockedError,
    DatabaseUnavailableError,
    IngestionFailedError,
    IngestResult,
    InvalidSourceContentError,
    KnowledgeBaseError,
    SourceLimitExceededError,
    UnknownSourceError,
)

MAX_TOTAL_CHUNKS_PER_INGESTION = 200_000


def _relative_path_key(relative_path: str) -> str:
    """A case-normalized key used only for internal document identity and
    diffing - never returned to callers. On Windows (a case-insensitive
    filesystem), two differently-cased spellings of the same path must
    collide to one document; elsewhere the exact relative path is
    already the correct, case-sensitive key."""

    if os.name == "nt":
        return os.path.normcase(relative_path)
    return relative_path


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _run_ingestion_transaction(conn: sqlite3.Connection, source_key: str, canonical_root: Path, candidates) -> dict:
    conn.execute("INSERT OR IGNORE INTO sources(source_key) VALUES (?)", (source_key,))

    existing_rows = conn.execute(
        "SELECT id, relative_path_key, content_hash, chunk_count FROM documents WHERE source_key = ?",
        (source_key,),
    ).fetchall()
    existing = {row[1]: (row[0], row[2], row[3]) for row in existing_rows}

    seen_keys: set[str] = set()
    documents_indexed = 0
    unchanged_documents = 0
    running_total_chunks = 0

    for candidate in candidates:
        relative_path_key = _relative_path_key(candidate.relative_path)
        seen_keys.add(relative_path_key)

        raw_bytes, opened_stat = read_source_file(canonical_root, candidate)
        try:
            normalized_text = normalize_text(raw_bytes)
        except InvalidDocumentEncodingError as exc:
            raise InvalidSourceContentError(
                "source contains unsupported or invalid content"
            ) from exc

        content_hash = compute_content_hash(normalized_text)

        existing_entry = existing.get(relative_path_key)
        if existing_entry is not None:
            old_id, old_hash, old_chunk_count = existing_entry
            if old_hash == content_hash:
                unchanged_documents += 1
                running_total_chunks += old_chunk_count
                if running_total_chunks > MAX_TOTAL_CHUNKS_PER_INGESTION:
                    raise SourceLimitExceededError("source exceeds the total chunk limit")
                continue
            conn.execute("DELETE FROM documents WHERE id = ?", (old_id,))

        chunk_spans = chunk_normalized_text(normalized_text)
        running_total_chunks += len(chunk_spans)
        if running_total_chunks > MAX_TOTAL_CHUNKS_PER_INGESTION:
            raise SourceLimitExceededError("source exceeds the total chunk limit")

        document_key = compute_document_key(source_key, relative_path_key)
        ingested_at = datetime.now(timezone.utc).isoformat()

        cursor = conn.execute(
            """
            INSERT INTO documents(
                source_key, relative_path, relative_path_key, content_hash,
                byte_size, mtime_ns, chunk_count, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_key,
                candidate.relative_path,
                relative_path_key,
                content_hash,
                opened_stat.st_size,
                opened_stat.st_mtime_ns,
                len(chunk_spans),
                ingested_at,
            ),
        )
        document_id = cursor.lastrowid

        for span in chunk_spans:
            chunk_text_hash = compute_chunk_text_hash(span.text)
            chunk_id = compute_chunk_id(document_key, content_hash, span.ordinal, chunk_text_hash)
            conn.execute(
                """
                INSERT INTO chunks(document_id, chunk_ordinal, chunk_id, text, char_start, char_end)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (document_id, span.ordinal, chunk_id, span.text, span.char_start, span.char_end),
            )

        documents_indexed += 1

    removed_keys = set(existing) - seen_keys
    for key in removed_keys:
        conn.execute("DELETE FROM documents WHERE id = ?", (existing[key][0],))
    removed_documents = len(removed_keys)

    document_count = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE source_key = ?", (source_key,)
    ).fetchone()[0]
    chunk_count = conn.execute(
        "SELECT COALESCE(SUM(chunk_count), 0) FROM documents WHERE source_key = ?", (source_key,)
    ).fetchone()[0]

    current_generation = conn.execute(
        "SELECT generation FROM sources WHERE source_key = ?", (source_key,)
    ).fetchone()[0]
    new_generation = current_generation + 1
    last_ingested_at = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        UPDATE sources
        SET generation = ?, document_count = ?, chunk_count = ?, last_ingested_at = ?
        WHERE source_key = ?
        """,
        (new_generation, document_count, chunk_count, last_ingested_at, source_key),
    )

    return {
        "source_key": source_key,
        "documents_indexed": documents_indexed,
        "unchanged_documents": unchanged_documents,
        "removed_documents": removed_documents,
        "chunks_indexed": chunk_count,
        "generation": new_generation,
    }


def ingest_source(
    source_key: str,
    *,
    config: KnowledgeBaseConfig | None = None,
    db_path: Path | None = None,
) -> IngestResult:
    """Ingest one approved, symbolic source key. Raises a
    KnowledgeBaseError subclass (see types.py) for every recognized
    failure; the prior generation is always left intact on failure."""

    resolved_config = config if config is not None else load_knowledge_base_config()
    spec = resolved_config.approved_sources.get(source_key)
    if spec is None:
        raise UnknownSourceError("unknown source key")

    canonical_root = resolve_canonical_root(spec.path)
    candidates = list_source_candidates(canonical_root, spec.recursive)

    resolved_db_path = db_path if db_path is not None else resolve_database_path()

    start = time.monotonic()
    conn = open_writer_connection(resolved_db_path)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorname", "") == "SQLITE_BUSY":
                raise DatabaseLockedError("knowledge database is busy") from exc
            raise DatabaseUnavailableError("knowledge database is unavailable") from exc

        try:
            result = _run_ingestion_transaction(conn, source_key, canonical_root, candidates)
        except KnowledgeBaseError:
            _safe_rollback(conn)
            raise
        except Exception as exc:
            _safe_rollback(conn)
            raise IngestionFailedError("ingestion failed") from exc

        conn.execute("COMMIT")
    finally:
        conn.close()

    elapsed_seconds = time.monotonic() - start
    return IngestResult(elapsed_seconds=elapsed_seconds, **result)
