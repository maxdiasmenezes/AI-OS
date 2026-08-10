"""Tests for kernel/employee_tasks/repository.py: TaskRepository. Every
test opens a writer connection against a tmp_path database - never the
real database under storage/tasks/."""

import json
import sqlite3
import threading

import pytest

from kernel.employee_tasks.db import open_reader_connection, open_writer_connection
from kernel.employee_tasks.repository import TaskRepository
from kernel.employee_tasks.types import (
    MAX_DEDUP_KEY_CHARS,
    MAX_FAILURE_CODE_CHARS,
    MAX_FAILURE_SUMMARY_CHARS,
    MAX_LIST_LIMIT,
    MAX_METADATA_JSON_CHARS,
    MAX_PLAN_JSON_CHARS,
    MAX_REASON_CODE_CHARS,
    MAX_REQUEST_TEXT_CHARS,
    MAX_SAFE_SUMMARY_CHARS,
    MAX_SOURCE_CHARS,
    TERMINAL_STATES,
    DuplicateTaskError,
    InvalidTransitionError,
    TaskAlreadyTerminalError,
    TaskInputTooLargeError,
    TaskNotFoundError,
    TaskState,
    TaskStorageUnavailableError,
    generate_display_id,
    generate_task_id,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "tasks.sqlite3"


@pytest.fixture
def repo(db_path):
    conn = open_writer_connection(db_path)
    yield TaskRepository(conn)
    conn.close()


# --- IDs ----------------------------------------------------------------


def test_generate_task_id_is_a_uuid7_string():
    task_id = generate_task_id()
    parsed = __import__("uuid").UUID(task_id)
    assert parsed.version == 7


def test_generate_display_id_format():
    task_id = generate_task_id()
    display_id = generate_display_id(task_id)
    assert display_id.startswith("TASK-")
    suffix = display_id[len("TASK-"):]
    assert len(suffix) == 8
    for ambiguous in "01ILO":
        assert ambiguous not in suffix
    assert suffix == suffix.upper()


def test_generate_display_id_is_deterministic_from_task_id():
    task_id = generate_task_id()
    assert generate_display_id(task_id) == generate_display_id(task_id)


def test_task_id_and_display_id_unique_across_large_sample(repo):
    task_ids = set()
    display_ids = set()
    for i in range(300):
        record = repo.create_task(f"request {i}", "whatsapp")
        assert record.task_id not in task_ids
        assert record.display_id not in display_ids
        task_ids.add(record.task_id)
        display_ids.add(record.display_id)
    assert len(task_ids) == 300
    assert len(display_ids) == 300


def test_create_task_retries_on_simulated_display_id_collision(monkeypatch, repo):
    """Force generate_display_id to return a fixed value for the first
    two attempts, then a real one - simulating an extremely unlikely
    collision without waiting for one to occur naturally. Must retry
    with a fresh task_id/display_id pair rather than overwriting."""

    import kernel.employee_tasks.repository as repository_module

    existing = repo.create_task("first task", "whatsapp")

    call_count = {"n": 0}
    real_generate_display_id = repository_module.generate_display_id

    def fake_generate_display_id(task_id):
        call_count["n"] += 1
        if call_count["n"] <= 1:
            return existing.display_id  # forces a UNIQUE collision
        return real_generate_display_id(task_id)

    monkeypatch.setattr(repository_module, "generate_display_id", fake_generate_display_id)

    new_record = repo.create_task("second task", "whatsapp")

    assert new_record.display_id != existing.display_id
    assert call_count["n"] >= 2
    # The original row must be completely untouched.
    reloaded = repo.get_task(existing.task_id)
    assert reloaded.request_text == "first task"


def test_create_task_exhausts_retries_and_fails_closed(monkeypatch, repo):
    import kernel.employee_tasks.repository as repository_module

    existing = repo.create_task("first task", "whatsapp")
    monkeypatch.setattr(
        repository_module, "generate_display_id", lambda task_id: existing.display_id
    )

    with pytest.raises(TaskStorageUnavailableError):
        repo.create_task("will never get a unique display_id", "whatsapp")

    # No partial/duplicate row was left behind.
    assert len(repo.list_tasks(limit=100)) == 1


# --- creation -------------------------------------------------------------


def test_create_task_valid(repo):
    record = repo.create_task("book a flight to Lisbon", "whatsapp")
    assert record.state == TaskState.CREATED
    assert record.request_text == "book a flight to Lisbon"
    assert record.source == "whatsapp"
    assert record.version == 1
    assert record.metadata_json == "{}"
    assert record.protocol_version == 1
    assert record.started_at is None
    assert record.completed_at is None
    assert record.failure_code is None
    assert record.dedup_key is None
    assert record.created_at == record.updated_at


def test_create_task_unicode_request(repo):
    text = "café 你好 \U0001F600 café combining ́ mark"
    record = repo.create_task(text, "whatsapp")
    reloaded = repo.get_task(record.task_id)
    assert reloaded.request_text == text


def test_create_task_exactly_max_size_request(repo):
    text = "x" * MAX_REQUEST_TEXT_CHARS
    record = repo.create_task(text, "whatsapp")
    assert len(record.request_text) == MAX_REQUEST_TEXT_CHARS


def test_create_task_oversized_request_rejected(repo):
    with pytest.raises(TaskInputTooLargeError):
        repo.create_task("x" * (MAX_REQUEST_TEXT_CHARS + 1), "whatsapp")


def test_create_task_empty_request_rejected(repo):
    with pytest.raises(TaskInputTooLargeError):
        repo.create_task("", "whatsapp")


def test_create_task_request_with_nul_byte_rejected(repo):
    with pytest.raises(TaskInputTooLargeError):
        repo.create_task("hello\x00world", "whatsapp")


def test_create_task_source_validation(repo):
    with pytest.raises(TaskInputTooLargeError):
        repo.create_task("request", "")
    with pytest.raises(TaskInputTooLargeError):
        repo.create_task("request", "x" * (MAX_SOURCE_CHARS + 1))
    record = repo.create_task("request", "x" * MAX_SOURCE_CHARS)
    assert len(record.source) == MAX_SOURCE_CHARS


def test_create_task_metadata_json_validation(repo):
    ok = repo.create_task("request", "whatsapp", metadata_json='{"k": "v"}')
    assert ok.metadata_json == '{"k": "v"}'

    with pytest.raises(TaskInputTooLargeError):
        repo.create_task("request", "whatsapp", metadata_json="not json")
    with pytest.raises(TaskInputTooLargeError):
        repo.create_task("request", "whatsapp", metadata_json="{" + "x" * MAX_METADATA_JSON_CHARS)


def test_create_task_dedup_key_uniqueness(repo):
    first = repo.create_task("request one", "whatsapp", dedup_key="msg-1")
    assert first.dedup_key == "msg-1"

    with pytest.raises(DuplicateTaskError):
        repo.create_task("request two", "whatsapp", dedup_key="msg-1")

    # A second, different dedup_key is fine; None (unset) never collides.
    repo.create_task("request three", "whatsapp", dedup_key="msg-2")
    repo.create_task("request four", "whatsapp")
    repo.create_task("request five", "whatsapp")


def test_create_task_dedup_key_length_validation(repo):
    with pytest.raises(TaskInputTooLargeError):
        repo.create_task("request", "whatsapp", dedup_key="x" * (MAX_DEDUP_KEY_CHARS + 1))


def test_create_task_no_partial_rows_after_failed_creation(repo):
    with pytest.raises(TaskInputTooLargeError):
        repo.create_task("x" * (MAX_REQUEST_TEXT_CHARS + 1), "whatsapp")
    assert repo.list_tasks(limit=100) == []

    repo.create_task("request one", "whatsapp", dedup_key="dup")
    try:
        repo.create_task("request two", "whatsapp", dedup_key="dup")
    except DuplicateTaskError:
        pass
    assert len(repo.list_tasks(limit=100)) == 1


# --- lookup / listing -------------------------------------------------------


def test_get_task_by_task_id(repo):
    created = repo.create_task("request", "whatsapp")
    fetched = repo.get_task(created.task_id)
    assert fetched == created


def test_get_task_by_display_id(repo):
    created = repo.create_task("request", "whatsapp")
    fetched = repo.get_task_by_display_id(created.display_id)
    assert fetched == created


def test_get_task_not_found(repo):
    with pytest.raises(TaskNotFoundError):
        repo.get_task("00000000-0000-7000-8000-000000000000")


def test_get_task_by_display_id_not_found(repo):
    with pytest.raises(TaskNotFoundError):
        repo.get_task_by_display_id("TASK-ZZZZZZZZ")


def test_list_tasks_deterministic_order(repo):
    for i in range(10):
        repo.create_task(f"request {i}", "whatsapp")
    first = repo.list_tasks(limit=5)
    second = repo.list_tasks(limit=5)
    assert [r.task_id for r in first] == [r.task_id for r in second]
    assert len(first) == 5


def test_list_tasks_limit_boundaries(repo):
    for i in range(3):
        repo.create_task(f"request {i}", "whatsapp")

    assert len(repo.list_tasks(limit=1)) == 1
    assert len(repo.list_tasks(limit=MAX_LIST_LIMIT)) == 3

    with pytest.raises(TaskInputTooLargeError):
        repo.list_tasks(limit=0)
    with pytest.raises(TaskInputTooLargeError):
        repo.list_tasks(limit=MAX_LIST_LIMIT + 1)
    with pytest.raises(TaskInputTooLargeError):
        repo.list_tasks(limit=-1)


# --- state transitions -------------------------------------------------------


def test_full_happy_path_lifecycle(repo):
    record = repo.create_task("request", "whatsapp")
    tid = record.task_id

    record = repo.transition_task(tid, "created", "planning")
    assert record.state == TaskState.PLANNING
    assert record.version == 2

    record = repo.transition_task(tid, "planning", "ready")
    assert record.state == TaskState.READY
    assert record.version == 3

    record = repo.transition_task(tid, "ready", "running")
    assert record.state == TaskState.RUNNING
    assert record.started_at is not None
    started_at = record.started_at
    assert record.version == 4

    # Milestone 42 P2: entering/leaving waiting_for_confirmation goes
    # through the dedicated confirmation operations, never a generic
    # transition_task() call - see
    # test_generic_transition_task_rejects_waiting_for_confirmation_edges().
    record = repo.propose_confirmation(tid, 1, "repository_backup", "ai_os", ttl_seconds=120)
    assert record.state == TaskState.WAITING_FOR_CONFIRMATION
    assert record.version == 5

    pending = repo.get_pending_confirmation(tid)
    record = repo.consume_confirmation_and_claim_step(
        tid, pending.confirmation_id, 1, "repository_backup", "ai_os"
    )
    assert record.state == TaskState.RUNNING
    assert record.started_at == started_at  # must not be overwritten
    assert record.version == 6

    record = repo.transition_task(tid, "running", "completed")
    assert record.state == TaskState.COMPLETED
    assert record.completed_at is not None
    assert record.version == 7


@pytest.mark.parametrize(
    "expected_state,new_state",
    [
        ("created", "planning"),
        ("created", "cancelled"),
        ("created", "failed"),
        ("planning", "ready"),
        ("planning", "cancelled"),
        ("planning", "failed"),
        ("ready", "running"),
        ("ready", "cancelled"),
        ("ready", "failed"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "cancelled"),
    ],
)
def test_every_allowed_edge(repo, expected_state, new_state):
    """Every edge here is reachable through the generic transition_task().
    The four edges into/out of waiting_for_confirmation are deliberately
    NOT included - see
    test_generic_transition_task_rejects_waiting_for_confirmation_edges()
    below: they remain valid in ALLOWED_TRANSITIONS (the abstract
    lifecycle graph is unchanged), but Milestone 42 P2 requires them to go
    through the dedicated confirmation operations instead, never this
    generic method."""

    record = repo.create_task("request", "whatsapp")
    tid = record.task_id
    _drive_to_state(repo, tid, expected_state)

    result = repo.transition_task(tid, expected_state, new_state)
    assert result.state == TaskState(new_state)


@pytest.mark.parametrize(
    "expected_state,new_state",
    [
        ("running", "waiting_for_confirmation"),
        ("waiting_for_confirmation", "running"),
        ("waiting_for_confirmation", "cancelled"),
        ("waiting_for_confirmation", "failed"),
    ],
)
def test_generic_transition_task_rejects_waiting_for_confirmation_edges(
    repo, expected_state, new_state
):
    """Milestone 42 P2: these four edges remain valid in
    ALLOWED_TRANSITIONS, but transition_task() must mechanically refuse
    all of them - only propose_confirmation() (in) and
    consume_confirmation_and_claim_step()/deny_confirmation()/
    fail_pending_confirmation() (out) may enter/leave
    waiting_for_confirmation, since only they keep the durable
    task_pending_confirmation row synchronized with task state."""

    record = repo.create_task("request", "whatsapp")
    tid = record.task_id
    _drive_to_state(repo, tid, expected_state)

    with pytest.raises(InvalidTransitionError):
        repo.transition_task(tid, expected_state, new_state)


def _drive_to_state(repo, task_id, state: str) -> None:
    """Walk a freshly created task ("created") to `state` via one
    conservative allowed path, for tests that only care about the edge
    starting at `state`. Reaching "waiting_for_confirmation" goes through
    propose_confirmation() (Milestone 42 P2) - never a generic
    transition_task() call, which correctly refuses that edge (see
    _reject_generic_waiting_for_confirmation() in repository.py)."""

    if state == "waiting_for_confirmation":
        _drive_to_state(repo, task_id, "running")
        repo.propose_confirmation(task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
        return

    path = {
        "created": [],
        "planning": ["planning"],
        "ready": ["planning", "ready"],
        "running": ["planning", "ready", "running"],
    }[state]
    current = "created"
    for step in path:
        repo.transition_task(task_id, current, step)
        current = step


def test_skip_transition_rejected(repo):
    record = repo.create_task("request", "whatsapp")
    with pytest.raises(InvalidTransitionError):
        repo.transition_task(record.task_id, "created", "running")


def test_backward_transition_rejected(repo):
    record = repo.create_task("request", "whatsapp")
    tid = record.task_id
    repo.transition_task(tid, "created", "planning")
    repo.transition_task(tid, "planning", "ready")
    with pytest.raises(InvalidTransitionError):
        repo.transition_task(tid, "ready", "planning")


def test_unknown_state_rejected(repo):
    record = repo.create_task("request", "whatsapp")
    with pytest.raises(InvalidTransitionError):
        repo.transition_task(record.task_id, "created", "paused")
    with pytest.raises(InvalidTransitionError):
        repo.transition_task(record.task_id, "bogus", "planning")


def test_stale_expected_state_rejected(repo):
    record = repo.create_task("request", "whatsapp")
    tid = record.task_id
    repo.transition_task(tid, "created", "planning")
    with pytest.raises(InvalidTransitionError):
        repo.transition_task(tid, "created", "planning")  # already moved on


@pytest.mark.parametrize("terminal_state", ["completed", "failed", "cancelled"])
def test_terminal_state_protected(repo, terminal_state):
    record = repo.create_task("request", "whatsapp")
    tid = record.task_id
    if terminal_state == "completed":
        _drive_to_state(repo, tid, "running")
        repo.transition_task(tid, "running", "completed")
    elif terminal_state == "failed":
        repo.mark_failed(tid, "created", "tool_error", "something failed")
    else:
        repo.mark_cancelled(tid, "created")

    with pytest.raises(TaskAlreadyTerminalError):
        repo.transition_task(tid, terminal_state, "planning")
    with pytest.raises(TaskAlreadyTerminalError):
        repo.transition_task(tid, terminal_state, "running")

    final = repo.get_task(tid)
    assert final.state == TaskState(terminal_state)


def test_nonexistent_task_rejected(repo):
    with pytest.raises(TaskNotFoundError):
        repo.transition_task("00000000-0000-7000-8000-000000000000", "created", "planning")


def test_version_increments_on_every_transition(repo):
    record = repo.create_task("request", "whatsapp")
    assert record.version == 1
    record = repo.transition_task(record.task_id, "created", "planning")
    assert record.version == 2
    record = repo.transition_task(record.task_id, "planning", "cancelled")
    assert record.version == 3


def test_updated_at_changes_on_every_transition(repo):
    record = repo.create_task("request", "whatsapp")
    created_updated_at = record.updated_at
    record = repo.transition_task(record.task_id, "created", "cancelled")
    assert record.updated_at >= created_updated_at


def test_started_at_set_once(repo):
    record = repo.create_task("request", "whatsapp")
    tid = record.task_id
    repo.transition_task(tid, "created", "planning")
    repo.transition_task(tid, "planning", "ready")
    record = repo.transition_task(tid, "ready", "running")
    first_started_at = record.started_at
    assert first_started_at is not None

    # Milestone 42 P2: RUNNING <-> WAITING_FOR_CONFIRMATION goes through
    # the dedicated confirmation operations, never transition_task().
    repo.propose_confirmation(tid, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(tid)
    record = repo.consume_confirmation_and_claim_step(
        tid, pending.confirmation_id, 1, "repository_backup", "ai_os"
    )
    assert record.started_at == first_started_at


def test_terminal_completed_at_set_for_each_terminal_path(repo):
    completed = repo.create_task("request", "whatsapp")
    _drive_to_state(repo, completed.task_id, "running")
    result = repo.transition_task(completed.task_id, "running", "completed")
    assert result.completed_at is not None

    failed = repo.create_task("request", "whatsapp")
    result = repo.mark_failed(failed.task_id, "created", "tool_error", "boom")
    assert result.completed_at is not None

    cancelled = repo.create_task("request", "whatsapp")
    result = repo.mark_cancelled(cancelled.task_id, "created")
    assert result.completed_at is not None


# --- journal -------------------------------------------------------------


def test_creation_history_records_synthetic_null_transition(repo):
    record = repo.create_task("request", "whatsapp")
    transitions = repo.list_transitions(record.task_id)
    assert len(transitions) == 1
    assert transitions[0].from_state is None
    assert transitions[0].to_state == TaskState.CREATED
    assert transitions[0].task_version == 1


def test_every_mutation_records_exactly_one_journal_entry(repo):
    record = repo.create_task("request", "whatsapp")
    tid = record.task_id
    repo.transition_task(tid, "created", "planning")
    repo.transition_task(tid, "planning", "ready")
    repo.transition_task(tid, "ready", "running")

    transitions = repo.list_transitions(tid)
    assert len(transitions) == 4  # creation + 3 transitions
    assert [t.to_state for t in transitions] == [
        TaskState.CREATED,
        TaskState.PLANNING,
        TaskState.READY,
        TaskState.RUNNING,
    ]
    assert [t.from_state for t in transitions] == [
        None,
        TaskState.CREATED,
        TaskState.PLANNING,
        TaskState.READY,
    ]
    assert [t.task_version for t in transitions] == [1, 2, 3, 4]


def test_journal_cannot_diverge_from_current_row(repo):
    record = repo.create_task("request", "whatsapp")
    tid = record.task_id
    repo.transition_task(tid, "created", "planning")
    repo.transition_task(tid, "planning", "cancelled")

    current = repo.get_task(tid)
    transitions = repo.list_transitions(tid)
    assert transitions[-1].to_state == current.state
    assert transitions[-1].task_version == current.version


def test_rejected_transition_does_not_add_journal_entry(repo):
    record = repo.create_task("request", "whatsapp")
    tid = record.task_id
    before = repo.list_transitions(tid)

    with pytest.raises(InvalidTransitionError):
        repo.transition_task(tid, "created", "running")

    after = repo.list_transitions(tid)
    assert before == after


# --- failure / cancel -------------------------------------------------------


def test_mark_failed(repo):
    record = repo.create_task("request", "whatsapp")
    result = repo.mark_failed(record.task_id, "created", "tool_error", "a tool failed safely")
    assert result.state == TaskState.FAILED
    assert result.failure_code == "tool_error"
    assert result.failure_summary == "a tool failed safely"
    assert result.completed_at is not None


def test_mark_cancelled(repo):
    record = repo.create_task("request", "whatsapp")
    result = repo.mark_cancelled(record.task_id, "created")
    assert result.state == TaskState.CANCELLED
    transitions = repo.list_transitions(record.task_id)
    assert transitions[-1].reason_code == "user_cancelled"


def test_mark_cancelled_custom_reason_and_summary(repo):
    record = repo.create_task("request", "whatsapp")
    result = repo.mark_cancelled(
        record.task_id, "created", reason_code="superseded", safe_summary="replaced by a newer request"
    )
    assert result.state == TaskState.CANCELLED
    transitions = repo.list_transitions(record.task_id)
    assert transitions[-1].reason_code == "superseded"
    assert transitions[-1].safe_summary == "replaced by a newer request"


def test_mark_failed_bounded_fields(repo):
    record = repo.create_task("request", "whatsapp")
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_failed(record.task_id, "created", "x" * (MAX_FAILURE_CODE_CHARS + 1), "summary")
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_failed(record.task_id, "created", "tool_error", "x" * (MAX_FAILURE_SUMMARY_CHARS + 1))
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_failed(record.task_id, "created", "", "summary")
    with pytest.raises(TaskInputTooLargeError):
        repo.mark_failed(record.task_id, "created", "tool_error", "")


def test_transition_reason_and_summary_bounded(repo):
    record = repo.create_task("request", "whatsapp")
    with pytest.raises(TaskInputTooLargeError):
        repo.transition_task(
            record.task_id, "created", "planning", reason_code="x" * (MAX_REASON_CODE_CHARS + 1)
        )
    with pytest.raises(TaskInputTooLargeError):
        repo.transition_task(
            record.task_id, "created", "planning", safe_summary="x" * (MAX_SAFE_SUMMARY_CHARS + 1)
        )


def test_mark_failed_and_cancelled_respect_terminal_protection(repo):
    record = repo.create_task("request", "whatsapp")
    repo.mark_cancelled(record.task_id, "created")
    with pytest.raises(TaskAlreadyTerminalError):
        repo.mark_failed(record.task_id, "cancelled", "tool_error", "too late")
    with pytest.raises(TaskAlreadyTerminalError):
        repo.mark_cancelled(record.task_id, "cancelled")


# --- concurrency -----------------------------------------------------------


def test_concurrent_reader_during_writer(db_path):
    writer_conn = open_writer_connection(db_path)
    writer_repo = TaskRepository(writer_conn)

    errors = []
    stop_event = threading.Event()

    def reader_loop():
        try:
            reader_conn = open_reader_connection(db_path)
            reader_repo = TaskRepository(reader_conn)
            while not stop_event.is_set():
                reader_repo.list_tasks(limit=5)
            reader_conn.close()
        except Exception as exc:  # pragma: no cover - diagnostic only
            errors.append(exc)

    reader_thread = threading.Thread(target=reader_loop)
    reader_thread.start()
    for i in range(30):
        writer_repo.create_task(f"request {i}", "whatsapp")
    stop_event.set()
    reader_thread.join(timeout=5)

    writer_conn.close()
    assert errors == []


def test_two_writers_racing_same_transition_exactly_one_winner(db_path):
    conn_a = open_writer_connection(db_path)
    conn_b = open_writer_connection(db_path)
    repo_a = TaskRepository(conn_a)
    repo_b = TaskRepository(conn_b)

    record = repo_a.create_task("race target", "whatsapp")
    repo_a.transition_task(record.task_id, "created", "planning")
    repo_a.transition_task(record.task_id, "planning", "ready")

    outcomes = {}

    def attempt(repo, tag):
        try:
            repo.transition_task(record.task_id, "ready", "running")
            outcomes[tag] = "succeeded"
        except InvalidTransitionError:
            outcomes[tag] = "rejected"

    t_a = threading.Thread(target=attempt, args=(repo_a, "a"))
    t_b = threading.Thread(target=attempt, args=(repo_b, "b"))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert sorted(outcomes.values()) == ["rejected", "succeeded"]

    final = repo_a.get_task(record.task_id)
    assert final.state == TaskState.RUNNING
    assert final.version == 4  # created(1) -> planning(2) -> ready(3) -> running(4)

    transitions = repo_a.list_transitions(record.task_id)
    running_transitions = [t for t in transitions if t.to_state == TaskState.RUNNING]
    assert len(running_transitions) == 1

    conn_a.close()
    conn_b.close()


# --- crash / rollback -------------------------------------------------------


class _FaultInjectingConnection:
    """Wraps a real sqlite3.Connection and raises once the first time
    `execute()` is called with SQL containing `fail_on_substring` -
    simulating a process crash between the current-row UPDATE and the
    journal INSERT, entirely from the test side. Never modifies
    production code to support this."""

    def __init__(self, conn: sqlite3.Connection, fail_on_substring: str):
        self._conn = conn
        self._fail_on_substring = fail_on_substring
        self._triggered = False

    def execute(self, sql, params=()):
        if not self._triggered and self._fail_on_substring in sql:
            self._triggered = True
            raise RuntimeError("simulated crash")
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_crash_between_row_update_and_journal_insert_rolls_back(db_path):
    conn = open_writer_connection(db_path)
    repo = TaskRepository(conn)
    record = repo.create_task("request", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")

    faulty = _FaultInjectingConnection(conn, "INSERT INTO task_transitions")
    faulty_repo = TaskRepository(faulty)

    with pytest.raises(RuntimeError):
        faulty_repo.transition_task(record.task_id, "planning", "ready")

    post_crash = repo.get_task(record.task_id)
    assert post_crash.state == TaskState.PLANNING  # rolled back, not "ready"
    assert post_crash.version == 2  # unchanged from before the crashed attempt

    transitions = repo.list_transitions(record.task_id)
    assert [t.to_state for t in transitions] == [TaskState.CREATED, TaskState.PLANNING]

    conn.close()


def test_transaction_rollback_leaves_no_partial_row_on_integrity_violation(repo):
    repo.create_task("first", "whatsapp", dedup_key="dup")
    count_before = len(repo.list_tasks(limit=100))

    with pytest.raises(DuplicateTaskError):
        repo.create_task("second", "whatsapp", dedup_key="dup")

    count_after = len(repo.list_tasks(limit=100))
    assert count_before == count_after


# --- Milestone 41 P2: plan_json / persist_plan_and_ready ---------------------


def test_task_record_exposes_plan_json(repo):
    record = repo.create_task("request", "whatsapp")
    assert hasattr(record, "plan_json")


def test_new_tasks_start_with_plan_json_none(repo):
    record = repo.create_task("request", "whatsapp")
    assert record.plan_json is None


def test_persist_plan_and_ready_happy_path(repo):
    record = repo.create_task("request", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")

    result = repo.persist_plan_and_ready(
        record.task_id, "planning", '{"objective":"x"}', reason_code="plan_ready"
    )

    assert result.state == TaskState.READY
    assert result.plan_json == '{"objective":"x"}'
    assert result.version == 3  # created(1) -> planning(2) -> ready(3)


def test_persisted_plan_survives_repository_reopen(db_path):
    conn1 = open_writer_connection(db_path)
    repo1 = TaskRepository(conn1)
    record = repo1.create_task("request", "whatsapp")
    repo1.transition_task(record.task_id, "created", "planning")
    repo1.persist_plan_and_ready(record.task_id, "planning", '{"objective":"x"}')
    conn1.close()

    conn2 = open_writer_connection(db_path)
    repo2 = TaskRepository(conn2)
    reloaded = repo2.get_task(record.task_id)
    assert reloaded.state == TaskState.READY
    assert reloaded.plan_json == '{"objective":"x"}'
    conn2.close()


def test_persist_plan_and_ready_records_one_journal_entry(repo):
    record = repo.create_task("request", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")
    repo.persist_plan_and_ready(record.task_id, "planning", '{"objective":"x"}')

    transitions = repo.list_transitions(record.task_id)
    assert [t.to_state for t in transitions] == [
        TaskState.CREATED,
        TaskState.PLANNING,
        TaskState.READY,
    ]


def test_persist_plan_and_ready_rejects_second_write(repo):
    record = repo.create_task("request", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")
    repo.persist_plan_and_ready(record.task_id, "planning", '{"objective":"x"}')

    # The task is now "ready", not "planning" - a second attempt with the
    # correct original expected_state fails on the state check alone.
    with pytest.raises(InvalidTransitionError):
        repo.persist_plan_and_ready(record.task_id, "planning", '{"objective":"y"}')


def test_persist_plan_and_ready_rejects_second_write_even_when_state_is_planning(db_path):
    # A more targeted proof that the guard is plan_json IS NULL, not just
    # "state == planning": force the row back to "planning" via a
    # separate raw connection (simulating a database that already carries
    # a persisted plan for some other reason) and confirm a second write
    # through the repository API is still rejected.
    conn = open_writer_connection(db_path)
    repo = TaskRepository(conn)
    record = repo.create_task("request", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")
    repo.persist_plan_and_ready(record.task_id, "planning", '{"objective":"x"}')

    raw = sqlite3.connect(str(db_path))
    raw.execute("UPDATE tasks SET state = 'planning' WHERE task_id = ?", (record.task_id,))
    raw.commit()
    raw.close()

    with pytest.raises(InvalidTransitionError):
        repo.persist_plan_and_ready(record.task_id, "planning", '{"objective":"y"}')

    unchanged = repo.get_task(record.task_id)
    assert unchanged.plan_json == '{"objective":"x"}'
    conn.close()


def test_persist_plan_and_ready_rejects_wrong_state(repo):
    record = repo.create_task("request", "whatsapp")
    # Still "created" - never transitioned to "planning".
    with pytest.raises(InvalidTransitionError):
        repo.persist_plan_and_ready(record.task_id, "planning", '{"objective":"x"}')


def test_persist_plan_and_ready_rejects_terminal_task(repo):
    record = repo.create_task("request", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")
    repo.mark_failed(record.task_id, "planning", "some_code", "some summary")

    with pytest.raises(TaskAlreadyTerminalError):
        repo.persist_plan_and_ready(record.task_id, "planning", '{"objective":"x"}')


def test_persist_plan_and_ready_rejects_nonexistent_task(repo):
    with pytest.raises(TaskNotFoundError):
        repo.persist_plan_and_ready(
            "00000000-0000-7000-8000-000000000000", "planning", '{"objective":"x"}'
        )


def test_persist_plan_and_ready_validates_plan_json_size(repo):
    record = repo.create_task("request", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")

    oversized = '{"x":"' + ("a" * MAX_PLAN_JSON_CHARS) + '"}'
    with pytest.raises(TaskInputTooLargeError):
        repo.persist_plan_and_ready(record.task_id, "planning", oversized)


def test_persist_plan_and_ready_rejects_non_json_plan_json(repo):
    record = repo.create_task("request", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")

    with pytest.raises(TaskInputTooLargeError):
        repo.persist_plan_and_ready(record.task_id, "planning", "not json")


def test_plan_and_transition_journal_are_atomic_on_crash(db_path):
    conn = open_writer_connection(db_path)
    repo = TaskRepository(conn)
    record = repo.create_task("request", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")

    faulty = _FaultInjectingConnection(conn, "INSERT INTO task_transitions")
    faulty_repo = TaskRepository(faulty)

    with pytest.raises(RuntimeError):
        faulty_repo.persist_plan_and_ready(record.task_id, "planning", '{"objective":"x"}')

    post_crash = repo.get_task(record.task_id)
    assert post_crash.state == TaskState.PLANNING  # rolled back, not "ready"
    assert post_crash.plan_json is None  # rolled back, not persisted
    assert post_crash.version == 2  # unchanged from before the crashed attempt

    transitions = repo.list_transitions(record.task_id)
    assert [t.to_state for t in transitions] == [TaskState.CREATED, TaskState.PLANNING]

    conn.close()
