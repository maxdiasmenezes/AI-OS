"""
Milestone 46/47 - WhatsApp Task Control glue.

Thin glue between the WhatsApp transport (interfaces/whatsapp/server.py,
handler.py) and the existing, durable task engine (kernel/employee_tasks/,
kernel/task_planner/, kernel/task_orchestration/, kernel/task_execution/).
This module is deliberately small: it recognizes "/task ..." text, performs
one bounded, durable TaskRepository operation per inbound /task message,
hands the resulting task_id (never raw text) to the existing planning
orchestration, then (Milestone 46 P2A) drives that task through the
existing bounded execution runner and delivers exactly one WhatsApp
lifecycle message per newly-produced lifecycle event. It is not a second
task engine, a second executor, or a second confirmation store - it never
plans, executes, confirms, or stores anything beyond what
kernel.task_planner/kernel.task_execution/kernel.employee_tasks already own.

P2A scope: durable acceptance (P1, unchanged), the CREATED -> planning
handoff (P1, unchanged), and now (P2A) planning-failure delivery, normal
execution progression through the existing run_task_until_blocked(),
non-sensitive action execution through SafeTaskExecutor (the only action
execution boundary this module ever calls - never a handler, never a tool
directly), RESPOND synthesis through an injected general conversational
ModelProvider (never the structured planner provider), terminal
result/failure delivery, and confirmation-*request* delivery when a
sensitive step blocks.

P2B scope: deterministic "CONFIRM <id>"/"REJECT <id>" command recognition
(classify_confirmation_text()), resolving a confirmation_id back to its
durably-waiting task (TaskRepository.get_task_by_pending_confirmation_id()),
WhatsApp-source/current-state authorization, and approval/denial through
the SAME existing approve_task_confirmation()/deny_task_confirmation()
functions this module previously only referenced in comments - see
dispatch_confirmation_work()'s own docstring for the full authority order,
the post-approval continuation (still exclusively through
run_task_until_blocked(), never a duplicated execution loop), and why
every kind of invalid/stale/wrong-source confirmation attempt produces the
identical generic reply. Like dispatch_task_work(), approve/deny execution
is reachable only from the worker thread - never the webhook HTTP thread -
because approve_task_confirmation() may execute a sensitive action
synchronously.

P2 scope (Milestone 47, schema version 6) closes the P2B crash window:
CONFIRM/REJECT reaching the webhook, HTTP 200 returning, and the process
crashing before the worker ever consumed the decision could lose it. Now,
record_confirmation_decision_durably() durably records the decision -
atomically, inside TaskRepository.record_confirmation_decision()'s own
BEGIN IMMEDIATE transaction, gated on the exact same source/state
eligibility dispatch_confirmation_work() itself re-verifies - synchronously
on the webhook HTTP request thread, BEFORE that request may ever return
HTTP 200 (see interfaces/whatsapp/server.py's own do_POST and module
docstring for the exact RECORDED/ALREADY_DECIDED/NOT_ELIGIBLE HTTP
contract). This durable write still never approves, denies, executes, or
touches task state - only the durable task_pending_confirmation.decision/
decided_at columns; approve_task_confirmation()/deny_task_confirmation()
remain reachable only from the worker thread, exactly as P2B established.
QUEUE ITEM IS NOT AUTHORITY: the worker queue's TaskConfirmationWork now
carries confirmation_id only (never decision - see that dataclass's own
docstring) - dispatch_confirmation_work() always reloads the durable
decision fresh from TaskRepository, whether reached via the normal queue
or via run_confirmation_decision_recovery_checkpoint() (the periodic
worker recovery-checkpoint pickup for a decision that was durably
recorded but never reached the worker before a crash - the P2 analogue of
run_outbound_lifecycle_recovery_checkpoint() above it in this file).
Schema version 6 adds exactly two nullable columns
(task_pending_confirmation.decision/decided_at, closed to
kernel.employee_tasks.ConfirmationDecision's two values) - no new table,
no second "decision history": the existing task_pending_confirmation row
remains the sole durable record of an in-flight decision, consumed
(deleted) atomically by whichever of consume_confirmation_and_claim_step()/
deny_confirmation()/fail_pending_confirmation() actually resolves it,
exactly as before.

TRANSITION-TRIGGERED DELIVERY (load-bearing - see dispatch_task_work()'s and
_deliver_execution_result()'s own docstrings for the exact mechanics): a
WhatsApp lifecycle message is sent if and only if THIS worker operation is
what newly produced the lifecycle state - never merely because a reloaded
TaskRecord happens to already be in some state. This is what prevents a
duplicate/redundant TaskExecutionWork dispatch of an already-terminal or
already-waiting task from resending a result or confirmation request. Two
existing facts make this provable without any new durable "delivered" flag:
(1) ExecutionAdvanceStatus.CONFIRMATION_REQUIRED is returned exclusively by
the RUNNING-task code path (kernel.task_execution.service._process_running_task())
- an already-WAITING_FOR_CONFIRMATION task can only ever produce
WAITING_FOR_CONFIRMATION or TASK_FAILED, never CONFIRMATION_REQUIRED again -
so that status value alone always means "freshly proposed on this exact
call"; (2) every dispatch function in this module gates on the task's state
BEFORE calling into the execution engine, so a terminal status can only be
reached here from a call that was allowed to happen precisely because the
input state was not already terminal.

Connection lifecycle (Milestone 46 P1 concurrency validation, unchanged by
P2A): kernel/employee_tasks/db.py's own module docstring is explicit that a
single sqlite3 connection must never be handed to multiple threads
simultaneously - "sqlite3 does not serialize concurrent calls on the same
connection object for you." Two different calling contexts need two
different connection lifecycles:

  - accept_task_message() and (Milestone 47 P2) record_confirmation_decision_durably()
    are both called from a ThreadingHTTPServer request thread - a fresh
    thread per inbound HTTP request, never reused. Each opens its own
    connection, does its bounded work, and closes it before returning -
    there is no reuse benefit to holding it open longer, and doing so
    would risk exactly the sharing hazard above if two concurrent requests
    ever touched the same instance.
  - dispatch_task_work() is called from the single, long-lived WhatsApp
    worker thread (interfaces/whatsapp/handler.py). It takes an
    already-open, caller-owned TaskRepository - the worker constructs one
    connection/repository once, at startup, and reuses it for the life of
    the process. Since it is always the same one thread, this is safe
    without any lifecycle management here. No execution, model call, or
    outbound WhatsApp send for a durable task ever happens on the HTTP
    request thread - dispatch_task_work() is reachable only from the
    worker.

Both entry points always resolve dedup/idempotency and dispatch decisions
from a FRESH read of TaskRepository - never from a value computed earlier
in the same request or a prior dispatch - matching kernel/task_execution's
own "always fresh, never a stale caller-held record" discipline.

EXECUTION-TIME CONFIG (Milestone 46 P2A - security-relevant): the
ToolsConfig/ActionRegistry used for PLANNING (P1's task_catalog, built once
at startup) is never treated as execution authority. Immediately before
every run_task_until_blocked() call, dispatch_task_work() reloads
ToolsConfig fresh via the injected `tools_config_loader` (the same
"reload every time" discipline capabilities/tasks/TasksCapability already
established) and constructs a fresh SafeTaskExecutor from it - this is what
makes kernel.task_execution's own current-config revalidation
(evaluate_next_step()/revalidate_action()) meaningful in practice, rather
than a check against a snapshot that always trivially agrees with the plan.
"""

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass

from datetime import datetime, timedelta, timezone

from kernel.employee_tasks import (
    ConfirmationDecision,
    ConfirmationDecisionOutcome,
    ConfirmationMismatchError,
    DuplicateTaskError,
    InvalidTransitionError,
    LifecycleEventKind,
    LifecycleEventPayloadError,
    NoPendingConfirmationError,
    StepAlreadyClaimedError,
    StepStatus,
    TaskInputTooLargeError,
    TaskNotFoundError,
    TaskRecord,
    TaskRepository,
    TaskState,
    TaskStorageError,
    compute_outbox_retry_delay_seconds,
    deserialize_confirmation_required_payload,
)
from kernel.task_execution import (
    ExecutionAdvanceStatus,
    ObservationDeserializationError,
    approve_task_confirmation,
    deny_task_confirmation,
    deserialize_observation,
    run_task_until_blocked,
)
from kernel.task_orchestration import TaskNotInCreatedStateError, advance_task_planning
from kernel.task_planner import PlanDeserializationError, StepKind, deserialize_plan
from kernel.tools.executor import SafeTaskExecutor

from interfaces.whatsapp.client import WhatsAppClientError

logger = logging.getLogger(__name__)

# AI-OS application limit on an outbound task-control lifecycle message -
# mirrors interfaces/whatsapp/handler.py's own MAX_OUTGOING_TEXT_LENGTH
# (currently the same value, independently owned by each module - handler.py
# cannot be imported here without creating an import cycle, since it already
# imports from this module). Not a claim about any WhatsApp platform limit.
MAX_OUTGOING_TEXT_LENGTH = 4096

_LIFECYCLE_MESSAGE_TOO_LONG_TEXT = (
    "The task result was too long to send here. Check task history through another interface."
)

TASK_FAILED_FALLBACK_TEXT = "Task failed."
TASK_COMPLETED_FALLBACK_TEXT = "Task completed."
TASK_CANCELLED_TEXT = "Task cancelled."
# Milestone 47 P1: the defensive fallback for the structurally-shouldn't-
# happen case where a deliverable ExecutionAdvanceStatus has no
# corresponding task_lifecycle_outbox row at all - see
# _deliver_execution_result()'s own docstring. Deliberately generic across
# every event kind (confirmation-required or terminal alike), matching
# this module's established "never disclose which specific case happened"
# discipline.
_LIFECYCLE_EVENT_UNAVAILABLE_TEXT = (
    "A task update could not be delivered right now. Please check task status directly."
)
# Milestone 47 P1 adversarial-review correction (LOW-2), stated explicitly so
# this is never mistaken for a second, independent delivery path: this text
# is sent only from the "no outbox row found for this transition" branch
# inside _deliver_execution_result() below, which is reachable only if the
# task_lifecycle_outbox
# atomicity invariant documented on TaskRepository has ALREADY been
# violated by something upstream of this function. It is a best-effort,
# fail-safe notice for an already-broken invariant, not itself part of the
# at-least-once delivery guarantee - that guarantee is provided entirely by
# the outbox row (_deliver_outbox_event()/run_outbound_lifecycle_recovery_
# checkpoint()), which this fallback does not create, retry, or backfill.
# Milestone 47 P1: sent instead of a confirmation-required message whose
# durable payload could not be deserialized (a structurally-unreachable,
# read-time defense-in-depth case - see
# kernel.employee_tasks.LifecycleEventPayloadError's own docstring) -
# never the raw JSON, never a stack trace.
_MALFORMED_OUTBOX_PAYLOAD_TEXT = (
    "A task confirmation notification could not be reconstructed. Please check task status directly."
)

# Milestone 33's strict "/task" command prefix, reused here verbatim
# (kernel/orchestrator/router.py's own _TASK_COMMAND_PATTERN) - anchored to
# the start of the (already-stripped) message text, since this is a
# command, not a phrase that can appear mid-sentence. WhatsApp intercepts
# this itself now (see this module's own module docstring); the router's
# copy of this pattern is unchanged and untouched by this milestone.
_TASK_PREFIX_PATTERN = re.compile(r"^/task(\s|$)", re.IGNORECASE)

# Exact, case-insensitive legacy bare verbs this migrates away from the old
# Milestone 33 TasksCapability/kernel.tools.confirmation.py path. Deliberately
# exact-match only, mirroring capabilities/tasks/command_parser.py's own
# "no fuzzy matching, no partial-token matching" discipline - "/task confirm
# now" or "/task cancel my flight" are NOT legacy commands, and fall through
# to become ordinary natural-language request text below.
_LEGACY_HELP_VERBS = frozenset({"help"})
_LEGACY_MIGRATION_VERBS = frozenset({"confirm", "cancel"})

# Milestone 46 P2B: the CONFIRM/REJECT command grammar - deterministic,
# no LLM, no fuzzy matching (mirrors _TASK_PREFIX_PATTERN's own discipline
# exactly). The verb is matched case-insensitively; the zero-width
# (?=\s|$) lookahead (rather than a consuming group, as _TASK_PREFIX_PATTERN
# uses) means match.end() lands immediately after the verb with nothing
# extra to strip off, and - critically - means "CONFIRMabc"/"REJECTIONxyz"
# (no separator, or a longer word that merely starts with the verb) never
# matches at all, since the lookahead requires the very next character to
# be whitespace or end-of-string. Ordinary prose that happens to start
# with these words as a longer word ("CONFIRMATION is important",
# "REJECTION reason") is therefore never mistaken for a command - see
# classify_confirmation_text()'s own docstring for the full grammar.
_CONFIRMATION_PREFIX_PATTERN = re.compile(r"^(CONFIRM|REJECT)(?=\s|$)", re.IGNORECASE)

# Milestone 46 P2B: one fixed, code-owned reply for every way a CONFIRM/
# REJECT command can fail to produce a real durable decision - malformed
# shape, a repository-invalid token, an unknown token, an already-
# consumed/rejected token, a wrong-source token, or a wrong-state token.
# Deliberately identical text for every one of these - see
# dispatch_confirmation_work()'s own docstring for why disclosing which
# case actually happened would be an oracle leak. Public (Milestone 47
# P2, not merely module-private) since server.py's do_POST now also uses
# it directly for the NOT_ELIGIBLE durable-decision outcome - the exact
# same fixed text, whether the failure is detected on the webhook HTTP
# thread (before a decision could even be durably recorded) or later, on
# the worker (a race-lost or otherwise-invalid confirmation_id).
GENERIC_INVALID_CONFIRMATION_TEXT = "That confirmation is no longer valid."

TASK_SOURCE = "whatsapp"

# Namespaces every dedup_key this module ever writes - never a raw provider
# message ID (see compute_dedup_key()) and never shared with any other
# TaskRecord source, even though tasks.dedup_key's UNIQUE constraint is
# global rather than composite with source (kernel/employee_tasks/db.py).
_DEDUP_NAMESPACE = "whatsapp:"

TASK_HELP_TEXT = (
    "AI-OS task control:\n"
    "/task <what you want done> - start a new task\n"
    "/task help - show this message\n\n"
    "Confirmations use CONFIRM <id> / REJECT <id> - use the id from the "
    "confirmation request message."
)
_TASK_MIGRATION_TEXT = (
    "Confirmations now use CONFIRM <id> or REJECT <id>. Use the id from "
    "the confirmation request message."
)


@dataclass(frozen=True)
class TaskFixedReply:
    """A pre-decided fixed reply for a /task message that never touches
    TaskRepository - bare /task, /task help, and the legacy /task
    confirm/cancel migration notices. An oversized request never reaches
    this function in the first place - see classify_task_text()'s own
    docstring."""

    reply_text: str


@dataclass(frozen=True)
class TaskRequestText:
    """A /task message whose remainder is genuine natural-language request
    text, ready for durable acceptance via accept_task_message(). Carries
    only the text - no I/O, no task identity yet."""

    request_text: str


def classify_task_text(text: str) -> TaskFixedReply | TaskRequestText | None:
    """Pure - no I/O, no database access. Returns None if `text` is not a
    /task command at all (ordinary chat, unchanged - the caller should fall
    through to the existing conversational path).

    `text` must already be bounded by the caller
    (interfaces/whatsapp/handler.py:classify_message() enforces
    MAX_INCOMING_TEXT_LENGTH, currently equal to
    kernel.employee_tasks.MAX_REQUEST_TEXT_CHARS, before this function is
    ever called) - this function performs no length check of its own.
    Milestone 46 adversarial review: an internal oversized-request check
    here was found to be permanently unreachable dead code, since the
    "/task "-stripped remainder is always strictly shorter than the whole
    message the caller already bounded - removed rather than kept as an
    inert duplicate. TaskRepository.create_task() independently re-validates
    request_text's length regardless of what any caller here assumes, so
    removing this redundant check introduces no unbounded-ingestion path
    even for a hypothetical future caller that skips handler.py's bound.

    Deterministic and exact-match only, matching
    capabilities/tasks/command_parser.py's own discipline: no LLM, no
    fuzzy matching, no partial-token matching. Every other "/task <text>"
    form becomes verbatim natural-language request_text for the durable
    planner - this is the Milestone 46 migration away from Milestone 33's
    fixed command grammar (see this module's own module docstring)."""

    if not _TASK_PREFIX_PATTERN.match(text):
        return None

    remainder = text[len("/task"):].strip()
    if not remainder:
        return TaskFixedReply(TASK_HELP_TEXT)

    lowered = remainder.casefold()
    if lowered in _LEGACY_HELP_VERBS:
        return TaskFixedReply(TASK_HELP_TEXT)
    if lowered in _LEGACY_MIGRATION_VERBS:
        return TaskFixedReply(_TASK_MIGRATION_TEXT)

    return TaskRequestText(remainder)


@dataclass(frozen=True)
class ConfirmationCommandText:
    """Milestone 47 P2: the pure parse result of a well-formed "CONFIRM
    <id>"/"REJECT <id>" command - confirmation_id and the requested
    ConfirmationDecision only, produced by classify_confirmation_text()
    with no I/O, no repository access, and no eligibility check of any
    kind (exactly like TaskRequestText is the pure parse result of a
    well-formed "/task ..." message - see that dataclass's own
    docstring). NOT a worker queue item: server.py's do_POST special-cases
    this type exactly like it special-cases TaskRequestText, calling
    TaskRepository.record_confirmation_decision() durably BEFORE this
    request can ever return HTTP 200 - see this module's own module
    docstring for why. Once that durable write succeeds, do_POST hands
    the worker only a decision-free TaskConfirmationWork(confirmation_id)
    - `decision` here is transient parse output, consumed exactly once by
    the durable-recording call, never itself treated as execution
    authority and never itself queued."""

    confirmation_id: str
    decision: ConfirmationDecision


@dataclass(frozen=True)
class TaskConfirmationWork:
    """Minimal, trusted correlation handed to the WhatsApp worker queue for
    a CONFIRM/REJECT command - confirmation_id ONLY (Milestone 47 P2;
    previously also carried `decision` - see this module's own module
    docstring for why that was removed), exactly like TaskExecutionWork
    carries only a bare task_id (see that dataclass's own docstring).
    Deliberately carries no task_id, sender, provider message ID, raw
    message text, action_name, resource_key, or decision: QUEUE ITEM IS
    NOT AUTHORITY. The worker (dispatch_confirmation_work()) always
    reloads BOTH the owning task AND the durably-recorded decision fresh
    from TaskRepository by confirmation_id alone - never by trusting
    anything this queue item happens to carry, and never by trusting a
    decision value that arrived with this item at construction time,
    since by the time the worker actually processes it, a durable
    decision is the only value that could possibly still be correct (the
    queue item may have been sitting unprocessed for an arbitrary amount
    of time, or reconstructed fresh by a recovery checkpoint after a
    crash - see run_confirmation_decision_recovery_checkpoint())."""

    confirmation_id: str


@dataclass(frozen=True)
class ConfirmationFixedReply:
    """A pre-decided fixed reply for a recognized-but-malformed CONFIRM/
    REJECT command that never touches TaskRepository - the command-shape
    equivalent of TaskFixedReply for bare/malformed "/task ...". Always
    carries GENERIC_INVALID_CONFIRMATION_TEXT today; a distinct dataclass
    from TaskFixedReply purely so a caller can route it through the exact
    same authorized-sender-derived FixedReplyTask delivery path without
    conflating the two command families' own docstrings."""

    reply_text: str


def classify_confirmation_text(text: str) -> ConfirmationFixedReply | ConfirmationCommandText | None:
    """Pure - no I/O, no database access, no repository-bound validation
    of confirmation_id itself (that remains
    TaskRepository.get_task_by_pending_confirmation_id()'s job - see this
    module's own module docstring on why the parser deliberately does not
    duplicate it).

    PRECONDITION (adversarial review): `text` must already be stripped of
    leading/trailing whitespace by the caller - this function's own
    `_CONFIRMATION_PREFIX_PATTERN` is anchored at position 0 and does not
    skip leading whitespace itself, exactly like classify_task_text()'s
    own `_TASK_PREFIX_PATTERN` has the identical precondition for "/task".
    interfaces/whatsapp/handler.py:classify_message() (this function's
    sole production caller) already strips every inbound message
    (`text = (message.text or "").strip()`) before calling either parser,
    so in production, leading/trailing whitespace around "CONFIRM"/"REJECT"
    is transparently tolerated (confirmed empirically: "   CONFIRM abc",
    "\\tCONFIRM abc", and "\\nCONFIRM abc" all correctly parse once passed
    through classify_message()) - but calling this function directly, in
    isolation, with unstripped leading whitespace returns None instead of
    the expected command, exactly as classify_task_text() would.

    Returns None if `text` is not a CONFIRM/REJECT command
    at all (ordinary chat, unchanged - the caller should fall through to
    the existing conversational path exactly like a non-"/task" message
    already does).

    Grammar: the verb (CONFIRM or REJECT) is matched case-insensitively,
    and only when immediately followed by whitespace or end-of-string
    (_CONFIRMATION_PREFIX_PATTERN's own zero-width lookahead) - so
    "CONFIRMabc", "CONFIRMATION is important", and "REJECTION reason" are
    never mistaken for a command; they fall through as ordinary chat
    exactly like any other prose. Once the prefix matches, the remainder
    is whitespace-stripped and split on any run of whitespace (handles
    multiple spaces, tabs, or embedded newlines identically - Python's own
    str.split() with no arguments). Exactly one resulting word is a
    well-formed command - that word becomes confirmation_id, preserved
    EXACTLY as typed (never case-folded, never trimmed further - the
    repository's own confirmation_id comparison is exact-equality and
    case-sensitive, since real confirmation_id values are lowercase UUID7
    text and a case-mismatched CONFIRM/REJECT must fail exactly like an
    unknown token does, not be "helpfully" normalized into a match nobody
    asked for). Zero words (a bare "CONFIRM"/"REJECT") or two-or-more
    words ("CONFIRM abc extra", "CONFIRM token token") are BOTH
    recognized-but-malformed - the identical ConfirmationFixedReply either
    way, decided entirely here with no repository/worker involvement at
    all, and no different from an unknown-but-well-formed token's eventual
    worker-side reply (see dispatch_confirmation_work()'s own docstring on
    why every one of these must produce indistinguishable text)."""

    match = _CONFIRMATION_PREFIX_PATTERN.match(text)
    if match is None:
        return None

    decision = (
        ConfirmationDecision.CONFIRM if match.group(1).upper() == "CONFIRM" else ConfirmationDecision.REJECT
    )
    remainder = text[match.end():].strip()
    parts = remainder.split()
    if len(parts) != 1:
        return ConfirmationFixedReply(GENERIC_INVALID_CONFIRMATION_TEXT)

    return ConfirmationCommandText(confirmation_id=parts[0], decision=decision)


class DurableAcceptanceFailed(Exception):
    """Raised when a /task message could not be durably accepted or
    resolved - a missing/malformed provider message ID, a genuine
    TaskStorageError, or a raw sqlite3.Error escaping connection open/use
    (see this module's own module docstring on kernel/employee_tasks/db.py's
    documented concurrent-first-open caveat). Never carries the underlying
    exception's own message or any SQL/database/path detail - callers must
    treat this as a fixed, generic signal only (log a bounded category),
    never surface str(exc) anywhere.

    Milestone 46 adversarial review (M1): every current raise site behind
    this exception is a genuinely transient/retryable condition (storage
    unavailable, a defense-in-depth connection-open race) or an
    unreachable-in-production structural guard (a missing provider message
    ID - kernel.employee_tasks.MAX_REQUEST_TEXT_CHARS-independent, and
    already filtered upstream by interfaces/whatsapp/payload.py before this
    module is ever reached), so callers treating every instance as a
    retryable webhook status remains correct. A permanent, deterministic
    validation failure that a caller could plausibly reach in production
    (e.g. an oversized request) is handled entirely before this exception
    type is ever involved - see classify_task_text()."""


def compute_dedup_key(provider_message_id: str) -> str:
    """Deterministic, namespaced SHA-256 digest of the provider message ID
    - never the raw ID itself (see this module's own module docstring: the
    raw ID is transport idempotency/correlation input only, never
    persisted, never exposed to the planner). Fixed 73-character output
    ("whatsapp:" + 64 hex characters) regardless of the input's length,
    comfortably within kernel.employee_tasks.MAX_DEDUP_KEY_CHARS (128).

    Milestone 46 adversarial review (M1): deliberately imposes no
    provider-specific maximum length on `provider_message_id` - no such
    contract is documented anywhere in this repository, and inventing one
    (a prior version of this function used an arbitrary 256-character
    limit) risked permanently rejecting a legitimate, authenticated Meta
    message ID for no evidenced reason. The real, load-bearing bound is
    already upstream and structural: interfaces/whatsapp/server.py rejects
    the entire webhook body above DEFAULT_MAX_BODY_BYTES (1,000,000 bytes)
    before any JSON parsing occurs, so provider_message_id - a single field
    extracted from that already-bounded, already-authenticated body - can
    never exceed roughly that size. hashlib.sha256() over an input that
    size is not a meaningful computational cost, so no separate,
    provider-specific field limit is needed to keep hashing bounded.

    Only a structural precondition remains: the ID must actually exist.
    Unreachable in production today (interfaces/whatsapp/payload.py already
    filters out any message missing an "id" field before it ever reaches
    this module), but kept as a defense-in-depth guard for any future/direct
    caller."""

    if not isinstance(provider_message_id, str) or not provider_message_id:
        raise DurableAcceptanceFailed("missing provider message id")

    digest = hashlib.sha256(provider_message_id.encode("utf-8")).hexdigest()
    return f"{_DEDUP_NAMESPACE}{digest}"


# Milestone 47 P2 adversarial-review correction: a distinct namespace from
# _DEDUP_NAMESPACE above - this key identifies a WINNING CONFIRM/REJECT
# provider message in task_confirmation_ingress_receipts, a completely
# separate durable concept from tasks.dedup_key's own /task-ingress
# idempotency boundary. Never reused across the two tables/purposes, even
# though both are SHA-256 digests of a provider message ID, so the two
# spaces can never collide or be confused for one another.
_CONFIRMATION_INGRESS_DEDUP_NAMESPACE = "whatsapp-confirmation:"


def compute_confirmation_ingress_dedup_key(provider_message_id: str) -> str:
    """Deterministic, namespaced SHA-256 digest of the provider message ID
    for a CONFIRM/REJECT command - never the raw ID itself, exactly like
    compute_dedup_key() above, but under a distinct namespace
    (_CONFIRMATION_INGRESS_DEDUP_NAMESPACE) so the two never collide.
    Fixed-length output regardless of input length, comfortably within
    kernel.employee_tasks.MAX_DEDUP_KEY_CHARS (128) - see
    compute_dedup_key()'s own docstring for why no separate provider-
    specific length bound is needed beyond the existing upstream webhook-
    body size limit.

    This is the value TaskRepository.record_confirmation_decision()'s own
    `provider_dedup_key` parameter expects - see that method's own
    docstring for the durable receipt this key identifies, and why it
    survives task_pending_confirmation's own row being consumed/deleted.

    Only a structural precondition remains: the ID must actually exist -
    unreachable in production today (interfaces/whatsapp/payload.py
    already filters out any message missing an "id" field before it ever
    reaches this module), kept as a defense-in-depth guard only."""

    if not isinstance(provider_message_id, str) or not provider_message_id:
        raise ConfirmationDecisionRecordingFailed("missing provider message id")

    digest = hashlib.sha256(provider_message_id.encode("utf-8")).hexdigest()
    return f"{_CONFIRMATION_INGRESS_DEDUP_NAMESPACE}{digest}"


def accept_task_message(
    open_writer_connection,
    db_path,
    request_text: str,
    provider_message_id: str,
) -> TaskRecord:
    """Durably accept a /task message as exactly one TaskRecord, or
    resolve the one that already exists for this provider message.

    Opens and closes its OWN sqlite3 connection/TaskRepository instance for
    this one call - see this module's own module docstring for why this is
    the correct lifecycle for a ThreadingHTTPServer request-thread caller.
    `open_writer_connection` is passed in (rather than imported and called
    directly) purely so tests can inject a fake/instrumented opener without
    monkeypatching kernel.employee_tasks.db - the real caller always passes
    kernel.employee_tasks.open_writer_connection.

    The SQLite UNIQUE constraint on tasks.dedup_key is the sole
    concurrency/idempotency authority here (see the Milestone 46 P1 design
    report's empirical validation) - not SeenMessageCache, which this
    module never imports or touches.

    Raises DurableAcceptanceFailed - never a raw TaskStorageError or
    sqlite3.Error - on any failure to durably accept or resolve this
    message. The caller must treat that as "no durable acceptance
    occurred" and respond with a retryable webhook status, never HTTP
    success.
    """

    dedup_key = compute_dedup_key(provider_message_id)

    conn = None
    try:
        conn = open_writer_connection(db_path)
        repository = TaskRepository(conn)
        try:
            return repository.create_task(request_text, TASK_SOURCE, dedup_key=dedup_key)
        except DuplicateTaskError:
            existing = repository.get_task_by_dedup_key(dedup_key)
            if existing is None:
                # Structurally shouldn't happen - a DuplicateTaskError means
                # a row with this exact dedup_key already exists - but never
                # assumed; fail closed rather than returning something a
                # caller would have to null-check unexpectedly.
                raise DurableAcceptanceFailed("duplicate task could not be resolved")
            return existing
    except TaskStorageError as exc:
        logger.warning("task_ingress_storage_error")
        raise DurableAcceptanceFailed("task storage unavailable") from exc
    except sqlite3.Error as exc:
        # Defense in depth against kernel/employee_tasks/db.py's documented
        # concurrent-first-open race (open_writer_connection() can leak a
        # raw, unconverted sqlite3.OperationalError under concurrent
        # first-ever schema creation - see that module's own docstring).
        # The composition root pre-initializes the schema once at startup
        # (interfaces/whatsapp/server.py:build_server()) specifically to
        # make this branch unreachable in practice; it remains here as a
        # safety net, never relied on as the primary mitigation.
        logger.warning("task_ingress_storage_error")
        raise DurableAcceptanceFailed("task storage unavailable") from exc
    finally:
        if conn is not None:
            conn.close()


class ConfirmationDecisionRecordingFailed(Exception):
    """Milestone 47 P2: raised when a CONFIRM/REJECT command's decision
    could not be durably recorded at all - a genuine TaskStorageError or a
    raw sqlite3.Error escaping connection open/use (the same defense-in-
    depth concurrent-first-open caveat accept_task_message() already
    guards against - see this module's own module docstring). Never
    carries the underlying exception's own message or any SQL/database/
    path detail - callers must treat this as a fixed, generic signal only
    (log a bounded category), never surface str(exc) anywhere. The caller
    must treat this as "no durable decision exists" and respond with a
    retryable webhook status (503), exactly like DurableAcceptanceFailed -
    never HTTP success, and never a fallback to queueing the command
    without a durable decision (that would silently resurrect the exact
    M46 crash window this milestone closes)."""


def record_confirmation_decision_durably(
    open_writer_connection,
    db_path,
    confirmation_id: str,
    decision: ConfirmationDecision,
    *,
    required_source: str,
    provider_dedup_key: str,
) -> ConfirmationDecisionOutcome:
    """Durably record a CONFIRM/REJECT command's decision, from the
    webhook HTTP request thread, BEFORE this request may ever return
    HTTP 200 - closing the Milestone 46 crash window where a decision
    existed only in the volatile worker queue between HTTP 200 and
    worker consumption (see this module's own module docstring).

    Opens and closes its OWN sqlite3 connection/TaskRepository instance
    for this one call, exactly like accept_task_message() - see that
    function's own docstring and this module's own module docstring for
    why this is the correct connection lifecycle for a ThreadingHTTPServer
    request-thread caller. `open_writer_connection` is passed in (rather
    than imported and called directly) for the same test-injection reason
    accept_task_message() takes it as a parameter.

    `provider_dedup_key` (Milestone 47 P2 adversarial-review correction)
    must already be compute_confirmation_ingress_dedup_key(message_id)'s
    own output - never a raw provider message ID. Threaded straight
    through to TaskRepository.record_confirmation_decision(), which
    atomically checks it against task_confirmation_ingress_receipts
    BEFORE any other eligibility check, so an exact redelivery of a
    provider message that already durably won returns
    ConfirmationDecisionOutcome.DUPLICATE_INGRESS - crash-safe and
    restart-surviving, unlike SeenMessageCache, and correct even after
    task_pending_confirmation's own row has long since been consumed.

    Delegates the entire eligibility check and atomic write to
    TaskRepository.record_confirmation_decision() - this function performs
    no authorization or eligibility logic of its own, only connection
    lifecycle and exception translation. Returns the SAME
    ConfirmationDecisionOutcome that method returns (RECORDED/
    ALREADY_DECIDED/DUPLICATE_INGRESS/NOT_ELIGIBLE) for these four
    ordinary outcomes; raises ConfirmationDecisionRecordingFailed - never
    a raw TaskStorageError or sqlite3.Error - only for a genuine storage
    failure, mirroring accept_task_message()'s own exception-translation
    contract exactly.

    A malformed confirmation_id (oversized, wrong type, NUL-containing -
    TaskInputTooLargeError from record_confirmation_decision()'s own
    input validation) is deliberately NOT treated as a storage failure:
    it is a deterministic, never-retryable validation rejection, exactly
    like dispatch_confirmation_work()'s own reverse lookup already treats
    an oversized token as an ordinary NOT_ELIGIBLE case (generic-invalid
    reply, HTTP 200) rather than a worker/storage error - retrying the
    identical malformed value would never succeed, so this must never
    surface as ConfirmationDecisionRecordingFailed (which callers treat as
    retryable, HTTP 503)."""

    conn = None
    try:
        conn = open_writer_connection(db_path)
        repository = TaskRepository(conn)
        try:
            return repository.record_confirmation_decision(
                confirmation_id,
                decision,
                required_source=required_source,
                provider_dedup_key=provider_dedup_key,
            )
        except TaskInputTooLargeError:
            return ConfirmationDecisionOutcome.NOT_ELIGIBLE
    except TaskStorageError as exc:
        logger.warning("confirmation_decision_storage_error")
        raise ConfirmationDecisionRecordingFailed("task storage unavailable") from exc
    except sqlite3.Error as exc:
        # Defense in depth against kernel/employee_tasks/db.py's documented
        # concurrent-first-open race - see accept_task_message()'s own
        # comment on the identical branch for why this is a safety net,
        # never the primary mitigation (build_server() pre-initializes the
        # schema once at startup).
        logger.warning("confirmation_decision_storage_error")
        raise ConfirmationDecisionRecordingFailed("task storage unavailable") from exc
    finally:
        if conn is not None:
            conn.close()


@dataclass(frozen=True)
class TaskExecutionWork:
    """Minimal, trusted correlation handed to the WhatsApp worker queue for
    a durable task - task_id only, never raw request text, the provider
    message ID, the sender ID, a planner prompt, an ActionRequest, a plan,
    or a resource_key. TaskRepository remains the sole source of truth:
    the worker always reloads the authoritative TaskRecord fresh by
    task_id (see dispatch_task_work()) rather than trusting anything cached
    in this queue item."""

    task_id: str


def needs_dispatch(task: TaskRecord) -> bool:
    """True only for TaskState.CREATED - the only state P1 ever advances
    (the CREATED -> planning handoff). Every other state (already planning/
    ready/running/waiting_for_confirmation, or terminal) means either the
    single worker already owns this task or nothing further is owed to it
    by ingress - see the Milestone 46 P1 design report's dispatch-decision
    table. No new durable "queued" state/column is introduced: TaskState
    alone is exactly the signal needed."""

    return task.state is TaskState.CREATED


def _format_failure_message(failure_summary: str | None) -> str:
    """TaskRecord.failure_summary is already fully safe for direct display
    for every failure path in kernel.task_execution/kernel.task_orchestration
    - every failure_summary those packages ever persist is a fixed,
    code-authored constant (never raw exception text, model output, or
    unvalidated dynamic content) and bounded to
    kernel.employee_tasks.MAX_FAILURE_SUMMARY_CHARS (512). Still defends
    against an unexpectedly absent value rather than assuming one."""

    if not failure_summary:
        return TASK_FAILED_FALLBACK_TEXT
    return f"Task failed: {failure_summary}"


def _format_confirmation_message(pending) -> str:
    """Fixed, code-owned template - never model-generated. Only trusted,
    already-bounded fields: action_name (a fixed registry action name),
    resource_key (a config-defined symbolic key, never a resolved path),
    and confirmation_id (an opaque UUID7 token). Never step_position, a
    resolved path, tool arguments, plan/observation JSON, or any other
    internal detail."""

    return (
        "Confirmation required.\n\n"
        f"Action: {pending.action_name}\n"
        f"Resource: {pending.resource_key}\n\n"
        "Reply:\n"
        f"CONFIRM {pending.confirmation_id}\n"
        "or\n"
        f"REJECT {pending.confirmation_id}"
    )


def select_terminal_result_text(repository: TaskRepository, task: TaskRecord) -> str:
    """Deterministically select the user-facing result text for a newly
    COMPLETED task, from durable data only - never result_json, a raw
    ActionResult, TaskPlan JSON, or observation JSON.

    Rule (Milestone 46 P2 design report): if one or more successfully
    completed RESPOND steps exist in the persisted plan, use the LAST one
    by persisted plan POSITION (never DB insertion order, a timestamp, or
    any other incidental ordering - a plan is not required to end in
    RESPOND, and more than one successful RESPOND step is possible).
    Otherwise use the last successfully completed step of any kind, by the
    same position-based ordering. If no usable successful observation
    exists at all, a fixed fallback notice is used - never a claim that
    something happened without durable evidence of it."""

    if task.plan_json is None:
        return TASK_COMPLETED_FALLBACK_TEXT
    try:
        plan = deserialize_plan(task.plan_json)
    except PlanDeserializationError:
        return TASK_COMPLETED_FALLBACK_TEXT

    progress_by_position = {
        progress.step_position: progress for progress in repository.list_step_progress(task.task_id)
    }

    def _safe_summary_at(position: int) -> str | None:
        progress = progress_by_position.get(position)
        if progress is None or progress.status is not StepStatus.SUCCEEDED or progress.result_json is None:
            return None
        try:
            observation = deserialize_observation(progress.result_json)
        except ObservationDeserializationError:
            return None
        if not observation.success:
            return None
        return observation.safe_summary

    respond_positions = sorted(step.position for step in plan.steps if step.kind is StepKind.RESPOND)
    for position in reversed(respond_positions):
        summary = _safe_summary_at(position)
        if summary is not None:
            return summary

    all_positions = sorted(step.position for step in plan.steps)
    for position in reversed(all_positions):
        summary = _safe_summary_at(position)
        if summary is not None:
            return summary

    return TASK_COMPLETED_FALLBACK_TEXT


def _bounded_lifecycle_text(text: str) -> str:
    """Never truncates, never chunks: a composed lifecycle message over the
    outbound bound is discarded outright and replaced with one fixed
    notice - mirrors interfaces/whatsapp/handler.py's own
    _prepare_outbound_text()/LONG_RESPONSE_NOTICE discipline exactly (see
    this module's own MAX_OUTGOING_TEXT_LENGTH comment for why that
    function isn't imported directly)."""

    if len(text) > MAX_OUTGOING_TEXT_LENGTH:
        return _LIFECYCLE_MESSAGE_TOO_LONG_TEXT
    return text


def _send_lifecycle_message(client, recipient: str, text: str) -> None:
    """Send one bounded task-control lifecycle message (confirmation
    request, or terminal result/failure/cancellation) to the trusted,
    caller-supplied recipient - never derived from request text, TaskPlan,
    StepObservation, model output, resource_key, or confirmation_id (the
    caller is responsible for passing the fixed configured authorized
    sender - this function performs no recipient logic of its own). A send
    failure is logged as one generic, bounded category and dropped - never
    retried, never rolled back, never a reason to re-execute anything (see
    this module's own module docstring on the Milestone 46/47 delivery
    boundary)."""

    try:
        client.send_text_message(recipient, _bounded_lifecycle_text(text))
    except WhatsAppClientError:
        logger.warning("task_control_outbound_failure")


# Milestone 47 P1: exactly the ExecutionAdvanceStatus values that ever
# correspond to a durable task_lifecycle_outbox row - kept as one shared
# set so this mapping cannot silently diverge from
# kernel.employee_tasks.TaskRepository's own
# _DELIVERABLE_EVENT_KIND_BY_TARGET_STATE (WAITING_FOR_CONFIRMATION's own
# CONFIRMATION_REQUIRED status is included here even though it is not a
# key in that other mapping - propose_confirmation() inserts its own
# outbox row directly, see that method's own docstring).
_DELIVERABLE_STATUSES = frozenset(
    {
        ExecutionAdvanceStatus.CONFIRMATION_REQUIRED,
        ExecutionAdvanceStatus.TASK_COMPLETED,
        ExecutionAdvanceStatus.TASK_FAILED,
        ExecutionAdvanceStatus.TASK_CANCELLED,
    }
)


def _render_outbox_event_text(event, repository: TaskRepository) -> str:
    """Render the exact user-facing text for one durable
    task_lifecycle_outbox event - the same rendering rules
    _deliver_execution_result() always used, just fed from the durable
    event rather than an ExecutionAdvanceResult, so a fresh send and a
    later recovery redelivery are indistinguishable in content.

    For CONFIRMATION_REQUIRED: deserializes the event's OWN captured,
    immutable payload - never a live task_pending_confirmation row, which
    may already have been replaced by a later confirmation round (see
    kernel.employee_tasks.ConfirmationRequiredPayload's own docstring for
    why). A malformed/undeserializable payload (structurally unreachable
    through this module's own write path - see
    kernel.employee_tasks.LifecycleEventPayloadError's own docstring) gets
    a fixed, bounded, code-owned fallback notice instead - never raw JSON,
    never a stack trace, never silently treated as "nothing to send".

    For every terminal kind (TASK_COMPLETED/TASK_FAILED/TASK_CANCELLED):
    re-derives from CURRENT task/step-progress state, which is provably
    safe here (unlike the confirmation case) because every terminal
    transition's own inputs - plan_json, terminal step-progress rows,
    failure_summary - are permanently immutable the instant that
    transition commits (terminal states have no outgoing edges - see
    kernel.employee_tasks.ALLOWED_TRANSITIONS), so "current" and
    "at-creation-time" state are provably identical forever for a
    terminal task."""

    if event.event_kind is LifecycleEventKind.CONFIRMATION_REQUIRED:
        if event.payload_json is None:
            logger.warning("task_control_malformed_outbox_payload")
            return _MALFORMED_OUTBOX_PAYLOAD_TEXT
        try:
            payload = deserialize_confirmation_required_payload(event.payload_json)
        except LifecycleEventPayloadError:
            logger.warning("task_control_malformed_outbox_payload")
            return _MALFORMED_OUTBOX_PAYLOAD_TEXT
        return _format_confirmation_message(payload)

    task = repository.get_task(event.task_id)
    if event.event_kind is LifecycleEventKind.TASK_COMPLETED:
        return select_terminal_result_text(repository, task)
    if event.event_kind is LifecycleEventKind.TASK_FAILED:
        return _format_failure_message(task.failure_summary)
    # LifecycleEventKind.TASK_CANCELLED - the only remaining member of the
    # closed enum.
    return TASK_CANCELLED_TEXT


def _deliver_outbox_event(
    event, repository: TaskRepository, client, authorized_sender: str
) -> None:
    """The single render+send+record path for every durable
    task_lifecycle_outbox event, whether reached immediately (via
    _deliver_execution_result(), right after the transition that produced
    it) or later, via run_outbound_lifecycle_recovery_checkpoint() - both
    paths render, send, and record success/failure identically, which is
    what makes this module's at-least-once delivery guarantee actually
    uniform rather than depending on which path happened to run.

    On success: durably marks `event` delivered (conditional - a no-op if
    something else already marked it, e.g. a defensive-in-depth race under
    this milestone's single-runtime-ownership guarantee) - no further
    retry. On failure: never marks delivered; persists bounded,
    deterministic retry metadata (attempt_count, next_attempt_at - a
    capped exponential backoff, see
    kernel.employee_tasks.compute_outbox_retry_delay_seconds()) - never
    the raw provider error/exception text itself, matching this module's
    own no-detail logging discipline throughout."""

    text = _render_outbox_event_text(event, repository)
    try:
        client.send_text_message(authorized_sender, _bounded_lifecycle_text(text))
    except WhatsAppClientError:
        logger.warning("task_control_outbound_failure")
        attempt_count = event.attempt_count + 1
        delay_seconds = compute_outbox_retry_delay_seconds(attempt_count)
        # Milestone 47 P1 adversarial-review correction: a real,
        # timezone-aware UTC datetime, never a pre-formatted string -
        # mark_lifecycle_event_delivery_failed() is now the sole authority
        # for how this gets canonically serialized (see that method's own
        # docstring for why).
        next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        repository.mark_lifecycle_event_delivery_failed(event.event_id, attempt_count, next_attempt_at)
        return

    repository.mark_lifecycle_event_delivered(event.event_id)


def _deliver_execution_result(
    result, repository: TaskRepository, client, authorized_sender: str
) -> None:
    """Deliver exactly one WhatsApp message as the direct consequence of
    `result` - never from independent state inspection. See this module's
    own module docstring for why ExecutionAdvanceStatus.CONFIRMATION_REQUIRED
    can only ever mean "freshly proposed on this exact call", and why every
    caller of this function has already gated on the task's PRE-CALL state
    so that a terminal status reaching here can only mean this call is what
    produced it.

    Milestone 47 P1: delivery is now durable-event-backed end to end -
    every status in _DELIVERABLE_STATUSES corresponds to EXACTLY one
    task_lifecycle_outbox row, atomically created by TaskRepository in the
    SAME transaction as the transition that produced it (see that
    module's own docstring for the exact atomicity invariant: the
    transition commits with its outbox row, or neither commits). This
    function looks that event up via result.task's own (task_id, version)
    - an exact, unambiguous correlation, never an "oldest/latest pending"
    guess even across many historical confirmation rounds - then renders
    and sends through the SAME _deliver_outbox_event() path
    run_outbound_lifecycle_recovery_checkpoint() also uses."""

    if result.status not in _DELIVERABLE_STATUSES:
        # ExecutionAdvanceStatus.WAITING_FOR_CONFIRMATION (already
        # delivered on a prior advance) or STEP_SUCCEEDED (unreachable
        # from run_task_until_blocked()'s own stopping set) - nothing to
        # deliver.
        return

    event = repository.get_outbox_event_for_task_version(result.task.task_id, result.task.version)
    if event is None:
        # Structurally shouldn't happen - every deliverable transition
        # atomically creates its own outbox row in the SAME transaction
        # (see kernel.employee_tasks.TaskRepository's own module
        # docstring) - but never assumed away. No fabricated content, no
        # execution, no destructive mutation - a bounded generic notice
        # only, and the task's durable state is left exactly as the
        # execution engine already left it. This notice is best-effort
        # only (see _LIFECYCLE_EVENT_UNAVAILABLE_TEXT's own comment): it is
        # sent once, here, with no outbox row of its own, so it is never
        # retried by run_outbound_lifecycle_recovery_checkpoint() and is
        # not covered by this module's at-least-once delivery guarantee -
        # that guarantee applies only to events that DO have an outbox row.
        logger.warning("task_control_missing_outbox_event")
        _send_lifecycle_message(client, authorized_sender, _LIFECYCLE_EVENT_UNAVAILABLE_TEXT)
        return

    _deliver_outbox_event(event, repository, client, authorized_sender)


def run_outbound_lifecycle_recovery_checkpoint(
    repository: TaskRepository, client, authorized_sender: str
) -> None:
    """Milestone 47 P1: attempt AT MOST ONE due, undelivered WhatsApp
    lifecycle-outbox redelivery per call - deliberately bounded to exactly
    one external send, never a whole page, so a burst of pending
    notifications (or a permanently-failing one) can never block the
    worker's own normal queue responsiveness for longer than one outbound
    Cloud API attempt (see interfaces/whatsapp/server.py's own
    monotonic-deadline recovery-checkpoint scheduling, which calls this
    function at most once per checkpoint). Filters strictly on
    channel=TASK_SOURCE ("whatsapp") - this is what makes it structurally
    impossible for this function to ever consume, and therefore ever
    misdeliver, a different source/channel's lifecycle event (see
    kernel.employee_tasks.LifecycleOutboxEvent's own docstring). A
    complete no-op, cheaply, if nothing is currently due."""

    due_events = repository.list_due_lifecycle_outbox_events(channel=TASK_SOURCE, limit=1)
    if not due_events:
        return
    _deliver_outbox_event(due_events[0], repository, client, authorized_sender)


def run_confirmation_decision_recovery_checkpoint(
    repository: TaskRepository,
    registry,
    tools_config_loader,
    respond_provider,
    client,
    authorized_sender: str,
) -> None:
    """Milestone 47 P2: attempt AT MOST ONE durable confirmation-decision
    pickup per call - the recovery-checkpoint counterpart of
    run_outbound_lifecycle_recovery_checkpoint() above, closing the same
    kind of crash gap for a decision that was durably recorded
    (TaskRepository.record_confirmation_decision() returned RECORDED) but
    never reached dispatch_confirmation_work() - e.g. the worker queue was
    full at recording time, or the process crashed between the durable
    write and worker consumption. Deliberately bounded to exactly one
    pickup per checkpoint, for the same reason the lifecycle-outbox
    checkpoint is bounded to one send: a burst of recovered decisions (or
    one that keeps failing) must never block the worker's own normal
    queue responsiveness for longer than one confirmation dispatch (see
    interfaces/whatsapp/server.py's own monotonic-deadline
    recovery-checkpoint scheduling, which calls this function at most once
    per checkpoint, interleaved with the lifecycle-outbox checkpoint
    above - see WhatsAppServer._run_worker()'s own docstring for the exact
    ordering).

    Filters strictly on source=TASK_SOURCE ("whatsapp") via
    TaskRepository.find_recoverable_confirmation_decision() - this is what
    makes it structurally impossible for this function to ever act on a
    different source's confirmation decision. That same lookup is also
    now DUE-filtered (Milestone 47 P2 adversarial-review correction,
    MEDIUM-2 - see find_recoverable_confirmation_decision()'s own
    docstring): it only ever returns a decision whose
    decision_next_attempt_at has already arrived, so a decision durably
    deferred below (see FAILURE HANDLING) cannot be picked again before
    its own backoff elapses. Dispatches DIRECTLY through the existing
    dispatch_confirmation_work() - never re-queued back onto the worker's
    own queue - since this call already runs on the single worker thread,
    which already owns all serialization; queueing back to itself would
    add nothing but an unnecessary extra hop. A complete no-op, cheaply,
    if nothing is currently due.

    FAILURE HANDLING (Milestone 47 P2 adversarial-review correction,
    MEDIUM-2): if dispatch_confirmation_work() raises BEFORE it could
    consume the pending confirmation (an execution-engine defect, or any
    exception outside its own narrow race-lost set - see that function's
    own EXCEPTION SCOPE docstring), this durably DEFERS the SAME decision
    (TaskRepository.defer_confirmation_decision_retry(), a small, capped-
    exponential backoff reusing compute_outbox_retry_delay_seconds() -
    the identical bounded policy P1's own lifecycle-outbox retry already
    uses, with no confirmation-specific semantics of its own to justify a
    separate copy) so this one failing decision can never permanently
    starve every later durable decision behind it in the due-ordered
    queue above. The deferral is itself conditional on the pending row
    (and its decision) still existing - see
    defer_confirmation_decision_retry()'s own docstring for why a
    dispatch failure AFTER consumption must never fabricate a retry for
    state that is already gone. The original exception is always
    re-raised afterward - this function never silently reports success
    for a genuine defect; the worker's own existing generic recovery-
    error boundary (WhatsAppServer._run_worker()'s
    `except Exception: logger.warning("worker_recovery_error")`) still
    catches it and keeps the worker alive, exactly as it already does for
    every other recovery-checkpoint failure mode."""

    confirmation_id = repository.find_recoverable_confirmation_decision(TASK_SOURCE)
    if confirmation_id is None:
        return
    try:
        dispatch_confirmation_work(
            repository,
            TaskConfirmationWork(confirmation_id),
            registry,
            tools_config_loader,
            respond_provider,
            client,
            authorized_sender,
        )
    except Exception:
        # Re-resolve fresh, by confirmation_id alone - never trust
        # anything already in hand from before the failed dispatch
        # attempt. None here means the pending confirmation (and its
        # decision) is already gone - see
        # defer_confirmation_decision_retry()'s own docstring for why
        # that case is deliberately left alone, never fabricated.
        still_pending_task = repository.get_task_by_pending_confirmation_id(confirmation_id)
        if still_pending_task is not None:
            pending = repository.get_pending_confirmation(still_pending_task.task_id)
            if (
                pending is not None
                and pending.confirmation_id == confirmation_id
                and pending.decision is not None
            ):
                new_attempt_count = pending.decision_attempt_count + 1
                delay_seconds = compute_outbox_retry_delay_seconds(new_attempt_count)
                next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
                repository.defer_confirmation_decision_retry(
                    confirmation_id, new_attempt_count, next_attempt_at
                )
        raise


def dispatch_task_work(
    repository: TaskRepository,
    task_id: str,
    catalog,
    planner_provider,
    registry,
    tools_config_loader,
    respond_provider,
    client,
    authorized_sender: str,
) -> None:
    """Worker-side: the full Milestone 46 P2A CREATED -> planning ->
    execution -> delivery flow for one durable WhatsApp task. This is P2A's
    ONLY execution boundary - it never calls
    approve_task_confirmation()/deny_task_confirmation() (Milestone 46 P2B).

    TRANSITION-TRIGGERED DELIVERY (see this module's own module docstring
    for the full proof): a WhatsApp message is sent only as the direct
    result of an operation THIS call performed - never merely because a
    reloaded task happens to already be in some state:

      - CREATED -> planning: if advance_task_planning() freshly produces
        FAILED, the failure is delivered exactly once, then this call
        returns without attempting execution. If it produces anything
        other than READY or FAILED (should not happen -
        advance_task_planning() only ever produces one of those two from
        CREATED), this call returns without sending anything rather than
        assuming a lifecycle outcome that was never actually reached.
      - READY/RUNNING: current ToolsConfig is reloaded fresh (see this
        module's own module docstring on why - never the startup-time
        planning snapshot) and a fresh SafeTaskExecutor is constructed
        from it, then run_task_until_blocked() drives normal progression;
        _deliver_execution_result() sends whatever that call's own result
        implies, and nothing else.
      - WAITING_FOR_CONFIRMATION or an already-terminal state
        (COMPLETED/FAILED/CANCELLED): a no-op. The confirmation request or
        terminal message for that state was already sent by whichever
        earlier dispatch actually produced it; this call does not touch
        the execution engine again and does not resend anything. (P2B
        will add the CONFIRM/REJECT-driven continuation past
        WAITING_FOR_CONFIRMATION - not this function.)

    Safe to call more than once for the same task_id as defense in depth,
    never relied on as the primary dedup mechanism (that remains
    accept_task_message()'s dedup_key/UNIQUE-constraint boundary).
    TaskNotInCreatedStateError from a genuine race is caught and treated as
    "already advanced" - not a user-facing failure. Every other exception
    (a genuine planning/execution/storage defect, or ToolsConfigError if
    tools.yaml is currently unavailable) propagates unchanged to the
    caller's own generic worker-error boundary - never hidden here, and
    never sent to the user as raw detail.

    `repository` is the caller's own long-lived TaskRepository instance
    (see this module's own module docstring) - never opened or closed
    here."""

    task = repository.get_task(task_id)

    if task.state is TaskState.CREATED:
        try:
            task = advance_task_planning(task, repository, catalog, planner_provider)
        except TaskNotInCreatedStateError:
            return
        if task.state is TaskState.FAILED:
            _send_lifecycle_message(client, authorized_sender, _format_failure_message(task.failure_summary))
            return
        if task.state is not TaskState.READY:
            return

    if task.state not in (TaskState.READY, TaskState.RUNNING):
        # WAITING_FOR_CONFIRMATION or an already-terminal state: whatever
        # lifecycle message this state implies was already sent by the
        # dispatch that actually produced it. Nothing to do here.
        return

    tools_config = tools_config_loader()
    executor = SafeTaskExecutor(tools_config, registry)
    result = run_task_until_blocked(task, repository, registry, tools_config, executor, respond_provider)
    _deliver_execution_result(result, repository, client, authorized_sender)


# Milestone 46 adversarial review, "narrow expected-exception handling":
# every one of these is what a genuine, already-lost race for the SAME
# confirmation_id looks like from approve_task_confirmation()/
# deny_task_confirmation()'s own internals - the pending row or task state
# changed out from under a call that had just, moments earlier in this
# exact worker turn, observed it as valid. Structurally near-unreachable
# given the worker's own single-threaded serialization (see
# dispatch_confirmation_work()'s own docstring), but never assumed away,
# exactly like dispatch_task_work() catches TaskNotInCreatedStateError as
# defense in depth. TaskStorageUnavailableError and
# TaskNotWaitingForConfirmationError are deliberately NOT in this set -
# the former is a genuine storage defect, and the latter would only ever
# fire if this module's own pre-call state check below had a bug - both
# must propagate to the worker's own generic error boundary, never be
# mislabeled as an ordinary invalid confirmation.
_CONFIRMATION_RACE_LOST_EXCEPTIONS = (
    NoPendingConfirmationError,
    ConfirmationMismatchError,
    InvalidTransitionError,
    StepAlreadyClaimedError,
    TaskNotFoundError,
)


def _stale_confirmation_work(category: str) -> None:
    """Milestone 47 P2 adversarial-review correction (MEDIUM-1): the
    silent, no-op fail-safe path for dispatch_confirmation_work() below -
    a bounded, code-owned log category ONLY, never a user-facing reply.
    See that function's own module-level framing for why: a
    TaskConfirmationWork can only ever legitimately exist because
    TaskRepository.record_confirmation_decision() already returned
    RECORDED for it - so if dispatch_confirmation_work() ever reaches here
    anyway, it is because something else (the worker's own immediate
    dispatch, or a later recovery-checkpoint pickup) already resolved this
    exact confirmation_id first. That is internal, redundant work this
    architecture itself creates by design (an intentionally best-effort
    queue handoff racing an independent recovery checkpoint) - never a
    NEW user-entered command, which is handled entirely at HTTP ingress
    (record_confirmation_decision_durably()'s own NOT_ELIGIBLE outcome,
    see server.py's own do_POST). Sending
    "That confirmation is no longer valid." here would be actively
    misleading whenever the real result was already delivered by whichever
    dispatch attempt won this race."""

    logger.warning(category)


def dispatch_confirmation_work(
    repository: TaskRepository,
    work: TaskConfirmationWork,
    registry,
    tools_config_loader,
    respond_provider,
    client,
    authorized_sender: str,
) -> None:
    """Worker-side: the full Milestone 47 P2 reverse-lookup -> source/
    state authorization -> durable-decision reload -> approve-or-deny ->
    (for CONFIRM only) post-approval continuation -> delivery flow for one
    CONFIRM/REJECT command. This is the ONLY confirmation-decision
    execution boundary - never called from the webhook HTTP thread (see
    this module's own module docstring on the connection-lifecycle split).
    interfaces/whatsapp/server.py's do_POST durably records the decision
    itself (TaskRepository.record_confirmation_decision()) BEFORE this
    function is ever reached - by the time `work` arrives here, it carries
    only confirmation_id (Milestone 47 P2 - see TaskConfirmationWork's own
    docstring for why: QUEUE ITEM IS NOT AUTHORITY). This function always
    reloads the durable decision itself, fresh, from TaskRepository -
    never from `work`, which no longer even has a decision field to trust.

    QUEUE ITEM IS INTERNAL, NOT A USER COMMAND (Milestone 47 P2
    adversarial-review correction MEDIUM-1 - load-bearing): a
    TaskConfirmationWork reaching this function is ALWAYS internal work
    created only after record_confirmation_decision() already returned
    RECORDED - never a direct translation of raw, still-unauthorized user
    input (that boundary is entirely at HTTP ingress - see server.py's own
    do_POST NOT_ELIGIBLE handling, which is the ONLY place a genuinely new
    or invalid user command produces GENERIC_INVALID_CONFIRMATION_TEXT).
    Because this function may be reached TWICE for the exact same durable
    decision - once via the worker's own best-effort immediate enqueue,
    once via a LATER recovery-checkpoint pickup, in either order, racing
    each other by design (see run_confirmation_decision_recovery_checkpoint()'s
    own docstring) - every fail-safe branch below is now SILENT
    (_stale_confirmation_work(): a bounded, code-owned log category only,
    no user reply, no mutation, no execution) rather than a user-facing
    "no longer valid" reply: by the time a SECOND dispatch attempt for the
    same confirmation_id reaches any of these checks, the FIRST attempt
    has almost always already delivered the real result, and repeating
    that as an alarming "invalid" message would be actively misleading,
    not merely redundant.

    AUTHORITY ORDER (security-critical - every check below MUST precede
    any mutating call): (1) resolve confirmation_id via
    TaskRepository.get_task_by_pending_confirmation_id() - a bare token
    alone proves nothing; (2) the resolved task must exist; (3) its source
    must be TASK_SOURCE ("whatsapp") - a valid confirmation_id belonging to
    some OTHER source's task must never be actionable from this channel,
    even though today's deployment has exactly one authorized WhatsApp
    sender; (4) its state must still be WAITING_FOR_CONFIRMATION; (5) the
    pending confirmation row's own confirmation_id must still match
    `work.confirmation_id` exactly (defense in depth against an
    exceedingly narrow race: the row was consumed and a brand-new one
    proposed for the same task between get_task_by_pending_confirmation_id()
    and this re-read); (6) its durably-recorded `decision` must be non-NULL
    - see FAIL-SAFE ON MISSING DECISION below for why (6) exists at all.
    Only once all six hold does this function ever call
    approve_task_confirmation()/deny_task_confirmation() - every failure of
    any of these six checks, plus a malformed/oversized token
    (TaskInputTooLargeError from the reverse lookup itself) and the narrow
    race-lost exception set below, are all now silent stale-internal-work
    no-ops (see above), never disclosing anything to the user, exactly as
    a purely-internal correlation failure should.

    FAIL-SAFE ON MISSING DECISION: reaching this function at all (via the
    queue, or via run_confirmation_decision_recovery_checkpoint()) should
    only ever happen once record_confirmation_decision() has already
    returned RECORDED for this exact confirmation_id - so pending.decision
    should always be non-NULL here. Never assumed: if it is unexpectedly
    NULL anyway (a defensive-in-depth guard against a future caller of
    this function that skips the durable-recording step), this function
    fails safe silently - never guesses a decision, never fabricates one,
    never falls back to any other authority.

    EXCEPTION SCOPE (Milestone 46 adversarial review, H1 correction -
    load-bearing, unchanged by the MEDIUM-1 correction above):
    _CONFIRMATION_RACE_LOST_EXCEPTIONS is caught ONLY around the
    approve_task_confirmation()/deny_task_confirmation() call itself -
    never around the post-approval run_task_until_blocked() continuation
    below. Once approve_task_confirmation() has returned successfully, the
    confirmation was genuinely valid, has already been durably consumed,
    and (for a sensitive step) the approved action has already executed -
    "is this confirmation still valid" is no longer a meaningful question
    for anything that happens afterward. run_task_until_blocked() from
    that point on is ordinary autonomous execution, identical in kind to
    dispatch_task_work()'s own call to the same function - which is
    likewise never wrapped in any confirmation-specific exception
    handling. An exception escaping the continuation therefore propagates
    UNCAUGHT to the caller's own generic worker-error boundary, exactly
    like dispatch_task_work()'s does - it must never be silently treated
    as stale internal work either, which would both lie about the
    confirmation's own validity (it WAS valid and DID execute) and hide a
    genuine execution-engine defect from every log/monitor that would
    otherwise see it as worker_error (or, via
    run_confirmation_decision_recovery_checkpoint(), a durably-deferred
    retry).

    CONFIRM: reloads ToolsConfig fresh (see this module's own module
    docstring on why - never a snapshot from whenever the confirmation was
    originally proposed, possibly minutes or hours earlier) and constructs
    a fresh SafeTaskExecutor from it, then calls
    approve_task_confirmation() - the ONLY function this module ever calls
    that may execute a sensitive action; this module never calls
    SafeTaskExecutor.execute() directly. approve_task_confirmation() only
    ever returns STEP_SUCCEEDED (the approved step executed; more of the
    plan may remain) or TASK_FAILED (current-config revalidation failure,
    plan-integrity failure, or genuine confirmation expiry - the latter
    handled entirely inside approve_task_confirmation() itself via
    ConfirmationExpiredError, never surfacing here as an exception) -
    verified directly from kernel/task_execution/service.py's own source,
    never assumed. STEP_SUCCEEDED continues, unconditionally and OUTSIDE
    the race-lost try/except (see above), through the EXISTING
    run_task_until_blocked() - never a second, duplicated execution loop -
    which may itself complete the task, fail it, execute further
    non-sensitive steps, synthesize a RESPOND step, or reach a LATER
    sensitive step and propose a brand-new, distinct confirmation_id
    (unlimited bounded-by-plan confirmation rounds - no "already used
    once" assumption anywhere in this module).

    REJECT: calls deny_task_confirmation() only - no ToolsConfig load, no
    SafeTaskExecutor, no model, matching that function's own signature
    (it needs neither), and no continuation of any kind (deny never
    produces STEP_SUCCEEDED). deny_confirmation() (the repository layer
    beneath it) does NOT check confirmation_id's expires_at at all - a
    plain rejection can never authorize or execute anything, so an exact
    REJECT against a still-pending, still-WAITING_FOR_CONFIRMATION row is
    allowed to cancel the task even past its nominal TTL. This is
    deliberate, not an inconsistency with CONFIRM's own expiry
    enforcement: CONFIRM must fail closed on expiry because it is an
    authorization decision; REJECT never was one.

    DELIVERY: whatever result either path produces - TASK_FAILED,
    TASK_CANCELLED, or (via the run_task_until_blocked() continuation)
    TASK_COMPLETED/CONFIRMATION_REQUIRED - is delivered through the
    EXISTING, unmodified _deliver_execution_result() (P2A's own
    transition-triggered delivery boundary - see that function's and this
    module's own module docstring for the exact proof). No standalone
    "Confirmation accepted" acknowledgement is ever sent for a successful
    CONFIRM - the eventual result (or the next confirmation request) is
    the only message a successful approval ever produces, deliberately
    avoiding both extra lifecycle noise and a second place duplicate
    delivery would need to be proven safe.

    `repository` is the caller's own long-lived TaskRepository instance
    (see this module's own module docstring) - never opened or closed
    here. Every send uses `authorized_sender` only - never derived from
    `task`, the confirmation lookup, work.confirmation_id, or model
    output."""

    try:
        task = repository.get_task_by_pending_confirmation_id(work.confirmation_id)
    except TaskInputTooLargeError:
        _stale_confirmation_work("confirmation_work_malformed_token")
        return

    if task is None or task.source != TASK_SOURCE or task.state is not TaskState.WAITING_FOR_CONFIRMATION:
        _stale_confirmation_work("confirmation_work_stale")
        return

    # Milestone 47 P2: QUEUE ITEM IS NOT AUTHORITY - `work` carries only
    # confirmation_id; the durable decision is always reloaded fresh here,
    # never trusted from any value the caller might otherwise have handed
    # in. get_pending_confirmation() is a plain read, never a mutation.
    pending = repository.get_pending_confirmation(task.task_id)
    if pending is None or pending.confirmation_id != work.confirmation_id:
        _stale_confirmation_work("confirmation_work_stale")
        return
    if pending.decision is None:
        # Structurally shouldn't happen - see this function's own FAIL-SAFE
        # ON MISSING DECISION docstring section above - but never assumed
        # away. No durable mutation, no fabricated decision.
        _stale_confirmation_work("confirmation_decision_missing")
        return

    if pending.decision is ConfirmationDecision.REJECT:
        try:
            result = deny_task_confirmation(task, repository, work.confirmation_id)
        except _CONFIRMATION_RACE_LOST_EXCEPTIONS:
            _stale_confirmation_work("confirmation_work_race_lost")
            return
    else:
        tools_config = tools_config_loader()
        executor = SafeTaskExecutor(tools_config, registry)
        try:
            result = approve_task_confirmation(
                task, repository, registry, tools_config, executor, work.confirmation_id
            )
        except _CONFIRMATION_RACE_LOST_EXCEPTIONS:
            _stale_confirmation_work("confirmation_work_race_lost")
            return

        # Outside the race-lost try/except, deliberately: the confirmation
        # has already been validly consumed by this point (see this
        # function's own EXCEPTION SCOPE docstring section above) - any
        # exception from here on is ordinary execution-engine behavior,
        # never a stale-confirmation condition, and must reach the
        # worker's own generic error boundary uncaught, exactly like
        # dispatch_task_work()'s own unwrapped run_task_until_blocked()
        # call.
        if result.status is ExecutionAdvanceStatus.STEP_SUCCEEDED:
            result = run_task_until_blocked(
                result.task, repository, registry, tools_config, executor, respond_provider
            )

    _deliver_execution_result(result, repository, client, authorized_sender)
