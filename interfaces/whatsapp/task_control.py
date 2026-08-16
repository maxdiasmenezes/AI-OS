"""
Milestone 46 P1 - Durable WhatsApp Task Ingress.

Thin glue between the WhatsApp transport (interfaces/whatsapp/server.py,
handler.py) and the existing, previously-unwired durable task engine
(kernel/employee_tasks/, kernel/task_planner/, kernel/task_orchestration/).
This module is deliberately small: it recognizes "/task ..." text, performs
one bounded, durable TaskRepository operation per inbound /task message, and
hands the resulting task_id (never raw text) to the existing planning
orchestration. It is not a second task engine - it never plans, executes,
confirms, or stores anything beyond what TaskRepository already owns.

P1 scope only: durable acceptance (create-or-find, idempotent under
concurrent/duplicate delivery) and the CREATED -> planning handoff. This
module never calls kernel.task_execution (SafeTaskExecutor,
advance_task_execution, run_task_until_blocked, approve/deny_task_confirmation)
and never sends any outbound WhatsApp message - see module docstrings on
accept_task_message()/dispatch_planning() for the exact boundary. Result
delivery, confirmation request/reply delivery, and any action execution are
Milestone 46 P2.

Connection lifecycle (Milestone 46 concurrency validation - see the M46 P1
design report): kernel/employee_tasks/db.py's own module docstring is
explicit that a single sqlite3 connection must never be handed to multiple
threads simultaneously - "sqlite3 does not serialize concurrent calls on
the same connection object for you." Two different calling contexts need
two different connection lifecycles:

  - accept_task_message() is called from a ThreadingHTTPServer request
    thread - a fresh thread per inbound HTTP request, never reused. It
    opens its own connection, does its bounded work, and closes it before
    returning - there is no reuse benefit to holding it open longer, and
    doing so would risk exactly the sharing hazard above if two concurrent
    requests ever touched the same instance.
  - dispatch_planning() is called from the single, long-lived WhatsApp
    worker thread (interfaces/whatsapp/handler.py). It takes an
    already-open, caller-owned TaskRepository - the worker constructs one
    connection/repository once, at startup, and reuses it for the life of
    the process. Since it is always the same one thread, this is safe
    without any lifecycle management here.

Both entry points always resolve dedup/idempotency and dispatch decisions
from a FRESH read of TaskRepository - never from a value computed earlier
in the same request or a prior dispatch - matching kernel/task_execution's
own "always fresh, never a stale caller-held record" discipline.
"""

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass

from kernel.employee_tasks import (
    DuplicateTaskError,
    TaskRecord,
    TaskRepository,
    TaskState,
    TaskStorageError,
)
from kernel.task_orchestration import TaskNotInCreatedStateError, advance_task_planning

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class TaskExecutionWork:
    """Minimal, trusted correlation handed to the WhatsApp worker queue for
    a durable task - task_id only, never raw request text, the provider
    message ID, the sender ID, a planner prompt, an ActionRequest, a plan,
    or a resource_key. TaskRepository remains the sole source of truth:
    the worker always reloads the authoritative TaskRecord fresh by
    task_id (see dispatch_planning()) rather than trusting anything cached
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


def dispatch_planning(repository: TaskRepository, task_id: str, catalog, planner_provider) -> None:
    """Worker-side: reload the TaskRecord fresh by task_id and, if (and
    only if) it is still CREATED, advance it through planning via the
    existing kernel.task_orchestration.advance_task_planning(). This is
    P1's ONLY execution boundary - it never calls
    run_task_until_blocked()/advance_task_execution()/
    approve_task_confirmation()/deny_task_confirmation()/SafeTaskExecutor,
    and it never sends any outbound WhatsApp message (no "Task accepted"
    notice, no result, no confirmation request - all Milestone 46 P2). A
    successfully-planned task may end P1 in TaskState.READY and stay there
    until P2 wiring exists - that is deliberate, not a bug.

    Safe to call more than once for the same task_id as defense in depth
    (never relied on as the primary dedup mechanism - that is
    accept_task_message()'s dedup_key/UNIQUE-constraint boundary): a
    second call reloads state that has already moved past CREATED and
    safely no-ops without planning again, without sending any message,
    and without executing anything. TaskNotInCreatedStateError from a
    genuine race (two dispatches of the same task_id, one already past
    CREATED by the time this one's transition_task() call runs) is caught
    and treated as "already advanced" - not a user-facing failure. Every
    other exception (a genuine planning/storage defect) propagates
    unchanged, never hidden here.

    `repository` is the caller's own long-lived TaskRepository instance
    (see this module's own module docstring) - never opened or closed
    here."""

    task = repository.get_task(task_id)
    if task.state is not TaskState.CREATED:
        return

    try:
        advance_task_planning(task, repository, catalog, planner_provider)
    except TaskNotInCreatedStateError:
        pass
