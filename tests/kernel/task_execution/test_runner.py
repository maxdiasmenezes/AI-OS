"""End-to-end tests for kernel/task_execution/service.py's
run_task_until_blocked() - Milestone 42 P3's bounded autonomous execution
runner. Every test uses a real tmp_path SQLite database and a real
TaskRepository/ActionRegistry, and deterministic fakes for SafeTaskExecutor
and ModelProvider - never a real process, subprocess, network, or model-
host call. The runner itself is only ever exercised through its public
signature (task, repository, registry, tools_config, executor,
model_provider) - never by reaching into service.py's private helpers."""

import inspect

import pytest

from kernel.employee_tasks import TaskRepository, TaskState, open_writer_connection
from kernel.task_execution.observation import deserialize_observation
from kernel.task_execution.service import approve_task_confirmation, run_task_until_blocked
from kernel.task_execution.types import ExecutionAdvanceStatus
from kernel.models.base import ModelResponse
from kernel.task_planner import MAX_PLAN_STEPS, PlanStep, StepKind, TaskPlan, serialize_plan
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
    ModelResponses in call order - never a real network/subprocess call."""

    def __init__(self, responses=None):
        self._responses = list(responses) if responses is not None else []
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        return self._responses.pop(0)


class _RaisingModelProvider:
    """Proves an ACTION-only run never touches the model at all."""

    def send_prompt(self, prompt, *, options=None):
        raise AssertionError("model_provider.send_prompt() must not be called for an ACTION-only plan")


def _action_step(position, action_name, resource_key, *, depends_on=()):
    return PlanStep(
        step_id=f"step_{position}",
        position=position,
        kind=StepKind.ACTION,
        action_name=action_name,
        resource_key=resource_key,
        catalog_id="action_1",
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


def _ready_task(repo, steps):
    record = repo.create_task("do the plan", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")
    plan = TaskPlan(
        plan_version=1,
        task_id=record.task_id,
        objective="do the plan",
        steps=tuple(steps),
        created_at="2026-08-08T00:00:00+00:00",
    )
    return repo.persist_plan_and_ready(record.task_id, "planning", serialize_plan(plan))


# --- 1: ACTION -> ACTION -------------------------------------------------


def test_1_action_then_action_runs_automatically_to_completion(repo, registry, tools_config):
    task = _ready_task(
        repo, [_action_step(1, "list_files", "downloads"), _action_step(2, "system_status", None)]
    )
    executor = _FakeExecutor([ActionResult(True, "3 files.", "executed"), ActionResult(True, "healthy.", "executed")])
    model = _RaisingModelProvider()

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert len(executor.calls) == 2
    assert executor.calls[0] == ActionRequest(action="list_files", resource_key="downloads")
    assert executor.calls[1] == ActionRequest(action="system_status", resource_key=None)


# --- 2: ACTION -> RESPOND -------------------------------------------------


def test_2_action_then_respond_synthesizes_from_durable_observation(repo, registry, tools_config):
    task = _ready_task(
        repo, [_action_step(1, "list_files", "downloads"), _respond_step(2, depends_on=(1,))]
    )
    executor = _FakeExecutor([ActionResult(True, "3 files found.", "executed")])
    model = _FakeModelProvider(responses=[ModelResponse("There are 3 files in downloads.", "fake", 0, 0, 0.0)])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert len(executor.calls) == 1
    assert len(model.calls) == 1
    assert "3 files found." in model.calls[0]

    step = repo.get_step_progress(task.task_id, 2)
    observation = deserialize_observation(step.result_json)
    assert observation.safe_summary == "There are 3 files in downloads."


# --- 3/4: sensitive ACTION stops the runner; approval resumes it ---------


def test_3_sensitive_action_stops_runner_without_executing(repo, registry, tools_config):
    task = _ready_task(
        repo, [_action_step(1, "repository_backup", "ai_os"), _respond_step(2, depends_on=(1,))]
    )
    executor = _FakeExecutor([])
    model = _RaisingModelProvider()

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.CONFIRMATION_REQUIRED
    assert result.task.state == TaskState.WAITING_FOR_CONFIRMATION
    assert executor.calls == []

    pending = repo.get_pending_confirmation(task.task_id)
    assert pending.action_name == "repository_backup"


def test_4_approval_then_rerunning_the_runner_continues_remaining_steps(repo, registry, tools_config):
    task = _ready_task(
        repo, [_action_step(1, "repository_backup", "ai_os"), _respond_step(2, depends_on=(1,))]
    )
    executor = _FakeExecutor([])
    model = _RaisingModelProvider()
    stopped = run_task_until_blocked(task, repo, registry, tools_config, executor, model)
    assert stopped.status == ExecutionAdvanceStatus.CONFIRMATION_REQUIRED

    pending = repo.get_pending_confirmation(task.task_id)
    executor = _FakeExecutor([ActionResult(True, "Backup created.", "executed")])
    approved = approve_task_confirmation(
        stopped.task, repo, registry, tools_config, executor, pending.confirmation_id
    )
    assert approved.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert len(executor.calls) == 1

    model = _FakeModelProvider(responses=[ModelResponse("Backup complete.", "fake", 0, 0, 0.0)])
    result = run_task_until_blocked(approved.task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert len(model.calls) == 1
    step = repo.get_step_progress(task.task_id, 2)
    assert deserialize_observation(step.result_json).safe_summary == "Backup complete."


# --- 5: ACTION failure stops the runner -----------------------------------


def test_5_action_failure_stops_runner_and_later_steps_never_run(repo, registry, tools_config):
    task = _ready_task(
        repo, [_action_step(1, "list_files", "downloads"), _action_step(2, "system_status", None)]
    )
    executor = _FakeExecutor([ActionResult(False, "could not list files", "failed")])
    model = _RaisingModelProvider()

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.task.state == TaskState.FAILED
    assert len(executor.calls) == 1
    assert repo.get_step_progress(task.task_id, 2) is None


# --- 6: RESPOND failure stops the runner ------------------------------------


def test_6_respond_failure_stops_runner(repo, registry, tools_config):
    task = _ready_task(repo, [_respond_step(1)])
    executor = _FakeExecutor([])
    model = _FakeModelProvider(responses=[ModelResponse("", "fake", 0, 0, 0.0)])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "respond_invalid_output"


# --- 7: pre-existing unknown in_progress state --------------------------


def test_7_pre_existing_in_progress_step_stops_runner_without_retry(repo, registry, tools_config):
    task = _ready_task(
        repo, [_action_step(1, "list_files", "downloads"), _action_step(2, "system_status", None)]
    )
    running = repo.transition_task(task.task_id, "ready", "running")
    repo.claim_step(running.task_id, 1)  # left in_progress - uncertain outcome

    executor = _FakeExecutor([])
    model = _RaisingModelProvider()

    result = run_task_until_blocked(running, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "step_execution_uncertain"
    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1).status.value == "in_progress"


# --- 8: dependency enforcement remains intact end-to-end --------------------


def test_8_respond_uses_only_its_declared_dependency_not_unrelated_steps(repo, registry, tools_config):
    task = _ready_task(
        repo,
        [
            _action_step(1, "list_files", "downloads"),
            _action_step(2, "system_status", None, depends_on=(1,)),
            _respond_step(3, depends_on=(2,)),
        ],
    )
    executor = _FakeExecutor(
        [ActionResult(True, "3 files found.", "executed"), ActionResult(True, "all healthy.", "executed")]
    )
    model = _FakeModelProvider(responses=[ModelResponse("Everything is healthy.", "fake", 0, 0, 0.0)])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert len(model.calls) == 1
    assert "all healthy." in model.calls[0]
    assert "3 files found." not in model.calls[0]


# --- 9: the runner's own hard bound cannot be exceeded silently -----------


def test_9_runner_bound_fails_closed_rather_than_looping_unboundedly(
    repo, registry, tools_config, monkeypatch
):
    monkeypatch.setattr("kernel.task_execution.service.MAX_EXECUTION_ADVANCES", 2)

    task = _ready_task(
        repo,
        [
            _action_step(1, "list_files", "downloads"),
            _action_step(2, "system_status", None),
            _action_step(3, "system_status", None),
        ],
    )
    executor = _FakeExecutor(
        [
            ActionResult(True, "ok-1", "executed"),
            ActionResult(True, "ok-2", "executed"),
            ActionResult(True, "ok-3", "executed"),
        ]
    )
    model = _RaisingModelProvider()

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "execution_advance_limit_exceeded"
    assert result.task.state == TaskState.FAILED
    # Exactly the patched bound's worth of steps ran - the third step
    # (which would have made the plan complete) never executed.
    assert len(executor.calls) == 2
    assert repo.get_step_progress(task.task_id, 3) is None


def test_9b_max_size_valid_plan_completes_within_the_real_default_bound(repo, registry, tools_config):
    """MAX_EXECUTION_ADVANCES = MAX_PLAN_STEPS + 1 (types.py) - a plan at
    exactly the planner's own maximum size must always be able to finish
    without patching the bound at all: MAX_PLAN_STEPS successful advances
    (one per step) plus exactly one further advance to observe
    AllStepsComplete -> COMPLETED."""

    steps = [_action_step(position, "system_status", None) for position in range(1, MAX_PLAN_STEPS + 1)]
    task = _ready_task(repo, steps)
    executor = _FakeExecutor([ActionResult(True, f"ok-{i}", "executed") for i in range(1, MAX_PLAN_STEPS + 1)])
    model = _RaisingModelProvider()

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert len(executor.calls) == MAX_PLAN_STEPS


def test_9c_oversized_persisted_plan_is_rejected_immediately_never_reaches_execution(
    repo, registry, tools_config
):
    """Milestone 42 P3 correction: kernel.task_planner.parser.py's own
    MAX_PLAN_STEPS bound is enforced only at plan-PARSE time - never by
    kernel.task_planner.serialization.deserialize_plan(), and (before this
    correction) never re-checked anywhere in kernel/task_execution/ either.
    A persisted plan with MORE steps than the planner could ever produce
    (only reachable via a hand-crafted/corrupted plan_json, exactly as
    constructed here - never through the real planner) is a structurally
    invalid execution contract, not something MAX_EXECUTION_ADVANCES
    exists to paper over: it must be rejected on the very FIRST advance,
    before any step is ever claimed, executed, or sent to the model - see
    kernel.task_execution.eligibility's own PLAN SIZE INTEGRITY section.
    MAX_EXECUTION_ADVANCES remains a pure loop/progression safety bound,
    never a substitute for validating the persisted plan itself - see
    test_9/test_9b/test_9d for that distinct concern, exercised with
    legitimate, in-contract plans."""

    step_count = MAX_PLAN_STEPS + 3
    steps = [_action_step(position, "system_status", None) for position in range(1, step_count + 1)]
    task = _ready_task(repo, steps)
    executor = _FakeExecutor([ActionResult(True, f"ok-{i}", "executed") for i in range(1, step_count + 1)])
    model = _RaisingModelProvider()

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "plan_identity_violation"
    assert result.task.state == TaskState.FAILED
    assert executor.calls == []
    for position in range(1, step_count + 1):
        assert repo.get_step_progress(task.task_id, position) is None


def test_9d_runner_bound_still_trips_on_a_controlled_condition_with_a_legitimate_plan(
    repo, registry, tools_config, monkeypatch
):
    """The runner-limit test itself (see also test_9 above) - deliberately
    uses ONLY a legitimate, in-contract plan (well within MAX_PLAN_STEPS)
    with the bound patched down to a small, controlled value, so tripping
    MAX_EXECUTION_ADVANCES is exercised as a pure loop-safety concern,
    never by treating a structurally invalid oversized plan as
    executable (see test_9c above for that, now-corrected, distinct
    concern)."""

    monkeypatch.setattr("kernel.task_execution.service.MAX_EXECUTION_ADVANCES", 1)

    task = _ready_task(
        repo, [_action_step(1, "list_files", "downloads"), _action_step(2, "system_status", None)]
    )
    executor = _FakeExecutor([ActionResult(True, "ok-1", "executed")])
    model = _RaisingModelProvider()

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "execution_advance_limit_exceeded"
    assert result.task.state == TaskState.FAILED
    assert len(executor.calls) == 1
    assert repo.get_step_progress(task.task_id, 2) is None


# --- 10/11: no model for ACTION steps; no planner provider anywhere -------


def test_10_no_model_called_for_an_action_only_plan(repo, registry, tools_config):
    task = _ready_task(
        repo, [_action_step(1, "list_files", "downloads"), _action_step(2, "system_status", None)]
    )
    executor = _FakeExecutor([ActionResult(True, "ok", "executed"), ActionResult(True, "ok", "executed")])
    model = _RaisingModelProvider()  # raises if send_prompt() is ever called

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED


def test_11_runner_signature_has_no_planner_provider_parameter():
    """run_task_until_blocked() takes exactly the same kind of dependencies
    as advance_task_execution() - a conversational ModelProvider, never a
    planner provider or anything from kernel.task_planner.planner (see
    test_service.py's AST-based import-boundary tests for the exhaustive
    static check; this is a lightweight signature-level sanity check in
    this file)."""

    params = list(inspect.signature(run_task_until_blocked).parameters)
    assert params == ["task", "repository", "registry", "tools_config", "executor", "model_provider"]
