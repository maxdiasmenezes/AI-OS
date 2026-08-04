"""
Public interface of the local knowledge-base layer (Milestone 36).

Callers outside this package must import from here, never from the
individual submodules directly, so the rest of the kernel never needs to
know how sources are configured, traversed, chunked, stored, or searched.

This is deliberately separate from kernel/knowledge/ (the read-only
KnowledgeStore get()/list_records() contract used by capabilities such as
WineCapability) - search and ranking do not fit that contract, and this
package is not wired into it. Nothing here ever calls a model or the
network. Callers: scripts/knowledge.py (a human-invoked, offline CLI) and,
as of Milestone 37, capabilities/knowledge_commands/ (the deterministic,
trusted-context-gated `/knowledge` command capability) - both call into
this package, never the other way around.
"""

from kernel.knowledge_base.config import (
    KnowledgeBaseConfig,
    SourceSpec,
    load_knowledge_base_config,
)
from kernel.knowledge_base.ingest import ingest_source
from kernel.knowledge_base.messages import GENERIC_FAILURE_MESSAGE, message_for_error
from kernel.knowledge_base.search import (
    DEFAULT_RESULT_LIMIT,
    MAX_RESULT_LIMIT,
    search,
)
from kernel.knowledge_base.status import get_status
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
    SourceStatus,
    SourceUnavailableError,
    UnknownSourceError,
)

__all__ = [
    "KnowledgeBaseConfig",
    "SourceSpec",
    "load_knowledge_base_config",
    "ingest_source",
    "search",
    "get_status",
    "DEFAULT_RESULT_LIMIT",
    "MAX_RESULT_LIMIT",
    "message_for_error",
    "GENERIC_FAILURE_MESSAGE",
    "IngestResult",
    "KnowledgeSearchResult",
    "SourceStatus",
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
