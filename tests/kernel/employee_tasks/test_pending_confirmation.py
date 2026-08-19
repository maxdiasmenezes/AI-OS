"""Tests for kernel/employee_tasks/repository.py's durable confirmation
API (Milestone 42 P2): propose_confirmation(), get_pending_confirmation(),
consume_confirmation_and_claim_step(), deny_confirmation(),
fail_pending_confirmation(), fail_running_step()."""

import threading

import pytest

from kernel.employee_tasks.db import open_writer_connection
from kernel.employee_tasks.repository import TaskRepository
from kernel.employee_tasks.types import (
    MAX_CONFIRMATION_ID_CHARS,
    ConfirmationExpiredError,
    ConfirmationMismatchError,
    InvalidTransitionError,
    LifecycleEventKind,
    LifecycleEventPayloadError,
    NoPendingConfirmationError,
    StepAlreadyClaimedError,
    StepNotInProgressError,
    StepStatus,
    TaskAlreadyTerminalError,
    TaskInputTooLargeError,
    TaskState,
    TaskStorageCorruptError,
    deserialize_confirmation_required_payload,
)

_STATE_PATH = {
    "created": [],
    "planning": ["planning"],
    "ready": ["planning", "ready"],
    "running": ["planning", "ready", "running"],
}


def _drive_to_state(repo, task_id, state: str) -> None:
    """Reaching "waiting_for_confirmation" goes through
    propose_confirmation() (Milestone 42 P2) - never a generic
    transition_task() call, which correctly refuses that edge."""

    if state == "waiting_for_confirmation":
        _drive_to_state(repo, task_id, "running")
        repo.propose_confirmation(task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
        return

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
    """A task in RUNNING state - the only state propose_confirmation()
    ever authorizes a proposal from."""

    record = repo.create_task("back up the repository", "whatsapp")
    _drive_to_state(repo, record.task_id, "running")
    return repo.get_task(record.task_id)


# --- propose_confirmation ----------------------------------------------------


def test_propose_confirmation_creates_pending_row_and_waits(repo, task):
    result = repo.propose_confirmation(
        task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120
    )
    assert result.state == TaskState.WAITING_FOR_CONFIRMATION

    pending = repo.get_pending_confirmation(task.task_id)
    assert pending.task_id == task.task_id
    assert pending.step_position == 1
    assert pending.action_name == "repository_backup"
    assert pending.resource_key == "ai_os"
    assert pending.confirmation_id
    assert pending.created_at < pending.expires_at


def test_propose_confirmation_confirmation_id_is_not_task_id(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)
    assert pending.confirmation_id != task.task_id


def test_propose_confirmation_journals_the_transition(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    transitions = repo.list_transitions(task.task_id)
    assert transitions[-1].from_state == TaskState.RUNNING
    assert transitions[-1].to_state == TaskState.WAITING_FOR_CONFIRMATION


@pytest.mark.parametrize("state", ["created", "planning", "ready", "waiting_for_confirmation"])
def test_propose_confirmation_requires_running(repo, state):
    record = repo.create_task("back up the repository", "whatsapp")
    _drive_to_state(repo, record.task_id, state)
    with pytest.raises(InvalidTransitionError):
        repo.propose_confirmation(record.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)


def test_propose_confirmation_rejects_terminal_task(repo):
    record = repo.create_task("back up the repository", "whatsapp")
    repo.mark_cancelled(record.task_id, "created")
    with pytest.raises(TaskAlreadyTerminalError):
        repo.propose_confirmation(record.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)


def test_propose_confirmation_rejects_already_claimed_step(repo, task):
    repo.claim_step(task.task_id, 1)
    with pytest.raises(StepAlreadyClaimedError):
        repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)


def test_propose_confirmation_allows_null_resource_key(repo, task):
    result = repo.propose_confirmation(task.task_id, 1, "system_status", None, ttl_seconds=120)
    assert result.state == TaskState.WAITING_FOR_CONFIRMATION
    pending = repo.get_pending_confirmation(task.task_id)
    assert pending.resource_key is None


def test_propose_confirmation_rejects_non_positive_ttl(repo, task):
    with pytest.raises(TaskInputTooLargeError):
        repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=0)
    with pytest.raises(TaskInputTooLargeError):
        repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=-5)


# --- get_pending_confirmation -------------------------------------------------


def test_get_pending_confirmation_returns_none_when_absent(repo, task):
    assert repo.get_pending_confirmation(task.task_id) is None


# --- consume_confirmation_and_claim_step (approve) ----------------------------


def test_consume_confirmation_success(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    result = repo.consume_confirmation_and_claim_step(
        task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
    )
    assert result.state == TaskState.RUNNING
    assert repo.get_pending_confirmation(task.task_id) is None

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status == StepStatus.IN_PROGRESS


def test_consume_confirmation_rejects_wrong_confirmation_id(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    with pytest.raises(ConfirmationMismatchError):
        repo.consume_confirmation_and_claim_step(
            task.task_id, "wrong-id", 1, "repository_backup", "ai_os"
        )
    # Nothing consumed - the pending row and task state are untouched.
    assert repo.get_pending_confirmation(task.task_id) is not None
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


@pytest.mark.parametrize(
    "field,value",
    [("step_position", 2), ("action_name", "open_application"), ("resource_key", "other")],
)
def test_consume_confirmation_rejects_field_mismatch(repo, task, field, value):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    kwargs = {
        "step_position": pending.step_position,
        "action_name": pending.action_name,
        "resource_key": pending.resource_key,
    }
    kwargs[field] = value

    with pytest.raises(ConfirmationMismatchError):
        repo.consume_confirmation_and_claim_step(
            task.task_id,
            pending.confirmation_id,
            kwargs["step_position"],
            kwargs["action_name"],
            kwargs["resource_key"],
        )


def test_consume_confirmation_rejects_when_task_not_waiting(repo, task):
    with pytest.raises(InvalidTransitionError):
        repo.consume_confirmation_and_claim_step(
            task.task_id, "some-id", 1, "repository_backup", "ai_os"
        )


def test_consume_confirmation_rejects_when_none_pending_but_task_waiting(repo, task):
    """Structurally unreachable through the public API alone (every path
    into WAITING_FOR_CONFIRMATION also creates the pending row atomically),
    but NoPendingConfirmationError must still fire correctly if it ever
    happens - simulated here by deleting the row directly, bypassing the
    repository, exactly like test_repository.py's own fault-injection
    tests simulate an otherwise-unreachable crash window."""

    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    repo._conn.execute("DELETE FROM task_pending_confirmation WHERE task_id = ?", (task.task_id,))

    with pytest.raises(NoPendingConfirmationError):
        repo.consume_confirmation_and_claim_step(
            task.task_id, "some-id", 1, "repository_backup", "ai_os"
        )


def test_consume_confirmation_accepts_unexpired(repo, task):
    """Real datetime parse/compare (not lexical string ordering) correctly
    accepts a confirmation well inside its TTL window."""

    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    result = repo.consume_confirmation_and_claim_step(
        task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
    )
    assert result.state == TaskState.RUNNING


def test_consume_confirmation_rejects_when_expired(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    repo._conn.execute(
        "UPDATE task_pending_confirmation SET expires_at = '2000-01-01T00:00:00+00:00' "
        "WHERE task_id = ?",
        (task.task_id,),
    )
    pending = repo.get_pending_confirmation(task.task_id)

    with pytest.raises(ConfirmationExpiredError):
        repo.consume_confirmation_and_claim_step(
            task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
        )
    # Expiry alone never mutates anything - the pending row and task state
    # are left exactly as they were, for a separate fail_pending_confirmation()
    # call to resolve.
    assert repo.get_pending_confirmation(task.task_id) is not None
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


def test_consume_confirmation_expiry_uses_real_datetime_parse_not_lexical_order(repo, task):
    """A malformed-but-lexically-"later" expires_at would incorrectly read
    as unexpired under naive string comparison; parsing it as a real
    datetime must still work correctly for a well-formed value with
    different (but valid) ISO-8601 formatting - e.g. one that happens to
    include an explicit zero microseconds component, which
    datetime.isoformat() itself never emits, so this could only arise from
    a differently-formatted (but still valid) ISO-8601 write path."""

    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    # A well-formed ISO-8601 timestamp with an explicit zero-microseconds
    # component and a 'Z' UTC suffix - a shape datetime.isoformat() itself
    # never produces, but still validly parseable and still in the future.
    repo._conn.execute(
        "UPDATE task_pending_confirmation SET expires_at = ? WHERE task_id = ?",
        ("2099-01-01T00:00:00.000000Z", task.task_id),
    )
    pending = repo.get_pending_confirmation(task.task_id)

    result = repo.consume_confirmation_and_claim_step(
        task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
    )
    assert result.state == TaskState.RUNNING


def test_consume_confirmation_rejects_malformed_expires_at_and_mutates_nothing(repo, task):
    """Malformed persisted expires_at must fail closed as a storage/
    integrity problem (TaskStorageCorruptError) - never silently treated
    as valid (i.e. never treated as "not expired")."""

    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    repo._conn.execute(
        "UPDATE task_pending_confirmation SET expires_at = 'not-a-timestamp' WHERE task_id = ?",
        (task.task_id,),
    )
    pending = repo.get_pending_confirmation(task.task_id)

    with pytest.raises(TaskStorageCorruptError):
        repo.consume_confirmation_and_claim_step(
            task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
        )

    # Nothing was claimed or executed, and nothing was mutated.
    assert repo.get_pending_confirmation(task.task_id) is not None
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_step_progress(task.task_id, 1) is None


def test_duplicate_consume_after_success_fails_closed(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)
    repo.consume_confirmation_and_claim_step(
        task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
    )

    with pytest.raises(InvalidTransitionError):
        repo.consume_confirmation_and_claim_step(
            task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
        )
    # The step was claimed exactly once, not twice.
    step = repo.get_step_progress(task.task_id, 1)
    assert step.status == StepStatus.IN_PROGRESS


# --- deny_confirmation ---------------------------------------------------------


def test_deny_confirmation_cancels_and_deletes_pending(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    result = repo.deny_confirmation(task.task_id, pending.confirmation_id)
    assert result.state == TaskState.CANCELLED
    assert repo.get_pending_confirmation(task.task_id) is None


def test_deny_confirmation_rejects_wrong_confirmation_id(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    with pytest.raises(ConfirmationMismatchError):
        repo.deny_confirmation(task.task_id, "wrong-id")
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


def test_deny_confirmation_rejects_when_task_not_waiting(repo, task):
    with pytest.raises(InvalidTransitionError):
        repo.deny_confirmation(task.task_id, "some-id")


def test_deny_confirmation_rejects_when_none_pending_but_task_waiting(repo, task):
    """See test_consume_confirmation_rejects_when_none_pending_but_task_waiting's
    own docstring for why this is simulated via direct row deletion."""

    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    repo._conn.execute("DELETE FROM task_pending_confirmation WHERE task_id = ?", (task.task_id,))

    with pytest.raises(NoPendingConfirmationError):
        repo.deny_confirmation(task.task_id, "some-id")


# --- fail_pending_confirmation -------------------------------------------------


def test_fail_pending_confirmation_fails_and_deletes_pending(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    result = repo.fail_pending_confirmation(
        task.task_id, pending.confirmation_id, "confirmation_expired", "expired"
    )
    assert result.state == TaskState.FAILED
    assert result.failure_code == "confirmation_expired"
    assert repo.get_pending_confirmation(task.task_id) is None


def test_fail_pending_confirmation_rejects_wrong_confirmation_id(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    with pytest.raises(ConfirmationMismatchError):
        repo.fail_pending_confirmation(task.task_id, "wrong-id", "confirmation_expired", "expired")


# --- fail_running_step -----------------------------------------------------------


def test_fail_running_step_fails_step_and_task_atomically(repo, task):
    repo.claim_step(task.task_id, 1)
    result = repo.fail_running_step(task.task_id, 1, "failed", "the action failed safely")

    assert result.state == TaskState.FAILED
    assert result.failure_code == "failed"
    step = repo.get_step_progress(task.task_id, 1)
    assert step.status == StepStatus.FAILED
    assert step.failure_code == "failed"


def test_fail_running_step_requires_step_in_progress(repo, task):
    with pytest.raises(StepNotInProgressError):
        repo.fail_running_step(task.task_id, 1, "failed", "never claimed")


def test_fail_running_step_requires_task_running(repo, task):
    repo.claim_step(task.task_id, 1)
    repo.mark_cancelled(task.task_id, "running")

    with pytest.raises(TaskAlreadyTerminalError):
        repo.fail_running_step(task.task_id, 1, "failed", "too late")
    # The step's own row is untouched by the failed attempt.
    step = repo.get_step_progress(task.task_id, 1)
    assert step.status == StepStatus.IN_PROGRESS


# --- concurrency: propose racing propose (test A) -----------------------------


def test_two_workers_propose_confirmation_exactly_one_wins(db_path):
    conn_a = open_writer_connection(db_path)
    conn_b = open_writer_connection(db_path)
    repo_a = TaskRepository(conn_a)
    repo_b = TaskRepository(conn_b)

    record = repo_a.create_task("back up the repository", "whatsapp")
    _drive_to_state(repo_a, record.task_id, "running")

    outcomes = {}

    def attempt(repo, tag):
        try:
            repo.propose_confirmation(record.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
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
    assert final.state == TaskState.WAITING_FOR_CONFIRMATION

    conn_a.close()
    conn_b.close()


# --- concurrency: approval racing denial (test B) -----------------------------


def test_approval_racing_denial_exactly_one_wins(db_path):
    conn_a = open_writer_connection(db_path)
    conn_b = open_writer_connection(db_path)
    repo_a = TaskRepository(conn_a)
    repo_b = TaskRepository(conn_b)

    record = repo_a.create_task("back up the repository", "whatsapp")
    _drive_to_state(repo_a, record.task_id, "running")
    repo_a.propose_confirmation(record.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo_a.get_pending_confirmation(record.task_id)

    outcomes = {}

    def approve():
        try:
            repo_a.consume_confirmation_and_claim_step(
                record.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
            )
            outcomes["approve"] = "succeeded"
        except (InvalidTransitionError, NoPendingConfirmationError):
            outcomes["approve"] = "rejected"

    def deny():
        try:
            repo_b.deny_confirmation(record.task_id, pending.confirmation_id)
            outcomes["deny"] = "succeeded"
        except (InvalidTransitionError, NoPendingConfirmationError):
            outcomes["deny"] = "rejected"

    t_a = threading.Thread(target=approve)
    t_b = threading.Thread(target=deny)
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    # Exactly one of the two operations won.
    assert sorted(outcomes.values()) == ["rejected", "succeeded"]

    final = repo_a.get_task(record.task_id)
    if outcomes["approve"] == "succeeded":
        assert final.state == TaskState.RUNNING
        assert repo_a.get_step_progress(record.task_id, 1).status == StepStatus.IN_PROGRESS
    else:
        assert final.state == TaskState.CANCELLED
        assert repo_a.get_step_progress(record.task_id, 1) is None

    conn_a.close()
    conn_b.close()


# --- concurrency: duplicate approval (test C) ---------------------------------


def test_duplicate_approval_only_one_caller_consumes_and_claims(db_path):
    conn_a = open_writer_connection(db_path)
    conn_b = open_writer_connection(db_path)
    repo_a = TaskRepository(conn_a)
    repo_b = TaskRepository(conn_b)

    record = repo_a.create_task("back up the repository", "whatsapp")
    _drive_to_state(repo_a, record.task_id, "running")
    repo_a.propose_confirmation(record.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo_a.get_pending_confirmation(record.task_id)

    outcomes = {}

    def approve(repo, tag):
        try:
            repo.consume_confirmation_and_claim_step(
                record.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
            )
            outcomes[tag] = "succeeded"
        except (InvalidTransitionError, NoPendingConfirmationError):
            outcomes[tag] = "rejected"

    t_a = threading.Thread(target=approve, args=(repo_a, "a"))
    t_b = threading.Thread(target=approve, args=(repo_b, "b"))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert sorted(outcomes.values()) == ["rejected", "succeeded"]

    final = repo_a.get_task(record.task_id)
    assert final.state == TaskState.RUNNING
    step = repo_a.get_step_progress(record.task_id, 1)
    assert step.status == StepStatus.IN_PROGRESS
    # Claimed exactly once - task_version proves only one winning transition
    # actually committed the approval (not two): created(1) planning(2)
    # ready(3) running(4) waiting_for_confirmation(5) approved-running(6).
    assert final.version == 6

    conn_a.close()
    conn_b.close()


# --- bidirectional WAITING_FOR_CONFIRMATION invariant (Milestone 42 ------
# --- P2 correction) ---------------------------------------------------------
#
# A task in WAITING_FOR_CONFIRMATION must always have exactly one matching
# task_pending_confirmation row, and a task in any other state must never
# have one. Every test below proves this invariant is enforced at the
# repository API boundary itself - not merely documented as caller
# discipline - in BOTH directions: no generic transition_task()/
# mark_cancelled()/mark_failed() call may enter OR leave
# WAITING_FOR_CONFIRMATION, and every rejected attempt leaves task state
# and the pending-confirmation row exactly as they were before the call.


def test_transition_task_into_waiting_rejected_and_creates_no_pending_row(repo, task):
    with pytest.raises(InvalidTransitionError):
        repo.transition_task(task.task_id, "running", "waiting_for_confirmation")

    assert repo.get_task(task.task_id).state == TaskState.RUNNING
    assert repo.get_pending_confirmation(task.task_id) is None


def test_mark_cancelled_from_waiting_rejected_and_leaves_state_and_pending_unchanged(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending_before = repo.get_pending_confirmation(task.task_id)

    with pytest.raises(InvalidTransitionError):
        repo.mark_cancelled(task.task_id, "waiting_for_confirmation")

    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(task.task_id) == pending_before


def test_mark_failed_from_waiting_rejected_and_leaves_state_and_pending_unchanged(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending_before = repo.get_pending_confirmation(task.task_id)

    with pytest.raises(InvalidTransitionError):
        repo.mark_failed(task.task_id, "waiting_for_confirmation", "some_code", "some summary")

    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(task.task_id) == pending_before


def test_transition_task_out_of_waiting_to_running_rejected_and_pending_intact(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending_before = repo.get_pending_confirmation(task.task_id)

    with pytest.raises(InvalidTransitionError):
        repo.transition_task(task.task_id, "waiting_for_confirmation", "running")

    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(task.task_id) == pending_before
    assert repo.get_step_progress(task.task_id, 1) is None  # never claimed


def test_transition_task_out_of_waiting_to_cancelled_rejected(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending_before = repo.get_pending_confirmation(task.task_id)

    with pytest.raises(InvalidTransitionError):
        repo.transition_task(task.task_id, "waiting_for_confirmation", "cancelled")

    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(task.task_id) == pending_before


def test_transition_task_out_of_waiting_to_failed_rejected(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending_before = repo.get_pending_confirmation(task.task_id)

    with pytest.raises(InvalidTransitionError):
        repo.transition_task(task.task_id, "waiting_for_confirmation", "failed")

    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(task.task_id) == pending_before


def test_propose_confirmation_is_the_valid_atomic_path_in(repo, task):
    result = repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)

    assert result.state == TaskState.WAITING_FOR_CONFIRMATION
    pending = repo.get_pending_confirmation(task.task_id)
    assert pending is not None
    assert pending.task_id == task.task_id
    assert pending.step_position == 1


def test_consume_confirmation_is_the_valid_atomic_approval_path_out(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    result = repo.consume_confirmation_and_claim_step(
        task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
    )

    assert result.state == TaskState.RUNNING
    assert repo.get_pending_confirmation(task.task_id) is None
    step = repo.get_step_progress(task.task_id, 1)
    assert step is not None and step.status == StepStatus.IN_PROGRESS


def test_deny_confirmation_is_the_valid_atomic_cancellation_path_out(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    result = repo.deny_confirmation(task.task_id, pending.confirmation_id)

    assert result.state == TaskState.CANCELLED
    assert repo.get_pending_confirmation(task.task_id) is None
    assert repo.get_step_progress(task.task_id, 1) is None


def test_fail_pending_confirmation_is_the_valid_atomic_failure_path_out(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    result = repo.fail_pending_confirmation(
        task.task_id, pending.confirmation_id, "confirmation_expired", "expired"
    )

    assert result.state == TaskState.FAILED
    assert repo.get_pending_confirmation(task.task_id) is None
    assert repo.get_step_progress(task.task_id, 1) is None


# --- confirmation_id validation (Milestone 42 P2 correction) -----------------
#
# confirmation_id is code-generated at propose time, but
# consume_confirmation_and_claim_step()/deny_confirmation()/
# fail_pending_confirmation() receive it back from a caller - validated
# like every other externally-supplied text field (type, non-empty,
# bounded, NUL-rejected) before ever being compared against a persisted
# row, exact opaque-token matching only, never UUID-shape validation.


@pytest.mark.parametrize(
    "bad_confirmation_id",
    [None, 123, "", "x" * (MAX_CONFIRMATION_ID_CHARS + 1), "has\x00nul"],
)
def test_consume_confirmation_rejects_invalid_confirmation_id(repo, task, bad_confirmation_id):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)

    with pytest.raises(TaskInputTooLargeError):
        repo.consume_confirmation_and_claim_step(
            task.task_id, bad_confirmation_id, 1, "repository_backup", "ai_os"
        )
    # Nothing was mutated by a rejected, invalid confirmation_id.
    assert repo.get_pending_confirmation(task.task_id) is not None
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


@pytest.mark.parametrize(
    "bad_confirmation_id",
    [None, 123, "", "x" * (MAX_CONFIRMATION_ID_CHARS + 1), "has\x00nul"],
)
def test_deny_confirmation_rejects_invalid_confirmation_id(repo, task, bad_confirmation_id):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)

    with pytest.raises(TaskInputTooLargeError):
        repo.deny_confirmation(task.task_id, bad_confirmation_id)
    assert repo.get_pending_confirmation(task.task_id) is not None
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


@pytest.mark.parametrize(
    "bad_confirmation_id",
    [None, 123, "", "x" * (MAX_CONFIRMATION_ID_CHARS + 1), "has\x00nul"],
)
def test_fail_pending_confirmation_rejects_invalid_confirmation_id(repo, task, bad_confirmation_id):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)

    with pytest.raises(TaskInputTooLargeError):
        repo.fail_pending_confirmation(task.task_id, bad_confirmation_id, "code", "summary")
    assert repo.get_pending_confirmation(task.task_id) is not None
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


def test_consume_confirmation_accepts_confirmation_id_at_exactly_max_length(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    # The real confirmation_id is well under the bound - this proves the
    # bound itself doesn't reject legitimate, real confirmation_ids.
    assert len(pending.confirmation_id) <= MAX_CONFIRMATION_ID_CHARS
    result = repo.consume_confirmation_and_claim_step(
        task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
    )
    assert result.state == TaskState.RUNNING


# --- Milestone 47 P1: atomic lifecycle-outbox event creation ---------------


def test_propose_confirmation_atomically_creates_confirmation_required_event(repo, task):
    waiting = repo.propose_confirmation(
        task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120
    )
    pending = repo.get_pending_confirmation(task.task_id)

    event = repo.get_outbox_event_for_task_version(task.task_id, waiting.version)
    assert event is not None
    assert event.event_kind is LifecycleEventKind.CONFIRMATION_REQUIRED
    assert event.channel == "whatsapp"
    assert event.delivered_at is None
    assert event.attempt_count == 0

    payload = deserialize_confirmation_required_payload(event.payload_json)
    assert payload.confirmation_id == pending.confirmation_id
    assert payload.action_name == "repository_backup"
    assert payload.resource_key == "ai_os"


def test_consume_confirmation_and_claim_step_creates_no_outbox_event(repo, task):
    """RUNNING is never a deliverable target - approval continuing
    execution produces no standalone WhatsApp acknowledgement (see
    interfaces/whatsapp/task_control.py's own docstring on why)."""

    waiting = repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)
    running = repo.consume_confirmation_and_claim_step(
        task.task_id, pending.confirmation_id, 1, "repository_backup", "ai_os"
    )

    assert repo.get_outbox_event_for_task_version(task.task_id, running.version) is None
    # The earlier CONFIRMATION_REQUIRED event is untouched by this call.
    assert repo.get_outbox_event_for_task_version(task.task_id, waiting.version) is not None


def test_deny_confirmation_atomically_creates_task_cancelled_event(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    cancelled = repo.deny_confirmation(task.task_id, pending.confirmation_id)

    event = repo.get_outbox_event_for_task_version(task.task_id, cancelled.version)
    assert event is not None
    assert event.event_kind is LifecycleEventKind.TASK_CANCELLED
    assert event.payload_json is None
    assert event.channel == "whatsapp"


def test_fail_pending_confirmation_atomically_creates_task_failed_event(repo, task):
    repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)

    failed = repo.fail_pending_confirmation(
        task.task_id, pending.confirmation_id, "confirmation_expired", "The confirmation window expired."
    )

    event = repo.get_outbox_event_for_task_version(task.task_id, failed.version)
    assert event is not None
    assert event.event_kind is LifecycleEventKind.TASK_FAILED
    assert event.payload_json is None


def test_fail_running_step_atomically_creates_task_failed_event(repo, task):
    repo.claim_step(task.task_id, 1)

    failed = repo.fail_running_step(task.task_id, 1, "failed", "That action could not be completed.")

    event = repo.get_outbox_event_for_task_version(task.task_id, failed.version)
    assert event is not None
    assert event.event_kind is LifecycleEventKind.TASK_FAILED
    assert event.payload_json is None
    assert event.channel == "whatsapp"


def test_fail_running_step_task_already_terminal_creates_no_second_event(repo, task):
    """When a concurrent writer already moved the task to a terminal state
    before fail_running_step()'s own UPDATE runs, TaskAlreadyTerminalError
    is raised BEFORE this method's own task_transitions/outbox insert -
    the terminal state that actually won already has its own event from
    whatever produced IT; this call must not create a second, spurious
    one."""

    repo.claim_step(task.task_id, 1)
    cancelled = repo.mark_cancelled(task.task_id, TaskState.RUNNING)
    winning_event = repo.get_outbox_event_for_task_version(task.task_id, cancelled.version)
    assert winning_event is not None

    with pytest.raises(TaskAlreadyTerminalError):
        repo.fail_running_step(task.task_id, 1, "failed", "too late")

    # Still exactly the one event the winning CANCELLED transition created -
    # fail_running_step()'s own failed attempt never got far enough to
    # insert a second, spurious one.
    assert repo.get_outbox_event_for_task_version(task.task_id, cancelled.version) == winning_event


def test_historical_confirmation_event_survives_being_replaced_by_a_later_round(repo, task):
    """The central reconstruction-safety proof (Milestone 47 design): round
    1's own confirmation_required event must remain fully intact and
    correctly rendered even after its pending row is consumed and a
    SECOND, DIFFERENT confirmation round is proposed for the same task -
    never silently overwritten, never reinterpreted as round 2's data."""

    round1 = repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)
    pending1 = repo.get_pending_confirmation(task.task_id)
    round1_version = round1.version

    repo.consume_confirmation_and_claim_step(
        task.task_id, pending1.confirmation_id, 1, "repository_backup", "ai_os"
    )
    repo.mark_step_succeeded(task.task_id, 1, '{"ok": true}')

    # A fresh RUNNING task with a second sensitive step proposes a second,
    # DIFFERENT confirmation round.
    round2 = repo.propose_confirmation(task.task_id, 2, "open_application", "notepad", ttl_seconds=120)
    pending2 = repo.get_pending_confirmation(task.task_id)
    assert pending2.confirmation_id != pending1.confirmation_id

    # Round 1's historical event is untouched by round 2 ever happening.
    event1 = repo.get_outbox_event_for_task_version(task.task_id, round1_version)
    payload1 = deserialize_confirmation_required_payload(event1.payload_json)
    assert payload1.confirmation_id == pending1.confirmation_id
    assert payload1.action_name == "repository_backup"
    assert payload1.resource_key == "ai_os"

    event2 = repo.get_outbox_event_for_task_version(task.task_id, round2.version)
    payload2 = deserialize_confirmation_required_payload(event2.payload_json)
    assert payload2.confirmation_id == pending2.confirmation_id
    assert payload2.action_name == "open_application"
    assert payload2.resource_key == "notepad"

    assert event1.event_id != event2.event_id
    assert event1.transition_id != event2.transition_id


def test_outbox_insert_failure_rolls_back_the_whole_confirmation_proposal(repo, task, monkeypatch):
    """The atomicity invariant, adversarially proven: if the outbox insert
    step itself fails (here, forcing serialize_confirmation_required_payload()
    to raise), NOTHING commits - no state transition, no task_pending_confirmation
    row, no task_transitions row, no outbox row. Either everything commits
    together, or nothing does."""

    import kernel.employee_tasks.repository as repository_module

    def _raising_serializer(payload):
        raise LifecycleEventPayloadError("forced failure for this test")

    monkeypatch.setattr(repository_module, "serialize_confirmation_required_payload", _raising_serializer)

    with pytest.raises(LifecycleEventPayloadError):
        repo.propose_confirmation(task.task_id, 1, "repository_backup", "ai_os", ttl_seconds=120)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.RUNNING  # never moved to WAITING_FOR_CONFIRMATION
    assert reloaded.version == task.version  # no version bump at all
    assert repo.get_pending_confirmation(task.task_id) is None
    assert repo.get_step_progress(task.task_id, 1) is None  # never claimed either
