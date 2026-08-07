"""
Typed data, the closed state/transition table, code-only ID generation,
and the error taxonomy for kernel/employee_tasks/ (Milestone 40).

Nothing here performs I/O - these are plain, frozen data and pure
functions shared by db.py and repository.py, matching
kernel/knowledge_base/types.py's own convention. task_id/display_id
generation is code-only and deterministic-or-random exactly as documented
on each function below; the model never generates either.
"""

import uuid
from dataclasses import dataclass
from enum import Enum

SCHEMA_VERSION = 1
PROTOCOL_VERSION = 1

# Fixed, code-level bounds on every externally supplied text field. Not
# configurable - see kernel/employee_tasks/repository.py's validators.
MIN_REQUEST_TEXT_CHARS = 1
MAX_REQUEST_TEXT_CHARS = 4096
MIN_SOURCE_CHARS = 1
MAX_SOURCE_CHARS = 64
MAX_METADATA_JSON_CHARS = 4096
MAX_DEDUP_KEY_CHARS = 128
MAX_FAILURE_CODE_CHARS = 64
MAX_FAILURE_SUMMARY_CHARS = 512
MAX_REASON_CODE_CHARS = 64
MAX_SAFE_SUMMARY_CHARS = 512

DISPLAY_ID_PREFIX = "TASK-"
DISPLAY_ID_LENGTH = 8
# Bounded retry count for the extremely unlikely case that a freshly
# generated display_id (or, vanishingly less likely, task_id) collides
# with an existing row's UNIQUE constraint - see generate_display_id()'s
# docstring and TaskRepository.create_task().
MAX_DISPLAY_ID_ATTEMPTS = 5

MIN_LIST_LIMIT = 1
MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 20

# Crockford-style uppercase alphabet with visually ambiguous characters
# (0, 1, I, L, O) removed - safe to read aloud or copy from a WhatsApp
# message without misreading a character.
_DISPLAY_ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


class TaskState(str, Enum):
    """The closed set of lifecycle states a persisted task may be in.
    Inherits str so a TaskState member compares equal to, and can be
    stored/read as, its plain string value - no separate serialization
    step is needed at the SQLite boundary."""

    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Terminal states never accept a further transition, including back to
# themselves or to another active state - enforced both here (in
# TaskRepository, before any SQL runs) and structurally, since none of
# them appear as a key with a non-empty value in ALLOWED_TRANSITIONS.
TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})

# The exact, closed transition table. Cancellation is allowed from every
# non-terminal state - nothing in the current roadmap needs an
# uncancellable working state. No other states or edges exist.
ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.PLANNING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.PLANNING: frozenset({TaskState.READY, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.WAITING_FOR_CONFIRMATION,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.WAITING_FOR_CONFIRMATION: frozenset(
        {TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class TaskStorageError(Exception):
    """Base class for every error kernel/employee_tasks/ raises
    deliberately (as opposed to letting an unrelated exception escape).
    Never carries a SQL fragment, stack trace, or raw driver exception
    text meant for a caller to inspect via str() - callers select a
    fixed, safe message by exception *type*, matching
    kernel/knowledge_base/types.py's own convention."""


class TaskNotFoundError(TaskStorageError):
    """No task exists with the given task_id or display_id."""


class InvalidTransitionError(TaskStorageError):
    """The requested expected_state -> new_state pair is not in
    ALLOWED_TRANSITIONS, new_state is not a recognized TaskState, or the
    task's actual current state no longer matches the caller's
    expected_state (a stale write, including one lost to a concurrent
    writer). Neither the tasks row nor the journal is ever modified when
    this is raised."""


class TaskAlreadyTerminalError(TaskStorageError):
    """The task's actual current state is already terminal
    (completed/failed/cancelled). Distinct from InvalidTransitionError so
    a caller can tell "you tried to skip/reverse a step" apart from "this
    task is already done and cannot be touched again"."""


class DuplicateTaskError(TaskStorageError):
    """create_task() was called with a dedup_key that already exists."""


class TaskStorageUnavailableError(TaskStorageError):
    """The task database file or its containing directory could not be
    opened, created, initialized, or written to (including a write lock
    that could not be acquired within the busy timeout)."""


class TaskStorageCorruptError(TaskStorageError):
    """PRAGMA integrity_check reported an actual inconsistency in the
    database file - distinct from a schema version mismatch
    (TaskSchemaIncompatibleError), which is a perfectly readable file
    written by, or intended for, a different schema version."""


class TaskSchemaIncompatibleError(TaskStorageError):
    """The database's schema_meta.schema_version does not match the
    version this code expects."""


class TaskInputTooLargeError(TaskStorageError):
    """An externally supplied field violates this layer's bounded-input
    contract: the wrong type, too short, too long, or containing an
    embedded NUL byte - or, for metadata_json specifically, not
    syntactically valid JSON text. Named for the most common case (an
    oversized field) but covers the whole bounded-input contract, so the
    error taxonomy stays closed rather than growing one class per
    validation rule."""


@dataclass(frozen=True)
class TaskRecord:
    """One persisted task's current row. `metadata_json` is stored and
    returned as an opaque, size-capped JSON string - this layer never
    parses its keys or acts on its contents (see repository.py)."""

    task_id: str
    display_id: str
    state: TaskState
    request_text: str
    source: str
    dedup_key: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None
    failure_code: str | None
    failure_summary: str | None
    metadata_json: str
    protocol_version: int
    version: int


@dataclass(frozen=True)
class TaskTransition:
    """One append-only journal row. `from_state` is None only for the
    synthetic creation event (see TaskRepository.create_task) - every
    later transition always has both a from_state and a to_state."""

    transition_id: int
    task_id: str
    from_state: TaskState | None
    to_state: TaskState
    timestamp: str
    reason_code: str | None
    safe_summary: str | None
    task_version: int


def generate_task_id() -> str:
    """Code-generated, time-ordered (UUID7), globally-unique-enough-for-
    one-installation task identity. Never derived from, or influenced by,
    model output - see kernel/employee_tasks/__init__.py."""

    return str(uuid.uuid7())


def generate_display_id(task_id: str) -> str:
    """Deterministic short code derived from task_id's raw bytes - not an
    independent random draw, so there is nothing extra to generate,
    persist, or reconcile against task_id itself.

    Algorithm: interpret the UUID's 128 bits as one big-endian integer,
    then repeatedly divmod by len(_DISPLAY_ID_ALPHABET), taking
    DISPLAY_ID_LENGTH digits least-significant-first and reversing them
    at the end - i.e. this is exactly "encode the integer in base 31
    using this alphabet, keep the last 8 digits". Prefixed with
    DISPLAY_ID_PREFIX ("TASK-") for readability.

    This is a *lossy* projection of 128 bits down to 8 symbols from a
    31-character alphabet (~40 bits) - collisions between two different
    task_ids are possible in principle (roughly 1 in 31**8), which is
    exactly why TaskRepository.create_task() treats a display_id UNIQUE
    violation as a signal to retry with a fresh task_id/display_id pair
    (bounded by MAX_DISPLAY_ID_ATTEMPTS) rather than assuming this
    function is injective. Not a secret, and never used for
    authorization - see kernel/employee_tasks/__init__.py.
    """

    raw = uuid.UUID(task_id).bytes
    remaining = int.from_bytes(raw, "big")
    base = len(_DISPLAY_ID_ALPHABET)
    digits = []
    for _ in range(DISPLAY_ID_LENGTH):
        remaining, remainder = divmod(remaining, base)
        digits.append(_DISPLAY_ID_ALPHABET[remainder])
    return DISPLAY_ID_PREFIX + "".join(reversed(digits))
