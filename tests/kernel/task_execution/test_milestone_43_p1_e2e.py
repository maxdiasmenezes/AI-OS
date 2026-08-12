"""End-to-end acceptance tests for Milestone 43 P1 (file_metadata,
read_text_file, list_processes) driven through the REAL M42 pipeline:
real TaskRepository (tmp_path SQLite), real ActionRegistry/ToolsConfig,
real SafeTaskExecutor and real handlers - not just a fake executor
recording calls, unlike tests/kernel/task_execution/test_runner.py's own
generic runner tests. Process enumeration is still faked (see
_FakeProcess) - never touches real processes on the machine running these
tests. The conversational model is always a deterministic fake - never a
real model/network call, matching every other test_runner.py-style test in
this package.

Proves the new actions need ZERO changes to kernel/task_execution/service.py,
eligibility.py, or respond.py - they are reachable purely because they are
registered actions with configured resources, exactly like the six
Milestone 33-35 actions already are."""

import pytest

from kernel.employee_tasks import TaskRepository, TaskState, open_writer_connection
from kernel.models.base import ModelResponse
from kernel.task_execution.observation import deserialize_observation
from kernel.task_execution.service import run_task_until_blocked
from kernel.task_execution.types import ExecutionAdvanceStatus
from kernel.task_planner import PlanStep, StepKind, TaskPlan, serialize_plan
from kernel.tools.config import FileSpec, ToolsConfig
from kernel.tools.executor import SafeTaskExecutor
from kernel.tools.handlers import list_processes as list_processes_handler
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest, ActionResult


class _FakeProcess:
    def __init__(self, pid, name, status="running"):
        self.pid = pid
        self._name = name
        self._status = status

    def name(self):
        return self._name

    def status(self):
        return self._status


class _FakeExecutor:
    """Records every ActionRequest it receives - used only for scenario D
    (stale resource revalidation), where the point is proving the real
    handler is never reached at all. Scenarios A-C use a real
    SafeTaskExecutor instead."""

    def __init__(self):
        self.calls: list[ActionRequest] = []

    def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request)
        raise AssertionError("SafeTaskExecutor must never be called for a revalidation failure")


class _FakeModelProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        return self._responses.pop(0)


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


def _config(approved_files):
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_files=approved_files,
    )


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


@pytest.fixture
def repo(tmp_path):
    conn = open_writer_connection(tmp_path / "tasks.sqlite3")
    yield TaskRepository(conn)
    conn.close()


@pytest.fixture
def registry():
    return ActionRegistry()


# --- A: file_metadata alone completes the task -------------------------


def test_a_file_metadata_action_completes_via_the_real_pipeline(repo, registry, tmp_path):
    target = tmp_path / "resume.pdf"
    target.write_bytes(b"pdf bytes")
    tools_config = _config({"resume_pdf": FileSpec(path=str(target))})
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "file_metadata", "resume_pdf")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert model.calls == []

    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert observation.success is True
    assert observation.step_kind == StepKind.ACTION
    assert "resume_pdf" in observation.safe_summary
    assert str(tmp_path) not in observation.safe_summary


# --- B: read_text_file -> RESPOND depends on the durable observation ----


def test_b_read_text_file_then_respond_synthesizes_from_durable_observation(
    repo, registry, tmp_path
):
    target = tmp_path / "notes.txt"
    target.write_bytes(b"The quarterly report is on track.")
    tools_config = _config({"notes_txt": FileSpec(path=str(target))})
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(
        responses=[ModelResponse("The report is on track.", "fake", 0, 0, 0.0)]
    )

    task = _ready_task(
        repo,
        [
            _action_step(1, "read_text_file", "notes_txt"),
            _respond_step(2, depends_on=(1,)),
        ],
    )

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert len(model.calls) == 1
    # The model actually saw the durable, safe file-content observation as
    # its evidence - proving the RESPOND step used the real ACTION result,
    # not a stale/synthetic value.
    assert "The quarterly report is on track." in model.calls[0]

    step_2 = repo.get_step_progress(task.task_id, 2)
    respond_observation = deserialize_observation(step_2.result_json)
    assert respond_observation.safe_summary == "The report is on track."


# --- C: list_processes alone completes the task -------------------------


def test_c_list_processes_action_completes_via_the_real_pipeline(repo, registry, monkeypatch):
    monkeypatch.setattr(
        list_processes_handler.psutil,
        "process_iter",
        lambda: iter([_FakeProcess(1, "explorer.exe"), _FakeProcess(2, "notepad.exe")]),
    )
    tools_config = _config({})
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "list_processes", None)])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED

    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert "explorer.exe" in observation.safe_summary
    assert "notepad.exe" in observation.safe_summary


# --- D: stale/unknown approved_files resource fails closed, never executes


def test_d_stale_approved_files_resource_fails_closed_without_ever_calling_the_executor(
    repo, registry, tmp_path
):
    # The plan was persisted referencing "resume_pdf", but the CURRENT
    # tools_config (as of execution time) no longer configures it - e.g.
    # removed from kernel/config/tools.yaml since planning. M42's existing,
    # unmodified eligibility revalidation must fail this closed before
    # SafeTaskExecutor (here, one that raises if ever called) is reached.
    tools_config = _config({})  # "resume_pdf" is not configured
    executor = _FakeExecutor()
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "file_metadata", "resume_pdf")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "action_no_longer_valid"
    assert result.task.state == TaskState.FAILED
    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1) is None
