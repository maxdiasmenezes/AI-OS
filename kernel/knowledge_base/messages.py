"""
Centralized, fixed, privacy-safe error messages for kernel/knowledge_base/
(Milestone 37).

Every caller outside this package - scripts/knowledge.py and
capabilities/knowledge_commands/ - selects a user-facing message by
exception *type* through message_for_error(), never by relaying
str(error) or an exception's arguments. This is the one place that dict
exists, so the same message is never hand-copied a second or third time.
"""

from kernel.knowledge_base.types import (
    DatabaseLockedError,
    DatabaseUnavailableError,
    FTS5UnavailableError,
    IngestionFailedError,
    InvalidQueryError,
    InvalidSourceContentError,
    InvalidSourceFilterError,
    KnowledgeBaseError,
    KnowledgeConfigError,
    SchemaIncompatibleError,
    SearchFailedError,
    SourceLimitExceededError,
    SourceUnavailableError,
    UnknownSourceError,
)

# Used when an exception isn't (or isn't known to be) a KnowledgeBaseError
# at all - an unexpected failure at a user-facing boundary. Never derived
# from the exception itself.
GENERIC_FAILURE_MESSAGE = "Knowledge operation failed."

_ERROR_MESSAGES: dict[type, str] = {
    UnknownSourceError: "Unknown knowledge source.",
    SourceUnavailableError: "Knowledge source is not available.",
    InvalidSourceContentError: "Knowledge source contains unsupported or invalid content.",
    SourceLimitExceededError: "Knowledge source exceeds safety limits.",
    DatabaseUnavailableError: "Knowledge database is unavailable.",
    DatabaseLockedError: "Knowledge database is busy. Try again.",
    SchemaIncompatibleError: "Knowledge database schema is incompatible.",
    FTS5UnavailableError: "Knowledge search is unavailable on this system.",
    IngestionFailedError: "Ingestion failed.",
    InvalidQueryError: "Search query is invalid.",
    InvalidSourceFilterError: "Unknown knowledge source in filter.",
    SearchFailedError: "Search failed.",
    KnowledgeConfigError: "Knowledge base configuration is unavailable.",
}


def message_for_error(error: KnowledgeBaseError) -> str:
    """Return the one fixed, privacy-safe message for this error's type.
    Never includes str(error), exception arguments, a path, SQL, an FTS
    expression, query text, document content, or traceback data."""

    return _ERROR_MESSAGES.get(type(error), GENERIC_FAILURE_MESSAGE)
