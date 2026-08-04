"""
Safe, read-only lexical retrieval for kernel/knowledge_base/ (Milestone
36).

A caller supplies plain query text plus optional symbolic source-key
filters - never SQL, never an FTS5 expression, never a path. Query
validation, FTS5 literal transformation, source-filter validation, limit
validation, and deterministic ranking order all live in
kernel/knowledge_base/query.py (Milestone 38) - this module never
reimplements them, so evidence.py (bounded full-text retrieval for
`/knowledge ask`) can share exactly the same mechanics without a second,
independently-drifting implementation. build_match_expression, imported
here, is re-exported for backward compatibility - existing callers and
tests keep importing it from this module.

Nothing here invokes a model or contacts a network; every connection is
opened via db.open_reader_connection(), which sets PRAGMA query_only=ON.
"""

import sqlite3
from pathlib import Path

from kernel.knowledge_base.config import KnowledgeBaseConfig, load_knowledge_base_config
from kernel.knowledge_base.db import open_reader_connection, resolve_database_path
from kernel.knowledge_base.query import (
    MAX_QUERY_CHARACTERS,
    MAX_QUERY_TERMS,
    RANKED_ORDER_BY_SQL,
    build_match_expression,
    validate_limit,
    validate_source_filter,
)
from kernel.knowledge_base.types import KnowledgeSearchResult, SearchFailedError

DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 50
EXCERPT_MAX_CHARACTERS = 300


def search(
    query: str,
    source_keys: list[str] | None = None,
    limit: int = DEFAULT_RESULT_LIMIT,
    *,
    config: KnowledgeBaseConfig | None = None,
    db_path: Path | None = None,
) -> list[KnowledgeSearchResult]:
    """Read-only lexical search over indexed chunks. Raises
    InvalidQueryError, InvalidSourceFilterError, DatabaseUnavailableError,
    SchemaIncompatibleError, or SearchFailedError (see types.py) for
    every recognized failure. An empty list means no results - not an
    error."""

    match_expression = build_match_expression(query)
    validated_limit = validate_limit(limit, MAX_RESULT_LIMIT)

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
                    snippet(chunks_fts, 0, '[', ']', ' ... ', 10) AS excerpt,
                    bm25(chunks_fts) AS rank,
                    chunks.chunk_id
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

    return [
        KnowledgeSearchResult(
            source_key=row[0],
            relative_path=row[1],
            chunk_ordinal=row[2],
            excerpt=row[3][:EXCERPT_MAX_CHARACTERS],
            rank=row[4],
            chunk_id=row[5],
        )
        for row in rows
    ]
