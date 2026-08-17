"""
Milestone 46 - WhatsApp Task Control glue.

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
sensitive step blocks. This module still never calls
approve_task_confirmation()/deny_task_confirmation() and never resolves a
confirmation_id back to a task - CONFIRM/REJECT ingress, durable
approval/rejection, and post-confirmation continuation are Milestone 46 P2B.

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

  - accept_task_message() is called from a ThreadingHTTPServer request
    thread - a fresh thread per inbound HTTP request, never reused. It
    opens its own connection, does its bounded work, and closes it before
    returning - there is no reuse benefit to holding it open longer, and
    doing so would risk exactly the sharing hazard above if two concurrent
    requests ever touched the same instance.
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

from kernel.employee_tasks import (
    DuplicateTaskError,
    StepStatus,
    TaskRecord,
    TaskRepository,
    TaskState,
    TaskStorageError,
)
from kernel.task_execution import (
    ExecutionAdvanceStatus,
    ObservationDeserializationError,
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
_CONFIRMATION_UNAVAILABLE_TEXT = (
    "That task requires confirmation, but the confirmation details are not "
    "available right now. Please try again."
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


def _deliver_execution_result(
    result, repository: TaskRepository, client, authorized_sender: str
) -> None:
    """Deliver exactly one WhatsApp message as the direct consequence of
    `result` - never from independent state inspection. See this module's
    own module docstring for why ExecutionAdvanceStatus.CONFIRMATION_REQUIRED
    can only ever mean "freshly proposed on this exact call", and why every
    caller of this function has already gated on the task's PRE-CALL state
    so that a terminal status reaching here can only mean this call is what
    produced it."""

    if result.status is ExecutionAdvanceStatus.CONFIRMATION_REQUIRED:
        pending = repository.get_pending_confirmation(result.task.task_id)
        if pending is None:
            # Structurally shouldn't happen - CONFIRMATION_REQUIRED means
            # propose_confirmation() committed a matching row in this same
            # call - but never assumed away. No fabricated token, no
            # execution, no destructive mutation - a bounded generic
            # notice only, and the task's durable state is left exactly as
            # the execution engine already left it.
            logger.warning("task_control_missing_pending_confirmation")
            _send_lifecycle_message(client, authorized_sender, _CONFIRMATION_UNAVAILABLE_TEXT)
            return
        _send_lifecycle_message(client, authorized_sender, _format_confirmation_message(pending))
        return

    if result.status is ExecutionAdvanceStatus.TASK_COMPLETED:
        text = select_terminal_result_text(repository, result.task)
        _send_lifecycle_message(client, authorized_sender, text)
        return

    if result.status is ExecutionAdvanceStatus.TASK_FAILED:
        _send_lifecycle_message(client, authorized_sender, _format_failure_message(result.task.failure_summary))
        return

    if result.status is ExecutionAdvanceStatus.TASK_CANCELLED:
        _send_lifecycle_message(client, authorized_sender, TASK_CANCELLED_TEXT)
        return

    # ExecutionAdvanceStatus.WAITING_FOR_CONFIRMATION (already delivered on
    # a prior advance) or STEP_SUCCEEDED (unreachable from
    # run_task_until_blocked()'s own stopping set) - nothing to deliver.


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
