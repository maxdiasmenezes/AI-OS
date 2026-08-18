"""Tests for kernel/task_execution/service.py: advance_task_execution(),
run_task_until_blocked(), approve_task_confirmation(),
deny_task_confirmation(). Every test uses a real tmp_path SQLite database
and a real TaskRepository/ActionRegistry - never the real database under
storage/tasks/, never a real SafeTaskExecutor, and never a real model
provider (deterministic fakes stand in for both, so no real process,
subprocess, network, or model-host call ever happens)."""

import ast
import threading
from pathlib import Path

import pytest

from kernel.employee_tasks import (
    MAX_FAILURE_CODE_CHARS,
    MAX_FAILURE_SUMMARY_CHARS,
    MAX_STEP_RESULT_JSON_CHARS,
    TaskAlreadyTerminalError,
    TaskRepository,
    TaskState,
    TaskStorageCorruptError,
    open_writer_connection,
)
from kernel.models.base import ModelResponse
from kernel.task_execution.observation import (
    _ACTION_OUTCOME_INVALID_FALLBACK_CODE,
    _OVERSIZED_ACTION_FAILURE_SUMMARY,
    _OVERSIZED_ACTION_SUCCESS_SUMMARY,
    build_action_observation,
    deserialize_observation,
    normalize_action_result_outcome,
    serialize_observation,
)
from kernel.task_execution.service import (
    TaskNotReadyOrRunningError,
    TaskNotWaitingForConfirmationError,
    _finalize_action_step,
    advance_task_execution,
    approve_task_confirmation,
    deny_task_confirmation,
)
from kernel.task_execution.types import ExecutionAdvanceStatus, MAX_RESPOND_TEXT_CHARS
from kernel.task_planner import PlanStep, StepKind, TaskPlan, serialize_plan
from kernel.tools.config import RepoBackupSpec, ToolsConfig
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest, ActionResult


class _FakeExecutor:
    """Records every ActionRequest it receives and returns pre-programmed
    ActionResults in call order - never calls a real handler, subprocess,
    or network endpoint."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[ActionRequest] = []

    def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request)
        return self._results.pop(0)


class _FakeModelProvider:
    """Records every prompt it receives and returns pre-programmed
    ModelResponses (or raises a pre-programmed exception) in call order -
    never a real network/subprocess call."""

    def __init__(self, responses=None, exception=None):
        self._responses = list(responses) if responses is not None else []
        self._exception = exception
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        if self._exception is not None:
            raise self._exception
        return self._responses.pop(0)


class _RaisingModelProvider:
    """A ModelProvider that must never be called - the module-level
    `model_provider` name every ACTION-only test below resolves against
    (as a plain module global, not a pytest fixture - see this file's
    tests for why no per-test injection is needed), proving the ACTION
    path never touches the model (Milestone 42 P3 acceptance: "no model
    called for ACTION steps")."""

    def send_prompt(self, prompt, *, options=None):
        raise AssertionError("model_provider.send_prompt() must not be called for an ACTION-only plan")


# Every pre-existing ACTION-only test below calls advance_task_execution()/
# run_task_until_blocked() with this bare name - a plain module global, not
# a pytest fixture, since none of those tests need a distinct provider per
# test and Python resolves the free variable from module scope without any
# per-function signature change. RESPOND-specific tests further down
# construct and use their own local _FakeModelProvider instead.
model_provider = _RaisingModelProvider()


def _action_step(position, action_name, resource_key, *, catalog_id="action_1", depends_on=()):
    return PlanStep(
        step_id=f"step_{position}",
        position=position,
        kind=StepKind.ACTION,
        action_name=action_name,
        resource_key=resource_key,
        catalog_id=catalog_id,
        description="do the thing",
        expected_result="the thing is done",
        depends_on=depends_on,
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
        depends_on=depends_on,
        requires_confirmation=False,
    )


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "tasks.sqlite3"


@pytest.fixture
def repo(db_path):
    conn = open_writer_connection(db_path)
    yield TaskRepository(conn)
    conn.close()


@pytest.fixture
def tools_config():
    return ToolsConfig(
        approved_directories={"downloads": object()},
        approved_applications={"notepad": object()},
        approved_scripts={"whatsapp_test": object()},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )


@pytest.fixture
def registry():
    return ActionRegistry()


def _ready_task(repo, steps, task_id_override=None):
    record = repo.create_task("do the plan", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")
    plan = TaskPlan(
        plan_version=1,
        task_id=task_id_override or record.task_id,
        objective="do the plan",
        steps=tuple(steps),
        created_at="2026-08-08T00:00:00+00:00",
    )
    plan_json = serialize_plan(plan)
    return repo.persist_plan_and_ready(record.task_id, "planning", plan_json)


def _running_task(repo, steps):
    ready = _ready_task(repo, steps)
    return repo.transition_task(ready.task_id, "ready", "running")


# --- READY -> RUNNING -> one non-sensitive step -------------------------------


def test_advance_ready_task_transitions_and_executes_one_non_sensitive_step(
    repo, registry, tools_config
):
    task = _ready_task(repo, [_action_step(1, "list_files", "downloads")])
    executor = _FakeExecutor([ActionResult(True, "3 files found.", "executed")])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert result.task.state == TaskState.RUNNING
    assert len(executor.calls) == 1
    assert executor.calls[0] == ActionRequest(action="list_files", resource_key="downloads")

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"


def test_advance_running_task_all_steps_complete(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    repo.claim_step(task.task_id, 1)
    repo.mark_step_succeeded(task.task_id, 1, '{"ok": true}')

    executor = _FakeExecutor([])
    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert executor.calls == []


def test_advance_processes_only_one_step_per_call(repo, registry, tools_config):
    task = _ready_task(
        repo,
        [
            _action_step(1, "list_files", "downloads"),
            _action_step(2, "system_status", None),
        ],
    )
    executor = _FakeExecutor([ActionResult(True, "ok", "executed")])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert len(executor.calls) == 1
    assert repo.get_step_progress(task.task_id, 2) is None


# --- non-sensitive action failure ---------------------------------------------


def test_non_sensitive_action_failure_fails_task_and_step_atomically(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    executor = _FakeExecutor([ActionResult(False, "That action could not be completed.", "failed")])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.task.state == TaskState.FAILED
    assert result.task.failure_code == "failed"

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "failed"
    assert step.failure_code == "failed"


# --- oversized ActionResult.message (Milestone 46 adversarial-review H1) ------
#
# An action handler's ActionResult.message has no bound of its own (unlike
# RESPOND's already-bounded synthesized text - see the escaping-bound tests
# further down). By the time _finalize_action_step() runs, claim_step() has
# already committed the step as durably in_progress and executor.execute()
# has already returned - so an oversized message must never leave the step
# stuck in_progress, and must never flip a real success into a failure (or
# vice versa) merely because its descriptive text was too large to persist
# verbatim. Every test below proves this deterministically for both the
# MAX_STEP_RESULT_JSON_CHARS bound (the serialized StepObservation itself)
# and the independent MAX_FAILURE_SUMMARY_CHARS bound (failure_summary,
# reused only on the failure path) - see _finalize_action_step()'s own
# docstring for why both are checked.


def test_oversized_successful_action_message_finalizes_succeeded_with_bounded_fallback(
    repo, registry, tools_config
):
    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    oversized_message = "X" * (MAX_STEP_RESULT_JSON_CHARS + 1000)
    executor = _FakeExecutor([ActionResult(True, oversized_message, "executed")])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert result.task.state == TaskState.RUNNING
    assert len(executor.calls) == 1

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"
    assert len(step.result_json) <= MAX_STEP_RESULT_JSON_CHARS

    observation = deserialize_observation(step.result_json)
    assert observation.success is True
    assert observation.safe_summary == _OVERSIZED_ACTION_SUCCESS_SUMMARY
    assert oversized_message not in step.result_json


def test_oversized_failed_action_message_finalizes_failed_with_bounded_fallback(
    repo, registry, tools_config
):
    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    oversized_message = "X" * (MAX_STEP_RESULT_JSON_CHARS + 1000)
    executor = _FakeExecutor([ActionResult(False, oversized_message, "failed")])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.task.state == TaskState.FAILED
    assert result.task.failure_code == "failed"
    assert len(executor.calls) == 1

    assert result.task.failure_summary == _OVERSIZED_ACTION_FAILURE_SUMMARY
    assert len(result.task.failure_summary) <= MAX_FAILURE_SUMMARY_CHARS

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "failed"
    assert step.failure_code == "failed"
    assert len(step.result_json) <= MAX_STEP_RESULT_JSON_CHARS

    observation = deserialize_observation(step.result_json)
    assert observation.success is False
    assert observation.safe_summary == _OVERSIZED_ACTION_FAILURE_SUMMARY
    assert oversized_message not in step.result_json


def test_failure_message_between_failure_summary_and_json_bounds_still_finalizes_failed(
    repo, registry, tools_config
):
    """A message that fits within MAX_STEP_RESULT_JSON_CHARS on its own but
    exceeds the narrower MAX_FAILURE_SUMMARY_CHARS bound - a distinct
    trigger from the JSON-overflow case above, since serialize_observation()
    alone would not catch it (repository.fail_running_step()'s own
    failure_summary validation would, uncaught, without this function's
    fix)."""

    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    assert MAX_FAILURE_SUMMARY_CHARS < MAX_STEP_RESULT_JSON_CHARS
    midsized_message = "X" * (MAX_FAILURE_SUMMARY_CHARS + 100)
    executor = _FakeExecutor([ActionResult(False, midsized_message, "failed")])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.task.state == TaskState.FAILED
    assert len(executor.calls) == 1
    assert result.task.failure_summary == _OVERSIZED_ACTION_FAILURE_SUMMARY


def test_ordinary_bounded_action_message_is_unaffected_by_the_oversized_fallback(
    repo, registry, tools_config
):
    """The counterpart to the three tests above: an ordinary, already-small
    message is persisted verbatim, exactly as before this correction -
    proving the fallback path only activates when actually needed."""

    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    executor = _FakeExecutor([ActionResult(True, "3 files found.", "executed")])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert observation.safe_summary == "3 files found."


def test_real_list_files_with_many_long_filenames_completes_without_orphaning(
    tmp_path, repo, registry
):
    """Turns the adversarial-review reproduction into a permanent
    regression: the REAL list_files handler, the REAL SafeTaskExecutor, and
    the real execution service, against a real tmp_path directory
    containing 100 Windows-valid, realistically long filenames - no
    fakes/mocks anywhere in the execution path itself. Before this
    correction, this reproducibly raised ObservationSerializationError and
    left the task RUNNING with the step stuck IN_PROGRESS."""

    from kernel.tools.executor import SafeTaskExecutor

    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    for i in range(100):
        # Windows-valid: no reserved characters, well under MAX_PATH for a
        # single component, but long enough (65 chars) that 100 of them
        # comfortably overflow MAX_STEP_RESULT_JSON_CHARS when joined.
        name = f"Quarterly_Financial_Report_Draft_Review_Comments_Attached_{i:03d}.pdf"
        (downloads_dir / name).write_text("x")

    tools_config = ToolsConfig(
        approved_directories={"downloads": str(downloads_dir)},
        approved_applications={},
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
    )
    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    executor = SafeTaskExecutor(tools_config, registry)

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert result.task.state == TaskState.RUNNING

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"
    assert len(step.result_json) <= MAX_STEP_RESULT_JSON_CHARS

    observation = deserialize_observation(step.result_json)
    assert observation.success is True
    assert observation.safe_summary == _OVERSIZED_ACTION_SUCCESS_SUMMARY

    # A second advance now completes the (single-step) task normally -
    # proving the fallback observation did not leave anything uncertain.
    result2 = advance_task_execution(result.task, repo, registry, tools_config, executor, model_provider)
    assert result2.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result2.task.state == TaskState.COMPLETED


# --- oversized ActionResult.outcome (Milestone 46 adversarial re-review, M1) --
#
# ActionResult.outcome (kernel/tools/types.py) is typed as a plain,
# unconstrained str - "one of audit.py's fixed codes" is a convention every
# one of the 14 currently-registered handlers happens to follow, never a
# structural guarantee. normalize_action_result_outcome() closes the second-
# order gap this left in build_bounded_action_observation()'s own "provably
# bounded" claim: an oversized/empty/malformed outcome must be replaced by a
# fixed, short, code-owned fallback code BEFORE it ever reaches either the
# embedded StepObservation fields or the standalone failure_code parameter
# threaded into repository.fail_running_step() (bounded to
# MAX_FAILURE_CODE_CHARS, independent of and tighter than
# MAX_STEP_RESULT_JSON_CHARS) - never a truncation of the real value.


def test_oversized_outcome_with_normal_message_succeeds_normally(repo, registry, tools_config):
    # Message overflow and outcome overflow are independent triggers - this
    # isolates outcome overflow alone, with an otherwise completely normal,
    # already-short success message.
    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    oversized_outcome = "X" * (MAX_FAILURE_CODE_CHARS + 1000)
    executor = _FakeExecutor([ActionResult(True, "3 files found.", oversized_outcome)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert result.task.state == TaskState.RUNNING
    assert len(executor.calls) == 1

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"
    assert len(step.result_json) <= MAX_STEP_RESULT_JSON_CHARS

    observation = deserialize_observation(step.result_json)
    assert observation.success is True
    # The real, short message is preserved verbatim - only the oversized
    # machine-readable outcome was out of bounds.
    assert observation.safe_summary == "3 files found."
    assert observation.action_outcome == _ACTION_OUTCOME_INVALID_FALLBACK_CODE
    assert oversized_outcome not in step.result_json


def test_oversized_outcome_with_normal_message_fails_normally(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    oversized_outcome = "Y" * (MAX_FAILURE_CODE_CHARS + 1000)
    executor = _FakeExecutor([ActionResult(False, "small failure", oversized_outcome)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.task.state == TaskState.FAILED
    assert len(executor.calls) == 1

    # failure_code is normalized (it would otherwise violate
    # MAX_FAILURE_CODE_CHARS uncaught inside fail_running_step()) - but
    # failure_summary is the REAL short message, since only the outcome,
    # not the message, was out of bounds.
    assert result.task.failure_code == _ACTION_OUTCOME_INVALID_FALLBACK_CODE
    assert len(result.task.failure_code) <= MAX_FAILURE_CODE_CHARS
    assert result.task.failure_summary == "small failure"

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "failed"
    assert step.failure_code == _ACTION_OUTCOME_INVALID_FALLBACK_CODE
    observation = deserialize_observation(step.result_json)
    assert observation.success is False
    assert observation.action_outcome == _ACTION_OUTCOME_INVALID_FALLBACK_CODE
    assert oversized_outcome not in step.result_json


def test_both_message_and_outcome_oversized_succeeds_normally(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    oversized_message = "M" * (MAX_STEP_RESULT_JSON_CHARS + 1000)
    oversized_outcome = "O" * (MAX_FAILURE_CODE_CHARS + 1000)
    executor = _FakeExecutor([ActionResult(True, oversized_message, oversized_outcome)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert len(executor.calls) == 1

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"
    assert len(step.result_json) <= MAX_STEP_RESULT_JSON_CHARS
    observation = deserialize_observation(step.result_json)
    assert observation.success is True
    assert observation.safe_summary == _OVERSIZED_ACTION_SUCCESS_SUMMARY
    assert observation.action_outcome == _ACTION_OUTCOME_INVALID_FALLBACK_CODE
    assert oversized_message not in step.result_json
    assert oversized_outcome not in step.result_json


def test_both_message_and_outcome_oversized_fails_normally(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    oversized_message = "M" * (MAX_STEP_RESULT_JSON_CHARS + 1000)
    oversized_outcome = "O" * (MAX_FAILURE_CODE_CHARS + 1000)
    executor = _FakeExecutor([ActionResult(False, oversized_message, oversized_outcome)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.task.state == TaskState.FAILED
    assert len(executor.calls) == 1

    assert result.task.failure_code == _ACTION_OUTCOME_INVALID_FALLBACK_CODE
    assert result.task.failure_summary == _OVERSIZED_ACTION_FAILURE_SUMMARY

    step = repo.get_step_progress(task.task_id, 1)
    assert len(step.result_json) <= MAX_STEP_RESULT_JSON_CHARS
    observation = deserialize_observation(step.result_json)
    assert observation.success is False
    assert observation.safe_summary == _OVERSIZED_ACTION_FAILURE_SUMMARY
    assert observation.action_outcome == _ACTION_OUTCOME_INVALID_FALLBACK_CODE
    assert oversized_message not in step.result_json
    assert oversized_outcome not in step.result_json


def test_normal_short_outcome_is_unaffected_by_outcome_normalization(repo, registry, tools_config):
    """The counterpart to the oversized-outcome tests above: a real,
    already-short outcome code is preserved verbatim, exactly as before
    this correction - proving normalization only activates when actually
    needed, never gratuitously replacing a legitimate short code."""

    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    executor = _FakeExecutor([ActionResult(True, "3 files found.", "executed")])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert observation.action_outcome == "executed"

    # The failure-path counterpart, in one more advance.
    task2 = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    executor2 = _FakeExecutor([ActionResult(False, "not available", "failed")])
    result2 = advance_task_execution(task2, repo, registry, tools_config, executor2, model_provider)
    assert result2.task.failure_code == "failed"


def test_normalize_action_result_outcome_preserves_valid_outcomes_unchanged():
    for outcome in ("executed", "failed", "rejected", "timed_out"):
        result = ActionResult(True, "message", outcome)
        assert normalize_action_result_outcome(result) is result


def test_normalize_action_result_outcome_replaces_invalid_outcomes():
    cases = [
        "X" * (MAX_FAILURE_CODE_CHARS + 1),  # too long
        "",  # too short (empty)
        "has\x00nul",  # NUL character
    ]
    for outcome in cases:
        result = ActionResult(True, "message", outcome)
        normalized = normalize_action_result_outcome(result)
        assert normalized.outcome == _ACTION_OUTCOME_INVALID_FALLBACK_CODE
        assert normalized.success == result.success
        assert normalized.message == result.message
        assert len(normalized.outcome) <= MAX_FAILURE_CODE_CHARS


# --- sensitive action / durable confirmation ----------------------------------


def test_sensitive_action_creates_pending_confirmation_without_executing(
    repo, registry, tools_config
):
    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.CONFIRMATION_REQUIRED
    assert result.task.state == TaskState.WAITING_FOR_CONFIRMATION
    assert executor.calls == []

    pending = repo.get_pending_confirmation(task.task_id)
    assert pending.action_name == "repository_backup"
    assert pending.resource_key == "ai_os"


def test_approve_confirmation_executes_exactly_once(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    executor = _FakeExecutor([ActionResult(True, "Backup created.", "executed")])
    result = approve_task_confirmation(
        waiting_task, repo, registry, tools_config, executor, pending.confirmation_id
    )

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert result.task.state == TaskState.RUNNING
    assert len(executor.calls) == 1
    assert executor.calls[0] == ActionRequest(action="repository_backup", resource_key="ai_os")
    assert repo.get_pending_confirmation(task.task_id) is None


def test_approve_confirmation_action_no_longer_valid_fails_closed_without_executing(
    repo, registry, tools_config
):
    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    # The backup destination was removed from configuration since the
    # confirmation was proposed.
    revoked_config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={"ai_os": object()},
        approved_backups={},  # <- removed
    )
    executor = _FakeExecutor([])

    result = approve_task_confirmation(
        waiting_task, repo, registry, revoked_config, executor, pending.confirmation_id
    )

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.task.state == TaskState.FAILED
    assert executor.calls == []
    assert repo.get_pending_confirmation(task.task_id) is None


def test_approve_confirmation_expired_fails_task_without_executing(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    repo._conn.execute(
        "UPDATE task_pending_confirmation SET expires_at = '2000-01-01T00:00:00+00:00' "
        "WHERE task_id = ?",
        (task.task_id,),
    )

    executor = _FakeExecutor([])
    result = approve_task_confirmation(
        waiting_task, repo, registry, tools_config, executor, pending.confirmation_id
    )

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "confirmation_expired"
    assert executor.calls == []
    assert repo.get_pending_confirmation(task.task_id) is None


def test_deny_confirmation_cancels_task(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    result = deny_task_confirmation(waiting_task, repo, pending.confirmation_id)

    assert result.status == ExecutionAdvanceStatus.TASK_CANCELLED
    assert result.task.state == TaskState.CANCELLED
    assert repo.get_pending_confirmation(task.task_id) is None


def test_advance_waiting_task_unexpired_returns_unchanged_without_executing(
    repo, registry, tools_config
):
    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)

    executor = _FakeExecutor([])
    result = advance_task_execution(waiting_task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.WAITING_FOR_CONFIRMATION
    assert result.task.state == TaskState.WAITING_FOR_CONFIRMATION
    assert executor.calls == []
    assert repo.get_pending_confirmation(task.task_id) is not None


def test_advance_waiting_task_expired_fails_task(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)

    repo._conn.execute(
        "UPDATE task_pending_confirmation SET expires_at = '2000-01-01T00:00:00+00:00' "
        "WHERE task_id = ?",
        (task.task_id,),
    )

    executor = _FakeExecutor([])
    result = advance_task_execution(waiting_task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "confirmation_expired"
    assert result.task.state == TaskState.FAILED
    assert executor.calls == []


def test_advance_waiting_task_malformed_expires_at_fails_closed_without_executing(
    repo, registry, tools_config
):
    """Milestone 42 P2 correction: a malformed persisted expires_at must
    fail closed (TaskStorageCorruptError) rather than being silently
    treated as valid - and the executor must never be called either way."""

    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)

    repo._conn.execute(
        "UPDATE task_pending_confirmation SET expires_at = 'not-a-timestamp' WHERE task_id = ?",
        (task.task_id,),
    )

    executor = _FakeExecutor([])
    with pytest.raises(TaskStorageCorruptError):
        advance_task_execution(waiting_task, repo, registry, tools_config, executor, model_provider)
    assert executor.calls == []


def test_approve_confirmation_malformed_expires_at_fails_closed_without_executing(
    repo, registry, tools_config
):
    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    repo._conn.execute(
        "UPDATE task_pending_confirmation SET expires_at = 'not-a-timestamp' WHERE task_id = ?",
        (task.task_id,),
    )

    executor = _FakeExecutor([])
    with pytest.raises(TaskStorageCorruptError):
        approve_task_confirmation(
            waiting_task, repo, registry, tools_config, executor, pending.confirmation_id
        )
    assert executor.calls == []


# --- approval must re-bind to the persisted TaskPlan (Milestone 42 P2 -------
# --- correction) -------------------------------------------------------------
#
# task_pending_confirmation is a durable record of a confirmation PROPOSAL,
# never execution authority by itself - the immutable persisted TaskPlan
# remains the authority for WHAT the task was allowed to execute. Every
# test below directly tampers with the pending row via raw SQL - these
# states are unreachable through the normal repository API - to prove
# approve_task_confirmation() re-binds to, and fails closed against, the
# persisted plan rather than trusting the pending row's own fields.


def test_tampered_action_name_fails_closed_without_executing(repo, registry, tools_config):
    """Test A: action_name tampered to a DIFFERENT, currently registered
    and currently valid action. Registry/config validation ALONE would
    accept this - only re-binding to the persisted plan catches it."""

    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    repo._conn.execute(
        "UPDATE task_pending_confirmation SET action_name = 'open_application', "
        "resource_key = 'notepad' WHERE task_id = ?",
        (task.task_id,),
    )

    executor = _FakeExecutor([])
    result = approve_task_confirmation(
        waiting_task, repo, registry, tools_config, executor, pending.confirmation_id
    )

    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1) is None
    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "confirmation_plan_mismatch"
    assert repo.get_pending_confirmation(task.task_id) is None  # consumed, not stuck


def test_tampered_resource_key_fails_closed_without_executing(repo, registry, tools_config):
    """Test B: resource_key tampered to a DIFFERENT, currently configured
    valid resource for the SAME action - registry/config validation alone
    would accept this too."""

    two_backup_config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={"ai_os": object(), "other_repo": object()},
        approved_backups={
            "ai_os": RepoBackupSpec(destination_directory="/x"),
            "other_repo": RepoBackupSpec(destination_directory="/y"),
        },
    )
    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, two_backup_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    repo._conn.execute(
        "UPDATE task_pending_confirmation SET resource_key = 'other_repo' WHERE task_id = ?",
        (task.task_id,),
    )

    executor = _FakeExecutor([])
    result = approve_task_confirmation(
        waiting_task, repo, registry, two_backup_config, executor, pending.confirmation_id
    )

    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1) is None
    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "confirmation_plan_mismatch"
    assert repo.get_pending_confirmation(task.task_id) is None


def test_tampered_step_position_fails_closed_without_executing(repo, registry, tools_config):
    """Test C: step_position tampered to another valid/unclaimed position
    in the same plan. Approval must not execute that different step."""

    task = _running_task(
        repo,
        [
            _action_step(1, "repository_backup", "ai_os"),
            _action_step(2, "list_files", "downloads"),
        ],
    )
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    repo._conn.execute(
        "UPDATE task_pending_confirmation SET step_position = 2 WHERE task_id = ?",
        (task.task_id,),
    )

    executor = _FakeExecutor([])
    result = approve_task_confirmation(
        waiting_task, repo, registry, tools_config, executor, pending.confirmation_id
    )

    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1) is None
    assert repo.get_step_progress(task.task_id, 2) is None
    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "confirmation_plan_mismatch"


def test_persisted_plan_task_id_mismatch_fails_closed_without_executing(
    repo, registry, tools_config
):
    """Test D: fault-inject plan.task_id != TaskRecord.task_id before
    approval. No execution."""

    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    other_plan = TaskPlan(
        plan_version=1,
        task_id="00000000-0000-7000-8000-000000000000",  # wrong task_id
        objective="x",
        steps=(_action_step(1, "repository_backup", "ai_os"),),
        created_at="2026-08-08T00:00:00+00:00",
    )
    repo._conn.execute(
        "UPDATE tasks SET plan_json = ? WHERE task_id = ?",
        (serialize_plan(other_plan), task.task_id),
    )

    executor = _FakeExecutor([])
    result = approve_task_confirmation(
        waiting_task, repo, registry, tools_config, executor, pending.confirmation_id
    )

    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1) is None
    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "plan_identity_violation"


def test_pending_confirmation_now_maps_to_respond_step_fails_closed(repo, registry, tools_config):
    """Test E: the pending confirmation's position now resolves to a
    RESPOND step in the persisted plan (structural inconsistency). No
    execution."""

    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    respond_plan = TaskPlan(
        plan_version=1,
        task_id=task.task_id,
        objective="x",
        steps=(_respond_step(1),),
        created_at="2026-08-08T00:00:00+00:00",
    )
    repo._conn.execute(
        "UPDATE tasks SET plan_json = ? WHERE task_id = ?",
        (serialize_plan(respond_plan), task.task_id),
    )

    executor = _FakeExecutor([])
    result = approve_task_confirmation(
        waiting_task, repo, registry, tools_config, executor, pending.confirmation_id
    )

    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1) is None
    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "plan_identity_violation"


def test_untampered_pending_confirmation_still_executes_exactly_once(repo, registry, tools_config):
    """Test F: exact untampered pending confirmation - existing approval
    behavior still succeeds and the executor is called exactly once."""

    task = _running_task(repo, [_action_step(1, "repository_backup", "ai_os")])
    executor = _FakeExecutor([])
    advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    waiting_task = repo.get_task(task.task_id)
    pending = repo.get_pending_confirmation(task.task_id)

    executor = _FakeExecutor([ActionResult(True, "Backup created.", "executed")])
    result = approve_task_confirmation(
        waiting_task, repo, registry, tools_config, executor, pending.confirmation_id
    )

    assert len(executor.calls) == 1
    assert executor.calls[0] == ActionRequest(action="repository_backup", resource_key="ai_os")
    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert repo.get_step_progress(task.task_id, 1).status.value == "succeeded"


# --- RESPOND synthesis (Milestone 42 P3) ----------------------------------


def test_A_respond_success_with_dependency_calls_model_once_and_stays_running(repo, registry, tools_config):
    """Instruction test A."""

    task = _running_task(
        repo, [_action_step(1, "list_files", "downloads"), _respond_step(2, depends_on=(1,))]
    )
    executor = _FakeExecutor([ActionResult(True, "3 files found.", "executed")])
    fake_model = _FakeModelProvider(responses=[ModelResponse("Here are your 3 files.", "fake", 0, 0, 0.0)])

    advance_task_execution(task, repo, registry, tools_config, executor, fake_model)
    running_task = repo.get_task(task.task_id)

    result = advance_task_execution(running_task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert result.task.state == TaskState.RUNNING
    assert len(fake_model.calls) == 1
    assert "3 files found." in fake_model.calls[0]

    step = repo.get_step_progress(task.task_id, 2)
    assert step.status.value == "succeeded"
    observation = deserialize_observation(step.result_json)
    assert observation.step_kind == StepKind.RESPOND
    assert observation.success is True
    assert observation.safe_summary == "Here are your 3 files."


def test_B_next_advance_after_final_respond_completes_task(repo, registry, tools_config):
    """Instruction test B."""

    task = _running_task(repo, [_respond_step(1)])
    fake_model = _FakeModelProvider(responses=[ModelResponse("All done.", "fake", 0, 0, 0.0)])
    executor = _FakeExecutor([])

    advance_task_execution(task, repo, registry, tools_config, executor, fake_model)
    running_task = repo.get_task(task.task_id)

    result = advance_task_execution(running_task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert len(fake_model.calls) == 1


def test_C_provider_exception_fails_step_and_task_with_no_exception_detail(repo, registry, tools_config):
    """Instruction test C."""

    task = _running_task(repo, [_respond_step(1)])
    executor = _FakeExecutor([])
    fake_model = _FakeModelProvider(exception=ConnectionError("host unreachable: 10.0.0.5 secret"))

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "respond_provider_unavailable"
    assert result.task.state == TaskState.FAILED
    assert "10.0.0.5" not in (result.task.failure_summary or "")
    assert "secret" not in (result.task.failure_summary or "")

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "failed"
    assert step.failure_code == "respond_provider_unavailable"
    assert "10.0.0.5" not in step.result_json
    assert "secret" not in step.result_json


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(12345, "fake", 0, 0, 0.0),
        ModelResponse("   ", "fake", 0, 0, 0.0),
        ModelResponse("bad\x00text", "fake", 0, 0, 0.0),
        ModelResponse("x" * (MAX_RESPOND_TEXT_CHARS + 1), "fake", 0, 0, 0.0),
    ],
)
def test_D_invalid_model_output_fails_closed(repo, registry, tools_config, response):
    """Instruction test D."""

    task = _running_task(repo, [_respond_step(1)])
    executor = _FakeExecutor([])
    fake_model = _FakeModelProvider(responses=[response])

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "respond_invalid_output"


def test_E_dependency_result_json_missing_fails_closed_without_model_call(repo, registry, tools_config):
    """Instruction test E."""

    task = _running_task(repo, [_action_step(1, "list_files", "downloads"), _respond_step(2, depends_on=(1,))])
    repo.claim_step(task.task_id, 1)
    repo._conn.execute(
        "UPDATE task_step_progress SET status = 'succeeded', result_json = NULL "
        "WHERE task_id = ? AND step_position = 1",
        (task.task_id,),
    )
    executor = _FakeExecutor([])
    fake_model = _FakeModelProvider(responses=[ModelResponse("should not be used", "fake", 0, 0, 0.0)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "respond_dependency_integrity_violation"
    assert fake_model.calls == []


def test_F_dependency_observation_malformed_fails_closed_without_model_call(repo, registry, tools_config):
    """Instruction test F."""

    task = _running_task(repo, [_action_step(1, "list_files", "downloads"), _respond_step(2, depends_on=(1,))])
    repo.claim_step(task.task_id, 1)
    repo.mark_step_succeeded(task.task_id, 1, '{"not": "an observation"}')
    executor = _FakeExecutor([])
    fake_model = _FakeModelProvider(responses=[ModelResponse("should not be used", "fake", 0, 0, 0.0)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "respond_dependency_integrity_violation"
    assert fake_model.calls == []


def test_G_dependency_observation_step_position_mismatch_fails_closed_without_model_call(
    repo, registry, tools_config
):
    """Instruction test G."""

    task = _running_task(repo, [_action_step(1, "list_files", "downloads"), _respond_step(2, depends_on=(1,))])
    repo.claim_step(task.task_id, 1)
    mismatched = build_action_observation(
        99, ActionResult(True, "ok", "executed"), "2026-08-08T00:00:00+00:00"
    )
    repo.mark_step_succeeded(task.task_id, 1, serialize_observation(mismatched))
    executor = _FakeExecutor([])
    fake_model = _FakeModelProvider(responses=[ModelResponse("should not be used", "fake", 0, 0, 0.0)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "respond_dependency_integrity_violation"
    assert fake_model.calls == []


def test_H_cancellation_after_claim_may_still_record_response_but_task_stays_cancelled(
    repo, db_path, registry, tools_config
):
    """Instruction test H: cancellation races the RESPOND step AFTER it is
    claimed but BEFORE the provider returns. Uses threading.Event to force
    the exact interleaving deterministically (no sleeps)."""

    task = _running_task(repo, [_respond_step(1)])
    executor = _FakeExecutor([])
    claimed = threading.Event()
    cancelled = threading.Event()

    class _SlowModelProvider:
        def __init__(self):
            self.calls: list[str] = []

        def send_prompt(self, prompt, *, options=None):
            self.calls.append(prompt)
            claimed.set()
            cancelled.wait(timeout=5)
            return ModelResponse("The task finished.", "fake", 0, 0, 0.0)

    slow_model = _SlowModelProvider()
    result_holder = {}

    def run_advance():
        result_holder["result"] = advance_task_execution(
            task, repo, registry, tools_config, executor, slow_model
        )

    thread = threading.Thread(target=run_advance)
    thread.start()
    assert claimed.wait(timeout=5)

    # A second writer connection cancels the task while the model call is
    # still in flight - mirrors _finalize_action_step()'s own cancellation-
    # after-claim test (test E in the P2 suite above).
    second_conn = open_writer_connection(db_path)
    second_repo = TaskRepository(second_conn)
    second_repo.mark_cancelled(task.task_id, "running")
    cancelled.set()

    thread.join(timeout=5)
    second_conn.close()

    result = result_holder["result"]
    assert result.status == ExecutionAdvanceStatus.TASK_CANCELLED
    assert result.task.state == TaskState.CANCELLED

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"
    observation = deserialize_observation(step.result_json)
    assert observation.safe_summary == "The task finished."


# --- RESPOND persistence bound: raw text bound alone does not guarantee ------
# --- the serialized StepObservation fits within MAX_STEP_RESULT_JSON_CHARS ---
#
# A response passing respond.py's own MAX_RESPOND_TEXT_CHARS raw-length
# check can still fail to fit once wrapped in a StepObservation and run
# through serialize_observation()'s json.dumps(): quote/backslash/control-
# character escaping expands the serialized length past the raw one. Every
# test below proves this is caught deterministically (never an uncaught
# ObservationSerializationError, never a step left in_progress, never
# silently truncated) and fails the step/task closed with the SAME fixed
# respond_invalid_output code/summary an ordinary invalid response uses -
# never a new code, and never any of the actual (escaping-heavy) text.


def test_respond_output_with_many_quotes_overflows_serialized_bound_and_fails_closed(
    repo, registry, tools_config
):
    task = _running_task(repo, [_respond_step(1)])
    executor = _FakeExecutor([])
    # Under the raw MAX_RESPOND_TEXT_CHARS bound, but every quote doubles
    # to \" once JSON-escaped - the serialized StepObservation is roughly
    # 2x this text's own length, well past MAX_STEP_RESULT_JSON_CHARS.
    text = '"' * MAX_RESPOND_TEXT_CHARS
    assert len(text) <= MAX_RESPOND_TEXT_CHARS
    fake_model = _FakeModelProvider(responses=[ModelResponse(text, "fake", 0, 0, 0.0)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "respond_invalid_output"
    assert result.task.state == TaskState.FAILED
    assert '"""' not in (result.task.failure_summary or "")

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "failed"
    assert step.failure_code == "respond_invalid_output"
    assert text not in (step.result_json or "")


def test_respond_output_with_many_backslashes_overflows_serialized_bound_and_fails_closed(
    repo, registry, tools_config
):
    task = _running_task(repo, [_respond_step(1)])
    executor = _FakeExecutor([])
    # Every backslash doubles to \\ once JSON-escaped.
    text = "\\" * MAX_RESPOND_TEXT_CHARS
    assert len(text) <= MAX_RESPOND_TEXT_CHARS
    fake_model = _FakeModelProvider(responses=[ModelResponse(text, "fake", 0, 0, 0.0)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "respond_invalid_output"
    assert result.task.state == TaskState.FAILED

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "failed"
    assert step.failure_code == "respond_invalid_output"
    assert text not in (step.result_json or "")


def test_respond_output_with_many_unicode_characters_does_not_expand_when_escaped(
    repo, registry, tools_config
):
    """serialize_observation() calls json.dumps(..., ensure_ascii=False),
    so non-ASCII characters are NOT escaped to a \\uXXXX sequence - they
    stay exactly one Python string character each, unlike quotes/
    backslashes/control characters. A response made entirely of non-ASCII
    characters at the raw bound must therefore still fit and succeed -
    proving the escaping concern is specifically about quotes/backslashes/
    control characters, not Unicode content in general."""

    task = _running_task(repo, [_respond_step(1)])
    executor = _FakeExecutor([])
    text = "é中\U0001f600" * (MAX_RESPOND_TEXT_CHARS // 3)  # e.g. é中\U0001f600 repeated
    assert len(text) <= MAX_RESPOND_TEXT_CHARS
    fake_model = _FakeModelProvider(responses=[ModelResponse(text, "fake", 0, 0, 0.0)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"
    assert deserialize_observation(step.result_json).safe_summary == text


def test_respond_ordinary_near_limit_output_serializes_and_succeeds(repo, registry, tools_config):
    """The counterpart to the escaping-overflow tests above: an ordinary
    (non-escaping-heavy) response at the raw MAX_RESPOND_TEXT_CHARS bound
    genuinely fits within MAX_STEP_RESULT_JSON_CHARS and succeeds
    normally - the new serialize-time guard does not affect the common
    case."""

    task = _running_task(repo, [_respond_step(1)])
    executor = _FakeExecutor([])
    text = "y" * MAX_RESPOND_TEXT_CHARS
    fake_model = _FakeModelProvider(responses=[ModelResponse(text, "fake", 0, 0, 0.0)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"
    assert deserialize_observation(step.result_json).safe_summary == text


# --- RESPOND prompt size bound (Milestone 42 P3 correction pass) -------------


def test_respond_oversized_combined_dependency_context_fails_closed_end_to_end(
    repo, registry, tools_config
):
    """End-to-end counterpart to test_respond.py's own
    MAX_RESPOND_PROMPT_CHARS unit tests: two durable dependency
    observations whose COMBINED safe_summary size overflows the prompt
    bound (raw-SQL fault-injected past what a real, single persisted
    result_json could ever hold on its own - see
    test_respond.py:_oversized_dependency_progress()'s own docstring for
    why that is the only way to reach this combined-size condition at
    all) must fail closed with respond_context_too_large, never call the
    model, and never persist the oversized text."""

    task = _running_task(
        repo,
        [
            _action_step(1, "list_files", "downloads"),
            _action_step(2, "system_status", None),
            _respond_step(3, depends_on=(1, 2)),
        ],
    )
    repo.claim_step(task.task_id, 1)
    repo.claim_step(task.task_id, 2)
    oversized_json_1 = (
        '{"observation_version":1,"step_position":1,"step_kind":"action","success":true,'
        f'"safe_summary":"{"a" * 15000}","failure_code":null,"action_outcome":"executed",'
        '"completed_at":"2026-08-08T00:01:00+00:00"}'
    )
    oversized_json_2 = (
        '{"observation_version":1,"step_position":2,"step_kind":"action","success":true,'
        f'"safe_summary":"{"b" * 15000}","failure_code":null,"action_outcome":"executed",'
        '"completed_at":"2026-08-08T00:01:00+00:00"}'
    )
    repo._conn.execute(
        "UPDATE task_step_progress SET status = 'succeeded', result_json = ? "
        "WHERE task_id = ? AND step_position = 1",
        (oversized_json_1, task.task_id),
    )
    repo._conn.execute(
        "UPDATE task_step_progress SET status = 'succeeded', result_json = ? "
        "WHERE task_id = ? AND step_position = 2",
        (oversized_json_2, task.task_id),
    )
    executor = _FakeExecutor([])
    fake_model = _FakeModelProvider(responses=[ModelResponse("should not be used", "fake", 0, 0, 0.0)])

    result = advance_task_execution(task, repo, registry, tools_config, executor, fake_model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "respond_context_too_large"
    assert result.task.state == TaskState.FAILED
    assert fake_model.calls == []

    step = repo.get_step_progress(task.task_id, 3)
    assert step.status.value == "failed"
    assert step.failure_code == "respond_context_too_large"
    assert "a" * 15000 not in (step.result_json or "")
    assert "b" * 15000 not in (step.result_json or "")


# --- blocked / integrity / revalidation outcomes -------------------------------


def test_blocked_step_failed_fails_task(repo, registry, tools_config):
    task = _running_task(
        repo,
        [_action_step(1, "list_files", "downloads"), _action_step(2, "system_status", None)],
    )
    repo.claim_step(task.task_id, 1)
    repo.mark_step_failed(task.task_id, 1, "tool_error", "it failed")

    executor = _FakeExecutor([])
    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "step_failed"
    assert executor.calls == []


def test_blocked_step_in_progress_fails_task(repo, registry, tools_config):
    task = _running_task(
        repo,
        [_action_step(1, "list_files", "downloads"), _action_step(2, "system_status", None)],
    )
    repo.claim_step(task.task_id, 1)  # left in_progress - uncertain outcome

    executor = _FakeExecutor([])
    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "step_execution_uncertain"
    assert executor.calls == []


def test_plan_identity_mismatch_fails_task(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "system_status", None)])
    # Overwrite plan_json with one whose embedded task_id doesn't match.
    record = repo.create_task("unrelated", "whatsapp")
    other_plan = TaskPlan(
        plan_version=1,
        task_id=record.task_id,  # wrong task_id for `task`
        objective="x",
        steps=(_action_step(1, "system_status", None),),
        created_at="2026-08-08T00:00:00+00:00",
    )
    repo._conn.execute(
        "UPDATE tasks SET plan_json = ? WHERE task_id = ?",
        (serialize_plan(other_plan), task.task_id),
    )

    executor = _FakeExecutor([])
    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "plan_identity_violation"
    assert executor.calls == []


def test_plan_deserialization_failure_fails_task(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "system_status", None)])
    repo._conn.execute(
        "UPDATE tasks SET plan_json = 'not json' WHERE task_id = ?", (task.task_id,)
    )

    executor = _FakeExecutor([])
    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "plan_storage_corrupt"
    assert executor.calls == []


def test_action_revalidation_failure_fails_task(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "run_registered_script", "removed_script")])
    executor = _FakeExecutor([])

    result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "action_no_longer_valid"
    assert executor.calls == []


# --- caller-contract violations ------------------------------------------------


def test_advance_created_task_raises(repo, registry, tools_config):
    record = repo.create_task("do something", "whatsapp")
    executor = _FakeExecutor([])
    with pytest.raises(TaskNotReadyOrRunningError):
        advance_task_execution(record, repo, registry, tools_config, executor, model_provider)


def test_advance_terminal_task_is_a_no_op(repo, registry, tools_config):
    record = repo.create_task("do something", "whatsapp")
    cancelled = repo.mark_cancelled(record.task_id, "created")
    executor = _FakeExecutor([])

    result = advance_task_execution(cancelled, repo, registry, tools_config, executor, model_provider)

    assert result.status == ExecutionAdvanceStatus.TASK_CANCELLED
    assert executor.calls == []


def test_approve_wrong_state_raises(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "system_status", None)])
    executor = _FakeExecutor([])
    with pytest.raises(TaskNotWaitingForConfirmationError):
        approve_task_confirmation(task, repo, registry, tools_config, executor, "some-id")


def test_deny_wrong_state_raises(repo, registry, tools_config):
    task = _running_task(repo, [_action_step(1, "system_status", None)])
    with pytest.raises(TaskNotWaitingForConfirmationError):
        deny_task_confirmation(task, repo, "some-id")


# --- cancellation semantics (Milestone 42 P2 section 4/12 D-E) ----------------


def test_cancellation_before_claim_prevents_execution(repo, registry, tools_config):
    """Test D: a task cancelled before a non-sensitive step is claimed
    must reject the claim and never call the executor. claim_step()
    re-reads state fresh (P1) and finds the task already CANCELLED - a
    terminal state - so this surfaces as TaskAlreadyTerminalError."""

    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    repo.mark_cancelled(task.task_id, "running")

    executor = _FakeExecutor([])
    with pytest.raises(TaskAlreadyTerminalError):
        advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
    assert executor.calls == []


def test_cancellation_after_claim_success_preserves_observation_but_task_stays_cancelled(
    repo,
):
    """Test E (success case): the already-claimed action completes and its
    observation is persisted, but the task remains CANCELLED and is never
    reported as STEP_SUCCEEDED."""

    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    repo.claim_step(task.task_id, 1)
    repo.mark_cancelled(task.task_id, "running")

    result = _finalize_action_step(
        task.task_id, repo, 1, ActionResult(True, "3 files found.", "executed")
    )

    assert result.status == ExecutionAdvanceStatus.TASK_CANCELLED
    assert result.task.state == TaskState.CANCELLED

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"
    assert step.result_json is not None


def test_cancellation_after_claim_failure_preserves_observation_but_task_stays_cancelled(
    repo,
):
    """Test E (failure case): same as above, but the already-claimed
    action failed - the task must still remain CANCELLED, never
    overwritten to FAILED."""

    task = _running_task(repo, [_action_step(1, "list_files", "downloads")])
    repo.claim_step(task.task_id, 1)
    repo.mark_cancelled(task.task_id, "running")

    result = _finalize_action_step(
        task.task_id, repo, 1, ActionResult(False, "could not complete", "failed")
    )

    assert result.status == ExecutionAdvanceStatus.TASK_CANCELLED
    assert result.task.state == TaskState.CANCELLED

    step = repo.get_step_progress(task.task_id, 1)
    assert step.status.value == "failed"
    assert step.failure_code == "failed"


# --- concurrency: two workers race the same non-sensitive step (test F) ------


class _BarrierSynchronizedRepository:
    """Wraps a real TaskRepository, delaying exactly the claim_step() call
    until a second thread has ALSO reached its own claim_step() call - the
    actual contested boundary claim_step() guarantees exactly-one-winner
    for (its (task_id, step_position) PRIMARY KEY - see that method's own
    docstring). Every other method delegates straight through unchanged.

    Needed because firing two ordinary, unsynchronized threads does not
    reliably exercise the claim race this test means to prove: with no
    synchronization, OS thread scheduling may just as easily let one
    thread's ENTIRE advance_task_execution() call (evaluate_next_step ->
    claim_step -> execute -> finalize) run to completion before the second
    thread is scheduled at all. That is not a bug - it exercises a
    different, independently-correct interleaving (eligibility.py's own
    documented STEP_IN_PROGRESS/AllStepsComplete handling for two
    sequential callers, not two truly concurrent ones) - but it makes this
    specific test, which exists to prove the claim_step() race itself
    resolves to exactly one winner, flaky rather than deterministic.
    Synchronizing at claim_step() guarantees both threads have already
    independently completed their own evaluate_next_step() (both seeing
    the step as not-yet-claimed, by construction - neither could have
    claimed it before both arrive at the barrier) before either attempts
    the real, contested claim."""

    def __init__(self, repository: TaskRepository, barrier: threading.Barrier) -> None:
        self._repository = repository
        self._barrier = barrier

    def claim_step(self, *args, **kwargs):
        self._barrier.wait(timeout=5)
        return self._repository.claim_step(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._repository, name)


def test_two_workers_race_same_non_sensitive_step_exactly_one_executes(db_path, tools_config, registry):
    conn_a = open_writer_connection(db_path)
    conn_b = open_writer_connection(db_path)
    barrier = threading.Barrier(2)
    repo_a = _BarrierSynchronizedRepository(TaskRepository(conn_a), barrier)
    repo_b = _BarrierSynchronizedRepository(TaskRepository(conn_b), barrier)

    task = _running_task(repo_a, [_action_step(1, "list_files", "downloads")])

    executor_a = _FakeExecutor([ActionResult(True, "ok-a", "executed")])
    executor_b = _FakeExecutor([ActionResult(True, "ok-b", "executed")])
    outcomes = {}

    def attempt(repo, executor, tag):
        try:
            result = advance_task_execution(task, repo, registry, tools_config, executor, model_provider)
            outcomes[tag] = result.status
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed silently
            outcomes[tag] = type(exc).__name__

    t_a = threading.Thread(target=attempt, args=(repo_a, executor_a, "a"))
    t_b = threading.Thread(target=attempt, args=(repo_b, executor_b, "b"))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    succeeded = [tag for tag, status in outcomes.items() if status == ExecutionAdvanceStatus.STEP_SUCCEEDED]
    rejected = [tag for tag, status in outcomes.items() if status == "StepAlreadyClaimedError"]
    assert len(succeeded) == 1
    assert len(rejected) == 1

    # The executor was called at most once in total.
    total_calls = len(executor_a.calls) + len(executor_b.calls)
    assert total_calls == 1

    final = repo_a.get_task(task.task_id)
    assert final.state == TaskState.RUNNING
    step = repo_a.get_step_progress(task.task_id, 1)
    assert step.status.value == "succeeded"

    conn_a.close()
    conn_b.close()


# --- safety boundary: SafeTaskExecutor is the only execution boundary --------

_PACKAGE_DIR = Path(__file__).resolve().parents[3] / "kernel" / "task_execution"

_SERVICE_FORBIDDEN_IMPORT_PREFIXES = (
    "kernel.employee_tasks.db",
    "kernel.employee_tasks.repository",
    "kernel.tools.confirmation",
    "kernel.tools.process_control",
    "kernel.tools.handlers",
    "kernel.task_planner.catalog",
    "kernel.task_planner.planner",
    "kernel.task_planner.prompt",
    "kernel.task_planner.parser",
    # Milestone 42 P3: service.py may orchestrate the injected
    # conversational ModelProvider for RESPOND (see module docstring), so
    # only kernel.models.factory (which decides/constructs a CONCRETE
    # provider from config) and every concrete provider module are
    # forbidden here - the abstract kernel.models.base contract is
    # allowed, verified separately by
    # test_service_only_imports_base_from_kernel_models() below.
    "kernel.models.factory",
    "kernel.models.anthropic",
    "kernel.models.openai",
    "kernel.models.gemini",
    "kernel.models.ollama",
    "kernel.action_protocol",
    "interfaces",
)


def _imported_module_names(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_service_never_imports_confirmation_store_process_control_or_handlers():
    py_file = _PACKAGE_DIR / "service.py"
    imported = _imported_module_names(py_file)
    for forbidden in _SERVICE_FORBIDDEN_IMPORT_PREFIXES:
        matches = {
            name for name in imported if name == forbidden or name.startswith(forbidden + ".")
        }
        assert not matches, f"service.py imports forbidden module(s): {matches}"


def test_service_only_imports_executor_and_types_from_kernel_tools():
    py_file = _PACKAGE_DIR / "service.py"
    imported = _imported_module_names(py_file)
    tools_imports = {name for name in imported if name.startswith("kernel.tools")}
    assert tools_imports <= {
        "kernel.tools.executor",
        "kernel.tools.types",
        "kernel.tools.config",
        "kernel.tools.registry",
    }, f"service.py imports unexpected kernel.tools module(s): {tools_imports}"


def test_service_only_imports_base_from_kernel_models():
    py_file = _PACKAGE_DIR / "service.py"
    imported = _imported_module_names(py_file)
    models_imports = {name for name in imported if name.startswith("kernel.models")}
    assert models_imports <= {"kernel.models.base"}, (
        f"service.py imports unexpected kernel.models module(s): {models_imports}"
    )


def test_service_only_imports_the_plain_types_from_employee_tasks():
    py_file = _PACKAGE_DIR / "service.py"
    imported = _imported_module_names(py_file)
    employee_tasks_imports = {name for name in imported if name.startswith("kernel.employee_tasks")}
    assert employee_tasks_imports <= {"kernel.employee_tasks"}, (
        f"service.py imports from kernel.employee_tasks submodules directly: "
        f"{employee_tasks_imports}"
    )
