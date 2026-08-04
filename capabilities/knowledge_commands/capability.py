"""
KnowledgeCommandsCapability: Milestone 37's safe, deterministic
/knowledge command capability over the Milestone 36 local knowledge base.

Every command is handled deterministically - no model call is ever made
here, matching the pattern capabilities/tasks/TasksCapability established.
This capability never reads memory or the knowledge store either
(status/search/ingest are all one-shot, stateless-per-call operations
against kernel/knowledge_base/).

requires_computer_actions = True means Orchestrator refuses to call
handle() at all unless the request's RequestContext explicitly grants
allow_computer_actions - see kernel/orchestrator/context.py and
kernel/orchestrator/orchestrator.py. This capability trusts that gate
completely and performs no authorization of its own; status and search
(read-only) are gated exactly like ingest (state-changing) - there is no
per-verb trust tier, matching this milestone's approved architecture.

Deliberately does not use kernel/tools' ActionRegistry/SafeTaskExecutor/
ToolsConfig - those exist for OS-level process/file actions and don't fit
a free-text search query or an integer limit. It does reuse two small,
already-domain-agnostic kernel/tools/ primitives: ConfirmationStore (a
*separate* instance from capabilities/tasks' default_store - see
`default_knowledge_confirmation_store` below) and the audit module.
"""

import logging

from kernel.capabilities.base import Capability
from kernel.knowledge_base import (
    KnowledgeBaseError,
    UnknownSourceError,
    get_status,
    ingest_source,
    load_knowledge_base_config,
    message_for_error,
)
from kernel.knowledge_base import search as kb_search
from kernel.tools import audit
from kernel.tools.confirmation import ConfirmationStore

from capabilities.knowledge_commands.command_parser import (
    MAX_INTERFACE_RESULT_LIMIT,
    KnowledgeParseError,
    parse_knowledge_command,
)

logger = logging.getLogger(__name__)

# Fixed interface-level limits (Milestone 37) - in addition to, and always
# at least as strict as, kernel/knowledge_base/search.py's own
# service-level limits. MAX_INTERFACE_RESULT_LIMIT is defined in
# command_parser.py (the grammar's own "1 through 10" rule) and imported
# above so the request-cap value used by _execute_search and the
# reply-formatting cap are always the same single value.
DEFAULT_INTERFACE_RESULT_LIMIT = 5
MAX_INTERFACE_EXCERPT_CHARACTERS = 200
MAX_INTERFACE_PATH_CHARACTERS = 80
MAX_INTERFACE_REPLY_CHARACTERS = 3_500

_ELLIPSIS = "..."
_OMISSION_NOTICE = "Additional results were omitted to keep the reply within the safe output limit."

_INGEST_ACTION = "knowledge_ingest"

HELP_TEXT = (
    "Knowledge commands:\n"
    "/knowledge status\n"
    "/knowledge status --source <source-key>\n"
    "/knowledge search [--source <source-key>] [--limit <1-10>] -- <query>\n"
    "/knowledge ingest <source-key>\n"
    "/knowledge confirm\n"
    "/knowledge cancel"
)

_PARSE_ERROR_TEXT = "Invalid knowledge command. Use /knowledge help."
_NO_SOURCES_CONFIGURED_TEXT = "No knowledge sources are configured."
_NO_RESULTS_TEXT = "No results found."
_NOTHING_PENDING_TEXT = "There is no pending action to confirm."
_CONFIRMATION_EXPIRED_TEXT = "That confirmation has expired. Please send the command again."
_CANCELLED_TEXT = "Pending action cancelled."
_NOTHING_TO_CANCEL_TEXT = "There is no pending action to cancel."

# Module-scope singleton, deliberately separate from
# kernel.tools.confirmation.default_store (capabilities/tasks' own slot).
# CapabilityLoader constructs a brand new KnowledgeCommandsCapability
# instance on every request (see capabilities/loader.py) - an instance
# attribute would never survive between the "propose" message and the
# later "confirm" message, exactly as kernel/tools/confirmation.py's own
# docstring explains for default_store. Using a *distinct* instance here
# means a pending /knowledge ingest proposal can never collide with, or be
# silently evicted by, a pending /task action (and vice versa).
default_knowledge_confirmation_store = ConfirmationStore()


def _confirmation_prompt(source_key: str) -> str:
    return (
        f"This will refresh locally indexed knowledge for '{source_key}'.\n"
        "Reply /knowledge confirm within 2 minutes to proceed, or /knowledge cancel."
    )


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep = max(max_chars - len(_ELLIPSIS), 0)
    return text[:keep] + _ELLIPSIS


def _join_within_budget(lines: list, max_chars: int) -> str:
    included = []
    total = 0
    for line in lines:
        addition = len(line) + (1 if included else 0)
        if total + addition > max_chars:
            break
        included.append(line)
        total += addition
    return "\n".join(included)


def _format_status_reply(statuses) -> str:
    if not statuses:
        return _NO_SOURCES_CONFIGURED_TEXT

    lines = []
    for status in statuses:
        if not status.ingested:
            lines.append(f"{status.source_key}: not yet ingested")
        else:
            lines.append(
                f"{status.source_key}: {status.document_count} document(s), "
                f"{status.chunk_count} chunk(s), generation {status.generation}, "
                f"last ingested {status.last_ingested_at}"
            )
    return _join_within_budget(lines, MAX_INTERFACE_REPLY_CHARACTERS)


def _format_search_result_block(ordinal: int, result) -> str:
    path = _truncate(result.relative_path, MAX_INTERFACE_PATH_CHARACTERS)
    excerpt = _truncate(result.excerpt, MAX_INTERFACE_EXCERPT_CHARACTERS)
    return f"{ordinal}. [{result.source_key}] {path} (chunk {result.chunk_ordinal})\n   {excerpt}"


def _format_search_reply(results) -> str:
    if not results:
        return _NO_RESULTS_TEXT

    blocks = [_format_search_result_block(i, r) for i, r in enumerate(results, start=1)]

    included: list = []
    total_chars = 0
    for index, block in enumerate(blocks):
        separator_len = 2 if included else 0
        candidate_len = total_chars + separator_len + len(block)
        has_more_after = index + 1 < len(blocks)
        reserve = (2 + len(_OMISSION_NOTICE)) if has_more_after else 0
        if candidate_len + reserve > MAX_INTERFACE_REPLY_CHARACTERS:
            break
        included.append(block)
        total_chars = candidate_len

    reply = "\n\n".join(included)
    if len(included) < len(blocks):
        reply = f"{reply}\n\n{_OMISSION_NOTICE}" if reply else _OMISSION_NOTICE
    return reply


def _format_ingest_reply(result) -> str:
    return (
        f"source: {result.source_key}\n"
        f"documents indexed: {result.documents_indexed}\n"
        f"unchanged documents: {result.unchanged_documents}\n"
        f"removed documents: {result.removed_documents}\n"
        f"chunks indexed: {result.chunks_indexed}\n"
        f"generation: {result.generation}\n"
        f"duration: {result.elapsed_seconds:.3f}s"
    )


class KnowledgeCommandsCapability(Capability):
    name = "knowledge"
    requires_computer_actions = True

    def __init__(
        self,
        model_provider,
        memory_manager,
        knowledge_store,
        *,
        confirmation_store=None,
        config_loader=None,
        db_path=None,
        status_fn=None,
        search_fn=None,
        ingest_fn=None,
        audit_record=None,
    ) -> None:
        # model_provider/memory_manager/knowledge_store are accepted only
        # for constructor-signature parity with every other capability -
        # CapabilityLoader calls all of them the same way (see
        # capabilities/loader.py). This capability never calls a model and
        # never reads memory or the knowledge store.
        self._confirmations = (
            confirmation_store if confirmation_store is not None else default_knowledge_confirmation_store
        )
        self._load_config = config_loader or load_knowledge_base_config
        self._db_path = db_path
        self._status_fn = status_fn or get_status
        self._search_fn = search_fn or kb_search
        self._ingest_fn = ingest_fn or ingest_source
        self._audit_record = audit_record or audit.record

    @property
    def id(self) -> str:
        return self.name

    def _safe_audit(self, action: str, resource_key, outcome: str) -> None:
        try:
            self._audit_record(action, resource_key, outcome)
        except Exception:
            logger.warning("knowledge_audit_write_failed")

    def handle(self, prompt: str) -> str:
        parsed = parse_knowledge_command(prompt)
        if isinstance(parsed, KnowledgeParseError):
            return _PARSE_ERROR_TEXT

        if parsed.verb == "help":
            return HELP_TEXT

        if parsed.verb == "cancel":
            cancelled = self._confirmations.cancel()
            self._safe_audit("confirmation", None, "cancelled" if cancelled else "rejected")
            return _CANCELLED_TEXT if cancelled else _NOTHING_TO_CANCEL_TEXT

        if parsed.verb == "confirm":
            pending, expired = self._confirmations.consume()
            if pending is None:
                self._safe_audit("confirmation", None, "expired" if expired else "rejected")
                return _CONFIRMATION_EXPIRED_TEXT if expired else _NOTHING_PENDING_TEXT
            if pending.action != _INGEST_ACTION:
                # Defense in depth: this store should only ever hold
                # knowledge_ingest actions. An unexpected action name
                # (e.g. a corrupted or cross-domain pending state) is
                # never executed.
                self._safe_audit("confirmation", pending.resource_key, "rejected")
                return _NOTHING_PENDING_TEXT
            self._safe_audit(pending.action, pending.resource_key, "confirmed")
            return self._execute_ingest(pending.resource_key)

        if parsed.verb == "status":
            return self._execute_status(parsed.source_key)

        if parsed.verb == "search":
            return self._execute_search(parsed.source_key, parsed.limit, parsed.query)

        # ingest
        return self._propose_ingest(parsed.source_key)

    def _execute_status(self, source_key) -> str:
        try:
            config = self._load_config()
            source_keys = [source_key] if source_key else None
            statuses = self._status_fn(source_keys, config=config, db_path=self._db_path)
        except KnowledgeBaseError as exc:
            self._safe_audit("knowledge_status", source_key, "failed")
            return message_for_error(exc)
        self._safe_audit("knowledge_status", source_key, "executed")
        return _format_status_reply(statuses)

    def _execute_search(self, source_key, limit, query) -> str:
        resolved_limit = limit if limit is not None else DEFAULT_INTERFACE_RESULT_LIMIT
        resolved_limit = min(resolved_limit, MAX_INTERFACE_RESULT_LIMIT)
        try:
            config = self._load_config()
            source_keys = [source_key] if source_key else None
            results = self._search_fn(
                query, source_keys=source_keys, limit=resolved_limit, config=config, db_path=self._db_path
            )
        except KnowledgeBaseError as exc:
            self._safe_audit("knowledge_search", source_key, "failed")
            return message_for_error(exc)
        self._safe_audit("knowledge_search", source_key, "executed")
        return _format_search_reply(results)

    def _propose_ingest(self, source_key: str) -> str:
        try:
            config = self._load_config()
        except KnowledgeBaseError as exc:
            self._safe_audit(_INGEST_ACTION, source_key, "failed")
            return message_for_error(exc)

        if source_key not in config.approved_sources:
            self._safe_audit(_INGEST_ACTION, source_key, "rejected")
            return message_for_error(UnknownSourceError("unknown source key"))

        self._confirmations.propose(_INGEST_ACTION, source_key)
        self._safe_audit(_INGEST_ACTION, source_key, "proposed")
        return _confirmation_prompt(source_key)

    def _execute_ingest(self, source_key: str) -> str:
        try:
            config = self._load_config()
            if source_key not in config.approved_sources:
                raise UnknownSourceError("unknown source key")
            result = self._ingest_fn(source_key, config=config, db_path=self._db_path)
        except KnowledgeBaseError as exc:
            self._safe_audit(_INGEST_ACTION, source_key, "failed")
            return message_for_error(exc)
        self._safe_audit(_INGEST_ACTION, source_key, "executed")
        return _format_ingest_reply(result)
