"""
Safe, read-only lexical retrieval for kernel/knowledge_base/ (Milestone
36).

A caller supplies plain query text plus optional symbolic source-key
filters - never SQL, never an FTS5 expression, never a path. Every
extracted search term is treated as a quoted FTS5 string literal, so
caller input (quotes, wildcards, NEAR/OR/NOT, column filters, or any
other FTS5 syntax) can never become an FTS operator - it only ever
behaves as a plain alphanumeric term. The resulting MATCH expression is
still passed as a bound SQL parameter, never concatenated into the SQL
text.

Nothing here invokes a model or contacts a network; every connection is
opened via db.open_reader_connection(), which sets PRAGMA query_only=ON.
"""

import re
import sqlite3
import unicodedata
from pathlib import Path

from kernel.knowledge_base.config import KnowledgeBaseConfig, load_knowledge_base_config
from kernel.knowledge_base.db import open_reader_connection, resolve_database_path
from kernel.knowledge_base.types import (
    InvalidQueryError,
    InvalidSourceFilterError,
    KnowledgeSearchResult,
    SearchFailedError,
)

MAX_QUERY_CHARACTERS = 200
MAX_QUERY_TERMS = 20
DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 50
EXCERPT_MAX_CHARACTERS = 300

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _validate_query_text(query: str) -> str:
    if not isinstance(query, str):
        raise InvalidQueryError("search query is invalid")

    stripped = query.strip()
    if not stripped:
        raise InvalidQueryError("search query is invalid")
    if len(stripped) > MAX_QUERY_CHARACTERS:
        raise InvalidQueryError("search query is invalid")
    if _CONTROL_CHAR_RE.search(stripped):
        raise InvalidQueryError("search query is invalid")

    return stripped


def build_match_expression(query: str) -> str:
    """Transform arbitrary plain-text query into a safe FTS5 MATCH
    expression: every extracted alphanumeric term becomes an individually
    quoted string literal, joined with AND. Terms extracted this way can
    never contain a quote, wildcard, column separator, or other FTS5
    punctuation, so nothing in the original query can be interpreted as
    an FTS5 operator."""

    stripped = _validate_query_text(query)
    normalized_query = unicodedata.normalize("NFC", stripped)
    tokens = _TERM_RE.findall(normalized_query)
    if not tokens:
        raise InvalidQueryError("search query is invalid")

    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(token)
        if len(deduped) >= MAX_QUERY_TERMS:
            break

    return " AND ".join(f'"{term}"' for term in deduped)


def _validate_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidQueryError("search query is invalid")
    if limit < 1 or limit > MAX_RESULT_LIMIT:
        raise InvalidQueryError("search query is invalid")
    return limit


def _validate_source_filter(
    source_keys: list[str] | None, config: KnowledgeBaseConfig
) -> list[str] | None:
    if source_keys is None:
        return None
    if not isinstance(source_keys, list) or not source_keys:
        raise InvalidSourceFilterError("unknown source in filter")

    normalized: list[str] = []
    for key in source_keys:
        if not isinstance(key, str):
            raise InvalidSourceFilterError("unknown source in filter")
        candidate_key = key.strip().casefold()
        if candidate_key not in config.approved_sources:
            raise InvalidSourceFilterError("unknown source in filter")
        normalized.append(candidate_key)
    return normalized


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
    validated_limit = _validate_limit(limit)

    resolved_config = config if config is not None else load_knowledge_base_config()
    normalized_filter = _validate_source_filter(source_keys, resolved_config)

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
                ORDER BY rank, documents.source_key, documents.relative_path_key,
                         chunks.chunk_ordinal, chunks.id
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
