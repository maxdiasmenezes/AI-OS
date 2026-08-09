"""Tests for kernel/employee_tasks/repository.py's step-progress API
(Milestone 42 P1): claim_step(), mark_step_succeeded(), mark_step_failed(),
get_step_progress(), list_step_progress(). Every test opens a writer
connection against a tmp_path database - never the real database under
storage/tasks/."""

import json
import threading

import pytest

from kernel.employee_tasks.db import open_writer_connection
from kernel.employee_tasks.repository import TaskRepository
from kernel.employee_tasks.types import (
    MAX_FAILURE_CODE_CHARS,
    MAX_FAILURE_SUMMARY_CHARS,
    MAX_STEP_RESULT_JSON_CHARS,
    InvalidTransitionError,
    StepAlreadyClaimedError,
    StepNotInProgressError,
    StepStatus,
    TaskAlreadyTerminalError,
    TaskInputTooLargeError,
    TaskNotFoundError,
)

# Walks a freshly created task ("created") to `state` via one conservative
# allowed path - mirrors test_repository.py's own _drive_to_state() helper.
_STATE_PATH = {
    "created": [],
    "planning": ["planning"],
    "ready": ["planning", "ready"],
    "running": ["planning", "ready", "running"],
    "waiting_for_confirmation": ["planning", "ready", "running", "waiting_for_confirmation"],
}


def _drive_to_state(repo, task_id, state: str) -> None:
    current = "created"
    for next_state in _STATE_PATH[state]:
        repo.transition_task(task_id, current, next_state)
        current = next_state


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "tasks.sqlite3"


@pytest.fixture
def repo(db_path):
    conn = open_writer_connection(db_path)
    yield TaskRepository(conn)
    conn.close()


@pytest.fixture
def task(repo):
    """A task in RUNNING state - the only state claim_step() ever
    authorizes a claim from. See the "claim requires RUNNING" section
    below for tests exercising every other state explicitly."""

    record = repo.create_task("book a flight", "whatsapp")
    _drive_to_state(repo, record.task_id, "running")
    return repo.get_task(record.task_id)


# --- absence / claim --------------------------------------------------------


def test_absence_of_row_means_not_started(repo, task):
    assert repo.get_step_progress(task.task_id, 1) is None
    assert repo.list_step_progress(task.task_id) == []


def test_claim_step_creates_in_progress_row(repo, task):
    progress = repo.claim_step(task.task_id, 1)
    assert progress.task_id == task.task_id
    assert progress.step_position == 1
    assert progress.status == StepStatus.IN_PROGRESS
    assert progress.started_at is not None
    assert progress.completed_at is None
    assert progress.result_json is None
    assert progress.failure_code is None
    assert progress.failure_summary is None


def test_claim_step_records_current_task_version(repo, task):
    current_version = repo.get_task(task.task_id).version
    progress = repo.claim_step(task.task_id, 1)
    assert progress.task_version == current_version


def test_claim_step_visible_via_get_and_list(repo, task):
    repo.claim_step(task.task_id, 1)
    fetched = repo.get_step_progress(task.task_id, 1)
    assert fetched.status == StepStatus.IN_PROGRESS
    assert repo.list_step_progress(task.task_id) == [fetched]


def test_claim_step_unknown_task_rejected(repo):
    with pytest.raises(TaskNotFoundError):
        repo.claim_step("00000000-0000-7000-8000-000000000000", 1)


def test_duplicate_claim_rejected(repo, task):
    repo.claim_step(task.task_id, 1)
    with pytest.raises(StepAlreadyClaimedError):
        repo.claim_step(task.task_id, 1)


def test_duplicate_claim_after_terminal_status_still_rejected(repo, task):
    repo.claim_step(task.task_id, 1)
    repo.mark_step_succeeded(task.task_id, 1, "{}")
    with pytest.raises(StepAlreadyClaimedError):
        repo.claim_step(task.task_id, 1)


def test_claim_step_does_not_affect_other_positions(repo, task):
    repo.claim_step(task.task_id, 1)
    progress_2 = repo.claim_step(task.task_id, 2)
    assert progress_2.step_position == 2
    assert repo.get_step_progress(task.task_id, 1).status == StepStatus.IN_PROGRESS


def test_claim_step_does_not_affect_other_tasks(repo, task):
    other_task = repo.create_task("second task", "whatsapp")
    repo.claim_step(task.task_id, 1)
    assert repo.get_step_progress(other_task.task_id, 1) is None


def test_claim_step_rejects_non_positive_position(repo, task):
    with pytest.raises(TaskInputTooLargeError):
        repo.claim_step(task.task_id, 0)
    with pytest.raises(TaskInputTooLargeError):
        repo.claim_step(task.task_id, -1)


def test_claim_step_rejects_non_integer_position(repo, task):
    with pytest.raises(TaskInputTooLargeError):
        repo.claim_step(task.task_id, "1")
    with pytest.raises(TaskInputTooLargeError):
        repo.claim_step(task.task_id, True)


# --- claim requires RUNNING (Milestone 42 P1 correction) --------------------


def test_created_task_cannot_claim_a_step(repo):
    record = repo.create_task("book a flight", "whatsapp")
    with pytest.raises(InvalidTransitionError):
        repo.claim_step(record.task_id, 1)
    assert repo.get_step_progress(record.task_id, 1) is None


def test_planning_task_cannot_claim_a_step(repo):
    record = repo.create_task("book a flight", "whatsapp")
    _drive_to_state(repo, record.task_id, "planning")
    with pytest.raises(InvalidTransitionError):
        repo.claim_step(record.task_id, 1)


def test_ready_task_cannot_claim_a_step(repo):
    record = repo.create_task("book a flight", "whatsapp")
    _drive_to_state(repo, record.task_id, "ready")
    with pytest.raises(InvalidTransitionError):
        repo.claim_step(record.task_id, 1)
    assert repo.get_step_progress(record.task_id, 1) is None


def test_waiting_for_confirmation_task_cannot_claim_a_step(repo):
    record = repo.create_task("book a flight", "whatsapp")
    _drive_to_state(repo, record.task_id, "waiting_for_confirmation")
    with pytest.raises(InvalidTransitionError):
        repo.claim_step(record.task_id, 1)


def test_completed_task_cannot_claim_a_step(repo):
    record = repo.create_task("book a flight", "whatsapp")
    _drive_to_state(repo, record.task_id, "running")
    repo.transition_task(record.task_id, "running", "completed")
    with pytest.raises(TaskAlreadyTerminalError):
        repo.claim_step(record.task_id, 1)
    assert repo.get_step_progress(record.task_id, 1) is None


def test_failed_task_cannot_claim_a_step(repo):
    record = repo.create_task("book a flight", "whatsapp")
    repo.mark_failed(record.task_id, "created", "tool_error", "planning failed")
    with pytest.raises(TaskAlreadyTerminalError):
        repo.claim_step(record.task_id, 1)


def test_cancelled_task_cannot_claim_a_step(repo):
    record = repo.create_task("book a flight", "whatsapp")
    repo.mark_cancelled(record.task_id, "created")
    with pytest.raises(TaskAlreadyTerminalError):
        repo.claim_step(record.task_id, 1)


def test_running_task_can_claim(repo, task):
    progress = repo.claim_step(task.task_id, 1)
    assert progress.status == StepStatus.IN_PROGRESS


def test_stale_caller_cannot_claim_after_concurrent_cancellation(db_path):
    """Worker A observes the task as RUNNING; before it calls claim_step(),
    a different connection (worker B) commits RUNNING -> CANCELLED. Worker
    A's claim_step() must re-read the task's state itself (never trust an
    earlier in-memory TaskRecord) and must fail closed."""

    conn_a = open_writer_connection(db_path)
    conn_b = open_writer_connection(db_path)
    repo_a = TaskRepository(conn_a)
    repo_b = TaskRepository(conn_b)

    try:
        record = repo_a.create_task("book a flight", "whatsapp")
        _drive_to_state(repo_a, record.task_id, "running")

        # Worker A "observes" RUNNING here (this stale record is never
        # re-checked by claim_step() - the whole point of this test).
        observed = repo_a.get_task(record.task_id)
        assert observed.state.value == "running"

        # Worker B, on a separate connection, cancels the task.
        repo_b.mark_cancelled(record.task_id, "running")

        # Worker A's claim must fail closed - it must not rely on `observed`.
        with pytest.raises(TaskAlreadyTerminalError):
            repo_a.claim_step(record.task_id, 1)

        assert repo_a.get_step_progress(record.task_id, 1) is None
    finally:
        conn_a.close()
        conn_b.close()


# --- terminal transitions ----------------------------------------------------


def test_mark_step_succeeded_transitions_in_progress_to_succeeded(repo, task):
    repo.claim_step(task.task_id, 1)
    result = repo.mark_step_succeeded(task.task_id, 1, '{"summary": "done"}')
    assert result.status == StepStatus.SUCCEEDED
    assert result.completed_at is not None
    assert result.result_json == '{"summary": "done"}'
    assert result.failure_code is None
    assert result.failure_summary is None


def test_mark_step_failed_transitions_in_progress_to_failed(repo, task):
    repo.claim_step(task.task_id, 1)
    result = repo.mark_step_failed(task.task_id, 1, "tool_error", "the action failed safely")
    assert result.status == StepStatus.FAILED
    assert result.completed_at is not None
    assert result.failure_code == "tool_error"
    assert result.failure_summary == "the action failed safely"
    assert result.result_json is None


def test_mark_step_failed_accepts_optional_result_json(repo, task):
    repo.claim_step(task.task_id, 1)
    result = repo.mark_step_failed(
        task.task_id, 1, "tool_error", "failed", result_json='{"partial": true}'
    )
    assert result.result_json == '{"partial": true}'


def test_mark_step_succeeded_without_claim_rejected(repo, task):
    with pytest.raises(StepNotInProgressError):
        repo.mark_step_succeeded(task.task_id, 1, "{}")


def test_mark_step_failed_without_claim_rejected(repo, task):
    with pytest.raises(StepNotInProgressError):
        repo.mark_step_failed(task.task_id, 1, "tool_error", "failed")


def test_succeeded_step_cannot_become_failed(repo, task):
    repo.claim_step(task.task_id, 1)
    repo.mark_step_succeeded(task.task_id, 1, "{}")
    with pytest.raises(StepNotInProgressError):
        repo.mark_step_failed(task.task_id, 1, "tool_error", "too late")


def test_failed_step_cannot_become_succeeded(repo, task):
    repo.claim_step(task.task_id, 1)
    repo.mark_step_failed(task.task_id, 1, "tool_error", "failed")
    with pytest.raises(StepNotInProgressError):
        repo.mark_step_succeeded(task.task_id, 1, "{}")


def test_terminal_observation_cannot_be_overwritten(repo, task):
    repo.claim_step(task.task_id, 1)
    repo.mark_step_succeeded(task.task_id, 1, '{"first": true}')
    with pytest.raises(StepNotInProgressError):
        repo.mark_step_succeeded(task.task_id, 1, '{"second": true}')

    reloaded = repo.get_step_progress(task.task_id, 1)
    assert reloaded.result_json == '{"first": true}'


# --- bounded validation -------------------------------------------------------


def test_result_json_must_be_valid_json(repo, task):
    repo.claim_step(task.task_id, 1)
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_step_succeeded(task.task_id, 1, "not json")


def test_result_json_oversized_rejected(repo, task):
    repo.claim_step(task.task_id, 1)
    oversized = json.dumps({"x": "y" * MAX_STEP_RESULT_JSON_CHARS})
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_step_succeeded(task.task_id, 1, oversized)


def test_result_json_exactly_max_size_accepted(repo, task):
    repo.claim_step(task.task_id, 1)
    padding = MAX_STEP_RESULT_JSON_CHARS - len('{"x":""}')
    result_json = json.dumps({"x": "y" * padding}, separators=(",", ":"))
    assert len(result_json) <= MAX_STEP_RESULT_JSON_CHARS
    result = repo.mark_step_succeeded(task.task_id, 1, result_json)
    assert result.result_json == result_json


def test_result_json_with_nul_byte_rejected(repo, task):
    repo.claim_step(task.task_id, 1)
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_step_succeeded(task.task_id, 1, '{"x": "a\x00b"}')


def test_failure_code_and_summary_bounded(repo, task):
    repo.claim_step(task.task_id, 1)
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_step_failed(task.task_id, 1, "x" * (MAX_FAILURE_CODE_CHARS + 1), "summary")

    repo.claim_step(task.task_id, 2)
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_step_failed(task.task_id, 2, "tool_error", "x" * (MAX_FAILURE_SUMMARY_CHARS + 1))


def test_failure_code_and_summary_cannot_be_empty(repo, task):
    repo.claim_step(task.task_id, 1)
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_step_failed(task.task_id, 1, "", "summary")

    repo.claim_step(task.task_id, 2)
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_step_failed(task.task_id, 2, "tool_error", "")


# --- listing -------------------------------------------------------------


def test_list_step_progress_ordered_by_position(repo, task):
    repo.claim_step(task.task_id, 3)
    repo.claim_step(task.task_id, 1)
    repo.claim_step(task.task_id, 2)

    positions = [record.step_position for record in repo.list_step_progress(task.task_id)]
    assert positions == [1, 2, 3]


# --- persistence across reconnects -------------------------------------------


def test_progress_survives_connection_close_and_reopen(db_path, task):
    conn = open_writer_connection(db_path)
    repo1 = TaskRepository(conn)
    repo1.claim_step(task.task_id, 1)
    repo1.mark_step_succeeded(task.task_id, 1, '{"done": true}')
    conn.close()

    conn2 = open_writer_connection(db_path)
    try:
        repo2 = TaskRepository(conn2)
        reloaded = repo2.get_step_progress(task.task_id, 1)
        assert reloaded.status == StepStatus.SUCCEEDED
        assert reloaded.result_json == '{"done": true}'
    finally:
        conn2.close()


def test_failed_step_failure_code_and_summary_round_trip_across_reconnect(db_path, task):
    """Targeted regression test for _row_to_step_progress(): every column,
    including failure_code specifically, must round-trip through a fresh
    connection - not just the columns a happy-path test happens to read."""

    conn = open_writer_connection(db_path)
    repo1 = TaskRepository(conn)
    repo1.claim_step(task.task_id, 1)
    repo1.mark_step_failed(task.task_id, 1, "known_code", "a known failure summary")
    conn.close()

    conn2 = open_writer_connection(db_path)
    try:
        repo2 = TaskRepository(conn2)
        reloaded = repo2.get_step_progress(task.task_id, 1)
        assert reloaded.status == StepStatus.FAILED
        assert reloaded.failure_code == "known_code"
        assert reloaded.failure_summary == "a known failure summary"
    finally:
        conn2.close()


# --- concurrency -----------------------------------------------------------


def test_two_competing_claims_exactly_one_wins(db_path, task):
    conn_a = open_writer_connection(db_path)
    conn_b = open_writer_connection(db_path)
    repo_a = TaskRepository(conn_a)
    repo_b = TaskRepository(conn_b)

    outcomes = {}

    def attempt(repo, tag):
        try:
            repo.claim_step(task.task_id, 1)
            outcomes[tag] = "succeeded"
        except StepAlreadyClaimedError:
            outcomes[tag] = "rejected"

    t_a = threading.Thread(target=attempt, args=(repo_a, "a"))
    t_b = threading.Thread(target=attempt, args=(repo_b, "b"))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert sorted(outcomes.values()) == ["rejected", "succeeded"]

    final = repo_a.get_step_progress(task.task_id, 1)
    assert final.status == StepStatus.IN_PROGRESS

    conn_a.close()
    conn_b.close()


def test_two_competing_claims_on_different_positions_both_win(db_path, task):
    conn_a = open_writer_connection(db_path)
    conn_b = open_writer_connection(db_path)
    repo_a = TaskRepository(conn_a)
    repo_b = TaskRepository(conn_b)

    outcomes = {}

    def attempt(repo, tag, position):
        try:
            repo.claim_step(task.task_id, position)
            outcomes[tag] = "succeeded"
        except StepAlreadyClaimedError:
            outcomes[tag] = "rejected"

    t_a = threading.Thread(target=attempt, args=(repo_a, "a", 1))
    t_b = threading.Thread(target=attempt, args=(repo_b, "b", 2))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert outcomes == {"a": "succeeded", "b": "succeeded"}

    conn_a.close()
    conn_b.close()
