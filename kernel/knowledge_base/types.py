"""
Typed request/result objects and error classes for kernel/knowledge_base/.

Nothing here performs I/O - these are plain data and error types shared
across config.py, db.py, traversal.py, chunking.py, ingest.py, and
search.py. Every error class is a fail-closed signal for its own category;
callers (in particular scripts/knowledge.py) must map each one to a fixed,
privacy-safe message and never relay its str() to a user or a log.
"""

from dataclasses import dataclass


class KnowledgeBaseError(Exception):
    """Base class for every error this package raises deliberately (as
    opposed to letting an unrelated exception escape). Never carries a
    path, SQL fragment, or other sensitive detail in its message - callers
    must select a fixed message by exception *type*, not by inspecting
    str(exc)."""


class KnowledgeConfigError(KnowledgeBaseError):
    """kernel/config/knowledge_base.yaml is present but invalid: malformed
    YAML, a duplicate or case-colliding key, an unknown field, a missing
    required field, a relative path, or an unsafe source key. A *missing*
    file is not an error - see config.load_knowledge_base_config()."""


class UnknownSourceError(KnowledgeBaseError):
    """The caller supplied a source key that is not in the currently
    loaded, approved configuration."""


class SourceUnavailableError(KnowledgeBaseError):
    """The configured source path does not exist, is not the expected
    type (regular file or directory), or is a symlink/junction/reparse
    point/special file - at the root or at any candidate beneath it."""


class SourceLimitExceededError(KnowledgeBaseError):
    """A fixed, code-level safety limit (file count, file size, total
    bytes, recursion depth, document characters, chunk count) was
    exceeded during traversal or chunking. The whole ingestion for this
    source must fail and roll back - never a silent truncation."""


class InvalidSourceContentError(KnowledgeBaseError):
    """A candidate file within an approved extension failed content
    validation (invalid UTF-8, a NUL byte, or an identity/mutation check
    during race-resistant reading). The whole source ingestion fails."""


class DatabaseUnavailableError(KnowledgeBaseError):
    """The knowledge database file or its containing directory could not
    be opened, created, or initialized."""


class DatabaseLockedError(KnowledgeBaseError):
    """A write transaction could not acquire the database's write lock
    within the fixed busy timeout."""


class SchemaIncompatibleError(KnowledgeBaseError):
    """The database's schema_meta.schema_version does not match the
    version this code expects."""


class FTS5UnavailableError(KnowledgeBaseError):
    """SQLite FTS5 could not create or query a virtual table on this
    Python/SQLite build. Never a reason to fall back to LIKE or another
    dependency - ingestion and search must both refuse to proceed."""


class InvalidQueryError(KnowledgeBaseError):
    """The search query is blank, too long, contains a rejected control
    character, or contains no extractable search terms."""


class InvalidSourceFilterError(KnowledgeBaseError):
    """A search call's source_keys filter named a key that is not in the
    currently approved configuration."""


class SearchFailedError(KnowledgeBaseError):
    """A generic, internal search failure not covered by a more specific
    error type above (e.g. an unexpected SQLite error during an otherwise
    validated, safely-constructed query). Should be unreachable in
    practice given search.py's own query sanitization."""


class IngestionFailedError(KnowledgeBaseError):
    """A generic, internal ingestion failure (e.g. an unexpected SQLite
    error) not covered by a more specific error type above. Always means
    the transaction was rolled back and the prior generation is intact."""


@dataclass(frozen=True)
class IngestResult:
    """Everything a successful ingestion may report - see
    kernel/knowledge_base/ingest.py. No path, no SQL, no internal row ids
    beyond the aggregate counts below."""

    source_key: str
    documents_indexed: int
    unchanged_documents: int
    removed_documents: int
    chunks_indexed: int
    generation: int
    elapsed_seconds: float


@dataclass(frozen=True)
class KnowledgeSearchResult:
    """One ranked search hit - see kernel/knowledge_base/search.py.
    `relative_path` is always source-relative POSIX form, never an
    absolute path. `rank` is the raw bm25() score (more negative is a
    better match); ordering, not the numeric value itself, is what
    callers should rely on."""

    source_key: str
    relative_path: str
    chunk_ordinal: int
    excerpt: str
    rank: float
    chunk_id: str


@dataclass(frozen=True)
class SourceStatus:
    """One approved source's ingestion status - see
    kernel/knowledge_base/status.py. No path, no database location, no
    internal row id - only aggregate counts and the symbolic key. A
    source that has never been ingested reports ingested=False with
    generation/document_count/chunk_count all 0 and last_ingested_at
    None; a successfully ingested (even empty) source reports
    ingested=True with generation >= 1."""

    source_key: str
    ingested: bool
    generation: int
    document_count: int
    chunk_count: int
    last_ingested_at: str | None
