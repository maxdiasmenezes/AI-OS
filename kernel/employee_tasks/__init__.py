"""
Public interface of kernel/employee_tasks/ - the persistent AI-employee
Task subsystem (Milestone 40).

Deliberately named `employee_tasks`, not `tasks`, because "task" is
already spoken for twice in this codebase for two unrelated things:

- `capabilities/tasks/TasksCapability` (Milestone 33) is the existing,
  synchronous `/task ...` WhatsApp command capability for a small,
  allowlisted set of computer actions (open_application,
  run_registered_script, ...), gated by
  `kernel/tools/confirmation.py`'s single in-memory pending-action slot.
  It is untouched by this milestone.
- `interfaces/whatsapp/handler.py`'s `TextTask`/`FixedReplyTask` are an
  ephemeral, in-process classification of one inbound WhatsApp message
  for the existing worker queue - never persisted, unrelated to either
  of the above.

This package is a third, new concept: a durable, multi-message unit of
AI-employee work with a closed lifecycle (created -> planning -> ready ->
running -> waiting_for_confirmation -> completed, with failed/cancelled
as terminal alternatives), backed by its own SQLite database
(storage/tasks/tasks.sqlite3, schema-versioned independently of
kernel/knowledge_base's database).

Milestone 40 persists task identity and lifecycle state only. Milestone 41
P2 added opaque plan persistence (plan_json). Milestone 42 P1 added
durable, per-step execution progress (task_step_progress, schema version
3) - see TaskStepProgress and TaskRepository's claim_step()/
mark_step_succeeded()/mark_step_failed()/get_step_progress()/
list_step_progress(). Milestone 42 P2 adds durable, task-scoped pending
confirmations (task_pending_confirmation, schema version 4) - see
PendingTaskConfirmation and TaskRepository's propose_confirmation()/
get_pending_confirmation()/consume_confirmation_and_claim_step()/
deny_confirmation()/fail_pending_confirmation()/fail_running_step(). This
package still never plans, executes, calls a model, or calls a tool
itself, and this package itself still has no dependency on
kernel/action_protocol/, kernel/tools/, kernel/models/,
kernel/task_planner/, kernel/task_execution/, capabilities/, or
interfaces/whatsapp/ - that direction is a one-way rule enforced on THIS
package, not on them. The reverse is expected and already true:
kernel/task_planner/ (Milestone 41) and kernel/task_execution/
(Milestone 42) both legitimately import this package's plain, I/O-free
top-level contracts (TaskRecord; and, as of Milestone 42,
TaskStepProgress/StepStatus/PendingTaskConfirmation) - that is exactly
what this package's public interface exists to be depended on for. What
never happens is this package importing anything from them.

`task_pending_confirmation` is wholly independent of
kernel/tools/confirmation.py's in-memory, single-slot ConfirmationStore,
which remains untouched and keeps serving only the existing ad hoc
`/task ...` command path - this package never imports it, and it never
imports this package. `waiting_for_confirmation` as a persisted lifecycle
state predates this durable table (Milestone 40); as of Milestone 42 P2 it
is finally backed by one - see PendingTaskConfirmation's own docstring for
why task_id alone is not enough identity and confirmation_id exists. A
task in waiting_for_confirmation must always be resolved through
consume_confirmation_and_claim_step()/deny_confirmation()/
fail_pending_confirmation(), never through the general transition_task()/
mark_cancelled()/mark_failed() methods, which know nothing about the
pending-confirmation row and would leave it orphaned - see
mark_cancelled()'s own docstring note.

Step progress is deliberately plan-agnostic: `step_position` is treated as
an opaque, positive integer this package never validates against any
particular TaskPlan - kernel/task_execution/ is the layer that knows what
a plan step is and decides which position to claim next.

Callers outside this package must import from here, never from the
individual submodules directly.
"""

from kernel.employee_tasks.db import (
    DATABASE_FILENAME,
    check_integrity,
    open_reader_connection,
    open_writer_connection,
    resolve_database_path,
)
from kernel.employee_tasks.repository import TaskRepository
from kernel.employee_tasks.runtime_lock import (
    RuntimeLock,
    RuntimeOwnershipUnavailableError,
    acquire_runtime_ownership,
)
from kernel.employee_tasks.types import (
    ALLOWED_TRANSITIONS,
    DEFAULT_LIST_LIMIT,
    MAX_CONFIRMATION_ACTION_NAME_CHARS,
    MAX_CONFIRMATION_ID_CHARS,
    MAX_CONFIRMATION_RESOURCE_KEY_CHARS,
    MAX_DEDUP_KEY_CHARS,
    MAX_DISPLAY_ID_ATTEMPTS,
    MAX_FAILURE_CODE_CHARS,
    MAX_FAILURE_SUMMARY_CHARS,
    MAX_LIFECYCLE_CHANNEL_CHARS,
    MAX_LIFECYCLE_PAYLOAD_JSON_CHARS,
    MAX_LIST_LIMIT,
    MAX_METADATA_JSON_CHARS,
    MAX_PLAN_JSON_CHARS,
    MAX_REASON_CODE_CHARS,
    MAX_REQUEST_TEXT_CHARS,
    MAX_SAFE_SUMMARY_CHARS,
    MAX_SOURCE_CHARS,
    MAX_STEP_RESULT_JSON_CHARS,
    MIN_LIST_LIMIT,
    MIN_REQUEST_TEXT_CHARS,
    MIN_SOURCE_CHARS,
    OUTBOX_BASE_DELAY_SECONDS,
    OUTBOX_MAX_DELAY_SECONDS,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    ConfirmationDecision,
    ConfirmationDecisionOutcome,
    ConfirmationExpiredError,
    ConfirmationMismatchError,
    ConfirmationRequiredPayload,
    DuplicateTaskError,
    InvalidTransitionError,
    LifecycleEventKind,
    LifecycleEventPayloadError,
    LifecycleOutboxEvent,
    NoPendingConfirmationError,
    PendingTaskConfirmation,
    StepAlreadyClaimedError,
    StepNotInProgressError,
    StepStatus,
    TaskAlreadyTerminalError,
    TaskInputTooLargeError,
    TaskNotFoundError,
    TaskRecord,
    TaskSchemaIncompatibleError,
    TaskState,
    TaskStepProgress,
    TaskStorageCorruptError,
    TaskStorageError,
    TaskStorageUnavailableError,
    TaskTransition,
    compute_outbox_retry_delay_seconds,
    deserialize_confirmation_required_payload,
    format_utc_timestamp,
    generate_display_id,
    generate_task_id,
    parse_task_timestamp,
    serialize_confirmation_required_payload,
)

__all__ = [
    "TaskRepository",
    "TaskRecord",
    "TaskTransition",
    "TaskState",
    "TERMINAL_STATES",
    "ALLOWED_TRANSITIONS",
    "TaskStepProgress",
    "StepStatus",
    "PendingTaskConfirmation",
    "TaskStorageError",
    "TaskNotFoundError",
    "InvalidTransitionError",
    "TaskAlreadyTerminalError",
    "DuplicateTaskError",
    "StepAlreadyClaimedError",
    "StepNotInProgressError",
    "NoPendingConfirmationError",
    "ConfirmationMismatchError",
    "ConfirmationExpiredError",
    "TaskStorageUnavailableError",
    "TaskStorageCorruptError",
    "TaskSchemaIncompatibleError",
    "TaskInputTooLargeError",
    "generate_task_id",
    "generate_display_id",
    "parse_task_timestamp",
    "SCHEMA_VERSION",
    "MIN_REQUEST_TEXT_CHARS",
    "MAX_REQUEST_TEXT_CHARS",
    "MIN_SOURCE_CHARS",
    "MAX_SOURCE_CHARS",
    "MAX_METADATA_JSON_CHARS",
    "MAX_PLAN_JSON_CHARS",
    "MAX_STEP_RESULT_JSON_CHARS",
    "MAX_CONFIRMATION_ACTION_NAME_CHARS",
    "MAX_CONFIRMATION_RESOURCE_KEY_CHARS",
    "MAX_CONFIRMATION_ID_CHARS",
    "MAX_DEDUP_KEY_CHARS",
    "MAX_FAILURE_CODE_CHARS",
    "MAX_FAILURE_SUMMARY_CHARS",
    "MAX_REASON_CODE_CHARS",
    "MAX_SAFE_SUMMARY_CHARS",
    "MAX_DISPLAY_ID_ATTEMPTS",
    "MIN_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "DEFAULT_LIST_LIMIT",
    "DATABASE_FILENAME",
    "resolve_database_path",
    "open_writer_connection",
    "open_reader_connection",
    "check_integrity",
    "RuntimeLock",
    "RuntimeOwnershipUnavailableError",
    "acquire_runtime_ownership",
    "LifecycleEventKind",
    "LifecycleOutboxEvent",
    "ConfirmationRequiredPayload",
    "LifecycleEventPayloadError",
    "MAX_LIFECYCLE_CHANNEL_CHARS",
    "MAX_LIFECYCLE_PAYLOAD_JSON_CHARS",
    "OUTBOX_BASE_DELAY_SECONDS",
    "OUTBOX_MAX_DELAY_SECONDS",
    "serialize_confirmation_required_payload",
    "deserialize_confirmation_required_payload",
    "compute_outbox_retry_delay_seconds",
    "format_utc_timestamp",
    "ConfirmationDecision",
    "ConfirmationDecisionOutcome",
]
