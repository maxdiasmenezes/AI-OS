"""
Public interface of the local knowledge-base layer (Milestone 36).

Callers outside this package must import from here, never from the
individual submodules directly, so the rest of the kernel never needs to
know how sources are configured, traversed, chunked, stored, or searched.

This is deliberately separate from kernel/knowledge/ (the read-only
KnowledgeStore get()/list_records() contract used by capabilities such as
WineCapability) - search and ranking do not fit that contract, and this
package is not wired into it. Nothing here ever calls a model or the
network, except answer.py's pure prompt-data construction and
response-parsing helpers, which are used by capabilities/knowledge_commands/
to make exactly one explicit, capability-owned model call for
`/knowledge ask` - this package itself still never calls a model or the
network. Callers: scripts/knowledge.py (a human-invoked, offline CLI) and,
as of Milestone 37, capabilities/knowledge_commands/ (the deterministic,
trusted-context-gated `/knowledge` command capability) - both call into
this package, never the other way around.
"""

from kernel.knowledge_base.answer import (
    MAX_DISPLAYED_SOURCE_ENTRIES,
    MAX_GENERATED_ANSWER_CHARACTERS,
    AnswerOutcome,
    AnswerResult,
    CitationMeta,
    assign_citation_labels,
    build_prompt,
    build_source_section,
    extract_inline_citation_labels,
    parse_structured_answer,
)
from kernel.knowledge_base.config import (
    KnowledgeBaseConfig,
    SourceSpec,
    load_knowledge_base_config,
)
from kernel.knowledge_base.evidence import (
    DEFAULT_EVIDENCE_LIMIT,
    MAX_EVIDENCE_LIMIT,
    EvidenceChunk,
    retrieve_evidence,
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
    "EvidenceChunk",
    "retrieve_evidence",
    "DEFAULT_EVIDENCE_LIMIT",
    "MAX_EVIDENCE_LIMIT",
    "AnswerOutcome",
    "AnswerResult",
    "CitationMeta",
    "assign_citation_labels",
    "build_prompt",
    "parse_structured_answer",
    "build_source_section",
    "extract_inline_citation_labels",
    "MAX_GENERATED_ANSWER_CHARACTERS",
    "MAX_DISPLAYED_SOURCE_ENTRIES",
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
