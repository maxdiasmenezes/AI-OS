"""
Safe, read-only evidence retrieval for `/knowledge ask` (Milestone 38;
ask-specific matching revised in Milestone 38.1).

This is a sibling to search.py, not a modification of it: search.py's
public search() and its existing, already-reviewed SQL, including
build_match_expression()'s strict all-terms-AND semantics, are untouched.
retrieve_evidence() shares query.py's validation, term-extraction, and
ranking-order mechanics with search.py, but - as of Milestone 38.1 -
builds its own ask-only MATCH expression (see
_build_evidence_match_expression() below) rather than calling
build_match_expression(): natural questions carry generic framing words
("how", "does", "the", ...) that would make a strict AND match nothing,
even when the question's real content terms are present together in a
chunk. This module still runs at most one read-only query nearly
identical to search()'s - the only differences are the MATCH expression
and selecting chunks.text (the full stored chunk, itself already capped
at ingest time to chunking.MAX_CHUNK_CHARACTERS) instead of a bounded
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
    extract_query_terms,
    validate_limit,
    validate_source_filter,
)
from kernel.knowledge_base.types import SearchFailedError

DEFAULT_EVIDENCE_LIMIT = 3
MAX_EVIDENCE_LIMIT = 5
MAX_EVIDENCE_CHUNK_CHARACTERS = 1_500
MAX_TOTAL_EVIDENCE_CHARACTERS = 7_500

# Milestone 38.1: with query.py's MAX_QUERY_TERMS capped at 20, the
# worst-case three-or-more-useful-terms expression (see
# _build_evidence_match_expression) generates "20 choose 2" == 190
# deterministic two-term pair clauses. This is a defensive upper bound,
# asserted at generation time - it can never actually be exceeded given
# the upstream cap, but the assertion documents and guards the invariant
# rather than relying on it silently.
MAX_EVIDENCE_PAIR_CLAUSES = 190

_ELLIPSIS = "..."

# Ask-only, fixed, code-owned generic-term filter (Milestone 38.1). This is
# a small deterministic retrieval heuristic, not semantic language
# understanding: it never translates, stems, or otherwise transforms a
# term, and it only ever removes a term from the set used to build the
# MATCH expression below - it never adds, alters, or reorders one, and it
# is never applied to search()'s build_match_expression(). Matching is
# exact-whole-term-after-casefold only, never substring (e.g. "am" never
# removes "family"). Removing a framing/negation word from the retrieval
# anchor set does not remove it from the question itself: the complete,
# original question - negation included - still reaches the unchanged
# Milestone 38 grounded-answer prompt (see answer.py/build_prompt()), and
# the model must still answer only from the evidence actually retrieved.
_ASK_GENERIC_TERMS = frozenset(
    {
        # English articles and interrogatives
        "a", "an", "the",
        "how", "what", "where", "when", "why", "who", "whom", "whose", "which",
        # English auxiliary forms
        "am", "is", "are", "was", "were", "be", "been", "being",
        "do", "does", "did", "done",
        "have", "has", "had",
        "can", "could", "will", "would", "shall", "should", "may", "might", "must",
        # English pronouns and determiners
        "i", "you", "he", "she", "it", "we", "they",
        "me", "him", "her", "us", "them",
        "my", "your", "his", "its", "our", "their",
        "this", "that", "these", "those",
        # English function and framing words
        "of", "to", "in", "on", "at", "for", "with", "about", "from", "by", "as", "into",
        "and", "or", "but", "if", "so",
        "please", "tell", "explain", "describe",
        # English negation words
        "not", "no", "without",
        # Portuguese articles and interrogatives ("as" omitted here - already
        # listed once above as the English preposition, same spelling)
        "o", "os", "um", "uma", "uns", "umas",
        "como", "que", "qual", "quais", "quem", "onde", "quando", "porque", "porquê",
        # Portuguese auxiliary/common verb forms
        "é", "são", "era", "eram", "foi", "foram", "ser", "sido",
        "estar", "está", "estão", "estava", "estavam",
        "faz", "fazer", "feito",
        "tem", "têm", "tinha", "tinham",
        "pode", "podem", "poderia", "poderiam",
        "deve", "devem", "deveria", "deveriam",
        # Portuguese pronouns and determiners
        "eu", "você", "voce", "ele", "ela", "nós", "nos", "eles", "elas",
        "te", "se",
        "meu", "minha", "seu", "sua", "nosso", "nossa",
        "isso", "isto", "aquilo",
        # Portuguese function and framing words ("do" and "no" omitted here -
        # already listed once above as English auxiliary/negation words,
        # same spelling)
        "de", "da", "dos", "das",
        "em", "na", "nas",
        "para", "por", "com", "sobre", "desde", "até", "ate",
        "e", "ou", "mas", "então", "entao",
        "favor", "diga", "explique", "descreva",
        # Portuguese negation words
        "não", "nao", "sem",
    }
)


def _build_evidence_match_expression(question: str) -> str | None:
    """Ask-only MATCH-expression builder (Milestone 38.1), used only by
    retrieve_evidence() below - never by search()/build_match_expression().

    Shares extract_query_terms() (kernel/knowledge_base/query.py) for
    validation, NFC normalization, tokenization, case-insensitive
    deduplication, and the MAX_QUERY_TERMS cap - exactly the same first
    stage search()'s build_match_expression() uses, so a garbage or blank
    question still raises InvalidQueryError exactly as before. From that
    term list, terms whose casefolded form is in _ASK_GENERIC_TERMS are
    removed (order and deduplication otherwise preserved); if nothing
    remains, returns None so the caller can skip the database entirely.

    The remaining "useful" terms are individually quoted exactly like
    build_match_expression() does (never a caller-controlled operator,
    wildcard, or column filter), then combined by a deterministic minimum-
    match rule, enforced by FTS5's own tokenizer - not a second, possibly
    divergent Python-side tokenization of chunk text:
      - one useful term: that term alone.
      - two useful terms: both required (AND) - identical in shape to
        build_match_expression()'s two-term output, preserving Milestone
        38's exact "repository backup" behavior.
      - three or more useful terms: every deterministic two-term AND
        combination, in original term order, OR'd together - requiring
        any two of the useful terms to both appear in a chunk, never just
        one. Operators and parentheses come only from this code; nothing
        from the caller's question can contribute one.
    """

    terms = extract_query_terms(question)
    useful_terms = [term for term in terms if term.casefold() not in _ASK_GENERIC_TERMS]
    if not useful_terms:
        return None

    quoted = [f'"{term}"' for term in useful_terms]

    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} AND {quoted[1]}"

    pair_clauses = [
        f"({quoted[i]} AND {quoted[j]})"
        for i in range(len(quoted))
        for j in range(i + 1, len(quoted))
    ]
    assert len(pair_clauses) <= MAX_EVIDENCE_PAIR_CLAUSES
    return " OR ".join(pair_clauses)


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

    As of Milestone 38.1, the MATCH expression is ask-only (see
    _build_evidence_match_expression() above), not search()'s strict
    all-terms-AND build_match_expression(): a question that reduces to
    zero useful terms after ask-only generic-term filtering (e.g. "How are
    you?") returns [] immediately, without opening a database connection
    or executing any SQL - every other validation (limit, source filter)
    still runs first and can still raise, exactly as before.

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

    match_expression = _build_evidence_match_expression(question)
    validated_limit = validate_limit(limit, MAX_EVIDENCE_LIMIT)

    resolved_config = config if config is not None else load_knowledge_base_config()
    normalized_filter = validate_source_filter(source_keys, resolved_config)

    if match_expression is None:
        return []

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
