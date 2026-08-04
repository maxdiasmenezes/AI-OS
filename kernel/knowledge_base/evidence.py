"""
Safe, read-only evidence retrieval for `/knowledge ask` (Milestone 38).

This is a sibling to search.py, not a modification of it: search.py's
public search() and its existing, already-reviewed SQL are untouched.
retrieve_evidence() reuses the exact same query-building, validation, and
ranking-order mechanics from kernel/knowledge_base/query.py, and runs one
read-only query nearly identical to search()'s - the only difference is
selecting chunks.text (the full stored chunk, itself already capped at
ingest time to chunking.MAX_CHUNK_CHARACTERS) instead of a bounded
snippet(). There is no second, caller-controlled chunk-ID lookup: full
text and ranking both come from the single ranked query already scoped to
this request, so "the immediately preceding internal search" is the only
search that ever happens.

Nothing here invokes a model or contacts a network; the only connection
opened is db.open_reader_connection(), which sets PRAGMA query_only=ON.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from kernel.knowledge_base.config import KnowledgeBaseConfig, load_knowledge_base_config
from kernel.knowledge_base.db import open_reader_connection, resolve_database_path
from kernel.knowledge_base.query import (
    RANKED_ORDER_BY_SQL,
    build_match_expression,
    validate_limit,
    validate_source_filter,
)
from kernel.knowledge_base.types import SearchFailedError

DEFAULT_EVIDENCE_LIMIT = 3
MAX_EVIDENCE_LIMIT = 5
MAX_EVIDENCE_CHUNK_CHARACTERS = 1_500
MAX_TOTAL_EVIDENCE_CHARACTERS = 7_500

_ELLIPSIS = "..."


@dataclass(frozen=True)
class EvidenceChunk:
    """One ranked evidence chunk for `/knowledge ask` - full (bounded)
    chunk text rather than search.py's excerpt. No chunk_id: nothing
    downstream needs it (ordering is fully decided by the SQL ORDER BY,
    never recomputed from it), so it is never carried past this query."""

    source_key: str
    relative_path: str
    chunk_ordinal: int
    text: str
    rank: float


def _bounded_chunk_text(text: str) -> str:
    """Cap one chunk's text at MAX_EVIDENCE_CHUNK_CHARACTERS, preserving
    valid Unicode (Python string slicing is always codepoint-safe) and a
    visible ellipsis when truncated. Defensive: real chunks are already
    capped smaller at ingest time (chunking.MAX_CHUNK_CHARACTERS), so this
    should rarely if ever actually truncate."""

    if len(text) <= MAX_EVIDENCE_CHUNK_CHARACTERS:
        return text
    keep = max(MAX_EVIDENCE_CHUNK_CHARACTERS - len(_ELLIPSIS), 0)
    return text[:keep] + _ELLIPSIS


def retrieve_evidence(
    question: str,
    source_keys: list[str] | None = None,
    limit: int = DEFAULT_EVIDENCE_LIMIT,
    *,
    config: KnowledgeBaseConfig | None = None,
    db_path: Path | None = None,
) -> list[EvidenceChunk]:
    """Read-only lexical evidence retrieval for grounded answering. Raises
    InvalidQueryError, InvalidSourceFilterError, DatabaseUnavailableError,
    SchemaIncompatibleError, or SearchFailedError (see types.py) for every
    recognized failure - the same error vocabulary search() uses. An
    empty list means no results - not an error.

    Never accepts a path, document ID, chunk ID, SQL, or database
    location from the caller; never retrieves a complete document. Limits
    are applied deterministically over the already-ranked rows: at most
    `limit` chunks (itself capped at MAX_EVIDENCE_LIMIT), at most
    MAX_EVIDENCE_CHUNK_CHARACTERS per chunk, and at most
    MAX_TOTAL_EVIDENCE_CHARACTERS in total - a chunk that would push the
    running total over budget is dropped whole, never included partially,
    so the returned prefix always ends on a complete evidence-chunk
    boundary.
    """

    match_expression = build_match_expression(question)
    validated_limit = validate_limit(limit, MAX_EVIDENCE_LIMIT)

    resolved_config = config if config is not None else load_knowledge_base_config()
    normalized_filter = validate_source_filter(source_keys, resolved_config)

    resolved_db_path = db_path if db_path is not None else resolve_database_path()

    params: list = [match_expression]
    source_filter_sql = ""
    if normalized_filter:
        placeholders = ", ".join("?" for _ in normalized_filter)
        source_filter_sql = f" AND documents.source_key IN ({placeholders})"
        params.extend(normalized_filter)
    params.append(validated_limit)

    conn = open_reader_connection(resolved_db_path)
    try:
        try:
            rows = conn.execute(
                f"""
                SELECT
                    documents.source_key,
                    documents.relative_path,
                    chunks.chunk_ordinal,
                    chunks.text,
                    bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks ON chunks.id = chunks_fts.rowid
                JOIN documents ON documents.id = chunks.document_id
                WHERE chunks_fts MATCH ?{source_filter_sql}
                ORDER BY {RANKED_ORDER_BY_SQL}
                LIMIT ?
                """,
                params,
            ).fetchall()
        except sqlite3.Error as exc:
            raise SearchFailedError("search failed") from exc
    finally:
        conn.close()

    evidence: list[EvidenceChunk] = []
    total_chars = 0
    for row in rows:
        bounded_text = _bounded_chunk_text(row[3])
        if total_chars + len(bounded_text) > MAX_TOTAL_EVIDENCE_CHARACTERS:
            break
        evidence.append(
            EvidenceChunk(
                source_key=row[0],
                relative_path=row[1],
                chunk_ordinal=row[2],
                text=bounded_text,
                rank=row[4],
            )
        )
        total_chars += len(bounded_text)

    return evidence
