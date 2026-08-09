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

Milestone 40 persists task identity and lifecycle state only. It never
plans, executes, calls a model, or calls a tool, and has no dependency on
kernel/action_protocol/, kernel/tools/, kernel/models/, capabilities/, or
interfaces/whatsapp/ - none of those may depend on it either, in this
milestone. `waiting_for_confirmation` is only a persisted lifecycle
state; it is not wired to kernel/tools/confirmation.py's pending-action
store. A later milestone connects task -> protocol -> planner -> executor
- not this one.

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
from kernel.employee_tasks.types import (
    ALLOWED_TRANSITIONS,
    DEFAULT_LIST_LIMIT,
    MAX_DEDUP_KEY_CHARS,
    MAX_DISPLAY_ID_ATTEMPTS,
    MAX_FAILURE_CODE_CHARS,
    MAX_FAILURE_SUMMARY_CHARS,
    MAX_LIST_LIMIT,
    MAX_METADATA_JSON_CHARS,
    MAX_PLAN_JSON_CHARS,
    MAX_REASON_CODE_CHARS,
    MAX_REQUEST_TEXT_CHARS,
    MAX_SAFE_SUMMARY_CHARS,
    MAX_SOURCE_CHARS,
    MIN_LIST_LIMIT,
    MIN_REQUEST_TEXT_CHARS,
    MIN_SOURCE_CHARS,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    DuplicateTaskError,
    InvalidTransitionError,
    TaskAlreadyTerminalError,
    TaskInputTooLargeError,
    TaskNotFoundError,
    TaskRecord,
    TaskSchemaIncompatibleError,
    TaskState,
    TaskStorageCorruptError,
    TaskStorageError,
    TaskStorageUnavailableError,
    TaskTransition,
    generate_display_id,
    generate_task_id,
)

__all__ = [
    "TaskRepository",
    "TaskRecord",
    "TaskTransition",
    "TaskState",
    "TERMINAL_STATES",
    "ALLOWED_TRANSITIONS",
    "TaskStorageError",
    "TaskNotFoundError",
    "InvalidTransitionError",
    "TaskAlreadyTerminalError",
    "DuplicateTaskError",
    "TaskStorageUnavailableError",
    "TaskStorageCorruptError",
    "TaskSchemaIncompatibleError",
    "TaskInputTooLargeError",
    "generate_task_id",
    "generate_display_id",
    "SCHEMA_VERSION",
    "MIN_REQUEST_TEXT_CHARS",
    "MAX_REQUEST_TEXT_CHARS",
    "MIN_SOURCE_CHARS",
    "MAX_SOURCE_CHARS",
    "MAX_METADATA_JSON_CHARS",
    "MAX_PLAN_JSON_CHARS",
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
]
