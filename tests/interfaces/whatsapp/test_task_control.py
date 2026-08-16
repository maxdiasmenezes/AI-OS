"""
Tests for interfaces/whatsapp/task_control.py (Milestone 46 P1).

Every test uses a tmp_path SQLite database (never storage/tasks/) and a
fake ModelProvider (never a real Ollama call) - matching
tests/kernel/task_orchestration/test_service.py's own discipline, since
dispatch_planning() delegates directly to advance_task_planning().
"""

import json
import sqlite3

import pytest

from kernel.employee_tasks import (
    MAX_DEDUP_KEY_CHARS,
    TaskRepository,
    TaskState,
    TaskStorageUnavailableError,
    open_writer_connection,
)
from kernel.models.base import ModelRequestOptions, ModelResponse
from kernel.task_planner import build_catalog
from kernel.tools.config import RepoBackupSpec, ToolsConfig
from kernel.tools.registry import ActionRegistry

from interfaces.whatsapp.task_control import (
    TASK_HELP_TEXT,
    DurableAcceptanceFailed,
    TaskExecutionWork,
    TaskFixedReply,
    TaskRequestText,
    accept_task_message,
    classify_task_text,
    compute_dedup_key,
    dispatch_planning,
    needs_dispatch,
)


class _FakeModelProvider:
    """Records every call it received and returns canned responses in
    order - never makes a real network call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, ModelRequestOptions | None]] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append((prompt, options))
        text = self._responses.pop(0)
        return ModelResponse(
            text=text, model="fake", input_tokens=0, output_tokens=0, latency_seconds=0.0
        )


class _ClosingSpyConnection:
    """Wraps a real sqlite3.Connection, recording whether/how many times
    close() was called - everything else is delegated unchanged."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "tasks.sqlite3"


@pytest.fixture
def repo(db_path):
    conn = open_writer_connection(db_path)
    yield TaskRepository(conn)
    conn.close()


@pytest.fixture
def catalog():
    tools_config = ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": object()},
        approved_scripts={},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )
    return build_catalog(ActionRegistry(), tools_config)


def _valid_plan_raw(catalog, objective="Check repository health."):
    entry = next(e for e in catalog if e.action_name == "repo_health")
    return json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": objective,
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": entry.catalog_id,
                    "description": "Check repo health.",
                    "expected_result": "Health known.",
                    "depends_on": [],
                }
            ],
        }
    )


def _cannot_plan_raw():
    return json.dumps({"plan_version": 1, "result": "cannot_plan"})


# --- classify_task_text(): pure, no I/O -------------------------------------


def test_not_a_task_command_returns_none():
    assert classify_task_text("hello") is None
    assert classify_task_text("what wine goes with steak?") is None
    assert classify_task_text("") is None


def test_lookalike_prefixes_are_not_task_commands():
    # "/tasks" and "/taskx" must never match - only an exact "/task" token,
    # followed by whitespace or end of string.
    assert classify_task_text("/tasks status") is None
    assert classify_task_text("/taskx") is None


def test_bare_task_is_help():
    result = classify_task_text("/task")
    assert result == TaskFixedReply(TASK_HELP_TEXT)


def test_task_with_only_whitespace_after_is_help():
    result = classify_task_text("/task    ")
    assert result == TaskFixedReply(TASK_HELP_TEXT)


def test_task_help_case_insensitive():
    for text in ("/task help", "/task HELP", "/task Help", "/TASK help"):
        assert classify_task_text(text) == TaskFixedReply(TASK_HELP_TEXT)


def test_legacy_confirm_and_cancel_get_migration_reply():
    confirm = classify_task_text("/task confirm")
    cancel = classify_task_text("/task cancel")
    assert isinstance(confirm, TaskFixedReply)
    assert isinstance(cancel, TaskFixedReply)
    assert confirm == cancel  # same fixed migration text
    assert "CONFIRM" in confirm.reply_text
    assert "REJECT" in confirm.reply_text
    # Never the old help text, and never a TaskRequestText.
    assert confirm != TaskFixedReply(TASK_HELP_TEXT)


def test_legacy_confirm_cancel_case_insensitive():
    assert isinstance(classify_task_text("/task CONFIRM"), TaskFixedReply)
    assert isinstance(classify_task_text("/task Cancel"), TaskFixedReply)


def test_confirm_or_cancel_with_extra_text_is_natural_language():
    # Exact-match only - "confirm now" is not the bare legacy verb.
    result = classify_task_text("/task confirm now")
    assert result == TaskRequestText("confirm now")

    result2 = classify_task_text("/task cancel my flight")
    assert result2 == TaskRequestText("cancel my flight")


def test_ordinary_task_request_becomes_request_text():
    result = classify_task_text("/task open notepad")
    assert result == TaskRequestText("open notepad")


def test_task_request_whitespace_between_prefix_and_text_is_stripped():
    result = classify_task_text("/task    open notepad")
    assert result == TaskRequestText("open notepad")


def test_task_prefix_case_insensitive_for_request_text():
    result = classify_task_text("/TASK open notepad")
    assert result == TaskRequestText("open notepad")


def test_task_request_imposes_no_internal_length_bound():
    # Milestone 46 adversarial review (M1/dead-code cleanup): classify_task_text()
    # no longer enforces its own length bound - the caller
    # (handler.py:classify_message()) already bounds the whole message
    # before this function is ever called, and TaskRepository.create_task()
    # independently re-validates request_text length regardless. A very
    # long remainder is passed through unchanged, not rejected here.
    text = "x" * 50_000
    result = classify_task_text(f"/task {text}")
    assert result == TaskRequestText(text)


# --- compute_dedup_key(): deterministic, namespaced, bounded ----------------


def test_dedup_key_is_deterministic():
    assert compute_dedup_key("wamid.ABC123") == compute_dedup_key("wamid.ABC123")


def test_dedup_key_differs_for_different_ids():
    assert compute_dedup_key("wamid.ABC123") != compute_dedup_key("wamid.XYZ789")


def test_dedup_key_format():
    key = compute_dedup_key("wamid.ABC123")
    assert key.startswith("whatsapp:")
    digest = key[len("whatsapp:"):]
    assert len(digest) == 64
    int(digest, 16)  # valid hex
    assert len(key) == 73


def test_dedup_key_never_contains_raw_provider_id():
    key = compute_dedup_key("wamid.super-secret-looking-id")
    assert "wamid" not in key
    assert "super-secret-looking-id" not in key


def test_dedup_key_empty_id_rejected():
    with pytest.raises(DurableAcceptanceFailed):
        compute_dedup_key("")


def test_dedup_key_imposes_no_provider_id_length_bound():
    # Milestone 46 adversarial review (M1): no Meta provider-ID length
    # contract exists anywhere in this repository, so compute_dedup_key()
    # must never reject a long provider message ID merely on length - a
    # very long ID is still accepted and hashed, never rejected.
    key = compute_dedup_key("x" * 50_000)
    assert key.startswith("whatsapp:")


def test_dedup_key_result_always_within_repository_bound():
    # The fixed-length SHA-256 digest output - not a length check on the
    # input - is what keeps this safely within TaskRecord.dedup_key's
    # bound (kernel.employee_tasks.MAX_DEDUP_KEY_CHARS) regardless of how
    # long the real-world provider message ID turns out to be.
    key = compute_dedup_key("x" * 50_000)
    assert len(key) <= MAX_DEDUP_KEY_CHARS
    assert len(key) == 73


# --- accept_task_message(): durable create-or-find --------------------------


def test_accept_task_message_creates_new_task(db_path):
    record = accept_task_message(open_writer_connection, db_path, "check status", "wamid.1")
    assert record.state == TaskState.CREATED
    assert record.source == "whatsapp"
    assert record.request_text == "check status"
    assert record.dedup_key == compute_dedup_key("wamid.1")


def test_accept_task_message_duplicate_resolves_same_task(db_path):
    first = accept_task_message(open_writer_connection, db_path, "check status", "wamid.1")
    second = accept_task_message(open_writer_connection, db_path, "different text!", "wamid.1")

    assert second.task_id == first.task_id
    # The original request_text is untouched by the "duplicate" call.
    assert second.request_text == "check status"

    conn = open_writer_connection(db_path)
    try:
        repo = TaskRepository(conn)
        assert len(repo.list_tasks(limit=100)) == 1
    finally:
        conn.close()


def test_accept_task_message_different_ids_create_different_tasks(db_path):
    first = accept_task_message(open_writer_connection, db_path, "task one", "wamid.1")
    second = accept_task_message(open_writer_connection, db_path, "task two", "wamid.2")
    assert first.task_id != second.task_id


def test_accept_task_message_long_provider_id_is_accepted_not_rejected(db_path):
    # Milestone 46 adversarial review (M1): a long provider message ID must
    # never be treated as a validation failure - only its fixed-length
    # digest is persisted (see compute_dedup_key()), so durable acceptance
    # succeeds regardless of the real-world ID's length.
    long_id = "wamid." + "x" * 10_000
    record = accept_task_message(open_writer_connection, db_path, "check status", long_id)
    assert record.state == TaskState.CREATED
    assert record.dedup_key == compute_dedup_key(long_id)


def test_accept_task_message_opener_raising_task_storage_error_fails_closed(db_path):
    def failing_opener(_db_path):
        raise TaskStorageUnavailableError("simulated failure")

    with pytest.raises(DurableAcceptanceFailed):
        accept_task_message(failing_opener, db_path, "check status", "wamid.1")


def test_accept_task_message_opener_raising_raw_sqlite_error_fails_closed(db_path):
    # Defense-in-depth path: kernel/employee_tasks/db.py's own documented
    # concurrent-first-open caveat can leak a raw sqlite3.OperationalError
    # rather than the package's own TaskStorageError - accept_task_message()
    # must still fail closed, never propagate it raw.
    def failing_opener(_db_path):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(DurableAcceptanceFailed):
        accept_task_message(failing_opener, db_path, "check status", "wamid.1")


def test_accept_task_message_closes_connection_on_success(db_path):
    spies = []

    def spying_opener(path):
        conn = open_writer_connection(path)
        spy = _ClosingSpyConnection(conn)
        spies.append(spy)
        return spy

    accept_task_message(spying_opener, db_path, "check status", "wamid.1")
    assert len(spies) == 1
    assert spies[0].close_calls == 1


def test_accept_task_message_closes_connection_on_duplicate(db_path):
    accept_task_message(open_writer_connection, db_path, "check status", "wamid.1")

    spies = []

    def spying_opener(path):
        conn = open_writer_connection(path)
        spy = _ClosingSpyConnection(conn)
        spies.append(spy)
        return spy

    accept_task_message(spying_opener, db_path, "check status", "wamid.1")
    assert spies[0].close_calls == 1


def test_accept_task_message_closes_connection_on_storage_failure(db_path, monkeypatch):
    import kernel.employee_tasks.repository as repository_module

    def raise_storage_error(self, *args, **kwargs):
        raise TaskStorageUnavailableError("simulated failure mid-call")

    monkeypatch.setattr(repository_module.TaskRepository, "create_task", raise_storage_error)

    spies = []

    def spying_opener(path):
        conn = open_writer_connection(path)
        spy = _ClosingSpyConnection(conn)
        spies.append(spy)
        return spy

    with pytest.raises(DurableAcceptanceFailed):
        accept_task_message(spying_opener, db_path, "check status", "wamid.1")

    assert len(spies) == 1
    assert spies[0].close_calls == 1


def test_accept_task_message_never_exposes_raw_exception_text(db_path):
    def failing_opener(_db_path):
        raise sqlite3.OperationalError("database is locked at /some/secret/path")

    try:
        accept_task_message(failing_opener, db_path, "check status", "wamid.1")
    except DurableAcceptanceFailed as exc:
        assert "/some/secret/path" not in str(exc)
        assert "database is locked" not in str(exc)


# --- needs_dispatch(): TaskState -> dispatch decision ------------------------


def test_needs_dispatch_true_only_for_created(repo):
    created = repo.create_task("request", "whatsapp")
    assert needs_dispatch(created) is True


@pytest.mark.parametrize(
    "state",
    [
        TaskState.PLANNING,
        TaskState.READY,
        TaskState.RUNNING,
        TaskState.WAITING_FOR_CONFIRMATION,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    ],
)
def test_needs_dispatch_false_for_every_other_state(repo, state):
    from dataclasses import replace

    created = repo.create_task("request", "whatsapp")
    fake_record = replace(created, state=state)
    assert needs_dispatch(fake_record) is False


# --- dispatch_planning(): the CREATED -> planning handoff, P1's only ---------
# --- execution boundary -----------------------------------------------------


def test_dispatch_planning_advances_created_task(repo, catalog):
    task = repo.create_task("Check repository health.", "whatsapp")
    provider = _FakeModelProvider([_valid_plan_raw(catalog)])

    dispatch_planning(repo, task.task_id, catalog, provider)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.READY
    assert len(provider.calls) == 1


def test_dispatch_planning_records_failure_for_unplannable_request(repo, catalog):
    task = repo.create_task("Do something impossible.", "whatsapp")
    provider = _FakeModelProvider([_cannot_plan_raw()])

    dispatch_planning(repo, task.task_id, catalog, provider)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED


@pytest.mark.parametrize(
    "state",
    [
        TaskState.READY,
        TaskState.RUNNING,
        TaskState.WAITING_FOR_CONFIRMATION,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    ],
)
def test_dispatch_planning_no_ops_for_non_created_state(repo, catalog, state, monkeypatch):
    task = repo.create_task("Check repository health.", "whatsapp")
    provider = _FakeModelProvider([_valid_plan_raw(catalog)])

    # Force the persisted state without going through the real lifecycle -
    # only needs_dispatch()/dispatch_planning()'s own state-gating is under
    # test here, not how the state was reached.
    conn = repo._conn
    conn.execute("UPDATE tasks SET state = ? WHERE task_id = ?", (state.value, task.task_id))
    conn.execute(
        "INSERT INTO task_transitions (task_id, from_state, to_state, timestamp, task_version) "
        "VALUES (?, 'created', ?, datetime('now'), 1)",
        (task.task_id, state.value),
    )

    dispatch_planning(repo, task.task_id, catalog, provider)

    assert provider.calls == []
    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == state


def test_dispatch_planning_duplicate_dispatch_only_plans_once(repo, catalog):
    task = repo.create_task("Check repository health.", "whatsapp")
    provider = _FakeModelProvider([_valid_plan_raw(catalog)])

    dispatch_planning(repo, task.task_id, catalog, provider)
    assert len(provider.calls) == 1

    # A second dispatch of the same task_id (e.g. a duplicate work item)
    # reloads the now-READY state and must not plan again.
    dispatch_planning(repo, task.task_id, catalog, provider)
    assert len(provider.calls) == 1

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.READY


def test_dispatch_planning_swallows_task_not_in_created_state_error(repo, catalog, monkeypatch):
    """A direct unit test of the except-clause itself: even though the
    real single-threaded WhatsApp worker can never reach this branch
    today (dispatch_planning()'s own pre-check already prevents it - see
    this module's own module docstring), the catch is real defense in
    depth and must behave exactly as documented: swallow it silently,
    call nothing else, never raise to the caller."""

    import interfaces.whatsapp.task_control as task_control_module
    from kernel.task_orchestration import TaskNotInCreatedStateError

    task = repo.create_task("Check repository health.", "whatsapp")
    provider = _FakeModelProvider([_valid_plan_raw(catalog)])

    def raise_race(*args, **kwargs):
        raise TaskNotInCreatedStateError("simulated race")

    monkeypatch.setattr(task_control_module, "advance_task_planning", raise_race)

    dispatch_planning(repo, task.task_id, catalog, provider)  # must not raise

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.CREATED  # untouched - the fake never really ran


def test_dispatch_planning_does_not_swallow_unrelated_errors(repo, catalog, monkeypatch):
    import interfaces.whatsapp.task_control as task_control_module

    task = repo.create_task("Check repository health.", "whatsapp")
    provider = _FakeModelProvider([_valid_plan_raw(catalog)])

    def raise_something_else(*args, **kwargs):
        raise RuntimeError("a genuine defect, not a race")

    monkeypatch.setattr(task_control_module, "advance_task_planning", raise_something_else)

    with pytest.raises(RuntimeError):
        dispatch_planning(repo, task.task_id, catalog, provider)


# --- TaskExecutionWork: minimal, trusted correlation only --------------------


def test_task_execution_work_carries_only_task_id():
    work = TaskExecutionWork(task_id="abc-123")
    assert work.task_id == "abc-123"
    # Frozen dataclass - no other fields exist to accidentally carry raw
    # text, a provider message ID, or a sender.
    assert work.__dataclass_fields__.keys() == {"task_id"}
