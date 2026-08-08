"""
TaskRepository: the narrow persistence API for kernel/employee_tasks/
(Milestone 40). See kernel/employee_tasks/__init__.py for what this
package is and deliberately is not.

Every write method (create_task, transition_task, mark_failed,
mark_cancelled) wraps its current-row update and its append-only journal
insert in exactly one BEGIN IMMEDIATE ... COMMIT transaction: there is
never a committed state update without its matching journal entry, or a
journal entry without its matching state update. Any exception rolls the
whole transaction back before propagating - see _apply_transition().

Every state-changing method requires the caller's expected current state
and performs a SQL-level conditional UPDATE (... WHERE state = ?), so a
stale writer can never silently overwrite a state another writer already
moved on from - exactly one of two callers racing the same transition can
ever succeed; the other observes either TaskAlreadyTerminalError or
InvalidTransitionError, and modifies neither table.

metadata_json is treated as an opaque, size-capped JSON string end to
end: this module validates that it parses as JSON and enforces the
character limit, but never inspects its keys, never deserializes it into
a value callers can act on, and never uses it to make an execution or
authorization decision.
"""

import json
import sqlite3
from datetime import datetime, timezone

from kernel.employee_tasks.types import (
    ALLOWED_TRANSITIONS,
    DEFAULT_LIST_LIMIT,
    MAX_DEDUP_KEY_CHARS,
    MAX_DISPLAY_ID_ATTEMPTS,
    MAX_FAILURE_CODE_CHARS,
    MAX_FAILURE_SUMMARY_CHARS,
    MAX_LIST_LIMIT,
    MAX_METADATA_JSON_CHARS,
    MAX_REASON_CODE_CHARS,
    MAX_REQUEST_TEXT_CHARS,
    MAX_SAFE_SUMMARY_CHARS,
    MAX_SOURCE_CHARS,
    MIN_LIST_LIMIT,
    MIN_REQUEST_TEXT_CHARS,
    MIN_SOURCE_CHARS,
    TERMINAL_STATES,
    DuplicateTaskError,
    InvalidTransitionError,
    TaskAlreadyTerminalError,
    TaskInputTooLargeError,
    TaskNotFoundError,
    TaskRecord,
    TaskState,
    TaskStorageUnavailableError,
    TaskTransition,
    generate_display_id,
    generate_task_id,
)

_TERMINAL_STATE_VALUES = frozenset(state.value for state in TERMINAL_STATES)

_SELECT_COLUMNS = (
    "task_id, display_id, state, request_text, source, dedup_key, "
    "created_at, updated_at, started_at, completed_at, failure_code, "
    "failure_summary, metadata_json, protocol_version, version"
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
    )


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
        expected = _coerce_state(expected_state)
        target = _coerce_state(new_state)
        reason_code = _validate_optional_bounded_text(
            reason_code, "reason_code", MAX_REASON_CODE_CHARS
        )
        safe_summary = _validate_optional_bounded_text(
            safe_summary, "safe_summary", MAX_SAFE_SUMMARY_CHARS
        )

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
        expected = _coerce_state(expected_state)
        failure_code = _validate_bounded_text(
            failure_code, "failure_code", 1, MAX_FAILURE_CODE_CHARS
        )
        failure_summary = _validate_bounded_text(
            failure_summary, "failure_summary", 1, MAX_FAILURE_SUMMARY_CHARS
        )

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
        expected = _coerce_state(expected_state)
        reason_code = _validate_bounded_text(reason_code, "reason_code", 1, MAX_REASON_CODE_CHARS)
        safe_summary = _validate_optional_bounded_text(
            safe_summary, "safe_summary", MAX_SAFE_SUMMARY_CHARS
        )

        if expected in TERMINAL_STATES:
            raise TaskAlreadyTerminalError(expected.value)
        if TaskState.CANCELLED not in ALLOWED_TRANSITIONS[expected]:
            raise InvalidTransitionError(
                f"{expected.value} -> cancelled is not an allowed transition"
            )

        return self._apply_transition(
            task_id, expected, TaskState.CANCELLED, reason_code=reason_code, safe_summary=safe_summary
        )

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
