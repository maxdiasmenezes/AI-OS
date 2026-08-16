"""
TaskRepository: the narrow persistence API for kernel/employee_tasks/
(Milestone 40; persist_plan_and_ready() added in Milestone 41 P2;
claim_step()/mark_step_succeeded()/mark_step_failed()/get_step_progress()/
list_step_progress() added in Milestone 42 P1). See
kernel/employee_tasks/__init__.py for what this package is and
deliberately is not.

Every write method (create_task, transition_task, mark_failed,
mark_cancelled, persist_plan_and_ready) wraps its current-row update and
its append-only journal insert in exactly one BEGIN IMMEDIATE ... COMMIT
transaction: there is never a committed state update without its matching
journal entry, or a journal entry without its matching state update. Any
exception rolls the whole transaction back before propagating - see
_apply_transition() (used by transition_task/mark_failed/mark_cancelled)
and persist_plan_and_ready() (which needs its own conditional UPDATE, not
_apply_transition()'s, since it requires the extra plan_json IS NULL
guard - see that method's own docstring).

Every state-changing method requires the caller's expected current state
and performs a SQL-level conditional UPDATE (... WHERE state = ?), so a
stale writer can never silently overwrite a state another writer already
moved on from - exactly one of two callers racing the same transition can
ever succeed; the other observes either TaskAlreadyTerminalError or
InvalidTransitionError, and modifies neither table.

metadata_json, plan_json, and (Milestone 42 P1) result_json are all
treated as opaque, size-capped JSON strings end to end: this module
validates that each parses as JSON and enforces its character limit, but
never inspects its keys, never deserializes it into a value callers can
act on, and never uses it to make an execution or authorization decision.
This module has no dependency on, and never imports, kernel.task_planner
or any TaskPlan/PlanStep type - kernel/task_planner/serialization.py is
the only intended producer of a plan_json string, and a future
kernel/task_execution/ is the only intended producer of a result_json
string, but this layer has no knowledge of either.

Step progress (task_step_progress) follows the same "absence is
meaningful" pattern as the rest of this module. claim_step() combines two
distinct guards inside one BEGIN IMMEDIATE transaction: an explicit
SELECT-then-check of the task's CURRENT state (a step may only be claimed
while the task is RUNNING - TaskAlreadyTerminalError/InvalidTransitionError
otherwise, exactly the same typed conventions transition_task() uses),
followed by a plain INSERT whose (task_id, step_position) PRIMARY KEY is
itself the atomic per-step claim mechanism (of any number of concurrent
callers claiming the SAME step, exactly one INSERT succeeds; every other
one raises StepAlreadyClaimedError) - so unlike every other write method
here, claim_step() does not need its own conditional UPDATE (... WHERE
... = ?) to make the claim itself atomic, only to make the state
re-check atomic with respect to a concurrent writer, which BEGIN IMMEDIATE
alone already guarantees. mark_step_succeeded()/mark_step_failed() DO use
a conditional UPDATE (... WHERE status = 'in_progress'), mirroring
_apply_transition()'s own discipline, to make terminal step status
write-once - see StepNotInProgressError's docstring.

WAITING_FOR_CONFIRMATION carries a stronger persistence invariant than any
other TaskState: a task in that state must always have exactly one
matching task_pending_confirmation row, and a task in any other state must
never have one. ALLOWED_TRANSITIONS correctly lists
RUNNING -> WAITING_FOR_CONFIRMATION and
WAITING_FOR_CONFIRMATION -> {RUNNING, CANCELLED, FAILED} as valid
lifecycle edges - that graph is intentionally unchanged - but
transition_task()/mark_cancelled()/mark_failed() MECHANICALLY REFUSE any
call that would enter or leave WAITING_FOR_CONFIRMATION
(InvalidTransitionError, via the module-level
_reject_generic_waiting_for_confirmation() every one of them calls before
touching the database), because none of them know task_pending_confirmation
exists and none could keep it synchronized with task state. The ONLY
legal repository paths into or out of WAITING_FOR_CONFIRMATION are
propose_confirmation() (in), and consume_confirmation_and_claim_step()/
deny_confirmation()/fail_pending_confirmation() (out) - this is enforced
at the repository API boundary itself, not left as documented caller
discipline.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

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
    TERMINAL_STATES,
    ConfirmationExpiredError,
    ConfirmationMismatchError,
    DuplicateTaskError,
    InvalidTransitionError,
    NoPendingConfirmationError,
    PendingTaskConfirmation,
    StepAlreadyClaimedError,
    StepNotInProgressError,
    StepStatus,
    TaskAlreadyTerminalError,
    TaskInputTooLargeError,
    TaskNotFoundError,
    TaskRecord,
    TaskState,
    TaskStepProgress,
    TaskStorageUnavailableError,
    TaskTransition,
    generate_display_id,
    generate_task_id,
    parse_task_timestamp,
)

_TERMINAL_STATE_VALUES = frozenset(state.value for state in TERMINAL_STATES)

_SELECT_COLUMNS = (
    "task_id, display_id, state, request_text, source, dedup_key, "
    "created_at, updated_at, started_at, completed_at, failure_code, "
    "failure_summary, metadata_json, protocol_version, version, plan_json"
)

_STEP_PROGRESS_SELECT_COLUMNS = (
    "task_id, step_position, status, started_at, completed_at, "
    "result_json, failure_code, failure_summary, task_version"
)

_PENDING_CONFIRMATION_SELECT_COLUMNS = (
    "task_id, confirmation_id, step_position, action_name, resource_key, "
    "created_at, expires_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reject_nul(value: str, field_name: str) -> None:
    if "\x00" in value:
        raise TaskInputTooLargeError(f"{field_name} must not contain a NUL character")


def _validate_bounded_text(value, field_name: str, min_len: int, max_len: int) -> str:
    if not isinstance(value, str):
        raise TaskInputTooLargeError(f"{field_name} must be a string")
    _reject_nul(value, field_name)
    if not (min_len <= len(value) <= max_len):
        raise TaskInputTooLargeError(
            f"{field_name} must be between {min_len} and {max_len} characters"
        )
    return value


def _validate_optional_bounded_text(value, field_name: str, max_len: int) -> str | None:
    if value is None:
        return None
    return _validate_bounded_text(value, field_name, 1, max_len)


def _validate_metadata_json(value: str) -> str:
    if not isinstance(value, str):
        raise TaskInputTooLargeError("metadata_json must be a string")
    _reject_nul(value, "metadata_json")
    if len(value) > MAX_METADATA_JSON_CHARS:
        raise TaskInputTooLargeError(
            f"metadata_json must be at most {MAX_METADATA_JSON_CHARS} characters"
        )
    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        raise TaskInputTooLargeError("metadata_json must be valid JSON text") from exc
    return value


def _validate_plan_json(value: str) -> str:
    if not isinstance(value, str):
        raise TaskInputTooLargeError("plan_json must be a string")
    _reject_nul(value, "plan_json")
    if len(value) > MAX_PLAN_JSON_CHARS:
        raise TaskInputTooLargeError(
            f"plan_json must be at most {MAX_PLAN_JSON_CHARS} characters"
        )
    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        raise TaskInputTooLargeError("plan_json must be valid JSON text") from exc
    return value


def _validate_step_position(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TaskInputTooLargeError("step_position must be a positive integer")
    return value


def _validate_step_result_json(value) -> str:
    if not isinstance(value, str):
        raise TaskInputTooLargeError("result_json must be a string")
    _reject_nul(value, "result_json")
    if len(value) > MAX_STEP_RESULT_JSON_CHARS:
        raise TaskInputTooLargeError(
            f"result_json must be at most {MAX_STEP_RESULT_JSON_CHARS} characters"
        )
    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        raise TaskInputTooLargeError("result_json must be valid JSON text") from exc
    return value


def _validate_optional_step_result_json(value) -> str | None:
    if value is None:
        return None
    return _validate_step_result_json(value)


def _validate_confirmation_action_name(value) -> str:
    return _validate_bounded_text(
        value, "action_name", 1, MAX_CONFIRMATION_ACTION_NAME_CHARS
    )


def _validate_confirmation_resource_key(value) -> str | None:
    if value is None:
        return None
    return _validate_bounded_text(
        value, "resource_key", 1, MAX_CONFIRMATION_RESOURCE_KEY_CHARS
    )


def _validate_ttl_seconds(value) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
    ):
        raise TaskInputTooLargeError("ttl_seconds must be a positive number")
    return float(value)


def _validate_confirmation_id(value) -> str:
    """confirmation_id is code-generated at propose_confirmation() time,
    but consume_confirmation_and_claim_step()/deny_confirmation()/
    fail_pending_confirmation() receive it back from a caller (eventually
    an external one, once a later milestone wires up approval delivery) -
    validated here exactly like every other externally-supplied text field
    in this module (type, non-empty, bounded, NUL-rejected) before it is
    ever compared against a persisted row. Exact opaque-token equality is
    what actually authorizes anything here, not any particular string
    shape - this deliberately does not require confirmation_id to parse
    as a UUID."""

    return _validate_bounded_text(value, "confirmation_id", 1, MAX_CONFIRMATION_ID_CHARS)


def _validate_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not (
        MIN_LIST_LIMIT <= limit <= MAX_LIST_LIMIT
    ):
        raise TaskInputTooLargeError(
            f"limit must be an integer between {MIN_LIST_LIMIT} and {MAX_LIST_LIMIT}"
        )
    return limit


def _coerce_state(value) -> TaskState:
    try:
        return TaskState(value)
    except ValueError as exc:
        raise InvalidTransitionError(f"unknown task state: {value!r}") from exc


def _row_to_record(row) -> TaskRecord:
    return TaskRecord(
        task_id=row[0],
        display_id=row[1],
        state=TaskState(row[2]),
        request_text=row[3],
        source=row[4],
        dedup_key=row[5],
        created_at=row[6],
        updated_at=row[7],
        started_at=row[8],
        completed_at=row[9],
        failure_code=row[10],
        failure_summary=row[11],
        metadata_json=row[12],
        protocol_version=row[13],
        version=row[14],
        plan_json=row[15],
    )


def _row_to_step_progress(row) -> TaskStepProgress:
    return TaskStepProgress(
        task_id=row[0],
        step_position=row[1],
        status=StepStatus(row[2]),
        started_at=row[3],
        completed_at=row[4],
        result_json=row[5],
        failure_code=row[6],
        failure_summary=row[7],
        task_version=row[8],
    )


def _row_to_pending_confirmation(row) -> PendingTaskConfirmation:
    return PendingTaskConfirmation(
        task_id=row[0],
        confirmation_id=row[1],
        step_position=row[2],
        action_name=row[3],
        resource_key=row[4],
        created_at=row[5],
        expires_at=row[6],
    )


def _select_task_state_for_update(conn: sqlite3.Connection, task_id: str) -> tuple[str, int]:
    """Must be called after BEGIN IMMEDIATE, inside the write-locked
    transaction that will act on the result. Returns (state, version) for
    an existing task, or raises TaskNotFoundError. Shared by every method
    (claim_step, propose_confirmation, consume_confirmation_and_claim_step,
    deny_confirmation, fail_pending_confirmation, fail_running_step) that
    needs to check the task's CURRENT state before acting on it - never a
    caller-held, possibly-stale TaskRecord."""

    row = conn.execute("SELECT state, version FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskNotFoundError(task_id)
    return row[0], row[1]


def _require_task_state(actual_state: str, expected: TaskState) -> None:
    """Raises TaskAlreadyTerminalError for a terminal actual_state, or
    InvalidTransitionError for any other mismatch against `expected` -
    the same typed conventions every state-checking method in this class
    already uses (see _apply_transition())."""

    if actual_state in _TERMINAL_STATE_VALUES:
        raise TaskAlreadyTerminalError(actual_state)
    if actual_state != expected.value:
        raise InvalidTransitionError(
            f"expected task to be in {expected.value!r} but it is in {actual_state!r}"
        )


_WAITING_FOR_CONFIRMATION_BYPASS_MESSAGE = (
    "waiting_for_confirmation may only be entered or left through the dedicated "
    "confirmation operations (propose_confirmation(), "
    "consume_confirmation_and_claim_step(), deny_confirmation(), "
    "fail_pending_confirmation()) - never through transition_task(), mark_cancelled(), "
    "or mark_failed(), which know nothing about the durable task_pending_confirmation "
    "row and would desynchronize it from task state"
)


def _reject_generic_waiting_for_confirmation(*states: TaskState) -> None:
    """ALLOWED_TRANSITIONS correctly says RUNNING -> WAITING_FOR_CONFIRMATION
    and WAITING_FOR_CONFIRMATION -> {RUNNING, CANCELLED, FAILED} are valid
    lifecycle edges - that graph is not wrong and is deliberately left
    unchanged (see ALLOWED_TRANSITIONS' own docstring). What is wrong is
    reaching those edges through a GENERIC method that only ever touches
    `tasks`/`task_transitions`: WAITING_FOR_CONFIRMATION has a stronger
    persistence invariant than any other state - it must always have
    exactly one matching task_pending_confirmation row, and every other
    state must never have one. Only propose_confirmation() (creates the
    row atomically with the RUNNING -> WAITING_FOR_CONFIRMATION transition)
    and consume_confirmation_and_claim_step()/deny_confirmation()/
    fail_pending_confirmation() (each atomically consumes the row as part
    of their own transition) can uphold that invariant; a generic
    transition_task()/mark_cancelled()/mark_failed() call cannot, because
    none of them know task_pending_confirmation exists. This is therefore
    enforced here, at the repository API boundary, for every state-changing
    method that does not itself manage that table - never left as caller
    discipline or a docstring-only warning."""

    if TaskState.WAITING_FOR_CONFIRMATION in states:
        raise InvalidTransitionError(_WAITING_FOR_CONFIRMATION_BYPASS_MESSAGE)


class TaskRepository:
    """Narrow persistence API over one already-open
    kernel/employee_tasks SQLite connection (see db.py's
    open_writer_connection/open_reader_connection). Performs no
    execution, planning, model, or tool call of any kind."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- creation --------------------------------------------------------

    def create_task(
        self,
        request_text: str,
        source: str,
        *,
        metadata_json: str = "{}",
        dedup_key: str | None = None,
        protocol_version: int = 1,
    ) -> TaskRecord:
        request_text = _validate_bounded_text(
            request_text, "request_text", MIN_REQUEST_TEXT_CHARS, MAX_REQUEST_TEXT_CHARS
        )
        source = _validate_bounded_text(source, "source", MIN_SOURCE_CHARS, MAX_SOURCE_CHARS)
        metadata_json = _validate_metadata_json(metadata_json)
        dedup_key = _validate_optional_bounded_text(dedup_key, "dedup_key", MAX_DEDUP_KEY_CHARS)
        if (
            not isinstance(protocol_version, int)
            or isinstance(protocol_version, bool)
            or protocol_version < 1
        ):
            raise TaskInputTooLargeError("protocol_version must be a positive integer")

        conn = self._conn
        last_exc: Exception | None = None

        for _ in range(MAX_DISPLAY_ID_ATTEMPTS):
            task_id = generate_task_id()
            display_id = generate_display_id(task_id)
            now = _now()

            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO tasks ("
                    "task_id, display_id, state, request_text, source, dedup_key, "
                    "created_at, updated_at, metadata_json, protocol_version, version"
                    ") VALUES (?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        task_id,
                        display_id,
                        request_text,
                        source,
                        dedup_key,
                        now,
                        now,
                        metadata_json,
                        protocol_version,
                    ),
                )
                # Synthetic NULL -> created transition: creation itself is
                # recorded as one journal row with from_state NULL, rather
                # than leaving the journal silent about how the task came
                # to exist - see kernel/employee_tasks/__init__.py.
                conn.execute(
                    "INSERT INTO task_transitions ("
                    "task_id, from_state, to_state, timestamp, reason_code, "
                    "safe_summary, task_version"
                    ") VALUES (?, NULL, 'created', ?, 'task_created', NULL, 1)",
                    (task_id, now),
                )
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                message = str(exc)
                if "dedup_key" in message:
                    raise DuplicateTaskError(
                        "a task with this dedup_key already exists"
                    ) from exc
                if "display_id" in message or "tasks.task_id" in message:
                    # Extremely unlikely display_id (or task_id) collision -
                    # retry with a freshly generated pair rather than
                    # overwriting the existing row.
                    last_exc = exc
                    continue
                raise TaskStorageUnavailableError("task database rejected the write") from exc
            except sqlite3.OperationalError as exc:
                conn.execute("ROLLBACK")
                raise TaskStorageUnavailableError(
                    "task database is locked or unavailable"
                ) from exc
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
                return self.get_task(task_id)

        raise TaskStorageUnavailableError(
            "could not allocate a unique display_id after retrying"
        ) from last_exc

    # -- lookup / listing --------------------------------------------------

    def get_task(self, task_id: str) -> TaskRecord:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return _row_to_record(row)

    def get_task_by_display_id(self, display_id: str) -> TaskRecord:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM tasks WHERE display_id = ?", (display_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(display_id)
        return _row_to_record(row)

    def get_task_by_dedup_key(self, dedup_key: str) -> TaskRecord | None:
        """Exact-equality lookup by dedup_key, using the column's existing
        UNIQUE index (see kernel/employee_tasks/db.py's tasks table DDL) -
        no table scan, no new index. Unlike get_task()/get_task_by_display_id(),
        returns None rather than raising when nothing matches: this is a
        plain existence check for the Milestone 46 WhatsApp durable-ingress
        caller (create_task() -> DuplicateTaskError -> this lookup to find
        the row that already won the race), not an identity assertion about
        a caller-known task_id/display_id. Applies the same bounded-input
        validation create_task() itself applies to dedup_key - never a
        source-filtered query, since dedup_key is already namespaced by its
        own caller (e.g. "whatsapp:<digest>") and the column's UNIQUE
        constraint is global, not composite with source."""

        dedup_key = _validate_bounded_text(dedup_key, "dedup_key", 1, MAX_DEDUP_KEY_CHARS)
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM tasks WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def list_tasks(self, limit: int = DEFAULT_LIST_LIMIT) -> list[TaskRecord]:
        limit = _validate_limit(limit)
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM tasks ORDER BY created_at DESC, task_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_transitions(self, task_id: str) -> list[TaskTransition]:
        """Read-only view of one task's append-only journal, oldest
        first. Not part of the milestone's required API surface, but the
        smallest addition that lets a caller (or a test) verify the
        journal without reaching into SQL directly."""

        rows = self._conn.execute(
            "SELECT transition_id, task_id, from_state, to_state, timestamp, "
            "reason_code, safe_summary, task_version FROM task_transitions "
            "WHERE task_id = ? ORDER BY transition_id",
            (task_id,),
        ).fetchall()
        return [
            TaskTransition(
                transition_id=row[0],
                task_id=row[1],
                from_state=TaskState(row[2]) if row[2] is not None else None,
                to_state=TaskState(row[3]),
                timestamp=row[4],
                reason_code=row[5],
                safe_summary=row[6],
                task_version=row[7],
            )
            for row in rows
        ]

    # -- transitions -------------------------------------------------------

    def transition_task(
        self,
        task_id: str,
        expected_state,
        new_state,
        *,
        reason_code: str | None = None,
        safe_summary: str | None = None,
    ) -> TaskRecord:
        """Generic state transition for any edge in ALLOWED_TRANSITIONS
        EXCEPT one that enters or leaves waiting_for_confirmation - both
        directions are mechanically refused (InvalidTransitionError, via
        _reject_generic_waiting_for_confirmation() - see that function's
        own docstring for why): RUNNING -> WAITING_FOR_CONFIRMATION must
        go through propose_confirmation() (which creates the matching
        pending row atomically), and WAITING_FOR_CONFIRMATION -> anything
        must go through consume_confirmation_and_claim_step()/
        deny_confirmation()/fail_pending_confirmation() (each of which
        atomically consumes that row). This method has no knowledge of
        task_pending_confirmation and cannot keep it in sync, so it is
        never trusted with either edge."""

        expected = _coerce_state(expected_state)
        target = _coerce_state(new_state)
        reason_code = _validate_optional_bounded_text(
            reason_code, "reason_code", MAX_REASON_CODE_CHARS
        )
        safe_summary = _validate_optional_bounded_text(
            safe_summary, "safe_summary", MAX_SAFE_SUMMARY_CHARS
        )

        _reject_generic_waiting_for_confirmation(expected, target)

        if expected in TERMINAL_STATES:
            raise TaskAlreadyTerminalError(expected.value)
        if target not in ALLOWED_TRANSITIONS[expected]:
            raise InvalidTransitionError(
                f"{expected.value} -> {target.value} is not an allowed transition"
            )

        return self._apply_transition(
            task_id, expected, target, reason_code=reason_code, safe_summary=safe_summary
        )

    def mark_failed(
        self,
        task_id: str,
        expected_state,
        failure_code: str,
        failure_summary: str,
    ) -> TaskRecord:
        """Generic failure transition for any non-terminal state EXCEPT
        waiting_for_confirmation, which this method mechanically refuses
        (InvalidTransitionError, via _reject_generic_waiting_for_confirmation()
        - see that function's own docstring): a task in
        waiting_for_confirmation must be failed through
        fail_pending_confirmation() instead, which atomically consumes the
        pending task_pending_confirmation row as part of the same
        transaction - this general method cannot uphold that invariant."""

        expected = _coerce_state(expected_state)
        failure_code = _validate_bounded_text(
            failure_code, "failure_code", 1, MAX_FAILURE_CODE_CHARS
        )
        failure_summary = _validate_bounded_text(
            failure_summary, "failure_summary", 1, MAX_FAILURE_SUMMARY_CHARS
        )

        _reject_generic_waiting_for_confirmation(expected)

        if expected in TERMINAL_STATES:
            raise TaskAlreadyTerminalError(expected.value)
        if TaskState.FAILED not in ALLOWED_TRANSITIONS[expected]:
            raise InvalidTransitionError(
                f"{expected.value} -> failed is not an allowed transition"
            )

        return self._apply_transition(
            task_id,
            expected,
            TaskState.FAILED,
            reason_code=failure_code,
            safe_summary=failure_summary,
            failure_code=failure_code,
            failure_summary=failure_summary,
        )

    def mark_cancelled(
        self,
        task_id: str,
        expected_state,
        *,
        reason_code: str = "user_cancelled",
        safe_summary: str | None = None,
    ) -> TaskRecord:
        """General-purpose cancellation for any non-terminal state (see
        ALLOWED_TRANSITIONS) EXCEPT waiting_for_confirmation, which this
        method mechanically refuses (InvalidTransitionError, via
        _reject_generic_waiting_for_confirmation() - see that function's
        own docstring): a task in waiting_for_confirmation must be
        cancelled through deny_confirmation() instead, which atomically
        consumes the pending task_pending_confirmation row as part of the
        same transaction - this general method only ever touches
        `tasks`/`task_transitions` and cannot uphold that invariant. This
        is enforced here, not merely documented as caller discipline."""

        expected = _coerce_state(expected_state)
        reason_code = _validate_bounded_text(reason_code, "reason_code", 1, MAX_REASON_CODE_CHARS)
        safe_summary = _validate_optional_bounded_text(
            safe_summary, "safe_summary", MAX_SAFE_SUMMARY_CHARS
        )

        _reject_generic_waiting_for_confirmation(expected)

        if expected in TERMINAL_STATES:
            raise TaskAlreadyTerminalError(expected.value)
        if TaskState.CANCELLED not in ALLOWED_TRANSITIONS[expected]:
            raise InvalidTransitionError(
                f"{expected.value} -> cancelled is not an allowed transition"
            )

        return self._apply_transition(
            task_id, expected, TaskState.CANCELLED, reason_code=reason_code, safe_summary=safe_summary
        )

    def persist_plan_and_ready(
        self,
        task_id: str,
        expected_state,
        plan_json: str,
        *,
        reason_code: str | None = None,
        safe_summary: str | None = None,
    ) -> TaskRecord:
        """Atomically persist a validated plan and transition
        expected_state -> ready, in one transaction with the journal
        entry - matching every other transition method's guarantee that a
        state change and its journal row always commit together.

        `plan_json` is treated exactly as opaquely as metadata_json
        (kernel/task_planner/serialization.py:serialize_plan() is the only
        intended producer of this string; this layer only validates it for
        size/well-formedness, never for plan-specific shape). A plan may be
        persisted exactly once: the underlying UPDATE requires BOTH
        `state = <expected_state>` AND `plan_json IS NULL`, so a second
        call for the same task - even one that still correctly names
        `expected_state` - fails closed rather than overwriting the first
        plan. There is no corresponding "unset" or "replace" API; this is
        deliberately not a general plan-update operation."""

        expected = _coerce_state(expected_state)
        plan_json = _validate_plan_json(plan_json)
        reason_code = _validate_optional_bounded_text(
            reason_code, "reason_code", MAX_REASON_CODE_CHARS
        )
        safe_summary = _validate_optional_bounded_text(
            safe_summary, "safe_summary", MAX_SAFE_SUMMARY_CHARS
        )

        if expected in TERMINAL_STATES:
            raise TaskAlreadyTerminalError(expected.value)
        if TaskState.READY not in ALLOWED_TRANSITIONS[expected]:
            raise InvalidTransitionError(
                f"{expected.value} -> ready is not an allowed transition"
            )

        conn = self._conn
        now = _now()

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT state, plan_json, version FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(task_id)

            actual_state, existing_plan_json, current_version = row
            if actual_state != expected.value:
                if actual_state in _TERMINAL_STATE_VALUES:
                    raise TaskAlreadyTerminalError(actual_state)
                raise InvalidTransitionError(
                    f"expected task to be in {expected.value!r} but it is in {actual_state!r}"
                )
            if existing_plan_json is not None:
                raise InvalidTransitionError(
                    f"task {task_id} already has a persisted plan"
                )

            cursor = conn.execute(
                "UPDATE tasks SET plan_json = ?, state = ?, updated_at = ?, "
                "version = version + 1 "
                "WHERE task_id = ? AND state = ? AND plan_json IS NULL",
                (plan_json, TaskState.READY.value, now, task_id, expected.value),
            )
            if cursor.rowcount == 0:
                # Lost a race to a concurrent writer between our SELECT and
                # our UPDATE - re-inspect to report the precise conflict,
                # exactly like _apply_transition()'s own race handling.
                row2 = conn.execute(
                    "SELECT state, plan_json FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row2 is None:
                    raise TaskNotFoundError(task_id)
                actual_state2, existing_plan_json2 = row2
                if actual_state2 in _TERMINAL_STATE_VALUES:
                    raise TaskAlreadyTerminalError(actual_state2)
                if actual_state2 != expected.value:
                    raise InvalidTransitionError(
                        f"expected task to be in {expected.value!r} but it is in "
                        f"{actual_state2!r}"
                    )
                raise InvalidTransitionError(
                    f"task {task_id} already has a persisted plan"
                )

            new_version = current_version + 1
            conn.execute(
                "INSERT INTO task_transitions ("
                "task_id, from_state, to_state, timestamp, reason_code, "
                "safe_summary, task_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    expected.value,
                    TaskState.READY.value,
                    now,
                    reason_code,
                    safe_summary,
                    new_version,
                ),
            )
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            raise TaskStorageUnavailableError("task database is locked or unavailable") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

        return self.get_task(task_id)

    # -- step progress (Milestone 42 P1) -----------------------------------

    def claim_step(self, task_id: str, step_position: int) -> TaskStepProgress:
        """Atomically claim one plan step for execution: absence of a
        task_step_progress row means not_started (see StepStatus's own
        docstring), so claiming is a plain INSERT of an `in_progress` row.
        The table's (task_id, step_position) PRIMARY KEY is what makes a
        claim on one already-claimed step atomic - of any number of
        concurrent callers claiming the SAME step, SQLite guarantees
        exactly one INSERT succeeds; every other one raises
        StepAlreadyClaimedError and must not retry blindly (see
        kernel/task_execution/'s fail-closed doctrine, not implemented in
        this milestone).

        That alone is not sufficient execution authorization: a step may
        only ever be claimed while the task's CURRENT persisted state is
        TaskState.RUNNING - never from an earlier, possibly-stale
        in-memory TaskRecord a caller happens to be holding. This method
        re-reads `tasks.state` itself, inside the same BEGIN IMMEDIATE
        transaction as the INSERT, so the read and the claim are atomic
        with respect to every other writer (see the module docstring's
        "two distinct guards inside one BEGIN IMMEDIATE transaction" note
        for why claim_step specifically still needs this extra read
        despite not using a conditional UPDATE): a worker that observed
        RUNNING before another
        connection committed RUNNING -> CANCELLED can never go on to claim
        a step for that task - the state re-read inside this same
        transaction always sees the committed CANCELLED row, never the
        stale in-memory value. A non-RUNNING state is reported using the
        same typed conventions every other method in this class already
        uses: TaskAlreadyTerminalError for a terminal state
        (completed/failed/cancelled), InvalidTransitionError for any other
        non-RUNNING state (created/planning/ready/waiting_for_confirmation).
        No new error type exists solely for this check.

        A successful claim is the authorization boundary for this one
        step for the remainder of Milestone 42 P1: what CANCELLED means
        for a step already claimed before the cancellation - and whether/
        how execution of an in-flight claimed step is affected - is
        explicitly deferred to Milestone 42 P2's design, not decided or
        implemented here.

        `step_position` is treated as an opaque, positive integer this
        layer never validates against any particular TaskPlan - the caller
        (kernel/task_execution/, in a later milestone) is responsible for
        only ever claiming a position it already knows belongs to the
        task's persisted plan.
        """

        step_position = _validate_step_position(step_position)
        conn = self._conn
        now = _now()

        conn.execute("BEGIN IMMEDIATE")
        try:
            actual_state, task_version = _select_task_state_for_update(conn, task_id)
            _require_task_state(actual_state, TaskState.RUNNING)

            conn.execute(
                "INSERT INTO task_step_progress "
                "(task_id, step_position, status, started_at, task_version) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, step_position, StepStatus.IN_PROGRESS.value, now, task_version),
            )
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise StepAlreadyClaimedError(
                f"step {step_position} of task {task_id} has already been claimed"
            ) from exc
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            raise TaskStorageUnavailableError("task database is locked or unavailable") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

        return self.get_step_progress(task_id, step_position)

    def mark_step_succeeded(
        self, task_id: str, step_position: int, result_json: str
    ) -> TaskStepProgress:
        """in_progress -> succeeded, exactly once. `result_json` is a
        required, bounded, opaque observation - see TaskStepProgress's own
        docstring; this method never inspects its content."""

        step_position = _validate_step_position(step_position)
        result_json = _validate_step_result_json(result_json)
        return self._finalize_step(
            task_id,
            step_position,
            StepStatus.SUCCEEDED,
            result_json=result_json,
            failure_code=None,
            failure_summary=None,
        )

    def mark_step_failed(
        self,
        task_id: str,
        step_position: int,
        failure_code: str,
        failure_summary: str,
        *,
        result_json: str | None = None,
    ) -> TaskStepProgress:
        """in_progress -> failed, exactly once. `result_json` is optional
        here (unlike mark_step_succeeded) - a failure may have nothing
        beyond its failure_code/failure_summary to record."""

        step_position = _validate_step_position(step_position)
        failure_code = _validate_bounded_text(
            failure_code, "failure_code", 1, MAX_FAILURE_CODE_CHARS
        )
        failure_summary = _validate_bounded_text(
            failure_summary, "failure_summary", 1, MAX_FAILURE_SUMMARY_CHARS
        )
        result_json = _validate_optional_step_result_json(result_json)
        return self._finalize_step(
            task_id,
            step_position,
            StepStatus.FAILED,
            result_json=result_json,
            failure_code=failure_code,
            failure_summary=failure_summary,
        )

    def fail_running_step(
        self,
        task_id: str,
        step_position: int,
        failure_code: str,
        failure_summary: str,
        *,
        result_json: str | None = None,
    ) -> TaskRecord:
        """Atomically fail one in_progress step AND the RUNNING task that
        claimed it, in a single transaction (Milestone 42 P2) - so a
        step's failure and its task's failure can never drift apart (a
        step recorded as failed while its task is still RUNNING, or vice
        versa). Requires the task to still be RUNNING: if a concurrent
        writer already moved it to a terminal state (e.g. CANCELLED) since
        the step was claimed, this raises TaskAlreadyTerminalError WITHOUT
        touching task_step_progress at all - the already-authorized
        external action's outcome still needs to be recorded, but that is
        the caller's job via a separate mark_step_failed() call in that
        specific fallback case (see kernel/task_execution/service.py's own
        handling: an already-claimed, already-executed action's outcome
        must never be silently discarded just because the task moved on,
        but the task itself must never be overwritten out of a state
        another writer already committed)."""

        step_position = _validate_step_position(step_position)
        failure_code = _validate_bounded_text(
            failure_code, "failure_code", 1, MAX_FAILURE_CODE_CHARS
        )
        failure_summary = _validate_bounded_text(
            failure_summary, "failure_summary", 1, MAX_FAILURE_SUMMARY_CHARS
        )
        result_json = _validate_optional_step_result_json(result_json)

        conn = self._conn
        now = _now()

        conn.execute("BEGIN IMMEDIATE")
        try:
            actual_state, current_version = _select_task_state_for_update(conn, task_id)
            _require_task_state(actual_state, TaskState.RUNNING)

            step_cursor = conn.execute(
                "UPDATE task_step_progress SET status = ?, completed_at = ?, "
                "result_json = ?, failure_code = ?, failure_summary = ? "
                "WHERE task_id = ? AND step_position = ? AND status = ?",
                (
                    StepStatus.FAILED.value,
                    now,
                    result_json,
                    failure_code,
                    failure_summary,
                    task_id,
                    step_position,
                    StepStatus.IN_PROGRESS.value,
                ),
            )
            if step_cursor.rowcount == 0:
                raise StepNotInProgressError(
                    f"step {step_position} of task {task_id} is not currently in_progress"
                )

            task_cursor = conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ?, completed_at = ?, "
                "failure_code = ?, failure_summary = ?, version = version + 1 "
                "WHERE task_id = ? AND state = ?",
                (
                    TaskState.FAILED.value,
                    now,
                    now,
                    failure_code,
                    failure_summary,
                    task_id,
                    TaskState.RUNNING.value,
                ),
            )
            if task_cursor.rowcount == 0:
                # Lost a race to a concurrent writer between our SELECT and
                # this UPDATE - re-inspect to report the precise conflict,
                # exactly like _apply_transition()'s own race handling.
                row2 = conn.execute(
                    "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row2 is None:
                    raise TaskNotFoundError(task_id)
                _require_task_state(row2[0], TaskState.RUNNING)
                raise InvalidTransitionError(
                    f"task {task_id} is no longer running"
                )

            new_version = current_version + 1
            conn.execute(
                "INSERT INTO task_transitions ("
                "task_id, from_state, to_state, timestamp, reason_code, "
                "safe_summary, task_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    TaskState.RUNNING.value,
                    TaskState.FAILED.value,
                    now,
                    failure_code,
                    failure_summary,
                    new_version,
                ),
            )
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            raise TaskStorageUnavailableError("task database is locked or unavailable") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

        return self.get_task(task_id)

    def get_step_progress(self, task_id: str, step_position: int) -> TaskStepProgress | None:
        """None means no row exists for this (task_id, step_position) -
        i.e. not_started. Never raises TaskNotFoundError for an unknown
        task_id either; this is a plain lookup, not a task-identity check."""

        row = self._conn.execute(
            f"SELECT {_STEP_PROGRESS_SELECT_COLUMNS} FROM task_step_progress "
            "WHERE task_id = ? AND step_position = ?",
            (task_id, step_position),
        ).fetchone()
        if row is None:
            return None
        return _row_to_step_progress(row)

    def list_step_progress(self, task_id: str) -> list[TaskStepProgress]:
        """Every claimed step for one task, ordered by step_position. A
        step with no row here has never been claimed (not_started) - this
        method has no way to, and does not attempt to, represent that
        absence as an entry of its own."""

        rows = self._conn.execute(
            f"SELECT {_STEP_PROGRESS_SELECT_COLUMNS} FROM task_step_progress "
            "WHERE task_id = ? ORDER BY step_position",
            (task_id,),
        ).fetchall()
        return [_row_to_step_progress(row) for row in rows]

    # -- durable confirmation (Milestone 42 P2) -----------------------------

    def propose_confirmation(
        self,
        task_id: str,
        step_position: int,
        action_name: str,
        resource_key: str | None,
        ttl_seconds: float,
        *,
        reason_code: str | None = None,
        safe_summary: str | None = None,
    ) -> TaskRecord:
        """Atomically propose a durable, task-scoped confirmation for
        exactly one sensitive ACTION step, and transition
        RUNNING -> WAITING_FOR_CONFIRMATION, in one transaction. Requires
        the task to be RUNNING and step_position to never have been
        claimed (no task_step_progress row for it yet) - both checked
        inside this same transaction, never trusted from a caller-held
        TaskRecord/EligibleStep.

        `task_id` is task_pending_confirmation's PRIMARY KEY: at most one
        pending confirmation can ever exist per task (see
        PendingTaskConfirmation's own docstring for why). Of any number of
        concurrent callers proposing for the same RUNNING task, only one
        can win the RUNNING -> WAITING_FOR_CONFIRMATION conditional
        UPDATE below; every other one observes the task is no longer
        RUNNING and fails closed with the same typed conventions every
        other transition method in this class uses.

        `confirmation_id` is freshly code-generated here by reusing
        generate_task_id()'s generic UUID7 generator (see that function's
        own docstring - it is not task-specific despite its name) - never
        supplied by a caller, never derived from request text or model
        output. `ttl_seconds` is caller-supplied: kernel/task_execution/
        owns this policy value (e.g. TASK_CONFIRMATION_TTL_SECONDS) so
        this layer stays policy-free, exactly like every other opaque
        value it accepts - it only computes
        `expires_at = now + ttl_seconds` from its own single, consistent
        `now` instant."""

        step_position = _validate_step_position(step_position)
        action_name = _validate_confirmation_action_name(action_name)
        resource_key = _validate_confirmation_resource_key(resource_key)
        ttl_seconds = _validate_ttl_seconds(ttl_seconds)
        reason_code = _validate_optional_bounded_text(
            reason_code, "reason_code", MAX_REASON_CODE_CHARS
        )
        safe_summary = _validate_optional_bounded_text(
            safe_summary, "safe_summary", MAX_SAFE_SUMMARY_CHARS
        )

        conn = self._conn
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
        confirmation_id = generate_task_id()

        conn.execute("BEGIN IMMEDIATE")
        try:
            actual_state, current_version = _select_task_state_for_update(conn, task_id)
            _require_task_state(actual_state, TaskState.RUNNING)

            existing_step = conn.execute(
                "SELECT 1 FROM task_step_progress WHERE task_id = ? AND step_position = ?",
                (task_id, step_position),
            ).fetchone()
            if existing_step is not None:
                raise StepAlreadyClaimedError(
                    f"step {step_position} of task {task_id} has already been claimed"
                )

            task_cursor = conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ?, version = version + 1 "
                "WHERE task_id = ? AND state = ?",
                (
                    TaskState.WAITING_FOR_CONFIRMATION.value,
                    now,
                    task_id,
                    TaskState.RUNNING.value,
                ),
            )
            if task_cursor.rowcount == 0:
                row2 = conn.execute(
                    "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row2 is None:
                    raise TaskNotFoundError(task_id)
                _require_task_state(row2[0], TaskState.RUNNING)
                raise InvalidTransitionError(f"task {task_id} is no longer running")

            conn.execute(
                "INSERT INTO task_pending_confirmation "
                "(task_id, confirmation_id, step_position, action_name, resource_key, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    confirmation_id,
                    step_position,
                    action_name,
                    resource_key,
                    now,
                    expires_at,
                ),
            )

            new_version = current_version + 1
            conn.execute(
                "INSERT INTO task_transitions ("
                "task_id, from_state, to_state, timestamp, reason_code, "
                "safe_summary, task_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    TaskState.RUNNING.value,
                    TaskState.WAITING_FOR_CONFIRMATION.value,
                    now,
                    reason_code,
                    safe_summary,
                    new_version,
                ),
            )
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            raise TaskStorageUnavailableError("task database is locked or unavailable") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

        return self.get_task(task_id)

    def get_pending_confirmation(self, task_id: str) -> PendingTaskConfirmation | None:
        """None means no pending confirmation exists for this task - a
        plain lookup, not a task-identity check (never raises
        TaskNotFoundError for an unknown task_id)."""

        row = self._conn.execute(
            f"SELECT {_PENDING_CONFIRMATION_SELECT_COLUMNS} FROM task_pending_confirmation "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_pending_confirmation(row)

    def consume_confirmation_and_claim_step(
        self,
        task_id: str,
        confirmation_id: str,
        step_position: int,
        action_name: str,
        resource_key: str | None,
        *,
        reason_code: str | None = None,
        safe_summary: str | None = None,
    ) -> TaskRecord:
        """The approval path's atomic consume+claim - the replay-prevention
        boundary. Requires the task to be WAITING_FOR_CONFIRMATION and the
        pending confirmation to match EXACTLY (confirmation_id,
        step_position, action_name, resource_key) and be unexpired.
        Atomically: delete the pending confirmation, transition
        WAITING_FOR_CONFIRMATION -> RUNNING, and claim step_position as
        in_progress - all in one transaction, all before any external
        execution may occur.

        A duplicate/second approval attempt (even with the exact same
        confirmation_id) can never claim or execute the step twice: after
        the first successful call, the pending row is gone and the task is
        no longer WAITING_FOR_CONFIRMATION, so a second call fails closed
        with NoPendingConfirmationError or InvalidTransitionError before
        it ever reaches the claim step - see this method's own race
        handling below, identical in shape to every other conditional
        transition in this class."""

        confirmation_id = _validate_confirmation_id(confirmation_id)
        step_position = _validate_step_position(step_position)
        conn = self._conn
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()

        conn.execute("BEGIN IMMEDIATE")
        try:
            actual_state, current_version = _select_task_state_for_update(conn, task_id)
            _require_task_state(actual_state, TaskState.WAITING_FOR_CONFIRMATION)

            pending_row = conn.execute(
                f"SELECT {_PENDING_CONFIRMATION_SELECT_COLUMNS} FROM task_pending_confirmation "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if pending_row is None:
                raise NoPendingConfirmationError(task_id)
            pending = _row_to_pending_confirmation(pending_row)

            if (
                pending.confirmation_id != confirmation_id
                or pending.step_position != step_position
                or pending.action_name != action_name
                or pending.resource_key != resource_key
            ):
                raise ConfirmationMismatchError(task_id)

            # A real chronological comparison of parsed, timezone-aware
            # datetimes - never a lexical string comparison of the two
            # ISO-8601 strings (see parse_task_timestamp()'s own
            # docstring for why that is not safe for an authorization-
            # expiry decision). A malformed persisted expires_at fails
            # closed as TaskStorageCorruptError here, propagated
            # uncaught - never silently treated as "not yet expired".
            if now_dt > parse_task_timestamp(pending.expires_at, "expires_at"):
                raise ConfirmationExpiredError(task_id)

            conn.execute("DELETE FROM task_pending_confirmation WHERE task_id = ?", (task_id,))

            task_cursor = conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ?, version = version + 1 "
                "WHERE task_id = ? AND state = ?",
                (
                    TaskState.RUNNING.value,
                    now,
                    task_id,
                    TaskState.WAITING_FOR_CONFIRMATION.value,
                ),
            )
            if task_cursor.rowcount == 0:
                row2 = conn.execute(
                    "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row2 is None:
                    raise TaskNotFoundError(task_id)
                _require_task_state(row2[0], TaskState.WAITING_FOR_CONFIRMATION)
                raise InvalidTransitionError(
                    f"task {task_id} is no longer waiting for confirmation"
                )

            new_version = current_version + 1
            conn.execute(
                "INSERT INTO task_transitions ("
                "task_id, from_state, to_state, timestamp, reason_code, "
                "safe_summary, task_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    TaskState.WAITING_FOR_CONFIRMATION.value,
                    TaskState.RUNNING.value,
                    now,
                    reason_code,
                    safe_summary,
                    new_version,
                ),
            )

            conn.execute(
                "INSERT INTO task_step_progress "
                "(task_id, step_position, status, started_at, task_version) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, step_position, StepStatus.IN_PROGRESS.value, now, new_version),
            )
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise StepAlreadyClaimedError(
                f"step {step_position} of task {task_id} has already been claimed"
            ) from exc
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            raise TaskStorageUnavailableError("task database is locked or unavailable") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

        return self.get_task(task_id)

    def deny_confirmation(
        self,
        task_id: str,
        confirmation_id: str,
        *,
        reason_code: str = "confirmation_denied",
        safe_summary: str | None = None,
    ) -> TaskRecord:
        """WAITING_FOR_CONFIRMATION -> CANCELLED, consuming the pending
        confirmation atomically. No replanning: this is a plain
        cancellation, not a request for a different action."""

        reason_code = _validate_bounded_text(reason_code, "reason_code", 1, MAX_REASON_CODE_CHARS)
        safe_summary = _validate_optional_bounded_text(
            safe_summary, "safe_summary", MAX_SAFE_SUMMARY_CHARS
        )
        return self._resolve_pending_confirmation(
            task_id,
            confirmation_id,
            TaskState.CANCELLED,
            reason_code=reason_code,
            safe_summary=safe_summary,
        )

    def fail_pending_confirmation(
        self,
        task_id: str,
        confirmation_id: str,
        failure_code: str,
        failure_summary: str,
    ) -> TaskRecord:
        """WAITING_FOR_CONFIRMATION -> FAILED, consuming the pending
        confirmation atomically. Used both for an expired confirmation and
        for one whose action/resource is no longer valid at approval time
        (kernel/task_execution/service.py owns both fixed, code-authored
        failure_code/failure_summary values - this method never invents
        them itself, matching mark_failed()'s own contract)."""

        failure_code = _validate_bounded_text(
            failure_code, "failure_code", 1, MAX_FAILURE_CODE_CHARS
        )
        failure_summary = _validate_bounded_text(
            failure_summary, "failure_summary", 1, MAX_FAILURE_SUMMARY_CHARS
        )
        return self._resolve_pending_confirmation(
            task_id,
            confirmation_id,
            TaskState.FAILED,
            reason_code=failure_code,
            safe_summary=failure_summary,
            failure_code=failure_code,
            failure_summary=failure_summary,
        )

    def _resolve_pending_confirmation(
        self,
        task_id: str,
        confirmation_id: str,
        target_state: TaskState,
        *,
        reason_code: str | None,
        safe_summary: str | None,
        failure_code: str | None = None,
        failure_summary: str | None = None,
    ) -> TaskRecord:
        """Shared by deny_confirmation() (-> CANCELLED) and
        fail_pending_confirmation() (-> FAILED): verify the pending
        confirmation's confirmation_id matches exactly, delete it, and
        transition WAITING_FOR_CONFIRMATION -> target_state - all
        atomically, mirroring _apply_transition()'s own race-handling
        shape."""

        confirmation_id = _validate_confirmation_id(confirmation_id)
        conn = self._conn
        now = _now()

        conn.execute("BEGIN IMMEDIATE")
        try:
            actual_state, current_version = _select_task_state_for_update(conn, task_id)
            _require_task_state(actual_state, TaskState.WAITING_FOR_CONFIRMATION)

            pending_row = conn.execute(
                "SELECT confirmation_id FROM task_pending_confirmation WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if pending_row is None:
                raise NoPendingConfirmationError(task_id)
            if pending_row[0] != confirmation_id:
                raise ConfirmationMismatchError(task_id)

            conn.execute("DELETE FROM task_pending_confirmation WHERE task_id = ?", (task_id,))

            set_clauses = ["state = ?", "updated_at = ?", "completed_at = ?", "version = version + 1"]
            params: list = [target_state.value, now, now]
            if failure_code is not None:
                set_clauses.append("failure_code = ?")
                params.append(failure_code)
            if failure_summary is not None:
                set_clauses.append("failure_summary = ?")
                params.append(failure_summary)

            task_cursor = conn.execute(
                f"UPDATE tasks SET {', '.join(set_clauses)} WHERE task_id = ? AND state = ?",
                (*params, task_id, TaskState.WAITING_FOR_CONFIRMATION.value),
            )
            if task_cursor.rowcount == 0:
                row2 = conn.execute(
                    "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row2 is None:
                    raise TaskNotFoundError(task_id)
                _require_task_state(row2[0], TaskState.WAITING_FOR_CONFIRMATION)
                raise InvalidTransitionError(
                    f"task {task_id} is no longer waiting for confirmation"
                )

            new_version = current_version + 1
            conn.execute(
                "INSERT INTO task_transitions ("
                "task_id, from_state, to_state, timestamp, reason_code, "
                "safe_summary, task_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    TaskState.WAITING_FOR_CONFIRMATION.value,
                    target_state.value,
                    now,
                    reason_code,
                    safe_summary,
                    new_version,
                ),
            )
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            raise TaskStorageUnavailableError("task database is locked or unavailable") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

        return self.get_task(task_id)

    def _finalize_step(
        self,
        task_id: str,
        step_position: int,
        target_status: StepStatus,
        *,
        result_json: str | None,
        failure_code: str | None,
        failure_summary: str | None,
    ) -> TaskStepProgress:
        conn = self._conn
        now = _now()

        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "UPDATE task_step_progress SET status = ?, completed_at = ?, "
                "result_json = ?, failure_code = ?, failure_summary = ? "
                "WHERE task_id = ? AND step_position = ? AND status = ?",
                (
                    target_status.value,
                    now,
                    result_json,
                    failure_code,
                    failure_summary,
                    task_id,
                    step_position,
                    StepStatus.IN_PROGRESS.value,
                ),
            )
            if cursor.rowcount == 0:
                # Either no row was ever claimed for this (task_id,
                # step_position), or it already reached a terminal status -
                # both cases are the same fail-closed refusal: a terminal
                # step's status and result can never be overwritten, and a
                # never-claimed step can never be finalized directly.
                raise StepNotInProgressError(
                    f"step {step_position} of task {task_id} is not currently in_progress"
                )
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            raise TaskStorageUnavailableError("task database is locked or unavailable") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

        return self.get_step_progress(task_id, step_position)

    def _apply_transition(
        self,
        task_id: str,
        expected: TaskState,
        target: TaskState,
        *,
        reason_code: str | None,
        safe_summary: str | None,
        failure_code: str | None = None,
        failure_summary: str | None = None,
    ) -> TaskRecord:
        conn = self._conn
        now = _now()

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT state, started_at, version FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(task_id)

            actual_state, started_at, current_version = row
            if actual_state != expected.value:
                if actual_state in _TERMINAL_STATE_VALUES:
                    raise TaskAlreadyTerminalError(actual_state)
                raise InvalidTransitionError(
                    f"expected task to be in {expected.value!r} but it is in {actual_state!r}"
                )

            set_clauses = ["state = ?", "updated_at = ?", "version = version + 1"]
            params: list = [target.value, now]
            if target == TaskState.RUNNING and started_at is None:
                set_clauses.append("started_at = ?")
                params.append(now)
            if target in TERMINAL_STATES:
                set_clauses.append("completed_at = ?")
                params.append(now)
            if failure_code is not None:
                set_clauses.append("failure_code = ?")
                params.append(failure_code)
            if failure_summary is not None:
                set_clauses.append("failure_summary = ?")
                params.append(failure_summary)

            cursor = conn.execute(
                f"UPDATE tasks SET {', '.join(set_clauses)} WHERE task_id = ? AND state = ?",
                (*params, task_id, expected.value),
            )
            if cursor.rowcount == 0:
                # Lost a race to a concurrent writer between our SELECT
                # and our UPDATE (or the row vanished, which cannot
                # happen in this milestone - no delete API exists).
                row2 = conn.execute(
                    "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row2 is None:
                    raise TaskNotFoundError(task_id)
                actual_state2 = row2[0]
                if actual_state2 in _TERMINAL_STATE_VALUES:
                    raise TaskAlreadyTerminalError(actual_state2)
                raise InvalidTransitionError(
                    f"expected task to be in {expected.value!r} but it is in {actual_state2!r}"
                )

            new_version = current_version + 1
            conn.execute(
                "INSERT INTO task_transitions ("
                "task_id, from_state, to_state, timestamp, reason_code, "
                "safe_summary, task_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, expected.value, target.value, now, reason_code, safe_summary, new_version),
            )
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            raise TaskStorageUnavailableError("task database is locked or unavailable") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

        return self.get_task(task_id)
