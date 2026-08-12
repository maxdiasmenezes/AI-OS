"""End-to-end acceptance tests for Milestone 43 P2 (create_directory,
copy_file) driven through the REAL M42 pipeline: real TaskRepository
(tmp_path SQLite), real ActionRegistry/ToolsConfig, real SafeTaskExecutor
and real handlers - proving actual filesystem effects only ever occur
after explicit confirmation, never before, and never after denial. The
conversational model is always a deterministic fake - never a real
model/network call, matching every other test_runner.py-style test in
this package.

Proves the new actions need ZERO changes to
kernel/task_execution/service.py, eligibility.py, or respond.py - they
are reachable purely because they are registered, sensitive actions with
configured composite resources, exactly like repository_backup already
is."""

import pytest

from kernel.employee_tasks import TaskRepository, TaskState, open_writer_connection
from kernel.models.base import ModelResponse
from kernel.task_execution.observation import deserialize_observation
from kernel.task_execution.service import (
    approve_task_confirmation,
    deny_task_confirmation,
    run_task_until_blocked,
)
from kernel.task_execution.types import ExecutionAdvanceStatus
from kernel.task_planner import PlanStep, StepKind, TaskPlan, serialize_plan
from kernel.tools.config import DirectoryCreationSpec, FileCopySpec, FileSpec, ToolsConfig
from kernel.tools.executor import SafeTaskExecutor
from kernel.tools.registry import ActionRegistry


class _FakeModelProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        return self._responses.pop(0)


class _RaisingModelProvider:
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
        requires_confirmation=True,
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


# ============================= create_directory =============================


def _create_directory_config(tmp_path):
    parent = tmp_path / "documents"
    parent.mkdir()
    config = ToolsConfig(
        approved_directories={"documents": str(parent)},
        approved_applications={},
        approved_scripts={},
        approved_directory_creations={
            "project_exports": DirectoryCreationSpec("documents", "exports")
        },
    )
    return parent, config


def test_a_create_directory_stops_for_confirmation_with_filesystem_unchanged(
    repo, registry, tmp_path
):
    parent, tools_config = _create_directory_config(tmp_path)
    executor = SafeTaskExecutor(tools_config, registry)
    model = _RaisingModelProvider()

    task = _ready_task(repo, [_action_step(1, "create_directory", "project_exports")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.CONFIRMATION_REQUIRED
    assert result.task.state == TaskState.WAITING_FOR_CONFIRMATION
    assert not (parent / "exports").exists()

    pending = repo.get_pending_confirmation(task.task_id)
    assert pending.action_name == "create_directory"
    assert pending.resource_key == "project_exports"


def test_b_approving_create_directory_creates_the_exact_directory_and_completes(
    repo, registry, tmp_path
):
    parent, tools_config = _create_directory_config(tmp_path)
    executor = SafeTaskExecutor(tools_config, registry)
    model = _RaisingModelProvider()

    task = _ready_task(repo, [_action_step(1, "create_directory", "project_exports")])
    stopped = run_task_until_blocked(task, repo, registry, tools_config, executor, model)
    pending = repo.get_pending_confirmation(task.task_id)

    approved = approve_task_confirmation(
        stopped.task, repo, registry, tools_config, executor, pending.confirmation_id
    )
    assert approved.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert (parent / "exports").is_dir()

    # approve_task_confirmation() advances exactly the one approved step -
    # a further advance is needed to OBSERVE that the (single-step) plan
    # is now complete, exactly like test_runner.py's own
    # test_4_approval_then_rerunning_the_runner_continues_remaining_steps.
    result = run_task_until_blocked(approved.task, repo, registry, tools_config, executor, model)
    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED

    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert observation.success is True
    assert str(tmp_path) not in observation.safe_summary


def test_c_denying_create_directory_cancels_the_task_with_filesystem_unchanged(
    repo, registry, tmp_path
):
    parent, tools_config = _create_directory_config(tmp_path)
    executor = SafeTaskExecutor(tools_config, registry)
    model = _RaisingModelProvider()

    task = _ready_task(repo, [_action_step(1, "create_directory", "project_exports")])
    stopped = run_task_until_blocked(task, repo, registry, tools_config, executor, model)
    pending = repo.get_pending_confirmation(task.task_id)

    denied = deny_task_confirmation(stopped.task, repo, pending.confirmation_id)

    assert denied.status == ExecutionAdvanceStatus.TASK_CANCELLED
    assert denied.task.state == TaskState.CANCELLED
    assert not (parent / "exports").exists()
    assert repo.get_step_progress(task.task_id, 1) is None


def test_d_stale_create_directory_resource_fails_closed_without_creating_anything(
    repo, registry, tmp_path
):
    parent = tmp_path / "documents"
    parent.mkdir()
    # The plan references "project_exports", but the CURRENT tools_config
    # no longer configures it - e.g. removed from kernel/config/tools.yaml
    # since planning.
    tools_config = ToolsConfig(
        approved_directories={"documents": str(parent)},
        approved_applications={},
        approved_scripts={},
        approved_directory_creations={},
    )
    executor = SafeTaskExecutor(tools_config, registry)
    model = _RaisingModelProvider()

    task = _ready_task(repo, [_action_step(1, "create_directory", "project_exports")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "action_no_longer_valid"
    assert result.task.state == TaskState.FAILED
    assert not (parent / "exports").exists()
    assert repo.get_step_progress(task.task_id, 1) is None


# ================================ copy_file ==================================


def _copy_file_config(tmp_path, content=b"the report content"):
    source_dir = tmp_path / "documents"
    source_dir.mkdir()
    source = source_dir / "report.pdf"
    source.write_bytes(content)
    dest_dir = tmp_path / "archive"
    dest_dir.mkdir()
    config = ToolsConfig(
        approved_directories={"archive": str(dest_dir)},
        approved_applications={},
        approved_scripts={},
        approved_files={"monthly_report": FileSpec(path=str(source))},
        approved_copies={
            "monthly_report_archive": FileCopySpec(
                "monthly_report", "archive", "monthly_report.pdf"
            )
        },
    )
    return source, dest_dir, config


def test_e_copy_file_stops_for_confirmation_with_destination_absent(repo, registry, tmp_path):
    source, dest_dir, tools_config = _copy_file_config(tmp_path)
    executor = SafeTaskExecutor(tools_config, registry)
    model = _RaisingModelProvider()

    task = _ready_task(repo, [_action_step(1, "copy_file", "monthly_report_archive")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.CONFIRMATION_REQUIRED
    assert result.task.state == TaskState.WAITING_FOR_CONFIRMATION
    assert not (dest_dir / "monthly_report.pdf").exists()
    assert list(dest_dir.iterdir()) == []  # no temp file either

    pending = repo.get_pending_confirmation(task.task_id)
    assert pending.action_name == "copy_file"
    assert pending.resource_key == "monthly_report_archive"


def test_f_approving_copy_file_copies_exact_bytes_and_completes(repo, registry, tmp_path):
    content = b"the exact report bytes"
    source, dest_dir, tools_config = _copy_file_config(tmp_path, content=content)
    executor = SafeTaskExecutor(tools_config, registry)
    model = _RaisingModelProvider()

    task = _ready_task(
        repo,
        [
            _action_step(1, "copy_file", "monthly_report_archive"),
        ],
    )
    stopped = run_task_until_blocked(task, repo, registry, tools_config, executor, model)
    pending = repo.get_pending_confirmation(task.task_id)

    approved = approve_task_confirmation(
        stopped.task, repo, registry, tools_config, executor, pending.confirmation_id
    )
    assert approved.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    dest = dest_dir / "monthly_report.pdf"
    assert dest.read_bytes() == content
    assert source.read_bytes() == content  # source unchanged

    # approve_task_confirmation() advances exactly the one approved step -
    # a further advance is needed to OBSERVE that the (single-step) plan
    # is now complete.
    result = run_task_until_blocked(approved.task, repo, registry, tools_config, executor, model)
    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED

    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert observation.success is True
    assert str(tmp_path) not in observation.safe_summary


def test_f2_copy_file_then_respond_synthesizes_from_durable_observation(repo, registry, tmp_path):
    content = b"quarterly numbers look good"
    source, dest_dir, tools_config = _copy_file_config(tmp_path, content=content)
    executor = SafeTaskExecutor(tools_config, registry)
    model = _RaisingModelProvider()

    task = _ready_task(
        repo,
        [
            _action_step(1, "copy_file", "monthly_report_archive"),
            _respond_step(2, depends_on=(1,)),
        ],
    )
    stopped = run_task_until_blocked(task, repo, registry, tools_config, executor, model)
    pending = repo.get_pending_confirmation(task.task_id)

    executor = SafeTaskExecutor(tools_config, registry)
    approved = approve_task_confirmation(
        stopped.task, repo, registry, tools_config, executor, pending.confirmation_id
    )
    assert approved.status == ExecutionAdvanceStatus.STEP_SUCCEEDED

    model = _FakeModelProvider(responses=[ModelResponse("Report archived.", "fake", 0, 0, 0.0)])
    result = run_task_until_blocked(approved.task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert len(model.calls) == 1
    assert "Bytes copied" in model.calls[0]
    step_2 = repo.get_step_progress(task.task_id, 2)
    assert deserialize_observation(step_2.result_json).safe_summary == "Report archived."


def test_g_denying_copy_file_leaves_no_destination_or_temp_output(repo, registry, tmp_path):
    source, dest_dir, tools_config = _copy_file_config(tmp_path)
    executor = SafeTaskExecutor(tools_config, registry)
    model = _RaisingModelProvider()

    task = _ready_task(repo, [_action_step(1, "copy_file", "monthly_report_archive")])
    stopped = run_task_until_blocked(task, repo, registry, tools_config, executor, model)
    pending = repo.get_pending_confirmation(task.task_id)

    denied = deny_task_confirmation(stopped.task, repo, pending.confirmation_id)

    assert denied.status == ExecutionAdvanceStatus.TASK_CANCELLED
    assert denied.task.state == TaskState.CANCELLED
    assert list(dest_dir.iterdir()) == []
    assert repo.get_step_progress(task.task_id, 1) is None


def test_h_stale_copy_file_resource_fails_closed_without_copying_anything(
    repo, registry, tmp_path
):
    source, dest_dir, base_config = _copy_file_config(tmp_path)
    # The plan references "monthly_report_archive", but the CURRENT
    # tools_config no longer configures it.
    tools_config = ToolsConfig(
        approved_directories=base_config.approved_directories,
        approved_applications={},
        approved_scripts={},
        approved_files=base_config.approved_files,
        approved_copies={},
    )
    executor = SafeTaskExecutor(tools_config, registry)
    model = _RaisingModelProvider()

    task = _ready_task(repo, [_action_step(1, "copy_file", "monthly_report_archive")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "action_no_longer_valid"
    assert result.task.state == TaskState.FAILED
    assert list(dest_dir.iterdir()) == []
    assert repo.get_step_progress(task.task_id, 1) is None
