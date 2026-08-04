"""
KnowledgeCommandsCapability: Milestone 37's safe, deterministic
/knowledge command capability over the Milestone 36 local knowledge base,
extended in Milestone 38 with an explicit `/knowledge ask` verb.

status/search/ingest/confirm/cancel/help remain fully deterministic - no
model call is ever made for any of them, matching the pattern
capabilities/tasks/TasksCapability established. `ask` is the one
exception: it explicitly invokes the already-injected model provider
exactly once, to answer a question grounded only in retrieved local
excerpts - see kernel/knowledge_base/evidence.py and
kernel/knowledge_base/answer.py for the retrieval and prompt/citation
mechanics this method delegates to. This capability still never reads
memory or the knowledge store, and `ask` performs no memory recall either
- every command remains a one-shot, stateless-per-call operation.

requires_computer_actions = True means Orchestrator refuses to call
handle() at all unless the request's RequestContext explicitly grants
allow_computer_actions - see kernel/orchestrator/context.py and
kernel/orchestrator/orchestrator.py. This capability trusts that gate
completely and performs no authorization of its own; every verb, including
`ask`, is gated exactly the same way - there is no per-verb trust tier,
matching this milestone's approved architecture. A denied request never
reaches this class's handle() at all, so it never parses, retrieves
evidence, opens the knowledge database, or calls the model.

search and ask results are returned as EphemeralResult (see
kernel/capabilities/base.py), not a plain str: Orchestrator.handle()
persists every ordinary capability response to memory and the interaction
log, and query/question text together with a model-generated answer must
never be persisted - see kernel/orchestrator/orchestrator.py.

Deliberately does not use kernel/tools' ActionRegistry/SafeTaskExecutor/
ToolsConfig - those exist for OS-level process/file actions and don't fit
a free-text search query or an integer limit. It does reuse two small,
already-domain-agnostic kernel/tools/ primitives: ConfirmationStore (a
*separate* instance from capabilities/tasks' default_store - see
`default_knowledge_confirmation_store` below) and the audit module. `ask`
does not use ConfirmationStore at all - Milestone 38's approved v1 consent
policy is that the explicit `/knowledge ask` command is itself sufficient
consent (see docs/architecture.md and this package's README); this must be
revisited before a remote model provider is ever enabled for this
operation, since only the local Ollama provider is implemented today.
"""

import logging

from kernel.capabilities.base import Capability, EphemeralResult
from kernel.knowledge_base import (
    MAX_DISPLAYED_SOURCE_ENTRIES,
    MAX_GENERATED_ANSWER_CHARACTERS,
    AnswerOutcome,
    DEFAULT_EVIDENCE_LIMIT,
    GENERIC_FAILURE_MESSAGE,
    KnowledgeBaseError,
    UnknownSourceError,
    assign_citation_labels,
    build_prompt,
    build_source_section,
    extract_inline_citation_labels,
    get_status,
    ingest_source,
    load_knowledge_base_config,
    message_for_error,
    parse_structured_answer,
    retrieve_evidence,
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
_ASK_ACTION = "knowledge_ask"

HELP_TEXT = (
    "Knowledge commands:\n"
    "/knowledge status\n"
    "/knowledge status --source <source-key>\n"
    "/knowledge search [--source <source-key>] [--limit <1-10>] -- <query>\n"
    "/knowledge ask [--source <source-key>] [--limit <1-5>] -- <question>\n"
    "/knowledge ingest <source-key>\n"
    "/knowledge confirm\n"
    "/knowledge cancel\n"
    "The ask command sends your question and selected local excerpts to the configured model provider."
)

_PARSE_ERROR_TEXT = "Invalid knowledge command. Use /knowledge help."
_NO_SOURCES_CONFIGURED_TEXT = "No knowledge sources are configured."
_NO_RESULTS_TEXT = "No results found."
_NOTHING_PENDING_TEXT = "There is no pending action to confirm."
_CONFIRMATION_EXPIRED_TEXT = "That confirmation has expired. Please send the command again."
_CANCELLED_TEXT = "Pending action cancelled."
_NOTHING_TO_CANCEL_TEXT = "There is no pending action to cancel."

# Milestone 38 fixed /knowledge ask replies - see docs/architecture.md and
# this package's README for the full outcome table. Never conflated with
# each other: "no results" (nothing to answer from), "insufficient
# evidence" (the model itself says the evidence doesn't support an
# answer), and "unverifiable response" (the model's structured response
# could not be trusted at all - malformed, or citations that don't hold
# up) are deliberately distinct, privacy-safe, fixed messages.
_NO_RESULTS_ASK_TEXT = "No relevant local knowledge was found for that question."
_INSUFFICIENT_EVIDENCE_TEXT = (
    "The local knowledge sources do not contain enough information to answer that question."
)
_UNVERIFIABLE_ANSWER_TEXT = "The model did not produce a verifiable grounded answer."
_MODEL_UNAVAILABLE_TEXT = (
    "The knowledge answer service is temporarily unavailable. Please try again later."
)

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
        evidence_fn=None,
        audit_record=None,
    ) -> None:
        # memory_manager/knowledge_store are accepted only for
        # constructor-signature parity with every other capability -
        # CapabilityLoader calls all of them the same way (see
        # capabilities/loader.py). This capability never reads memory or
        # the knowledge store. model_provider *is* used, but only by
        # `ask` - every other verb remains fully model-free.
        self._model_provider = model_provider
        self._confirmations = (
            confirmation_store if confirmation_store is not None else default_knowledge_confirmation_store
        )
        self._load_config = config_loader or load_knowledge_base_config
        self._db_path = db_path
        self._status_fn = status_fn or get_status
        self._search_fn = search_fn or kb_search
        self._ingest_fn = ingest_fn or ingest_source
        self._evidence_fn = evidence_fn or retrieve_evidence
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

        if parsed.verb == "ask":
            return self._execute_ask(parsed.source_key, parsed.limit, parsed.query)

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

    def _execute_search(self, source_key, limit, query) -> EphemeralResult:
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
            return EphemeralResult(message_for_error(exc))
        self._safe_audit("knowledge_search", source_key, "executed")
        return EphemeralResult(_format_search_reply(results))

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

    def _execute_ask(self, source_key, limit, question) -> EphemeralResult:
        """Retrieve bounded evidence, call the injected model provider
        exactly once, and return a grounded, cited answer - or one of the
        fixed, privacy-safe outcome replies. Never audits, logs, or
        persists question/evidence/answer text - only the symbolic action,
        source key, and outcome ever reach audit.record() (see
        _safe_audit)."""

        resolved_limit = limit if limit is not None else DEFAULT_EVIDENCE_LIMIT
        try:
            config = self._load_config()
            source_keys = [source_key] if source_key else None
            evidence_chunks = self._evidence_fn(
                question,
                source_keys=source_keys,
                limit=resolved_limit,
                config=config,
                db_path=self._db_path,
            )
        except KnowledgeBaseError as exc:
            self._safe_audit(_ASK_ACTION, source_key, "failed")
            return EphemeralResult(message_for_error(exc))

        if not evidence_chunks:
            self._safe_audit(_ASK_ACTION, source_key, "executed")
            return EphemeralResult(_NO_RESULTS_ASK_TEXT)

        citations = assign_citation_labels(evidence_chunks)
        prompt = build_prompt(question, evidence_chunks, citations)

        try:
            model_response = self._model_provider.send_prompt(prompt)
        except Exception:
            # Never the raw provider exception - never in the reply, the
            # audit record, memory, or the interaction log.
            self._safe_audit(_ASK_ACTION, source_key, "failed")
            return EphemeralResult(_MODEL_UNAVAILABLE_TEXT)

        try:
            reply = self._build_ask_reply(model_response, citations)
        except Exception:
            self._safe_audit(_ASK_ACTION, source_key, "failed")
            return EphemeralResult(GENERIC_FAILURE_MESSAGE)

        self._safe_audit(_ASK_ACTION, source_key, "executed")
        return EphemeralResult(reply)

    def _build_ask_reply(self, model_response, citations) -> str:
        raw_text = getattr(model_response, "text", None)
        valid_labels = frozenset(citation.label for citation in citations)
        result = parse_structured_answer(raw_text, valid_labels)

        if result.outcome is AnswerOutcome.UNVERIFIABLE:
            return _UNVERIFIABLE_ANSWER_TEXT
        if result.outcome is AnswerOutcome.INSUFFICIENT:
            return _INSUFFICIENT_EVIDENCE_TEXT

        return self._format_ask_reply(result.answer, result.used_citations, citations)

    def _format_ask_reply(self, answer, used_citations, citations) -> str:
        """Bound the answer and append a deterministic source section
        within MAX_INTERFACE_REPLY_CHARACTERS. A citation is only ever
        displayed in the source section while its `[S#]` token still
        appears, complete, in the (possibly truncated) answer text -
        never orphaned in either direction. If truncating the answer to
        fit the reply budget would leave zero valid citations, this fails
        closed to the fixed unverifiable-answer reply rather than showing
        an ungrounded answer."""

        bounded_answer = _truncate(answer, MAX_GENERATED_ANSWER_CHARACTERS)
        required = self._citations_still_present(bounded_answer, used_citations)
        if not required:
            return _UNVERIFIABLE_ANSWER_TEXT

        source_section = build_source_section(
            required, citations, MAX_DISPLAYED_SOURCE_ENTRIES, MAX_INTERFACE_PATH_CHARACTERS
        )
        reply = f"{bounded_answer}\n\n{source_section}"
        if len(reply) <= MAX_INTERFACE_REPLY_CHARACTERS:
            return reply

        # Reply doesn't fit: reduce the answer (rather than dropping a
        # source entry whose citation is still inline) and recompute which
        # citations survive the reduced text.
        separator_length = 2
        available_for_answer = MAX_INTERFACE_REPLY_CHARACTERS - len(source_section) - separator_length
        if available_for_answer <= 0:
            return _UNVERIFIABLE_ANSWER_TEXT

        reduced_answer = _truncate(bounded_answer, available_for_answer)
        reduced_required = self._citations_still_present(reduced_answer, used_citations)
        if not reduced_required:
            return _UNVERIFIABLE_ANSWER_TEXT

        reduced_source_section = build_source_section(
            reduced_required, citations, MAX_DISPLAYED_SOURCE_ENTRIES, MAX_INTERFACE_PATH_CHARACTERS
        )
        final_reply = f"{reduced_answer}\n\n{reduced_source_section}"
        if len(final_reply) <= MAX_INTERFACE_REPLY_CHARACTERS:
            return final_reply
        return _UNVERIFIABLE_ANSWER_TEXT

    @staticmethod
    def _citations_still_present(text: str, used_citations) -> tuple:
        present = extract_inline_citation_labels(text)
        return tuple(label for label in used_citations if label in present)
