"""
Shared, package-internal lexical query mechanics for kernel/knowledge_base/
(Milestone 38).

Both search.py (bounded excerpts for `/knowledge search`) and evidence.py
(bounded full chunk text for `/knowledge ask`) need to build a literal,
injection-safe FTS5 MATCH expression from the same validated query text,
validate the same source-key-filter and limit shapes, and order ranked
rows identically. This module is the single place that logic lives, so
callers can never drift into two separate implementations of query
validation, source filtering, FTS literal transformation, or deterministic
ranking order.

Every extracted search term is treated as a quoted FTS5 string literal, so
caller input (quotes, wildcards, NEAR/OR/NOT, column filters, or any other
FTS5 syntax) can never become an FTS operator - it only ever behaves as a
plain alphanumeric term. Nothing here performs I/O; callers are
responsible for passing the resulting MATCH expression as a bound SQL
parameter, never concatenating it into SQL text.
"""

import re
import unicodedata

from kernel.knowledge_base.config import KnowledgeBaseConfig
from kernel.knowledge_base.types import InvalidQueryError, InvalidSourceFilterError

MAX_QUERY_CHARACTERS = 200
MAX_QUERY_TERMS = 20

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Deterministic tie-break order shared by every ranked lexical query against
# chunks_fts - both search.py's search() and evidence.py's
# retrieve_evidence() ORDER BY exactly this (referencing a `rank` column
# alias each query defines via `bm25(chunks_fts) AS rank`), so repeated
# calls against the same generation return identical order.
RANKED_ORDER_BY_SQL = (
    "rank, documents.source_key, documents.relative_path_key, "
    "chunks.chunk_ordinal, chunks.id"
)


def validate_query_text(query: str) -> str:
    """Validate raw query/question text: must be a non-blank string, at
    most MAX_QUERY_CHARACTERS after stripping, and free of C0 control
    characters or DEL. Returns the stripped text. Raises
    InvalidQueryError on any violation."""

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

    stripped = validate_query_text(query)
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


def validate_limit(limit: int, max_limit: int) -> int:
    """Validate a result/evidence limit against a caller-supplied ceiling
    (search.py's MAX_RESULT_LIMIT and evidence.py's MAX_EVIDENCE_LIMIT
    differ, so the ceiling is a parameter, not a module constant here).
    Raises InvalidQueryError on any violation."""

    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidQueryError("search query is invalid")
    if limit < 1 or limit > max_limit:
        raise InvalidQueryError("search query is invalid")
    return limit


def validate_source_filter(
    source_keys: list[str] | None, config: KnowledgeBaseConfig
) -> list[str] | None:
    """Validate an optional source-key filter against the currently
    approved configuration. None means "no filter". Raises
    InvalidSourceFilterError if the list is empty, contains a non-string,
    or names a key that isn't currently approved."""

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
