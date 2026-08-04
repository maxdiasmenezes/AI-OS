"""
Public interface of the local knowledge-base layer (Milestone 36).

Callers outside this package must import from here, never from the
individual submodules directly, so the rest of the kernel never needs to
know how sources are configured, traversed, chunked, stored, or searched.

This is deliberately separate from kernel/knowledge/ (the read-only
KnowledgeStore get()/list_records() contract used by capabilities such as
WineCapability) - search and ranking do not fit that contract, and this
package is not wired into it. Nothing here is reached by the orchestrator,
a capability, WhatsApp, memory, or a model; the only caller is
scripts/knowledge.py.
"""

from kernel.knowledge_base.config import (
    KnowledgeBaseConfig,
    SourceSpec,
    load_knowledge_base_config,
)
from kernel.knowledge_base.ingest import ingest_source
from kernel.knowledge_base.search import search
from kernel.knowledge_base.types import (
    DatabaseLockedError,
    DatabaseUnavailableError,
    FTS5UnavailableError,
    IngestResult,
    IngestionFailedError,
    InvalidQueryError,
    InvalidSourceContentError,
    InvalidSourceFilterError,
    KnowledgeBaseError,
    KnowledgeConfigError,
    KnowledgeSearchResult,
    SchemaIncompatibleError,
    SearchFailedError,
    SourceLimitExceededError,
    SourceUnavailableError,
    UnknownSourceError,
)

__all__ = [
    "KnowledgeBaseConfig",
    "SourceSpec",
    "load_knowledge_base_config",
    "ingest_source",
    "search",
    "IngestResult",
    "KnowledgeSearchResult",
    "KnowledgeBaseError",
    "KnowledgeConfigError",
    "UnknownSourceError",
    "SourceUnavailableError",
    "SourceLimitExceededError",
    "InvalidSourceContentError",
    "DatabaseUnavailableError",
    "DatabaseLockedError",
    "SchemaIncompatibleError",
    "FTS5UnavailableError",
    "InvalidQueryError",
    "InvalidSourceFilterError",
    "IngestionFailedError",
    "SearchFailedError",
]
