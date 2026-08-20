"""
Tests for interfaces/whatsapp/task_control.py (Milestone 46 P1 + P2A + P2B).

Every test uses a tmp_path SQLite database (never storage/tasks/). Planning
tests use a fake structured-planner ModelProvider; execution tests use the
REAL kernel.task_execution.run_task_until_blocked()/SafeTaskExecutor, with
either a real, safe, non-destructive action (list_files against a tmp_path
directory) or a fake RESPOND-only conversational ModelProvider - never a
real network call, never a real sensitive action (open_application etc. is
always proven to stop at WAITING_FOR_CONFIRMATION before SafeTaskExecutor
is ever reached, matching tests/kernel/task_execution/test_milestone_43_p1_e2e.py's
own "real pipeline, no real external effect" discipline). P2B confirmation
tests reuse the same real repository/registry/executor discipline - a
sensitive action never executes until a genuine approve_task_confirmation()
call, proven the same way P2A's own confirmation-gate tests already do.
"""

import itertools
import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kernel.employee_tasks import (
    MAX_DEDUP_KEY_CHARS,
    LifecycleEventKind,
    StepAlreadyClaimedError,
    StepStatus,
    TaskRepository,
    TaskState,
    TaskStorageUnavailableError,
    open_writer_connection,
)
from kernel.models.base import ModelRequestOptions, ModelResponse
from kernel.task_execution.observation import (
    build_action_observation,
    build_respond_observation,
    serialize_observation,
)
from kernel.task_planner import PlanStep, StepKind, TaskPlan, build_catalog, serialize_plan
from kernel.tools.config import RepoBackupSpec, ToolsConfig
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest, ActionResult

from interfaces.whatsapp.client import WhatsAppClientError
from interfaces.whatsapp.task_control import (
    TASK_CANCELLED_TEXT,
    TASK_COMPLETED_FALLBACK_TEXT,
    TASK_FAILED_FALLBACK_TEXT,
    TASK_HELP_TEXT,
    TASK_SOURCE,
    ConfirmationCommandText,
    ConfirmationDecision,
    ConfirmationDecisionOutcome,
    ConfirmationDecisionRecordingFailed,
    ConfirmationFixedReply,
    DurableAcceptanceFailed,
    TaskConfirmationWork,
    TaskExecutionWork,
    TaskFixedReply,
    TaskRequestText,
    GENERIC_INVALID_CONFIRMATION_TEXT,
    _MALFORMED_OUTBOX_PAYLOAD_TEXT,
    _RESTART_PLANNING_INTERRUPTED_REASON_CODE,
    accept_task_message,
    classify_confirmation_text,
    classify_task_text,
    compute_dedup_key,
    dispatch_confirmation_work,
    dispatch_task_work,
    needs_dispatch,
    record_confirmation_decision_durably,
    run_confirmation_decision_recovery_checkpoint,
    run_outbound_lifecycle_recovery_checkpoint,
    run_task_state_recovery_checkpoint,
    select_terminal_result_text,
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
def downloads_dir(tmp_path):
    directory = tmp_path / "downloads"
    directory.mkdir()
    (directory / "report.txt").write_text("hello", encoding="utf-8")
    return directory


@pytest.fixture
def catalog(downloads_dir):
    # References a real, safe, non-sensitive, non-destructive action
    # (list_files against a tmp_path directory) rather than repo_health -
    # P2A's dispatch_task_work() continues straight from a fresh plan into
    # REAL execution, so planning-focused tests need an action that can
    # genuinely execute safely, not just be referenced in a fake plan.
    tools_config = ToolsConfig(
        approved_directories={"downloads": str(downloads_dir)},
        approved_applications={"notepad": object()},
        approved_scripts={},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )
    return build_catalog(ActionRegistry(), tools_config)


def _valid_plan_raw(catalog, objective="List the downloads folder."):
    entry = next(e for e in catalog if e.action_name == "list_files" and e.resource_key == "downloads")
    return json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": objective,
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": entry.catalog_id,
                    "description": "List the downloads folder.",
                    "expected_result": "Files known.",
                    "depends_on": [],
                }
            ],
        }
    )


def _cannot_plan_raw():
    return json.dumps({"plan_version": 1, "result": "cannot_plan"})


# --- P2A execution/delivery test helpers ------------------------------------


class RecordingClient:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text_message(self, to, body):
        self.sent.append((to, body))
        return "wamid.OUT1"


class FailingClient:
    def send_text_message(self, to, body):
        raise WhatsAppClientError("boom")


class _NeverCalledExecutor:
    """Proves SafeTaskExecutor.execute() is structurally never reached for
    a sensitive step that should stop at CONFIRMATION_REQUIRED instead -
    mirrors tests/kernel/task_execution/test_milestone_43_p1_e2e.py's own
    _FakeExecutor pattern."""

    def __init__(self):
        self.calls: list[ActionRequest] = []

    def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request)
        raise AssertionError("SafeTaskExecutor must never be called for a step awaiting confirmation")


class _FakeSuccessExecutor:
    """Records every ActionRequest it receives and always reports success -
    used for P2B CONFIRM tests that need a sensitive action to genuinely
    reach and pass through the execution boundary (proving exactly-once
    invocation, revalidation, and continuation) without ever running a
    real computer action (no real Notepad launch, no real script)."""

    def __init__(self):
        self.calls: list[ActionRequest] = []

    def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request)
        return ActionResult(True, f"'{request.resource_key}' launched.", "executed")


_AUTHORIZED_SENDER = "15551234567"


def _tools_config(*, approved_directories=None, approved_applications=None):
    return ToolsConfig(
        approved_directories=approved_directories or {},
        approved_applications=approved_applications or {},
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
    )


def _action_step(position, action_name, resource_key, *, depends_on=()):
    return PlanStep(
        step_id=f"step_{position}",
        position=position,
        kind=StepKind.ACTION,
        action_name=action_name,
        resource_key=resource_key,
        catalog_id=f"action_{position}",
        description="do the thing",
        expected_result="the thing is done",
        depends_on=tuple(depends_on),
        requires_confirmation=False,
    )


def _respond_step(position, *, depends_on=()):
    return PlanStep(
        step_id=f"step_{position}",
        position=position,
        kind=StepKind.RESPOND,
        action_name=None,
        resource_key=None,
        catalog_id=None,
        description="summarize",
        expected_result="a summary",
        depends_on=tuple(depends_on),
        requires_confirmation=False,
    )


def _ready_task(repo, steps, request_text="do the plan"):
    """Builds a task straight into READY with a directly-constructed,
    hand-built TaskPlan - bypassing the planner entirely, exactly like
    tests/kernel/task_execution/test_milestone_43_p1_e2e.py's own
    _ready_task() helper. Used by every execution-focused test below, so
    execution behavior is tested independently of planning behavior."""

    record = repo.create_task(request_text, "whatsapp")
    repo.transition_task(record.task_id, TaskState.CREATED, TaskState.PLANNING)
    plan = TaskPlan(
        plan_version=1,
        task_id=record.task_id,
        objective=request_text,
        steps=tuple(steps),
        created_at="2026-08-08T00:00:00+00:00",
    )
    return repo.persist_plan_and_ready(record.task_id, TaskState.PLANNING, serialize_plan(plan))


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


# --- dispatch_task_work(): P2A's CREATED -> planning -> execution -> --------
# --- delivery flow ------------------------------------------------------


def _dispatch(
    repo,
    task_id,
    catalog,
    planner_provider,
    *,
    registry=None,
    tools_config_loader=None,
    respond_provider=None,
    client=None,
    authorized_sender=_AUTHORIZED_SENDER,
):
    return dispatch_task_work(
        repo,
        task_id,
        catalog,
        planner_provider,
        registry if registry is not None else ActionRegistry(),
        tools_config_loader if tools_config_loader is not None else _tools_config,
        respond_provider if respond_provider is not None else _FakeModelProvider([]),
        client if client is not None else RecordingClient(),
        authorized_sender,
    )


def test_dispatch_task_work_plans_and_executes_in_one_call(repo, catalog, downloads_dir):
    task = repo.create_task("Check the downloads folder.", "whatsapp")
    planner = _FakeModelProvider([_valid_plan_raw(catalog)])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    _dispatch(repo, task.task_id, catalog, planner, tools_config_loader=loader, client=client)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.COMPLETED
    assert len(planner.calls) == 1
    assert len(client.sent) == 1
    assert client.sent[0][0] == _AUTHORIZED_SENDER


def test_dispatch_task_work_delivers_failure_for_freshly_produced_planning_failure(repo, catalog):
    # Milestone 46 P2A design correction: a durable CREATED -> FAILED
    # transition produced by planning must be delivered, exactly once -
    # not silently dropped.
    task = repo.create_task("Do something impossible.", "whatsapp")
    planner = _FakeModelProvider([_cannot_plan_raw()])
    client = RecordingClient()

    _dispatch(repo, task.task_id, catalog, planner, client=client)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert len(client.sent) == 1
    assert client.sent[0][0] == _AUTHORIZED_SENDER
    assert client.sent[0][1].startswith("Task failed:")
    assert reloaded.failure_summary in client.sent[0][1]


def test_dispatch_task_work_duplicate_dispatch_of_planning_failure_sends_no_second_message(repo, catalog):
    task = repo.create_task("Do something impossible.", "whatsapp")
    planner = _FakeModelProvider([_cannot_plan_raw()])
    client = RecordingClient()

    _dispatch(repo, task.task_id, catalog, planner, client=client)
    assert len(client.sent) == 1

    # A duplicate TaskExecutionWork for the same, now-FAILED task_id.
    _dispatch(repo, task.task_id, catalog, planner, client=client)
    assert len(client.sent) == 1
    assert repo.get_task(task.task_id).state == TaskState.FAILED


@pytest.mark.parametrize(
    "state",
    [
        TaskState.WAITING_FOR_CONFIRMATION,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    ],
)
def test_dispatch_task_work_no_ops_for_already_waiting_or_terminal_state(repo, catalog, state):
    task = repo.create_task("Check the downloads folder.", "whatsapp")
    planner = _FakeModelProvider([_valid_plan_raw(catalog)])
    client = RecordingClient()

    # Force the persisted state directly - only dispatch_task_work()'s own
    # state-gating is under test here, not how the state was reached.
    conn = repo._conn
    conn.execute("UPDATE tasks SET state = ? WHERE task_id = ?", (state.value, task.task_id))
    conn.execute(
        "INSERT INTO task_transitions (task_id, from_state, to_state, timestamp, task_version) "
        "VALUES (?, 'created', ?, datetime('now'), 1)",
        (task.task_id, state.value),
    )

    _dispatch(repo, task.task_id, catalog, planner, client=client)

    assert planner.calls == []
    assert client.sent == []
    assert repo.get_task(task.task_id).state == state


def test_dispatch_task_work_swallows_task_not_in_created_state_error(repo, catalog, monkeypatch):
    """A direct unit test of the except-clause itself: even though the
    real single-threaded WhatsApp worker can never reach this branch today
    (dispatch_task_work()'s own pre-check already prevents it), the catch
    is real defense in depth and must behave exactly as documented:
    swallow it silently, send nothing, never raise to the caller."""

    import interfaces.whatsapp.task_control as task_control_module
    from kernel.task_orchestration import TaskNotInCreatedStateError

    task = repo.create_task("Check the downloads folder.", "whatsapp")
    planner = _FakeModelProvider([_valid_plan_raw(catalog)])
    client = RecordingClient()

    def raise_race(*args, **kwargs):
        raise TaskNotInCreatedStateError("simulated race")

    monkeypatch.setattr(task_control_module, "advance_task_planning", raise_race)

    _dispatch(repo, task.task_id, catalog, planner, client=client)  # must not raise

    assert client.sent == []
    assert repo.get_task(task.task_id).state == TaskState.CREATED  # untouched


def test_dispatch_task_work_does_not_swallow_unrelated_planning_errors(repo, catalog, monkeypatch):
    import interfaces.whatsapp.task_control as task_control_module

    task = repo.create_task("Check the downloads folder.", "whatsapp")
    planner = _FakeModelProvider([_valid_plan_raw(catalog)])

    def raise_something_else(*args, **kwargs):
        raise RuntimeError("a genuine defect, not a race")

    monkeypatch.setattr(task_control_module, "advance_task_planning", raise_something_else)

    with pytest.raises(RuntimeError):
        _dispatch(repo, task.task_id, catalog, planner)


# --- dispatch_task_work(): non-sensitive execution end-to-end ---------------


def test_non_sensitive_task_completes_and_delivers_one_result(repo, downloads_dir):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.COMPLETED
    assert len(client.sent) == 1
    assert client.sent[0][0] == _AUTHORIZED_SENDER
    assert "report.txt" in client.sent[0][1]


def test_non_sensitive_task_duplicate_dispatch_sends_no_second_result(repo, downloads_dir):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)
    assert len(client.sent) == 1

    # Duplicate TaskExecutionWork for the same, now-COMPLETED task_id.
    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)
    assert len(client.sent) == 1
    assert repo.get_task(task.task_id).state == TaskState.COMPLETED


def test_oversized_action_result_still_completes_and_delivers_bounded_result(repo, tmp_path):
    # Milestone 46 adversarial review, H1: a real, non-sensitive action can
    # execute successfully but produce an ActionResult.message too large to
    # persist verbatim (kernel/task_execution/service.py's
    # _finalize_action_step() now falls back to a compact, code-owned
    # observation rather than letting ObservationSerializationError escape
    # uncaught - see that function's own docstring). This is the full P2A
    # production path end to end: dispatch_task_work() must still complete
    # the task and deliver exactly one bounded WhatsApp result, never leave
    # it silently stuck RUNNING with the step stuck IN_PROGRESS.
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    for i in range(100):
        name = f"Quarterly_Financial_Report_Draft_Review_Comments_Attached_{i:03d}.pdf"
        (downloads_dir / name).write_text("x")

    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.COMPLETED
    assert len(client.sent) == 1
    assert client.sent[0][0] == _AUTHORIZED_SENDER
    assert len(client.sent[0][1]) <= 4096  # never sends raw, unbounded handler output

    # A duplicate TaskExecutionWork for the same, now-COMPLETED task_id must
    # not re-execute the action or resend the result.
    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)
    assert len(client.sent) == 1
    assert repo.get_task(task.task_id).state == TaskState.COMPLETED


def test_execution_failure_delivers_one_bounded_failure_message(repo):
    # "downloads" IS registered (passes revalidation), but points at a
    # path that does not actually exist - list_files.run() itself fails
    # deterministically (ActionResult(False, "That directory is not
    # available.", "failed")), a real, safe, non-destructive handler-level
    # failure - distinct from the revalidation failure covered by
    # test_fresh_config_revalidation_fails_closed_when_resource_removed.
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": "C:/does/not/exist/xyz"})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert len(client.sent) == 1
    assert client.sent[0][1].startswith("Task failed:")
    assert "not available" in client.sent[0][1]


def test_execution_failure_duplicate_dispatch_sends_no_second_message(repo):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": "C:/does/not/exist/xyz"})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)
    assert len(client.sent) == 1

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)
    assert len(client.sent) == 1


# --- dispatch_task_work(): sensitive action -> confirmation request --------


def test_sensitive_task_stops_at_confirmation_and_delivers_one_request(repo):
    task = _ready_task(repo, [_action_step(1, "open_application", "notepad")])
    client = RecordingClient()
    executor_calls = _NeverCalledExecutor()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    import interfaces.whatsapp.task_control as task_control_module

    real_safe_task_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
                  tools_config_loader=loader, client=client)
    finally:
        task_control_module.SafeTaskExecutor = real_safe_task_executor

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.WAITING_FOR_CONFIRMATION
    assert executor_calls.calls == []  # no real/sensitive side effect occurred

    assert len(client.sent) == 1
    recipient, body = client.sent[0]
    assert recipient == _AUTHORIZED_SENDER
    assert "Action: open_application" in body
    assert "Resource: notepad" in body

    pending = repo.get_pending_confirmation(task.task_id)
    assert pending is not None
    assert f"CONFIRM {pending.confirmation_id}" in body
    assert f"REJECT {pending.confirmation_id}" in body
    # Never a raw path, secret, tool argument, or internal DB row.
    assert "step_position" not in body.lower()


def test_sensitive_task_duplicate_dispatch_does_not_resend_or_execute(repo):
    task = _ready_task(repo, [_action_step(1, "open_application", "notepad")])
    client = RecordingClient()
    executor_calls = _NeverCalledExecutor()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    import interfaces.whatsapp.task_control as task_control_module

    real_safe_task_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
                  tools_config_loader=loader, client=client)
        assert len(client.sent) == 1

        # Duplicate TaskExecutionWork for the same, now-WAITING_FOR_CONFIRMATION task_id.
        _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
                  tools_config_loader=loader, client=client)
    finally:
        task_control_module.SafeTaskExecutor = real_safe_task_executor

    assert len(client.sent) == 1  # no resend
    assert executor_calls.calls == []  # still never executed
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


def test_missing_pending_confirmation_fails_safe_without_fabrication(repo, monkeypatch):
    """Defensive path (Milestone 47 P1): a deliverable
    ExecutionAdvanceStatus structurally implies TaskRepository already
    atomically created the matching task_lifecycle_outbox row, but this is
    never assumed - if get_outbox_event_for_task_version() somehow returns
    None, no token/text is fabricated, nothing executes, and a bounded
    generic message is sent instead."""

    task = _ready_task(repo, [_action_step(1, "open_application", "notepad")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    monkeypatch.setattr(
        TaskRepository, "get_outbox_event_for_task_version", lambda self, task_id, task_version: None
    )

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)

    assert len(client.sent) == 1
    assert "CONFIRM" not in client.sent[0][1]
    assert "REJECT" not in client.sent[0][1]
    # The durable state the execution engine already produced is left
    # untouched - no destructive mutation invented here.
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


def test_running_task_with_in_progress_step_fails_closed_never_retried(repo):
    """Milestone 46 adversarial review, M47-boundary pin: a RUNNING task
    whose next step is durably IN_PROGRESS represents genuine crash
    uncertainty (kernel/task_execution/eligibility.py's own
    STEP_IN_PROGRESS doctrine - the step may or may not have actually
    happened). dispatch_task_work() accepting RUNNING as a valid input
    state (alongside READY) must NEVER turn this into automatic retry/
    recovery - that remains exclusively Milestone 47's scope. This seeds
    exactly that crash-artifact shape directly (claim_step() commits the
    IN_PROGRESS row, but nothing ever finalizes it - simulating a process
    that died between claiming the step and executor.execute() returning),
    then proves the existing engine's fail-closed behavior is preserved
    unchanged through P2A's dispatch layer, and that the resulting freshly-
    produced TASK_FAILED is delivered exactly once (transition-triggered
    delivery still applies to this path)."""

    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    task = repo.transition_task(task.task_id, TaskState.READY, TaskState.RUNNING)
    repo.claim_step(task.task_id, 1)  # durably IN_PROGRESS - never finalized

    client = RecordingClient()
    executor_calls = _NeverCalledExecutor()
    loader = lambda: _tools_config(approved_directories={"downloads": "/does/not/matter"})

    import interfaces.whatsapp.task_control as task_control_module

    real_safe_task_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
                  tools_config_loader=loader, client=client)
    finally:
        task_control_module.SafeTaskExecutor = real_safe_task_executor

    assert executor_calls.calls == []  # the uncertain step is never retried

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert reloaded.failure_code == "step_execution_uncertain"

    assert len(client.sent) == 1
    assert client.sent[0][1].startswith("Task failed:")

    # A further duplicate dispatch of the same, now-terminal task must not
    # resend the failure or touch the executor again.
    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)
    assert len(client.sent) == 1
    assert executor_calls.calls == []


def test_confirmation_message_worst_case_size_fits_the_outbound_bound_intact(repo):
    """Milestone 46 adversarial review, §19: pins the confirmation
    message's true worst-case size explicitly, using the actual persisted-
    field bounds (MAX_CONFIRMATION_ACTION_NAME_CHARS/
    MAX_CONFIRMATION_RESOURCE_KEY_CHARS/MAX_CONFIRMATION_ID_CHARS - all
    128), rather than relying on realistic values happening to fit. The
    confirmation_id must always appear intact - never truncated - since it
    is the authority-bearing token CONFIRM/REJECT will need."""

    from kernel.employee_tasks import (
        MAX_CONFIRMATION_ACTION_NAME_CHARS,
        MAX_CONFIRMATION_ID_CHARS,
        MAX_CONFIRMATION_RESOURCE_KEY_CHARS,
    )
    from kernel.employee_tasks.types import PendingTaskConfirmation

    import interfaces.whatsapp.task_control as task_control_module

    confirmation_id = "c" * MAX_CONFIRMATION_ID_CHARS
    pending = PendingTaskConfirmation(
        task_id="t" * 32,
        confirmation_id=confirmation_id,
        step_position=1,
        action_name="a" * MAX_CONFIRMATION_ACTION_NAME_CHARS,
        resource_key="r" * MAX_CONFIRMATION_RESOURCE_KEY_CHARS,
        created_at="2026-08-08T00:00:00+00:00",
        expires_at="2026-08-08T00:02:00+00:00",
    )

    message = task_control_module._format_confirmation_message(pending)

    assert len(message) <= task_control_module.MAX_OUTGOING_TEXT_LENGTH
    # The complete token appears intact, twice (CONFIRM and REJECT) -
    # never truncated, never a prefix/suffix of it.
    assert f"CONFIRM {confirmation_id}" in message
    assert f"REJECT {confirmation_id}" in message


# --- dispatch_task_work(): fresh execution-time config revalidation --------


def test_fresh_config_revalidation_fails_closed_when_resource_removed(repo, downloads_dir):
    # The plan references "downloads", which was authorized when the plan
    # was built - but the INJECTED tools_config_loader, called fresh by
    # dispatch_task_work() immediately before execution, returns a config
    # that no longer authorizes it. Execution must fail closed through the
    # existing ActionRevalidationFailure path - the real list_files handler
    # must never be reached.
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={})  # "downloads" removed

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert reloaded.failure_code == "action_no_longer_valid"
    assert len(client.sent) == 1
    assert client.sent[0][1].startswith("Task failed:")


def test_fresh_config_revalidation_succeeds_when_resource_still_authorized(repo, downloads_dir):
    # Contrast case: the same plan, but the fresh config DOES still
    # authorize "downloads" - execution proceeds and completes normally,
    # proving the loader is genuinely consulted (not merely ignored).
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)

    assert repo.get_task(task.task_id).state == TaskState.COMPLETED
    assert len(client.sent) == 1


# --- dispatch_task_work(): RESPOND vs. non-RESPOND result selection --------


def test_respond_result_uses_conversational_provider_not_planner_provider(repo, downloads_dir):
    task = _ready_task(
        repo,
        [
            _action_step(1, "list_files", "downloads"),
            _respond_step(2, depends_on=(1,)),
        ],
    )
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})
    planner_provider = _FakeModelProvider([])  # must never be called for RESPOND
    respond_provider = _FakeModelProvider(["Here is your downloads summary."])

    _dispatch(
        repo, task.task_id, catalog=(), planner_provider=planner_provider,
        tools_config_loader=loader, respond_provider=respond_provider, client=client,
    )

    assert repo.get_task(task.task_id).state == TaskState.COMPLETED
    assert planner_provider.calls == []
    assert len(respond_provider.calls) == 1
    assert client.sent == [(_AUTHORIZED_SENDER, "Here is your downloads summary.")]


def test_no_respond_result_uses_last_successful_action_summary(repo, downloads_dir):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)

    assert repo.get_task(task.task_id).state == TaskState.COMPLETED
    assert len(client.sent) == 1
    assert client.sent[0][0] == _AUTHORIZED_SENDER
    assert "report.txt" in client.sent[0][1]


def test_multi_step_result_selection_prefers_last_successful_respond_by_position(repo, downloads_dir):
    task = _ready_task(
        repo,
        [
            _action_step(1, "list_files", "downloads"),
            _respond_step(2, depends_on=(1,)),
            _action_step(3, "list_files", "downloads", depends_on=(2,)),
            _respond_step(4, depends_on=(3,)),
        ],
    )
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})
    respond_provider = _FakeModelProvider(["first summary", "second summary"])

    _dispatch(
        repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
        tools_config_loader=loader, respond_provider=respond_provider, client=client,
    )

    assert repo.get_task(task.task_id).state == TaskState.COMPLETED
    # The LAST successful RESPOND (position 4) wins, not DB row order or an
    # earlier RESPOND at position 2.
    assert client.sent == [(_AUTHORIZED_SENDER, "second summary")]


# --- select_terminal_result_text(): deterministic, position-based ----------


def test_select_terminal_result_text_no_plan_json_returns_fallback(repo):
    task = repo.create_task("request", "whatsapp")
    assert select_terminal_result_text(repo, task) == TASK_COMPLETED_FALLBACK_TEXT


def test_select_terminal_result_text_corrupt_plan_json_returns_fallback(repo):
    task = repo.create_task("request", "whatsapp")
    broken = replace(task, plan_json="not valid plan json")
    assert select_terminal_result_text(repo, broken) == TASK_COMPLETED_FALLBACK_TEXT


def test_select_terminal_result_text_no_successful_steps_returns_fallback(repo, downloads_dir):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    # Never claimed/executed - list_step_progress() is empty.
    assert select_terminal_result_text(repo, task) == TASK_COMPLETED_FALLBACK_TEXT


def _mark_respond_step(repo, task_id, position, *, success, summary):
    """Directly claims and finalizes one RESPOND step's durable progress
    row with a real, serialized StepObservation - bypassing
    synthesize_response()/the model entirely, so select_terminal_result_text()
    is tested against real persisted data, independent of RESPOND synthesis
    itself (Milestone 46 adversarial review, §18 result-selection
    coverage)."""

    repo.claim_step(task_id, position)
    observation = build_respond_observation(
        position,
        success=success,
        safe_summary=summary,
        failure_code=None if success else "respond_invalid_output",
        completed_at="2026-08-08T00:00:01+00:00",
    )
    observation_json = serialize_observation(observation)
    if success:
        repo.mark_step_succeeded(task_id, position, observation_json)
    else:
        repo.mark_step_failed(
            task_id, position, failure_code="respond_invalid_output",
            failure_summary="The generated response did not meet the required format or size.",
            result_json=observation_json,
        )


def test_successful_respond_followed_by_later_successful_action_respond_still_wins(repo, downloads_dir):
    # Milestone 46 adversarial review, §18A: RESPOND wins even when a
    # chronologically/positionally LATER ACTION step also succeeded - the
    # documented rule is "last successful RESPOND, period", never "last
    # successful step of any kind unless a RESPOND exists earlier."
    task = _ready_task(
        repo,
        [
            _respond_step(1),
            _action_step(2, "list_files", "downloads", depends_on=(1,)),
        ],
    )
    task = repo.transition_task(task.task_id, TaskState.READY, TaskState.RUNNING)
    _mark_respond_step(repo, task.task_id, 1, success=True, summary="RESPOND SUMMARY")
    repo.claim_step(task.task_id, 2)
    action_observation = build_action_observation(
        2, ActionResult(True, "ACTION SUMMARY", "executed"), "2026-08-08T00:00:02+00:00"
    )
    repo.mark_step_succeeded(task.task_id, 2, serialize_observation(action_observation))

    reloaded = repo.get_task(task.task_id)
    assert select_terminal_result_text(repo, reloaded) == "RESPOND SUMMARY"


def test_earlier_successful_respond_followed_by_later_failed_respond_last_successful_wins(
    repo, downloads_dir
):
    # Milestone 46 adversarial review, §18B: the LAST successful RESPOND
    # wins - a later RESPOND that failed must never mask an earlier one
    # that succeeded, and must never itself be selected.
    task = _ready_task(
        repo,
        [
            _respond_step(1),
            _respond_step(2, depends_on=(1,)),
        ],
    )
    task = repo.transition_task(task.task_id, TaskState.READY, TaskState.RUNNING)
    _mark_respond_step(repo, task.task_id, 1, success=True, summary="FIRST SUMMARY")
    _mark_respond_step(repo, task.task_id, 2, success=False, summary="unused")

    reloaded = repo.get_task(task.task_id)
    assert select_terminal_result_text(repo, reloaded) == "FIRST SUMMARY"


# --- outbound message bounding / recipient / send-failure -------------------


def test_lifecycle_message_never_exceeds_outbound_bound(repo, downloads_dir, monkeypatch):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    # Force an oversized result text without needing a genuinely oversized
    # real observation - proves the bounding safety net itself, not just
    # that realistic results happen to fit.
    import interfaces.whatsapp.task_control as task_control_module

    monkeypatch.setattr(task_control_module, "select_terminal_result_text", lambda repo, task: "x" * 5000)

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)

    assert len(client.sent) == 1
    sent_text = client.sent[0][1]
    assert len(sent_text) <= 4096
    assert "x" * 100 not in sent_text  # discarded outright, never truncated


def test_send_failure_does_not_roll_back_task_state_or_raise(repo, downloads_dir):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = FailingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)  # must not raise

    # The durable state transition already committed before the send was
    # even attempted - a send failure never rolls it back.
    assert repo.get_task(task.task_id).state == TaskState.COMPLETED


def test_send_failure_on_planning_failure_delivery_does_not_roll_back_or_raise(repo, catalog):
    """Milestone 46 adversarial review, M2: the completed-result case above
    proved this once - this proves the SAME _send_lifecycle_message()
    helper behaves identically for the planning-failure delivery call
    site, which does not otherwise share any code path with the
    terminal-result delivery above."""

    task = repo.create_task("Do something impossible.", "whatsapp")
    planner = _FakeModelProvider([_cannot_plan_raw()])
    client = FailingClient()

    _dispatch(repo, task.task_id, catalog, planner, client=client)  # must not raise

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert reloaded.failure_summary  # durably recorded regardless of send outcome


def test_send_failure_on_execution_failure_delivery_does_not_roll_back_or_raise(repo):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = FailingClient()
    # A registered-but-nonexistent path - a genuine handler-level failure.
    loader = lambda: _tools_config(approved_directories={"downloads": "C:/does/not/exist/xyz"})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client)  # must not raise

    assert repo.get_task(task.task_id).state == TaskState.FAILED


def test_send_failure_on_confirmation_request_delivery_does_not_roll_back_execute_or_raise(repo):
    task = _ready_task(repo, [_action_step(1, "open_application", "notepad")])
    client = FailingClient()
    executor_calls = _NeverCalledExecutor()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    import interfaces.whatsapp.task_control as task_control_module

    real_safe_task_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
                  tools_config_loader=loader, client=client)  # must not raise
    finally:
        task_control_module.SafeTaskExecutor = real_safe_task_executor

    # A send failure on the confirmation request must never be treated as
    # authorization to proceed with the sensitive action.
    assert executor_calls.calls == []
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


def test_recipient_is_always_the_configured_authorized_sender(repo, downloads_dir):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    _dispatch(repo, task.task_id, catalog=(), planner_provider=_FakeModelProvider([]),
              tools_config_loader=loader, client=client, authorized_sender="19995551234")

    assert client.sent[0][0] == "19995551234"


def test_task_cancelled_uses_fixed_text_never_model_synthesized(repo):
    # TASK_CANCELLED is not reachable through dispatch_task_work() in P2A
    # (only deny_task_confirmation(), Milestone 46 P2B, ever produces it) -
    # this proves the delivery wrapper itself is correct and ready for P2B
    # to reuse. Milestone 47 P1: delivery is durable-outbox-backed end to
    # end, so this must be a REAL cancellation - mark_cancelled()
    # atomically creates the corresponding task_lifecycle_outbox row in
    # the same transaction - rather than a hand-constructed
    # ExecutionAdvanceResult with no matching durable event.
    from kernel.task_execution import ExecutionAdvanceResult, ExecutionAdvanceStatus

    import interfaces.whatsapp.task_control as task_control_module

    task = repo.create_task("request", "whatsapp")
    cancelled_task = repo.mark_cancelled(task.task_id, TaskState.CREATED)
    client = RecordingClient()
    result = ExecutionAdvanceResult(cancelled_task, ExecutionAdvanceStatus.TASK_CANCELLED)

    task_control_module._deliver_execution_result(result, repo, client, _AUTHORIZED_SENDER)

    assert client.sent == [(_AUTHORIZED_SENDER, TASK_CANCELLED_TEXT)]


# --- TaskExecutionWork: minimal, trusted correlation only --------------------


def test_task_execution_work_carries_only_task_id():
    work = TaskExecutionWork(task_id="abc-123")
    assert work.task_id == "abc-123"
    # Frozen dataclass - no other fields exist to accidentally carry raw
    # text, a provider message ID, or a sender.
    assert work.__dataclass_fields__.keys() == {"task_id"}


# =============================================================================
# Milestone 46 P2B: CONFIRM/REJECT ingress, durable decision, continuation
# =============================================================================


def test_task_confirmation_work_carries_only_confirmation_id():
    # Milestone 47 P2: QUEUE ITEM IS NOT AUTHORITY - decision was removed
    # from this dataclass entirely (see its own docstring for why).
    work = TaskConfirmationWork(confirmation_id="abc-123")
    assert work.confirmation_id == "abc-123"
    assert work.__dataclass_fields__.keys() == {"confirmation_id"}


def test_confirmation_command_text_carries_confirmation_id_and_decision():
    # The pure parse-result type (Milestone 47 P2) - distinct from the
    # queue item above.
    parsed = ConfirmationCommandText(confirmation_id="abc-123", decision=ConfirmationDecision.CONFIRM)
    assert parsed.confirmation_id == "abc-123"
    assert parsed.decision is ConfirmationDecision.CONFIRM
    assert parsed.__dataclass_fields__.keys() == {"confirmation_id", "decision"}


# --- classify_confirmation_text(): pure, no I/O -----------------------------


def test_not_a_confirmation_command_returns_none():
    assert classify_confirmation_text("hello") is None
    assert classify_confirmation_text("what wine goes with steak?") is None
    assert classify_confirmation_text("please confirm this") is None


@pytest.mark.parametrize(
    "text",
    [
        "CONFIRMabc",
        "REJECTIONabc",
        "CONFIRMATION is important",
        "REJECTION reason",
    ],
)
def test_prose_starting_with_verb_word_never_intercepted(text):
    # "CONFIRMabc"/"CONFIRMATION ..."/"REJECTION ..." must never be mistaken
    # for the command - the lookahead requires whitespace or end-of-string
    # immediately after the bare verb.
    assert classify_confirmation_text(text) is None


@pytest.mark.parametrize(
    "text,expected_decision",
    [
        ("CONFIRM abc", ConfirmationDecision.CONFIRM),
        ("confirm abc", ConfirmationDecision.CONFIRM),
        ("CoNfIrM abc", ConfirmationDecision.CONFIRM),
        ("REJECT abc", ConfirmationDecision.REJECT),
        ("reject abc", ConfirmationDecision.REJECT),
        ("ReJeCt    abc", ConfirmationDecision.REJECT),
    ],
)
def test_valid_command_shapes_parse_correctly(text, expected_decision):
    result = classify_confirmation_text(text)
    assert isinstance(result, ConfirmationCommandText)
    assert result.confirmation_id == "abc"
    assert result.decision is expected_decision


def test_confirmation_id_token_case_is_preserved_exactly_not_folded():
    result = classify_confirmation_text("CONFIRM AbC-123")
    assert result.confirmation_id == "AbC-123"  # not lowercased, not uppercased


@pytest.mark.parametrize(
    "text",
    [
        "CONFIRM",
        "REJECT",
        "CONFIRM abc extra",
        "REJECT a b",
        "CONFIRM token token",
    ],
)
def test_malformed_command_shapes_return_fixed_reply(text):
    result = classify_confirmation_text(text)
    assert isinstance(result, ConfirmationFixedReply)
    assert result.reply_text == GENERIC_INVALID_CONFIRMATION_TEXT


# --- dispatch_confirmation_work(): test helpers -----------------------------


def _waiting_task(
    repo,
    *,
    action_name="open_application",
    resource_key="notepad",
    request_text="do the plan",
    tools_config_loader=None,
):
    """Builds a task straight through to WAITING_FOR_CONFIRMATION via the
    REAL dispatch_task_work()/run_task_until_blocked()/propose_confirmation()
    pipeline - never hand-crafted - so the resulting confirmation_id is a
    genuine, freshly-generated token exactly like production would produce.
    Returns (task, pending)."""

    task = _ready_task(repo, [_action_step(1, action_name, resource_key)], request_text=request_text)
    loader = tools_config_loader or (
        lambda: _tools_config(approved_applications={resource_key: object()})
    )
    dispatch_task_work(
        repo, task.task_id, (), _FakeModelProvider([]), ActionRegistry(), loader,
        _FakeModelProvider([]), RecordingClient(), _AUTHORIZED_SENDER,
    )
    reloaded = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)
    return reloaded, pending


_dedup_key_counter = itertools.count()


def _dk() -> str:
    """A fresh, unique provider_dedup_key for each call - see
    tests/kernel/employee_tasks/test_pending_confirmation.py's own
    identical helper for why a fresh key per call is the correct default."""

    return f"test-provider-dedup-{next(_dedup_key_counter)}"


def _confirmation_work(repo, confirmation_id, decision, *, required_source=TASK_SOURCE):
    """Milestone 47 P2 test helper: durably records `decision` for
    `confirmation_id` exactly like record_confirmation_decision_durably()
    does on the real webhook HTTP thread, then returns the decision-free
    TaskConfirmationWork the worker actually receives - mirroring
    production's own record-then-dispatch split. A no-op, harmlessly
    returning NOT_ELIGIBLE, if confirmation_id does not currently resolve
    to an eligible pending confirmation (e.g. the "unknown token" tests
    below) - dispatch_confirmation_work() itself is still the one that
    must fail safe for that case, this helper does not special-case it."""

    repo.record_confirmation_decision(
        confirmation_id, decision, required_source=required_source, provider_dedup_key=_dk()
    )
    return TaskConfirmationWork(confirmation_id)


def _dispatch_confirmation(
    repo,
    work,
    *,
    registry=None,
    tools_config_loader=None,
    respond_provider=None,
    client=None,
    authorized_sender=_AUTHORIZED_SENDER,
):
    return dispatch_confirmation_work(
        repo,
        work,
        registry if registry is not None else ActionRegistry(),
        tools_config_loader if tools_config_loader is not None else _tools_config,
        respond_provider if respond_provider is not None else _FakeModelProvider([]),
        client if client is not None else RecordingClient(),
        authorized_sender,
    )


# --- dispatch_confirmation_work(): invalid/stale token handling ------------


def test_unknown_token_is_silent_stale_internal_work_no_mutation(repo, caplog):
    # Milestone 47 P2 adversarial-review correction (MEDIUM-1):
    # dispatch_confirmation_work() only ever receives internal,
    # already-RECORDED work - an unknown token here is stale internal
    # work (e.g. a queue item that lost a race to a recovery-checkpoint
    # pickup, or vice versa), never a genuinely new user command. It must
    # be a silent no-op, bounded-logged only - never a user-facing reply.
    # A genuinely new user command with an unknown token is handled
    # entirely at HTTP ingress (NOT_ELIGIBLE -> generic-invalid reply -
    # see server.py's own do_POST), never here.
    task, pending = _waiting_task(repo)
    client = RecordingClient()

    with caplog.at_level(logging.WARNING):
        _dispatch_confirmation(
            repo, _confirmation_work(repo, "does-not-exist", ConfirmationDecision.CONFIRM), client=client
        )

    assert client.sent == []
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(task.task_id) is not None
    messages = [record.getMessage() for record in caplog.records]
    assert "confirmation_work_stale" in messages


def test_repository_invalid_oversized_token_is_silent_not_worker_error(repo, caplog):
    from kernel.employee_tasks import MAX_CONFIRMATION_ID_CHARS

    client = RecordingClient()
    oversized = "x" * (MAX_CONFIRMATION_ID_CHARS + 1)

    # Must not raise - the narrow TaskInputTooLargeError from the reverse
    # lookup's own validation must be caught inside dispatch_confirmation_work(),
    # never escape to the caller's worker_error boundary, and (Milestone
    # 47 P2 adversarial-review correction) never produce a user-facing
    # reply either - this is stale/malformed internal work, not a new
    # user command. Deliberately NOT routed through _confirmation_work()'s
    # own durable-recording step - record_confirmation_decision() would
    # itself raise TaskInputTooLargeError for this same oversized value
    # (see the dedicated record_confirmation_decision_durably()
    # NOT_ELIGIBLE-mapping test for that boundary instead); this test
    # targets dispatch_confirmation_work()'s own independent reverse-
    # lookup validation.
    with caplog.at_level(logging.WARNING):
        _dispatch_confirmation(
            repo, TaskConfirmationWork(oversized), client=client
        )

    assert client.sent == []
    messages = [record.getMessage() for record in caplog.records]
    assert "confirmation_work_malformed_token" in messages


def test_wrong_source_token_never_mutates_remains_pending(repo):
    task = _ready_task(repo, [_action_step(1, "open_application", "notepad")], request_text="x")
    # Directly seed a task from a different source with its own pending
    # confirmation - never reachable via WhatsApp ingress in production,
    # but this proves the source check itself, independent of how such a
    # row could ever exist.
    other = repo.create_task("other channel request", "other_channel")
    repo.transition_task(other.task_id, TaskState.CREATED, TaskState.PLANNING)
    plan = TaskPlan(
        plan_version=1, task_id=other.task_id, objective="x",
        steps=(PlanStep(step_id="s1", position=1, kind=StepKind.ACTION, action_name="open_application",
                         resource_key="notepad", catalog_id="a1", description="d", expected_result="e",
                         depends_on=(), requires_confirmation=False),),
        created_at="2026-08-08T00:00:00+00:00",
    )
    repo.persist_plan_and_ready(other.task_id, TaskState.PLANNING, serialize_plan(plan))
    repo.transition_task(other.task_id, TaskState.READY, TaskState.RUNNING)
    repo.propose_confirmation(other.task_id, 1, "open_application", "notepad", ttl_seconds=120)
    pending = repo.get_pending_confirmation(other.task_id)

    client = RecordingClient()
    executor_calls = _NeverCalledExecutor()

    def loader_must_not_be_called():
        raise AssertionError("wrong-source token must be rejected before any config load")

    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader_must_not_be_called, client=client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    # Milestone 47 P2 adversarial-review correction (MEDIUM-1): silent
    # stale-internal-work no-op, never a user-facing reply - see
    # test_unknown_token_is_silent_stale_internal_work_no_mutation()'s own
    # comment for why.
    assert client.sent == []
    assert executor_calls.calls == []
    reloaded = repo.get_task(other.task_id)
    assert reloaded.state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(other.task_id) is not None

    # Same check for REJECT - REJECT never loads config regardless of
    # source authorization, but pin the source-authority gate explicitly
    # too (an assertion-raising loader would only matter if REJECT's own
    # code path ever changed to load config - it currently never does).
    client2 = RecordingClient()
    _dispatch_confirmation(
        repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.REJECT),
        tools_config_loader=loader_must_not_be_called, client=client2,
    )
    assert client2.sent == []
    assert repo.get_task(other.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(other.task_id) is not None


def test_wrong_state_token_is_silent_stale_internal_work(repo):
    # A task whose pending confirmation was already consumed (state moved
    # on) but whose OLD confirmation_id is replayed via internal work -
    # Milestone 47 P2 adversarial-review correction (MEDIUM-1): silent
    # no-op, never a user-facing reply.
    task, pending = _waiting_task(repo)
    _dispatch_confirmation(
        repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.REJECT)
    )
    assert repo.get_task(task.task_id).state == TaskState.CANCELLED

    client = RecordingClient()
    _dispatch_confirmation(
        repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM), client=client
    )
    assert client.sent == []


# --- dispatch_confirmation_work(): valid REJECT -----------------------------


def test_valid_reject_cancels_task_never_touches_executor_or_config(repo):
    task, pending = _waiting_task(repo)
    client = RecordingClient()

    def loader():
        raise AssertionError("REJECT must never load ToolsConfig")

    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    executor_calls = _NeverCalledExecutor()
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.REJECT),
            tools_config_loader=loader, client=client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.CANCELLED
    assert repo.get_pending_confirmation(task.task_id) is None
    assert executor_calls.calls == []
    assert client.sent == [(_AUTHORIZED_SENDER, TASK_CANCELLED_TEXT)]


def test_replayed_reject_no_second_cancellation_message(repo):
    task, pending = _waiting_task(repo)
    client = RecordingClient()

    _dispatch_confirmation(
        repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.REJECT), client=client
    )
    assert client.sent == [(_AUTHORIZED_SENDER, TASK_CANCELLED_TEXT)]

    # A second dispatch attempt for the same (already-consumed)
    # confirmation_id is stale internal work (Milestone 47 P2
    # adversarial-review correction, MEDIUM-1) - silent, never a second
    # message of any kind.
    _dispatch_confirmation(
        repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.REJECT), client=client
    )
    assert client.sent == [(_AUTHORIZED_SENDER, TASK_CANCELLED_TEXT)]
    assert repo.get_task(task.task_id).state == TaskState.CANCELLED


def test_expired_but_still_pending_reject_still_cancels(repo):
    # deny_confirmation() intentionally does not check expires_at - a
    # rejection can never authorize/execute anything, so it is allowed to
    # succeed even past the nominal TTL. Force the persisted row's own
    # expires_at durably into the past, directly - a real expiry, not a
    # mock of any engine logic.
    task, pending = _waiting_task(repo)
    repo._conn.execute(
        "UPDATE task_pending_confirmation SET expires_at = ? WHERE confirmation_id = ?",
        ("2020-01-01T00:00:00+00:00", pending.confirmation_id),
    )
    repo._conn.commit()

    client = RecordingClient()
    _dispatch_confirmation(
        repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.REJECT), client=client
    )
    assert repo.get_task(task.task_id).state == TaskState.CANCELLED
    assert client.sent == [(_AUTHORIZED_SENDER, TASK_CANCELLED_TEXT)]


# --- dispatch_confirmation_work(): valid CONFIRM ----------------------------


def test_valid_confirm_executes_sensitive_action_exactly_once(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    client = RecordingClient()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    executor_calls = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader, client=client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.COMPLETED  # single-step plan: executes, then completes
    assert repo.get_pending_confirmation(task.task_id) is None
    assert len(executor_calls.calls) == 1
    assert len(client.sent) == 1  # exactly one terminal message - no separate "accepted" notice
    assert client.sent[0][1] != "Confirmation accepted."


def test_replayed_confirm_no_second_execution_no_resend(repo):
    task, pending = _waiting_task(repo)
    client = RecordingClient()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    executor_calls = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader, client=client,
        )
        assert len(client.sent) == 1
        first_state = repo.get_task(task.task_id).state

        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader, client=client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    # Milestone 47 P2 adversarial-review correction (MEDIUM-1): the
    # second, stale-internal-work dispatch attempt is silent - never a
    # second message of any kind.
    assert len(executor_calls.calls) == 1  # never a second real invocation
    assert len(client.sent) == 1
    assert repo.get_task(task.task_id).state == first_state  # unchanged, no resend of the terminal result


def test_config_removed_before_confirm_fails_closed_no_execution(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    client = RecordingClient()
    # "notepad" was authorized when the confirmation was proposed, but the
    # CURRENT config no longer authorizes it.
    loader = lambda: _tools_config(approved_applications={})

    executor_calls = _NeverCalledExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader, client=client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert reloaded.failure_code == "action_no_longer_valid"
    assert executor_calls.calls == []  # revalidation failed BEFORE any execution
    assert len(client.sent) == 1
    assert client.sent[0][1].startswith("Task failed:")
    # The (now-deleted) confirmation cannot be replayed into execution
    # later - a second dispatch attempt is stale internal work (Milestone
    # 47 P2 adversarial-review correction, MEDIUM-1), silent.
    assert repo.get_pending_confirmation(task.task_id) is None
    client2 = RecordingClient()
    _dispatch_confirmation(
        repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
        tools_config_loader=loader, client=client2,
    )
    assert client2.sent == []


def test_expired_confirm_fails_closed_through_existing_engine(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")

    # Force the persisted pending confirmation into the past without
    # touching engine logic - a real, durable expiry, not a mock of
    # approve_task_confirmation()'s own expiry check.
    repo._conn.execute(
        "UPDATE task_pending_confirmation SET expires_at = ? WHERE confirmation_id = ?",
        ("2020-01-01T00:00:00+00:00", pending.confirmation_id),
    )
    repo._conn.commit()

    client = RecordingClient()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})
    executor_calls = _NeverCalledExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader, client=client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert reloaded.failure_code == "confirmation_expired"
    assert executor_calls.calls == []
    assert len(client.sent) == 1
    assert client.sent[0][1].startswith("Task failed:")


# --- dispatch_confirmation_work(): H1 correction - exception scope ---------
#
# Milestone 46 adversarial review, H1: once approve_task_confirmation() has
# genuinely succeeded (the confirmation was valid, was durably consumed, and
# the approved action already executed), a SUBSEQUENT exception from the
# post-approval run_task_until_blocked() continuation is NOT a stale/invalid
# confirmation condition - it is ordinary execution-engine behavior and must
# propagate uncaught to the worker's own generic error boundary, exactly
# like dispatch_task_work()'s own unwrapped run_task_until_blocked() call.
# The test below would have FAILED against the original, overly-broad
# try/except (which wrapped the continuation too, silently mapping this
# exception to "That confirmation is no longer valid." instead).


def test_post_approval_continuation_exception_propagates_not_generic_invalid(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    client = RecordingClient()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    executor_calls = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module

    real_executor = task_control_module.SafeTaskExecutor
    real_runner = task_control_module.run_task_until_blocked

    # A real, source-confirmed member of _CONFIRMATION_RACE_LOST_EXCEPTIONS
    # (kernel.employee_tasks.StepAlreadyClaimedError) - chosen specifically
    # because it IS caught around approve_task_confirmation() itself, so
    # this test proves the SCOPE boundary, not merely "some exception type
    # escapes" - the exact same exception type must be caught when raised
    # by approve/deny, but must NOT be caught when raised by the
    # continuation that runs strictly after approval already succeeded.
    def raising_runner(*args, **kwargs):
        raise StepAlreadyClaimedError("simulated post-approval continuation defect")

    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        task_control_module.run_task_until_blocked = raising_runner

        with pytest.raises(StepAlreadyClaimedError):
            _dispatch_confirmation(
                repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
                tools_config_loader=loader, client=client,
            )
    finally:
        task_control_module.SafeTaskExecutor = real_executor
        task_control_module.run_task_until_blocked = real_runner

    # The approval itself genuinely succeeded before the continuation blew
    # up - proven by durable state, not merely "no exception was raised
    # earlier": the action executed exactly once, and the confirmation was
    # durably consumed (never left pending, never available for replay).
    assert len(executor_calls.calls) == 1
    assert repo.get_pending_confirmation(task.task_id) is None

    # No misleading "invalid confirmation" reply was ever sent - the
    # exception propagated instead of being silently converted to one.
    assert client.sent == []

    # This test deliberately does NOT attempt to restore
    # WAITING_FOR_CONFIRMATION or recreate a token - the durable consequence
    # of the already-succeeded approval is exactly whatever the engine left
    # it as, and is not this test's concern to unwind.


def test_approve_task_confirmation_exception_itself_is_silent_stale_work(repo):
    # The complementary half of the H1 boundary proof above: the SAME
    # exception type must still be caught when it is
    # approve_task_confirmation() itself that raises it - never
    # SafeTaskExecutor.execute() reached in this case, since the
    # exception fires before any claim/execute could occur. Milestone 47
    # P2 adversarial-review correction (MEDIUM-1): a race-lost exception
    # here means something else already resolved this confirmation_id -
    # stale internal work, silent, never a user-facing reply.
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    client = RecordingClient()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    executor_calls = _NeverCalledExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    real_approve = task_control_module.approve_task_confirmation

    def raising_approve(*args, **kwargs):
        raise StepAlreadyClaimedError("simulated genuine confirmation race")

    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        task_control_module.approve_task_confirmation = raising_approve

        _dispatch_confirmation(  # must NOT raise
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader, client=client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor
        task_control_module.approve_task_confirmation = real_approve

    assert client.sent == []
    assert executor_calls.calls == []


# --- dispatch_confirmation_work(): approved action failure (adversarial --
# --- review M1) -------------------------------------------------------------


def test_approved_sensitive_action_that_fails_finalizes_task_failed_normally(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    client = RecordingClient()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    class _FakeFailureExecutor:
        def __init__(self):
            self.calls: list[ActionRequest] = []

        def execute(self, request: ActionRequest) -> ActionResult:
            self.calls.append(request)
            return ActionResult(False, "That application could not be launched.", "failed")

    executor_calls = _FakeFailureExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader, client=client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert len(executor_calls.calls) == 1  # the real approval path, genuinely executed once

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert reloaded.failure_code == "failed"
    assert repo.get_pending_confirmation(task.task_id) is None  # consumed, not left waiting

    assert len(client.sent) == 1
    assert client.sent[0][1].startswith("Task failed:")

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "failed"

    # Replay after the failure must not resend or re-execute - stale
    # internal work (Milestone 47 P2 adversarial-review correction,
    # MEDIUM-1), silent.
    client2 = RecordingClient()
    _dispatch_confirmation(
        repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
        tools_config_loader=loader, client=client2,
    )
    assert client2.sent == []
    assert len(executor_calls.calls) == 1  # still exactly once


def test_multiple_confirmation_rounds_each_new_id_each_executed_once(repo):
    # Sensitive A -> Sensitive B -> RESPOND.
    task = _ready_task(
        repo,
        [
            _action_step(1, "open_application", "notepad"),
            _action_step(2, "run_registered_script", "whatsapp_test", depends_on=(1,)),
            _respond_step(3, depends_on=(2,)),
        ],
        request_text="multi round",
    )
    # Only used for the initial P2A dispatch (planning -> execution ->
    # confirmation A) - deliberately NOT the same loader counted below, so
    # the counting assertion isolates just the two CONFIRM-time reloads
    # (adversarial review, §11).
    planning_loader = lambda: ToolsConfig(
        approved_directories={}, approved_applications={"notepad": object()},
        approved_scripts={"whatsapp_test": object()}, approved_repositories={}, approved_backups={},
    )
    client = RecordingClient()
    respond_provider = _FakeModelProvider(["final summary"])

    dispatch_task_work(
        repo, task.task_id, (), _FakeModelProvider([]), ActionRegistry(), planning_loader,
        respond_provider, client, _AUTHORIZED_SENDER,
    )
    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.WAITING_FOR_CONFIRMATION
    pending_a = repo.get_pending_confirmation(task.task_id)
    assert pending_a.action_name == "open_application"
    assert len(client.sent) == 1

    # A call-counting loader, returning a freshly-constructed ToolsConfig
    # object every call - used ONLY for the two CONFIRM dispatches below,
    # proving each confirmation round reloads config independently rather
    # than reusing an earlier round's object (adversarial review, §11/§12).
    confirm_loader_calls: list[ToolsConfig] = []

    def counting_confirm_loader():
        config = ToolsConfig(
            approved_directories={}, approved_applications={"notepad": object()},
            approved_scripts={"whatsapp_test": object()}, approved_repositories={}, approved_backups={},
        )
        confirm_loader_calls.append(config)
        return config

    executor_calls = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending_a.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=counting_confirm_loader, respond_provider=respond_provider, client=client,
        )
        reloaded = repo.get_task(task.task_id)
        assert reloaded.state == TaskState.WAITING_FOR_CONFIRMATION  # step B is also sensitive
        pending_b = repo.get_pending_confirmation(task.task_id)
        assert pending_b.action_name == "run_registered_script"
        assert pending_b.confirmation_id != pending_a.confirmation_id
        assert len(client.sent) == 2
        assert len(executor_calls.calls) == 1
        assert len(confirm_loader_calls) == 1  # A's confirmation dispatch loaded config exactly once

        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending_b.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=counting_confirm_loader, respond_provider=respond_provider, client=client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.COMPLETED
    assert len(client.sent) == 3
    assert client.sent[2][1] == "final summary"
    assert len(executor_calls.calls) == 2  # A once, B once - never a repeat
    assert len(confirm_loader_calls) == 2  # B's round loaded fresh config again, never A's
    assert confirm_loader_calls[0] is not confirm_loader_calls[1]  # distinct objects, never reused


# --- dispatch_confirmation_work(): send failure -----------------------------


def test_send_failure_after_confirm_does_not_roll_back_or_replay(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    client = FailingClient()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})

    executor_calls = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader, client=client,
        )  # must not raise

        assert repo.get_task(task.task_id).state == TaskState.COMPLETED

        # Replay after the send failure - stale internal work (Milestone
        # 47 P2 adversarial-review correction, MEDIUM-1), silent, no
        # second execution.
        recording_client = RecordingClient()
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            tools_config_loader=loader, client=recording_client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert recording_client.sent == []
    assert len(executor_calls.calls) == 1


def test_send_failure_after_reject_does_not_roll_back_or_replay(repo):
    task, pending = _waiting_task(repo)
    client = FailingClient()

    _dispatch_confirmation(
        repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.REJECT), client=client
    )  # must not raise

    assert repo.get_task(task.task_id).state == TaskState.CANCELLED
    assert repo.get_pending_confirmation(task.task_id) is None

    recording_client = RecordingClient()
    executor_calls = _NeverCalledExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        _dispatch_confirmation(
            repo, _confirmation_work(repo, pending.confirmation_id, ConfirmationDecision.CONFIRM),
            client=recording_client,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor
    # Stale internal work (Milestone 47 P2 adversarial-review correction,
    # MEDIUM-1), silent.
    assert recording_client.sent == []
    assert executor_calls.calls == []


# --- Milestone 47 P1: run_outbound_lifecycle_recovery_checkpoint() ---------


def test_recovery_checkpoint_noop_when_nothing_due(repo):
    client = RecordingClient()
    run_outbound_lifecycle_recovery_checkpoint(repo, client, _AUTHORIZED_SENDER)
    assert client.sent == []


def test_recovery_checkpoint_delivers_at_most_one_due_event_per_call(repo):
    task_a = repo.create_task("request a", "whatsapp")
    task_b = repo.create_task("request b", "whatsapp")
    repo.mark_cancelled(task_a.task_id, TaskState.CREATED)
    repo.mark_cancelled(task_b.task_id, TaskState.CREATED)

    client = RecordingClient()
    run_outbound_lifecycle_recovery_checkpoint(repo, client, _AUTHORIZED_SENDER)

    # Exactly one external send this checkpoint, never both at once - see
    # task_control.run_outbound_lifecycle_recovery_checkpoint()'s own
    # docstring for why this bound exists.
    assert len(client.sent) == 1
    assert client.sent[0] == (_AUTHORIZED_SENDER, TASK_CANCELLED_TEXT)

    still_due = repo.list_due_lifecycle_outbox_events("whatsapp")
    assert len(still_due) == 1  # the other event remains untouched, still pending

    # A second checkpoint call picks up the remaining one.
    client2 = RecordingClient()
    run_outbound_lifecycle_recovery_checkpoint(repo, client2, _AUTHORIZED_SENDER)
    assert len(client2.sent) == 1
    assert repo.list_due_lifecycle_outbox_events("whatsapp") == []


def test_recovery_checkpoint_never_consumes_a_different_channel_event(repo):
    other_task = repo.create_task("other-channel request", "other_channel")
    repo.mark_cancelled(other_task.task_id, TaskState.CREATED)

    client = RecordingClient()
    run_outbound_lifecycle_recovery_checkpoint(repo, client, _AUTHORIZED_SENDER)

    assert client.sent == []
    # The other channel's event remains completely untouched - never
    # marked delivered, never attempted.
    other_event = repo.get_outbox_event_for_task_version(
        other_task.task_id, repo.get_task(other_task.task_id).version
    )
    assert other_event.delivered_at is None
    assert other_event.attempt_count == 0


def test_recovery_checkpoint_send_failure_persists_retry_metadata(repo):
    task = repo.create_task("request", "whatsapp")
    repo.mark_cancelled(task.task_id, TaskState.CREATED)

    client = FailingClient()
    run_outbound_lifecycle_recovery_checkpoint(repo, client, _AUTHORIZED_SENDER)

    event = repo.list_due_lifecycle_outbox_events("whatsapp", limit=100)
    # A failed attempt does not clear the row from "pending" - but its
    # next_attempt_at is pushed into the future, so it may or may not still
    # be "due" depending on the exact backoff; check the underlying row
    # directly via get_outbox_event_for_task_version() instead.
    reloaded_task = repo.get_task(task.task_id)
    outbox_event = repo.get_outbox_event_for_task_version(task.task_id, reloaded_task.version)
    assert outbox_event.delivered_at is None
    assert outbox_event.attempt_count == 1
    assert outbox_event.next_attempt_at > outbox_event.created_at


def test_recovery_checkpoint_repeated_failure_never_marks_delivered(repo):
    task = repo.create_task("request", "whatsapp")
    repo.mark_cancelled(task.task_id, TaskState.CREATED)
    event_before = repo.get_outbox_event_for_task_version(task.task_id, repo.get_task(task.task_id).version)

    client = FailingClient()
    run_outbound_lifecycle_recovery_checkpoint(repo, client, _AUTHORIZED_SENDER)

    # Force it due again immediately (bypassing the real backoff delay,
    # exactly like the repository-level starvation test does) and fail
    # again, proving attempt_count accumulates and delivered_at is never
    # set purely because of repeated attempts.
    repo.mark_lifecycle_event_delivery_failed(event_before.event_id, 1, datetime.now(timezone.utc))
    run_outbound_lifecycle_recovery_checkpoint(repo, client, _AUTHORIZED_SENDER)

    final = repo.get_outbox_event_for_task_version(task.task_id, repo.get_task(task.task_id).version)
    assert final.delivered_at is None
    assert final.attempt_count == 2


# --- Milestone 47 P2: run_confirmation_decision_recovery_checkpoint() ------


def test_confirmation_recovery_checkpoint_noop_when_nothing_decided(repo):
    client = RecordingClient()
    run_confirmation_decision_recovery_checkpoint(
        repo, ActionRegistry(), _tools_config, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )
    assert client.sent == []


def test_confirmation_recovery_checkpoint_processes_one_recorded_confirm(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    outcome = repo.record_confirmation_decision(
        pending.confirmation_id, ConfirmationDecision.CONFIRM, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )
    assert outcome is ConfirmationDecisionOutcome.RECORDED

    executor_calls = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    client = RecordingClient()
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        run_confirmation_decision_recovery_checkpoint(
            repo, ActionRegistry(),
            lambda: _tools_config(approved_applications={"notepad": object()}),
            _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert len(executor_calls.calls) == 1
    assert repo.get_task(task.task_id).state == TaskState.COMPLETED
    assert client.sent == [(_AUTHORIZED_SENDER, "'notepad' launched.")]


def test_confirmation_recovery_checkpoint_processes_one_recorded_reject(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    outcome = repo.record_confirmation_decision(
        pending.confirmation_id, ConfirmationDecision.REJECT, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )
    assert outcome is ConfirmationDecisionOutcome.RECORDED

    client = RecordingClient()
    run_confirmation_decision_recovery_checkpoint(
        repo, ActionRegistry(), _tools_config, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )

    assert repo.get_task(task.task_id).state == TaskState.CANCELLED
    assert client.sent == [(_AUTHORIZED_SENDER, TASK_CANCELLED_TEXT)]


def test_confirmation_recovery_checkpoint_never_picks_up_a_different_source(repo):
    other_task = repo.create_task("other-channel request", "other_channel")
    repo.transition_task(other_task.task_id, TaskState.CREATED, TaskState.PLANNING)
    plan = TaskPlan(
        plan_version=1, task_id=other_task.task_id, objective="x",
        steps=(PlanStep(step_id="s1", position=1, kind=StepKind.ACTION, action_name="open_application",
                         resource_key="notepad", catalog_id="a1", description="d", expected_result="e",
                         depends_on=(), requires_confirmation=False),),
        created_at="2026-08-08T00:00:00+00:00",
    )
    repo.persist_plan_and_ready(other_task.task_id, TaskState.PLANNING, serialize_plan(plan))
    repo.transition_task(other_task.task_id, TaskState.READY, TaskState.RUNNING)
    repo.propose_confirmation(other_task.task_id, 1, "open_application", "notepad", ttl_seconds=120)
    other_pending = repo.get_pending_confirmation(other_task.task_id)
    repo.record_confirmation_decision(
        other_pending.confirmation_id, ConfirmationDecision.CONFIRM, required_source="other_channel",
        provider_dedup_key=_dk(),
    )

    client = RecordingClient()
    run_confirmation_decision_recovery_checkpoint(
        repo, ActionRegistry(), _tools_config, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )

    # Never touched - the other source's own durable decision remains
    # completely unprocessed by this (WhatsApp-only) checkpoint.
    assert client.sent == []
    assert repo.get_task(other_task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(other_task.task_id) is not None


def test_confirmation_recovery_checkpoint_defers_failing_decision_and_does_not_starve_next(repo):
    """Milestone 47 P2 adversarial-review correction (MEDIUM-2) - the full
    worker-level fairness/starvation regression: decision A (oldest) is
    forced to fail BEFORE consumption (a real exception escaping
    approve_task_confirmation() before any claim); decision B (newer)
    must still be selectable, processed, and never starved behind A."""

    task_a, pending_a = _waiting_task(repo, action_name="open_application", resource_key="notepad",
                                       request_text="task a")
    task_b, pending_b = _waiting_task(repo, action_name="open_application", resource_key="notepad",
                                       request_text="task b")

    repo.record_confirmation_decision(
        pending_a.confirmation_id, ConfirmationDecision.CONFIRM, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )
    repo.record_confirmation_decision(
        pending_b.confirmation_id, ConfirmationDecision.CONFIRM, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )

    import interfaces.whatsapp.task_control as task_control_module
    real_approve = task_control_module.approve_task_confirmation

    def raising_approve(*args, **kwargs):
        raise RuntimeError("simulated execution-engine defect for confirmation A only")

    client = RecordingClient()
    loader = lambda: _tools_config(approved_applications={"notepad": object()})
    try:
        task_control_module.approve_task_confirmation = raising_approve
        # Checkpoint 1: A is oldest and due - dispatch raises, A is
        # durably deferred, the exception propagates (never silently
        # swallowed as success).
        with pytest.raises(RuntimeError):
            run_confirmation_decision_recovery_checkpoint(
                repo, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
            )
    finally:
        task_control_module.approve_task_confirmation = real_approve

    # A remains durable, unconsumed, never executed - deferred, not lost.
    reloaded_a = repo.get_pending_confirmation(task_a.task_id)
    assert reloaded_a is not None
    assert reloaded_a.decision is ConfirmationDecision.CONFIRM
    assert reloaded_a.decision_attempt_count == 1
    assert repo.get_task(task_a.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
    assert client.sent == []  # the failed attempt never produced a user-facing reply

    # Checkpoint 2 (real approve_task_confirmation restored): B is now the
    # only due decision - A's own deferred backoff has not elapsed yet, so
    # B is correctly selected and processed, never starved behind A.
    executor_calls = _FakeSuccessExecutor()
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        run_confirmation_decision_recovery_checkpoint(
            repo, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert len(executor_calls.calls) == 1
    assert repo.get_task(task_b.task_id).state == TaskState.COMPLETED
    assert client.sent == [(_AUTHORIZED_SENDER, "'notepad' launched.")]
    # A is untouched by checkpoint 2 - still deferred, still durable.
    assert repo.get_task(task_a.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


def test_confirmation_recovery_checkpoint_survives_close_and_reopen(db_path):
    # Milestone 47 P2 crash/restart proof: durably record a decision, then
    # close the connection WITHOUT ever dispatching it (simulating a crash
    # between HTTP 200 and worker consumption) - open a brand-new
    # connection/TaskRepository (simulating process restart) and confirm
    # the recovery checkpoint alone, with no further CONFIRM/REJECT ever
    # posted again, still finds and correctly processes the decision.
    conn1 = open_writer_connection(db_path)
    repo1 = TaskRepository(conn1)
    task, pending = _waiting_task(repo1, action_name="open_application", resource_key="notepad")
    outcome = repo1.record_confirmation_decision(
        pending.confirmation_id, ConfirmationDecision.CONFIRM, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )
    assert outcome is ConfirmationDecisionOutcome.RECORDED
    conn1.close()  # crash simulation - no dispatch ever happened

    conn2 = open_writer_connection(db_path)
    repo2 = TaskRepository(conn2)
    try:
        assert repo2.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION

        executor_calls = _FakeSuccessExecutor()
        import interfaces.whatsapp.task_control as task_control_module
        real_executor = task_control_module.SafeTaskExecutor
        client = RecordingClient()
        try:
            task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
            run_confirmation_decision_recovery_checkpoint(
                repo2, ActionRegistry(),
                lambda: _tools_config(approved_applications={"notepad": object()}),
                _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
            )
        finally:
            task_control_module.SafeTaskExecutor = real_executor

        assert len(executor_calls.calls) == 1
        assert repo2.get_task(task.task_id).state == TaskState.COMPLETED
        assert client.sent == [(_AUTHORIZED_SENDER, "'notepad' launched.")]
    finally:
        conn2.close()


# --- Milestone 47 P2 adversarial-review correction (MEDIUM-1): the        --
# --- RECORDED -> recovery -> stale-queue-item race                        --


def test_recovery_consumes_confirm_before_stale_queue_item_no_invalid_reply(repo):
    """Deterministic reproduction of the exact interleaving the
    adversarial review found broken:

      1. record_confirmation_decision() commits RECORDED (the HTTP
         thread's own durable write).
      2. A recovery checkpoint races ahead of that SAME HTTP thread's own
         subsequent queue enqueue and fully processes the decision - the
         real lifecycle result is delivered, the pending row is consumed.
      3. The HTTP thread's own (now-stale) TaskConfirmationWork is later
         dispatched anyway.

    Required (this is what the correction fixes): exactly one action
    execution, exactly one lifecycle result/outbox event, and the stale
    dispatch in step 3 produces NO reply of any kind - silent, bounded-
    logged stale internal work, never
    "That confirmation is no longer valid.\""""

    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")

    # Step 1.
    outcome = repo.record_confirmation_decision(
        pending.confirmation_id, ConfirmationDecision.CONFIRM, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )
    assert outcome is ConfirmationDecisionOutcome.RECORDED

    executor_calls = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
    try:
        # Step 2: the recovery checkpoint races ahead and fully consumes
        # the decision, exactly as if it had won the race against the
        # HTTP thread's own subsequent put_nowait().
        recovery_client = RecordingClient()
        run_confirmation_decision_recovery_checkpoint(
            repo, ActionRegistry(),
            lambda: _tools_config(approved_applications={"notepad": object()}),
            _FakeModelProvider([]), recovery_client, _AUTHORIZED_SENDER,
        )
        assert repo.get_task(task.task_id).state == TaskState.COMPLETED
        assert recovery_client.sent == [(_AUTHORIZED_SENDER, "'notepad' launched.")]
        completed_version = repo.get_task(task.task_id).version
        outbox_event = repo.get_outbox_event_for_task_version(task.task_id, completed_version)
        assert outbox_event is not None

        # Step 3: the stale queue item, dispatched anyway.
        stale_client = RecordingClient()
        dispatch_confirmation_work(
            repo, TaskConfirmationWork(pending.confirmation_id), ActionRegistry(),
            lambda: _tools_config(approved_applications={"notepad": object()}),
            _FakeModelProvider([]), stale_client, _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert stale_client.sent == []  # no invalid reply, no anything
    assert len(executor_calls.calls) == 1  # executed exactly once, never twice
    assert repo.get_task(task.task_id).state == TaskState.COMPLETED  # unchanged by the stale dispatch
    # No second outbox event for the same (already-delivered) transition.
    assert repo.get_outbox_event_for_task_version(task.task_id, completed_version).event_id == outbox_event.event_id


def test_recovery_consumes_reject_before_stale_queue_item_no_invalid_reply(repo):
    """The REJECT analogue of the CONFIRM race test above - the same
    internal-stale-work mechanism applies identically."""

    task, pending = _waiting_task(repo)

    outcome = repo.record_confirmation_decision(
        pending.confirmation_id, ConfirmationDecision.REJECT, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )
    assert outcome is ConfirmationDecisionOutcome.RECORDED

    recovery_client = RecordingClient()
    run_confirmation_decision_recovery_checkpoint(
        repo, ActionRegistry(), _tools_config, _FakeModelProvider([]), recovery_client, _AUTHORIZED_SENDER,
    )
    assert repo.get_task(task.task_id).state == TaskState.CANCELLED
    assert recovery_client.sent == [(_AUTHORIZED_SENDER, TASK_CANCELLED_TEXT)]
    cancelled_version = repo.get_task(task.task_id).version
    outbox_event = repo.get_outbox_event_for_task_version(task.task_id, cancelled_version)
    assert outbox_event is not None

    stale_client = RecordingClient()
    dispatch_confirmation_work(
        repo, TaskConfirmationWork(pending.confirmation_id), ActionRegistry(),
        _tools_config, _FakeModelProvider([]), stale_client, _AUTHORIZED_SENDER,
    )

    assert stale_client.sent == []
    assert repo.get_task(task.task_id).state == TaskState.CANCELLED
    assert repo.get_outbox_event_for_task_version(task.task_id, cancelled_version).event_id == outbox_event.event_id


# --- Milestone 47 P1: malformed durable payload never crashes the worker ---


def _force_redelivery_with_corrupted_payload(repo, event) -> None:
    """_waiting_task() already drives a real dispatch_task_work() call
    internally, which already durably delivers the fresh
    CONFIRMATION_REQUIRED event via its own internal RecordingClient - so
    by the time a test gets `event`, it is already marked delivered. This
    resets it back to "due now" (delivered_at cleared, next_attempt_at
    moved to the past) AND corrupts its payload in the same step - never
    reachable through any supported write API, exactly like a manually
    altered/corrupted database row."""

    repo._conn.execute(
        "UPDATE task_lifecycle_outbox SET payload_json = ?, delivered_at = NULL, "
        "next_attempt_at = '2020-01-01T00:00:00+00:00' WHERE event_id = ?",
        ("not valid json{{{", event.event_id),
    )


def test_malformed_confirmation_payload_uses_fallback_never_crashes(repo):
    """Structurally unreachable through this module's own write path, but
    proven safe anyway (Milestone 47 design requirement): a durably
    corrupted confirmation_required payload must never crash the worker,
    never leak raw JSON/SQL detail, and must still respect the normal
    delivered/retry recording discipline."""

    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    event = repo.get_outbox_event_for_task_version(task.task_id, task.version)
    assert event is not None
    _force_redelivery_with_corrupted_payload(repo, event)

    client = RecordingClient()
    run_outbound_lifecycle_recovery_checkpoint(repo, client, _AUTHORIZED_SENDER)

    assert client.sent == [(_AUTHORIZED_SENDER, _MALFORMED_OUTBOX_PAYLOAD_TEXT)]
    reloaded = repo.get_outbox_event_for_task_version(task.task_id, task.version)
    assert reloaded.delivered_at is not None  # fallback send succeeded -> marked delivered


def test_malformed_confirmation_payload_fallback_send_failure_persists_retry(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    event = repo.get_outbox_event_for_task_version(task.task_id, task.version)
    _force_redelivery_with_corrupted_payload(repo, event)

    client = FailingClient()
    run_outbound_lifecycle_recovery_checkpoint(repo, client, _AUTHORIZED_SENDER)

    reloaded = repo.get_outbox_event_for_task_version(task.task_id, task.version)
    assert reloaded.delivered_at is None  # never marked delivered on a failed fallback send
    assert reloaded.attempt_count == 1
    assert reloaded.attempt_count == 1


# --- Milestone 47 P3: run_task_state_recovery_checkpoint() -----------------


def _planning_task(repo, request_text="do the plan"):
    """A task stranded exactly at the PLANNING crash window this
    milestone reconciles: CREATED -> PLANNING has committed, but no plan
    was ever persisted (advance_task_planning()'s own model call never
    happened, or crashed before persist_plan_and_ready())."""

    record = repo.create_task(request_text, "whatsapp")
    repo.transition_task(record.task_id, TaskState.CREATED, TaskState.PLANNING)
    return repo.get_task(record.task_id)


def _running_task_with_claimed_step(repo, action_name, resource_key, *, position=1, extra_steps=()):
    """A task stranded exactly at Milestone 42's own claim/execute/
    finalize crash window: claim_step() has durably committed the
    in_progress row, but nothing ever finalizes it - indistinguishable
    from a crash immediately after claiming (external action never ran)
    or a crash immediately after the external action ran (result never
    persisted). Both are the SAME persisted shape, deliberately - see
    test_running_with_in_progress_crash_window_equivalence below."""

    steps = [_action_step(position, action_name, resource_key), *extra_steps]
    task = _ready_task(repo, steps)
    task = repo.transition_task(task.task_id, TaskState.READY, TaskState.RUNNING)
    repo.claim_step(task.task_id, position)
    return task


def _running_task_all_steps_terminal(repo):
    """A task stranded between one step's own SUCCEEDED finalize and the
    execution loop's next iteration (which would otherwise recognize
    AllStepsComplete and transition to COMPLETED) - a real, reachable
    crash window distinct from an in_progress step."""

    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    task = repo.transition_task(task.task_id, TaskState.READY, TaskState.RUNNING)
    repo.claim_step(task.task_id, 1)
    observation = build_action_observation(
        1, ActionResult(True, "done", "listed"), "2026-08-08T00:00:00+00:00"
    )
    repo.mark_step_succeeded(task.task_id, 1, serialize_observation(observation))
    return repo.get_task(task.task_id)


def test_task_state_recovery_checkpoint_noop_when_nothing_stranded(repo):
    client = RecordingClient()
    run_task_state_recovery_checkpoint(
        repo, (), _FakeModelProvider([]), ActionRegistry(), _tools_config,
        _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )
    assert client.sent == []


# --- A: CREATED --------------------------------------------------------------


def test_created_task_survives_restart_and_resumes_exactly_once(repo, catalog, downloads_dir):
    """A durable CREATED task with no volatile queue item (simulating a
    crash before the worker ever dequeued its TaskExecutionWork, or a
    queue-full drop) is discovered and driven through planning and
    execution by the SAME dispatch_task_work() the normal queue path
    uses - no separate execution logic."""

    task = repo.create_task("Check the downloads folder.", "whatsapp")
    planner = _FakeModelProvider([_valid_plan_raw(catalog)])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    run_task_state_recovery_checkpoint(
        repo, catalog, planner, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.COMPLETED
    assert len(planner.calls) == 1
    assert len(client.sent) == 1

    # A later checkpoint must not duplicate the now-terminal task:
    # find_one_recoverable_task() no longer selects it, so this second
    # call is a complete no-op - proven by the fact that `planner` has no
    # second response queued (a second real call would raise IndexError).
    run_task_state_recovery_checkpoint(
        repo, catalog, planner, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )
    assert len(planner.calls) == 1
    assert len(client.sent) == 1


# --- B/C/D/E: PLANNING --------------------------------------------------------


def test_planning_task_is_discovered(repo):
    task = _planning_task(repo)
    assert repo.find_one_recoverable_task(TASK_SOURCE) == task.task_id


def test_planning_reconciles_to_created_with_fixed_reason_then_replans_fresh(repo, catalog, downloads_dir):
    """Sections A-D of the M47 P3 brief: PLANNING is discovered, a durable
    planning -> created transition is recorded with the exact fixed
    restart-recovery reason code, and a genuinely fresh planning call
    follows - never assuming or reusing any result from the interrupted
    attempt (there is none to reuse: the fake planner below has never
    been called before this checkpoint runs)."""

    task = _planning_task(repo, request_text="Check the downloads folder.")
    planner = _FakeModelProvider([_valid_plan_raw(catalog, objective="Check the downloads folder.")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    run_task_state_recovery_checkpoint(
        repo, catalog, planner, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )

    transitions = repo.list_transitions(task.task_id)
    reconciliation = [
        t for t in transitions if t.from_state == TaskState.PLANNING and t.to_state == TaskState.CREATED
    ]
    assert len(reconciliation) == 1
    assert reconciliation[0].reason_code == _RESTART_PLANNING_INTERRUPTED_REASON_CODE

    # The fresh planning attempt genuinely ran (the fake planner's one
    # queued response was consumed) and drove the task all the way to a
    # normal terminal outcome - nothing from "before" was assumed.
    assert len(planner.calls) == 1
    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.COMPLETED
    assert len(client.sent) == 1


def test_planning_reconciliation_produces_no_outbox_event(repo, catalog, downloads_dir):
    """The planning -> created transition itself is not user-visible
    (created is not in _DELIVERABLE_EVENT_KIND_BY_TARGET_STATE) - only
    the SUBSEQUENT fresh dispatch's own terminal transition may ever
    produce a lifecycle message. Proven directly against the journal
    rather than merely against client.sent, which could pass for the
    wrong reason (e.g. a message from a different transition)."""

    task = _planning_task(repo, request_text="Check the downloads folder.")
    planner = _FakeModelProvider([_valid_plan_raw(catalog, objective="Check the downloads folder.")])
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    run_task_state_recovery_checkpoint(
        repo, catalog, planner, ActionRegistry(), loader, _FakeModelProvider([]), RecordingClient(),
        _AUTHORIZED_SENDER,
    )

    transitions = repo.list_transitions(task.task_id)
    reconciliation = next(
        t for t in transitions if t.from_state == TaskState.PLANNING and t.to_state == TaskState.CREATED
    )
    assert repo.get_outbox_event_for_task_version(task.task_id, reconciliation.task_version) is None


def test_planning_recovery_does_not_loop_once_task_leaves_planning(repo, catalog, downloads_dir):
    """A second checkpoint call, after the first has already reconciled
    and completed the task, must never re-attempt planning -> created -
    the task is terminal and no longer selected at all."""

    _planning_task(repo, request_text="Check the downloads folder.")
    planner = _FakeModelProvider([_valid_plan_raw(catalog, objective="Check the downloads folder.")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    run_task_state_recovery_checkpoint(
        repo, catalog, planner, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )
    # A second call: nothing left to discover (terminal). A real second
    # planner call would raise IndexError (no response queued) - this
    # would fail loudly if the checkpoint incorrectly looped.
    run_task_state_recovery_checkpoint(
        repo, catalog, planner, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )
    assert len(planner.calls) == 1


def test_no_other_state_gains_an_unexpected_created_transition_via_checkpoint(repo, catalog, downloads_dir):
    """Behavioral counterpart to
    test_allowed_transitions_grants_exactly_one_new_created_target_edge()
    in tests/kernel/employee_tasks/test_repository.py: a READY task run
    through the SAME checkpoint must never pick up a planning -> created
    reconciliation branch it doesn't need."""

    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    run_task_state_recovery_checkpoint(
        repo, catalog, _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]),
        RecordingClient(), _AUTHORIZED_SENDER,
    )

    # Excludes the genesis row (from_state IS NULL, to_state == created) -
    # every task's very first journal entry, not a reconciliation - this
    # asserts no OTHER state ever transitions into created.
    transitions = repo.list_transitions(task.task_id)
    assert not any(
        t.to_state == TaskState.CREATED and t.from_state is not None for t in transitions
    )


# --- READY ---------------------------------------------------------------


def test_ready_task_resumes_and_executes_once_through_safe_task_executor(repo, downloads_dir):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    run_task_state_recovery_checkpoint(
        repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]), client,
        _AUTHORIZED_SENDER,
    )

    assert repo.get_task(task.task_id).state == TaskState.COMPLETED
    assert len(client.sent) == 1


def test_ready_task_fails_closed_when_config_revoked_since_plan_was_persisted(repo, downloads_dir):
    """The persisted plan references "downloads", authorized when built -
    the recovery checkpoint's OWN fresh tools_config_loader call returns a
    config that no longer authorizes it. Must fail closed through the
    existing ActionRevalidationFailure path; the real list_files handler
    (and therefore SafeTaskExecutor.execute()) must never be reached."""

    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    client = RecordingClient()
    executor_calls = _NeverCalledExecutor()
    loader = lambda: _tools_config(approved_directories={})  # "downloads" removed

    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        run_task_state_recovery_checkpoint(
            repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]), client,
            _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert executor_calls.calls == []
    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert reloaded.failure_code == "action_no_longer_valid"
    assert client.sent[0][1].startswith("Task failed:")


# --- RUNNING without an in-progress step ------------------------------------


def test_running_without_in_progress_resumes_next_step_normally(repo, downloads_dir):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    task = repo.transition_task(task.task_id, TaskState.READY, TaskState.RUNNING)
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    run_task_state_recovery_checkpoint(
        repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]), client,
        _AUTHORIZED_SENDER,
    )

    assert repo.get_task(task.task_id).state == TaskState.COMPLETED
    assert len(client.sent) == 1


def test_running_with_all_steps_terminal_reaches_completion_without_reexecuting(repo, downloads_dir):
    task = _running_task_all_steps_terminal(repo)
    assert task.state == TaskState.RUNNING

    client = RecordingClient()
    executor_calls = _NeverCalledExecutor()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        run_task_state_recovery_checkpoint(
            repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]), client,
            _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert executor_calls.calls == []  # nothing left to execute
    assert repo.get_task(task.task_id).state == TaskState.COMPLETED
    assert len(client.sent) == 1


# --- E/F/M/N: RUNNING with one IN_PROGRESS step - CRITICAL -----------------


@pytest.mark.parametrize(
    "action_name,resource_key",
    [
        pytest.param("open_application", "notepad", id="sensitive"),
        pytest.param("list_files", "downloads", id="non_sensitive"),
    ],
)
def test_running_with_one_in_progress_step_never_retries_fails_closed(
    repo, downloads_dir, action_name, resource_key
):
    """Sections E/F/M/N of the M47 P3 brief, combined: whether the
    stranded step's own action is sensitive or not, and whether the crash
    happened before or after the (unknowable) external side effect, the
    persisted shape is identical (RUNNING + one in_progress row) and the
    required outcome is identical - no retry, fail closed via the
    existing Milestone 42 STEP_IN_PROGRESS -> step_execution_uncertain
    path. run_task_state_recovery_checkpoint()'s only job is to make sure
    this stranded task is REACHED at all after a restart; it adds no
    action-inspection or success/failure-inference logic of its own."""

    loader = lambda: _tools_config(
        approved_directories={"downloads": str(downloads_dir)},
        approved_applications={"notepad": object()},
    )
    task = _running_task_with_claimed_step(repo, action_name, resource_key)

    client = RecordingClient()
    executor_calls = _NeverCalledExecutor()

    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        run_task_state_recovery_checkpoint(
            repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]), client,
            _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    # No retry, in any sense: the fake executor's own execute() would
    # raise AssertionError if ever called - it is never called at all.
    assert executor_calls.calls == []

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert reloaded.failure_code == "step_execution_uncertain"

    # The in_progress step row is left exactly as-is forever - the
    # durable, honest record that the external outcome is unknown. Never
    # reset to not-started, never overwritten to succeeded/failed.
    step = repo.get_step_progress(task.task_id, 1)
    assert step.status == StepStatus.IN_PROGRESS

    # A bounded, code-owned, non-raw user-facing message.
    assert len(client.sent) == 1
    assert client.sent[0][1].startswith("Task failed:")
    assert "step_execution_uncertain" not in client.sent[0][1]  # never a raw code in user text

    # The P1 TASK_FAILED lifecycle-outbox event was created atomically
    # with this same failure transition.
    outbox_event = repo.get_outbox_event_for_task_version(task.task_id, reloaded.version)
    assert outbox_event is not None
    assert outbox_event.event_kind is LifecycleEventKind.TASK_FAILED


def test_running_with_in_progress_crash_window_equivalence(repo, downloads_dir):
    """Sections M/N of the brief made explicit as its own test: "crash
    after claim, before the action call" and "crash after the action's
    real side effect, before its result was persisted" are DIFFERENT
    real-world moments that produce the IDENTICAL persisted shape
    (RUNNING + one in_progress row, nothing else). Since
    _running_task_with_claimed_step() below is exactly that shape - built
    the same way regardless of which moment it is meant to represent -
    this test proves there is no way to feed the recovery checkpoint
    "which one actually happened": both scenarios are literally the same
    call, and therefore always produce the identical outcome by
    construction, never an inferred distinction."""

    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    task_m = _running_task_with_claimed_step(repo, "list_files", "downloads")  # "moment M"
    task_n = _running_task_with_claimed_step(repo, "list_files", "downloads")  # "moment N"

    for task in (task_m, task_n):
        client = RecordingClient()
        executor_calls = _NeverCalledExecutor()
        import interfaces.whatsapp.task_control as task_control_module
        real_executor = task_control_module.SafeTaskExecutor
        try:
            task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
            run_task_state_recovery_checkpoint(
                repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]),
                client, _AUTHORIZED_SENDER,
            )
        finally:
            task_control_module.SafeTaskExecutor = real_executor

        assert executor_calls.calls == []
        reloaded = repo.get_task(task.task_id)
        assert reloaded.state == TaskState.FAILED
        assert reloaded.failure_code == "step_execution_uncertain"


# --- G: multiple/inconsistent IN_PROGRESS rows ------------------------------


def test_multiple_in_progress_rows_fail_closed_on_first_never_touch_the_rest(repo, downloads_dir):
    """Not a second reconciliation algorithm - the existing ascending-
    position evaluate_next_step() scan already blocks on the first
    non-terminal step it finds and never inspects the rest. Seeded
    directly via two claim_step() calls (never reachable through any
    single-worker production path, but never assumed away)."""

    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})
    task = _ready_task(
        repo, [_action_step(1, "list_files", "downloads"), _action_step(2, "list_files", "downloads")]
    )
    task = repo.transition_task(task.task_id, TaskState.READY, TaskState.RUNNING)
    repo.claim_step(task.task_id, 1)
    repo.claim_step(task.task_id, 2)  # inconsistent - never reachable in production

    client = RecordingClient()
    executor_calls = _NeverCalledExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        run_task_state_recovery_checkpoint(
            repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]), client,
            _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert executor_calls.calls == []
    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert reloaded.failure_code == "step_execution_uncertain"

    # Both rows are left completely untouched - position 1 (the one that
    # blocked) AND position 2 (never even inspected).
    assert repo.get_step_progress(task.task_id, 1).status == StepStatus.IN_PROGRESS
    assert repo.get_step_progress(task.task_id, 2).status == StepStatus.IN_PROGRESS


# --- H/I: WAITING_FOR_CONFIRMATION / terminal exclusion, via the checkpoint --


def test_checkpoint_never_touches_waiting_for_confirmation_decision_null(repo):
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")

    client = RecordingClient()
    run_task_state_recovery_checkpoint(
        repo, (), _FakeModelProvider([]), ActionRegistry(), _tools_config, _FakeModelProvider([]),
        client, _AUTHORIZED_SENDER,
    )

    assert client.sent == []
    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(task.task_id) is not None


def test_checkpoint_never_touches_waiting_for_confirmation_decision_non_null(repo):
    """A durably-decided pending confirmation belongs exclusively to
    run_confirmation_decision_recovery_checkpoint() (Milestone 47 P2) -
    P3's own checkpoint must never also pick up the owning task, which
    would let both mechanisms race the same task."""

    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    repo.record_confirmation_decision(
        pending.confirmation_id, ConfirmationDecision.CONFIRM, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )

    client = RecordingClient()
    run_task_state_recovery_checkpoint(
        repo, (), _FakeModelProvider([]), ActionRegistry(), _tools_config, _FakeModelProvider([]),
        client, _AUTHORIZED_SENDER,
    )

    assert client.sent == []
    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.WAITING_FOR_CONFIRMATION
    assert repo.get_pending_confirmation(task.task_id) is not None


@pytest.mark.parametrize("terminal_state", ["completed", "failed", "cancelled"])
def test_checkpoint_never_reopens_terminal_tasks(repo, downloads_dir, terminal_state):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    tid = task.task_id
    if terminal_state == "completed":
        repo.transition_task(tid, TaskState.READY, TaskState.RUNNING)
        repo.claim_step(tid, 1)
        observation = build_action_observation(
            1, ActionResult(True, "done", "listed"), "2026-08-08T00:00:00+00:00"
        )
        repo.mark_step_succeeded(tid, 1, serialize_observation(observation))
        repo.transition_task(tid, TaskState.RUNNING, TaskState.COMPLETED)
    elif terminal_state == "failed":
        repo.mark_failed(tid, TaskState.READY, "tool_error", "something failed")
    else:
        repo.mark_cancelled(tid, TaskState.READY)

    client = RecordingClient()
    run_task_state_recovery_checkpoint(
        repo, (), _FakeModelProvider([]), ActionRegistry(), _tools_config, _FakeModelProvider([]),
        client, _AUTHORIZED_SENDER,
    )

    assert client.sent == []
    assert repo.get_task(tid).state == TaskState(terminal_state)


# --- source isolation, via the checkpoint -----------------------------------


def test_checkpoint_never_selects_a_different_sources_task(repo, downloads_dir):
    other_task = repo.create_task("other-channel request", "other_channel")
    repo.transition_task(other_task.task_id, TaskState.CREATED, TaskState.PLANNING)

    client = RecordingClient()
    run_task_state_recovery_checkpoint(
        repo, (), _FakeModelProvider([]), ActionRegistry(), _tools_config, _FakeModelProvider([]),
        client, _AUTHORIZED_SENDER,
    )

    assert client.sent == []
    assert repo.get_task(other_task.task_id).state == TaskState.PLANNING


# --- P1/P2/P3 coexistence in one checkpoint -----------------------------------


def test_p1_p2_p3_checkpoints_coexist_without_duplicate_delivery_or_execution(repo, catalog, downloads_dir):
    """One repository holding all three kinds of recoverable durable work
    at once: a due P1 lifecycle-outbox redelivery, a due P2 durable
    confirmation decision, and a stranded P3 task. Each of the three
    bounded checkpoints processes at most its own one item, and none
    consumes another category's durable authority."""

    loader = lambda: _tools_config(
        approved_directories={"downloads": str(downloads_dir)},
        approved_applications={"notepad": object()},
    )

    # P1: a due, undelivered lifecycle-outbox event - a completed task
    # whose own delivery is forced back to "due now".
    outbox_task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    run_task_state_recovery_checkpoint(  # drive it to COMPLETED first, delivered
        repo, catalog, _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]),
        RecordingClient(), _AUTHORIZED_SENDER,
    )
    outbox_event = repo.get_outbox_event_for_task_version(
        outbox_task.task_id, repo.get_task(outbox_task.task_id).version
    )
    repo._conn.execute(
        "UPDATE task_lifecycle_outbox SET delivered_at = NULL, "
        "next_attempt_at = '2020-01-01T00:00:00+00:00' WHERE event_id = ?",
        (outbox_event.event_id,),
    )

    # P2: a durably-recorded confirmation decision, never yet dispatched.
    confirmation_task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    repo.record_confirmation_decision(
        pending.confirmation_id, ConfirmationDecision.CONFIRM, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )

    # P3: a stranded CREATED task, discovered fresh here.
    stranded_task = repo.create_task("Check the downloads folder.", "whatsapp")
    planner = _FakeModelProvider([_valid_plan_raw(catalog)])

    client = RecordingClient()
    notepad_executor = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: notepad_executor

        run_outbound_lifecycle_recovery_checkpoint(repo, client, _AUTHORIZED_SENDER)
        run_confirmation_decision_recovery_checkpoint(
            repo, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
        )
        run_task_state_recovery_checkpoint(
            repo, catalog, planner, ActionRegistry(), loader, _FakeModelProvider([]), client,
            _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    # P1: the redelivery happened exactly once.
    redelivered = repo.get_outbox_event_for_task_version(
        outbox_task.task_id, repo.get_task(outbox_task.task_id).version
    )
    assert redelivered.delivered_at is not None

    # P2: the confirmed task completed exactly once, via the confirmation
    # checkpoint alone.
    assert repo.get_task(confirmation_task.task_id).state == TaskState.COMPLETED

    # P3: the stranded task planned and completed exactly once.
    assert repo.get_task(stranded_task.task_id).state == TaskState.COMPLETED
    assert len(planner.calls) == 1

    # Exactly one executor invocation each for the confirmation task and
    # the stranded task - never a duplicate, never a cross-category reuse.
    assert len(notepad_executor.calls) == 2


# --- Milestone 47 P3 adversarial-review correction: starvation fairness ----


def test_fairness_regression_deferred_oldest_task_does_not_starve_newer_one(repo, downloads_dir):
    """The exact confirmed-MEDIUM adversarial-review finding, reproduced
    against the CORRECTED checkpoint: task A (oldest) is forced to raise
    on its own first recovery attempt (a stateful, once-broken
    tools_config_loader simulating a transient failure specific to that
    one attempt) BEFORE its own state ever changes; task B (newer) must
    still be selected and processed on the very next checkpoint, never
    starved behind A."""

    call_count = {"n": 0}

    def flaky_loader():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient failure on A's own first attempt")
        return _tools_config(approved_directories={"downloads": str(downloads_dir)})

    task_a = _ready_task(repo, [_action_step(1, "list_files", "downloads")], request_text="task a")
    task_b = _ready_task(repo, [_action_step(1, "list_files", "downloads")], request_text="task b")

    client = RecordingClient()
    executor_calls = []

    class _CountingSuccessExecutor:
        def execute(self, request):
            executor_calls.append(request)
            return ActionResult(True, "listed", "executed")

    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: _CountingSuccessExecutor()

        # Checkpoint 1: A is selected (oldest), raises, gets deferred. The
        # original exception still propagates - never swallowed.
        with pytest.raises(RuntimeError):
            run_task_state_recovery_checkpoint(
                repo, (), _FakeModelProvider([]), ActionRegistry(), flaky_loader, _FakeModelProvider([]),
                client, _AUTHORIZED_SENDER,
            )

        assert executor_calls == []  # no external action from A
        a_after_1 = repo.get_task(task_a.task_id)
        assert a_after_1.state == TaskState.READY  # never advanced
        assert a_after_1.recovery_attempt_count == 1
        assert a_after_1.recovery_next_attempt_at is not None
        b_after_1 = repo.get_task(task_b.task_id)
        assert b_after_1.state == TaskState.READY  # untouched
        assert b_after_1.recovery_attempt_count == 0

        # Checkpoint 2, before A becomes due again: B is selected and
        # successfully recovers - not starved.
        run_task_state_recovery_checkpoint(
            repo, (), _FakeModelProvider([]), ActionRegistry(), flaky_loader, _FakeModelProvider([]),
            client, _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert len(executor_calls) == 1  # B executes only once
    assert repo.get_task(task_b.task_id).state == TaskState.COMPLETED
    a_after_2 = repo.get_task(task_a.task_id)
    assert a_after_2.state == TaskState.READY  # still deferred, untouched
    assert a_after_2.recovery_attempt_count == 1  # not re-attempted yet - A remains durable and recoverable later


def test_fairness_regression_survives_restart(db_path, downloads_dir):
    """The same fairness regression, but proving the deferral itself is
    DURABLE - not merely an in-memory skip - by closing the connection
    and reopening a fresh TaskRepository against the same database file
    before checking anything."""

    conn1 = open_writer_connection(db_path)
    repo1 = TaskRepository(conn1)

    def always_broken_loader():
        raise RuntimeError("simulated persistent failure")

    task_a = _ready_task(repo1, [_action_step(1, "list_files", "downloads")], request_text="task a")
    task_b = _ready_task(repo1, [_action_step(1, "list_files", "downloads")], request_text="task b")

    with pytest.raises(RuntimeError):
        run_task_state_recovery_checkpoint(
            repo1, (), _FakeModelProvider([]), ActionRegistry(), always_broken_loader,
            _FakeModelProvider([]), RecordingClient(), _AUTHORIZED_SENDER,
        )
    a_before_close = repo1.get_task(task_a.task_id)
    assert a_before_close.recovery_attempt_count == 1
    assert a_before_close.recovery_next_attempt_at is not None
    conn1.close()

    conn2 = open_writer_connection(db_path)
    repo2 = TaskRepository(conn2)
    try:
        a_reopened = repo2.get_task(task_a.task_id)
        assert a_reopened.recovery_attempt_count == 1  # survived
        assert a_reopened.recovery_next_attempt_at == a_before_close.recovery_next_attempt_at  # survived

        # A is not selected before its due time - B remains selectable.
        assert repo2.find_one_recoverable_task(TASK_SOURCE) == task_b.task_id
    finally:
        conn2.close()


def test_fairness_regression_retry_after_due_recovers_normally(repo, downloads_dir):
    """Once A's own next-attempt time becomes due, it is selectable
    again, and if the underlying failure is corrected, recovers normally
    through the exact same checkpoint - attempt history alone never
    authorizes execution, and current-config/SafeTaskExecutor
    revalidation still applies. No real 60-second wait - the due instant
    is set deterministically, already in the past."""

    task_a = _ready_task(repo, [_action_step(1, "list_files", "downloads")], request_text="task a")
    already_due = datetime.now(timezone.utc) - timedelta(seconds=1)
    repo.defer_task_recovery_retry(task_a.task_id, TASK_SOURCE, 1, already_due)

    assert repo.find_one_recoverable_task(TASK_SOURCE) == task_a.task_id

    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})
    run_task_state_recovery_checkpoint(
        repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]), client,
        _AUTHORIZED_SENDER,
    )

    assert repo.get_task(task_a.task_id).state == TaskState.COMPLETED
    assert len(client.sent) == 1

    # Contrast: current config revoked since the deferral - still fails
    # closed via the existing ActionRevalidationFailure path, never
    # executes merely because attempt history exists.
    task_c = _ready_task(repo, [_action_step(1, "list_files", "downloads")], request_text="task c")
    repo.defer_task_recovery_retry(task_c.task_id, TASK_SOURCE, 2, already_due)
    executor_calls = _NeverCalledExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor_calls
        run_task_state_recovery_checkpoint(
            repo, (), _FakeModelProvider([]), ActionRegistry(), lambda: _tools_config(approved_directories={}),
            _FakeModelProvider([]), RecordingClient(), _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor
    assert executor_calls.calls == []
    assert repo.get_task(task_c.task_id).state == TaskState.FAILED
    assert repo.get_task(task_c.task_id).failure_code == "action_no_longer_valid"


# --- Milestone 47 P3 adversarial-review correction: deferral safety --------


def test_defer_task_recovery_retry_checkpoint_level_noop_when_waiting_for_confirmation(repo):
    """No clean deterministic injection point exists for forcing
    dispatch_task_work() to raise AFTER a task has already reached
    WAITING_FOR_CONFIRMATION (reaching that state is itself a normal,
    non-exceptional return - see run_task_until_blocked()'s own
    docstring) - so this exercises TaskRepository.defer_task_recovery_retry()
    directly instead, per the correction brief's own fallback
    instruction. (See tests/kernel/employee_tasks/test_repository.py's
    own test_defer_task_recovery_retry_is_a_noop_when_waiting_for_confirmation/
    _is_a_noop_for_terminal_tasks for the exhaustive per-state coverage;
    this is the task_control-level confirmation that the SAME safety
    applies from this module's own call shape.)"""

    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    before = repo.get_task(task.task_id)

    repo.defer_task_recovery_retry(
        task.task_id, TASK_SOURCE, 1, datetime.now(timezone.utc) + timedelta(seconds=60)
    )

    after = repo.get_task(task.task_id)
    assert after.recovery_attempt_count == 0
    assert after.recovery_next_attempt_at is None
    assert after.state == TaskState.WAITING_FOR_CONFIRMATION
    assert after.version == before.version
    assert repo.find_one_recoverable_task(TASK_SOURCE) is None


# --- Milestone 47 P3 adversarial-review correction: P2 -> P3 same-checkpoint --


def test_p2_terminal_completion_is_never_reselected_by_p3_in_the_same_checkpoint(repo, downloads_dir):
    """run_task_until_blocked() (proven from its own docstring/contract -
    see kernel/task_execution/service.py) always drives a task all the
    way to a stopping status: CONFIRMATION_REQUIRED/WAITING_FOR_CONFIRMATION,
    TASK_COMPLETED, TASK_FAILED, or TASK_CANCELLED - never leaves it in a
    plain P3-recoverable state. This proves the terminal case directly:
    a single sensitive-action plan, CONFIRMed via P2's own checkpoint,
    reaches COMPLETED - and P3's own checkpoint, run immediately after in
    the SAME sequence MessageHandler.run_recovery_checkpoint() itself
    uses, never reselects or re-executes it."""

    loader = lambda: _tools_config(approved_applications={"notepad": object()})
    task, pending = _waiting_task(repo, action_name="open_application", resource_key="notepad")
    repo.record_confirmation_decision(
        pending.confirmation_id, ConfirmationDecision.CONFIRM, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )

    client = RecordingClient()
    executor = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor
        run_confirmation_decision_recovery_checkpoint(
            repo, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
        )
        assert repo.get_task(task.task_id).state == TaskState.COMPLETED
        assert repo.find_one_recoverable_task(TASK_SOURCE) is None

        # Same checkpoint sequence P3 runs right after P2.
        run_task_state_recovery_checkpoint(
            repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]), client,
            _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert len(executor.calls) == 1  # never re-executed
    assert repo.get_task(task.task_id).state == TaskState.COMPLETED


def test_p2_continuation_into_a_second_confirmation_round_is_not_selected_by_p3(repo):
    """The WAITING_FOR_CONFIRMATION-again counterpart: a two-sensitive-step
    plan whose first step is CONFIRMed via P2's own checkpoint continues
    straight into the SECOND step's own fresh confirmation request
    (run_task_until_blocked() stops there, per its own docstring) - P3's
    checkpoint, run right after in the same sequence, must not select or
    touch this task either."""

    loader = lambda: _tools_config(approved_applications={"notepad": object(), "calc": object()})
    task = _ready_task(
        repo,
        [
            _action_step(1, "open_application", "notepad"),
            _action_step(2, "open_application", "calc"),
        ],
    )
    task = repo.transition_task(task.task_id, TaskState.READY, TaskState.RUNNING)
    repo.propose_confirmation(task.task_id, 1, "open_application", "notepad", ttl_seconds=120)
    pending = repo.get_pending_confirmation(task.task_id)
    repo.record_confirmation_decision(
        pending.confirmation_id, ConfirmationDecision.CONFIRM, required_source=TASK_SOURCE,
        provider_dedup_key=_dk(),
    )

    client = RecordingClient()
    executor = _FakeSuccessExecutor()
    import interfaces.whatsapp.task_control as task_control_module
    real_executor = task_control_module.SafeTaskExecutor
    try:
        task_control_module.SafeTaskExecutor = lambda *a, **k: executor
        run_confirmation_decision_recovery_checkpoint(
            repo, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
        )
        assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION
        assert len(executor.calls) == 1  # only step 1 executed
        assert repo.find_one_recoverable_task(TASK_SOURCE) is None

        run_task_state_recovery_checkpoint(
            repo, (), _FakeModelProvider([]), ActionRegistry(), loader, _FakeModelProvider([]), client,
            _AUTHORIZED_SENDER,
        )
    finally:
        task_control_module.SafeTaskExecutor = real_executor

    assert len(executor.calls) == 1  # P3 never touched it
    assert repo.get_task(task.task_id).state == TaskState.WAITING_FOR_CONFIRMATION


# --- Milestone 47 P3 adversarial-review correction: CREATED + stale queue work --


def test_created_task_recovered_by_p3_then_stale_queue_dispatch_is_a_safe_noop(repo, catalog, downloads_dir):
    """Verifies P3's discovery of a CREATED task is harmless during a
    LIVE process too, not just after restart. Simulates: task A is
    CREATED, an ordinary TaskExecutionWork(A) conceptually already exists
    in the worker's queue, but the recovery checkpoint reaches and fully
    processes A FIRST; the later, now-stale dispatch_task_work(A) call
    (standing in for the worker eventually dequeuing that pre-existing
    queue item) must be a safe no-op."""

    task = repo.create_task("Check the downloads folder.", "whatsapp")
    planner = _FakeModelProvider([_valid_plan_raw(catalog)])  # only ONE response ever queued
    client = RecordingClient()
    loader = lambda: _tools_config(approved_directories={"downloads": str(downloads_dir)})

    run_task_state_recovery_checkpoint(
        repo, catalog, planner, ActionRegistry(), loader, _FakeModelProvider([]), client, _AUTHORIZED_SENDER,
    )
    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.COMPLETED
    assert len(planner.calls) == 1
    assert len(client.sent) == 1
    outbox_event = repo.get_outbox_event_for_task_version(task.task_id, reloaded.version)
    assert outbox_event is not None

    # The stale, leftover ordinary dispatch arrives later - a second real
    # planner call would raise IndexError (no response queued).
    dispatch_task_work(
        repo, task.task_id, catalog, planner, ActionRegistry(), loader, _FakeModelProvider([]), client,
        _AUTHORIZED_SENDER,
    )

    assert len(planner.calls) == 1  # planner called once, total
    assert len(client.sent) == 1  # one terminal lifecycle result, total
    assert repo.get_task(task.task_id).state == TaskState.COMPLETED
    assert (
        repo.get_outbox_event_for_task_version(task.task_id, reloaded.version).event_id
        == outbox_event.event_id
    )  # no duplicate outbox event
