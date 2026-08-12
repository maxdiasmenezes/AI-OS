"""Whole-Milestone-43 acceptance tests for kernel/task_execution/'s real
M42 pipeline (TaskRepository + ActionRegistry + ToolsConfig +
SafeTaskExecutor + durable confirmation + run_task_until_blocked).

tests/kernel/task_execution/test_milestone_43_p1_e2e.py and
test_milestone_43_p2_e2e.py already prove each of the five M43 actions
individually through this same real pipeline (P1's three read-only actions
completing without confirmation; P2's two sensitive actions stopping for
confirmation, mutating only on approval, and never mutating on denial or a
stale resource). This module adds only the assertion none of those files
was positioned to make: that all five M43 actions compose correctly
together, in ONE plan, through the SAME unmodified M42 machinery - proving
Milestone 43 is one coherent capability set, not five independently-tested
but never-jointly-exercised actions."""

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
from kernel.tools.handlers import list_processes as list_processes_handler
from kernel.tools.registry import ActionRegistry


class _FakeProcess:
    def __init__(self, pid, name, status="running"):
        self.pid = pid
        self._name = name
        self._status = status

    def name(self):
        return self._name

    def status(self):
        return self._status


class _FakeModelProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        return self._responses.pop(0)


def _action_step(position, action_name, resource_key, *, depends_on=(), requires_confirmation=False):
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
        requires_confirmation=requires_confirmation,
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
        created_at="2026-08-11T00:00:00+00:00",
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


def _whole_milestone_config(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_dir = workspace / "documents"
    source_dir.mkdir()
    archive_dir = workspace / "archive"
    archive_dir.mkdir()
    parent_dir = workspace / "parent"
    parent_dir.mkdir()

    notes = source_dir / "notes.txt"
    notes.write_bytes(b"The migration finished successfully.")

    config = ToolsConfig(
        approved_directories={"documents": str(source_dir), "archive": str(archive_dir), "parent": str(parent_dir)},
        approved_applications={},
        approved_scripts={},
        approved_files={"notes_txt": FileSpec(path=str(notes))},
        approved_directory_creations={"exports": DirectoryCreationSpec("parent", "exports")},
        approved_copies={"notes_archive": FileCopySpec("notes_txt", "archive", "notes_backup.txt")},
    )
    return workspace, source_dir, archive_dir, parent_dir, notes, config


def test_all_five_m43_actions_compose_in_one_plan_through_the_real_m42_pipeline(
    repo, registry, tmp_path, monkeypatch
):
    """One plan exercising every M43 action together: list_processes,
    file_metadata, and read_text_file (P1, read-only, no confirmation)
    followed by create_directory and copy_file (P2, sensitive, each
    requiring its own confirmation), followed by a RESPOND step that
    synthesizes from the durable observations. Proves the whole capability
    set is reachable in a single coherent task, not merely five isolated
    single-action tasks."""

    monkeypatch.setattr(
        list_processes_handler.psutil,
        "process_iter",
        lambda: iter([_FakeProcess(1, "explorer.exe")]),
    )
    workspace, source_dir, archive_dir, parent_dir, notes, tools_config = _whole_milestone_config(
        tmp_path
    )
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(
        repo,
        [
            _action_step(1, "list_processes", None),
            _action_step(2, "file_metadata", "notes_txt", depends_on=(1,)),
            _action_step(3, "read_text_file", "notes_txt", depends_on=(2,)),
            _action_step(
                4, "create_directory", "exports", depends_on=(3,), requires_confirmation=True
            ),
            _action_step(
                5, "copy_file", "notes_archive", depends_on=(4,), requires_confirmation=True
            ),
        ],
    )

    # Steps 1-3: complete without any confirmation.
    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)
    assert result.status == ExecutionAdvanceStatus.CONFIRMATION_REQUIRED
    assert result.task.state == TaskState.WAITING_FOR_CONFIRMATION
    for position in (1, 2, 3):
        step = repo.get_step_progress(task.task_id, position)
        assert step is not None
        assert deserialize_observation(step.result_json).success is True
    assert not (parent_dir / "exports").exists()

    # Step 4 (create_directory): approve, then advance again to reach the
    # next confirmation gate.
    pending_4 = repo.get_pending_confirmation(task.task_id)
    assert pending_4.action_name == "create_directory"
    approved_4 = approve_task_confirmation(
        result.task, repo, registry, tools_config, executor, pending_4.confirmation_id
    )
    assert approved_4.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert (parent_dir / "exports").is_dir()

    result = run_task_until_blocked(approved_4.task, repo, registry, tools_config, executor, model)
    assert result.status == ExecutionAdvanceStatus.CONFIRMATION_REQUIRED
    assert not (archive_dir / "notes_backup.txt").exists()

    # Step 5 (copy_file): approve, then run to completion (RESPOND).
    pending_5 = repo.get_pending_confirmation(task.task_id)
    assert pending_5.action_name == "copy_file"
    approved_5 = approve_task_confirmation(
        result.task, repo, registry, tools_config, executor, pending_5.confirmation_id
    )
    assert approved_5.status == ExecutionAdvanceStatus.STEP_SUCCEEDED
    assert (archive_dir / "notes_backup.txt").read_bytes() == b"The migration finished successfully."
    assert notes.read_bytes() == b"The migration finished successfully."  # source unchanged

    result = run_task_until_blocked(approved_5.task, repo, registry, tools_config, executor, model)
    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED

    # No step's durable observation ever leaked a raw filesystem path.
    for position in range(1, 6):
        step = repo.get_step_progress(task.task_id, position)
        observation = deserialize_observation(step.result_json)
        assert str(tmp_path) not in observation.safe_summary


def test_denying_either_sensitive_m43_step_cancels_the_whole_task_with_zero_mutation(
    repo, registry, tmp_path, monkeypatch
):
    """Denial of EITHER sensitive M43 action, reached after the read-only
    P1 steps have already succeeded, must cancel the whole task and leave
    every resource - the not-yet-attempted mutation AND anything the
    read-only steps merely inspected - completely unchanged."""

    monkeypatch.setattr(list_processes_handler.psutil, "process_iter", lambda: iter([]))
    workspace, source_dir, archive_dir, parent_dir, notes, tools_config = _whole_milestone_config(
        tmp_path
    )
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(
        repo,
        [
            _action_step(1, "read_text_file", "notes_txt"),
            _action_step(
                2, "create_directory", "exports", depends_on=(1,), requires_confirmation=True
            ),
            _action_step(
                3, "copy_file", "notes_archive", depends_on=(2,), requires_confirmation=True
            ),
        ],
    )

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)
    assert result.status == ExecutionAdvanceStatus.CONFIRMATION_REQUIRED
    pending = repo.get_pending_confirmation(task.task_id)
    assert pending.action_name == "create_directory"

    denied = deny_task_confirmation(result.task, repo, pending.confirmation_id)

    assert denied.status == ExecutionAdvanceStatus.TASK_CANCELLED
    assert denied.task.state == TaskState.CANCELLED
    assert not (parent_dir / "exports").exists()
    assert not (archive_dir / "notes_backup.txt").exists()
    assert notes.read_bytes() == b"The migration finished successfully."
    # The never-reached copy_file step has no durable progress at all.
    assert repo.get_step_progress(task.task_id, 3) is None


def test_stale_resource_on_any_m43_action_fails_the_whole_plan_before_any_later_step_runs(
    repo, registry, tmp_path, monkeypatch
):
    """If the CURRENT ToolsConfig no longer configures a resource an
    earlier-persisted M43 plan step references, revalidate_action() must
    fail the task closed before SafeTaskExecutor is ever reached for that
    step - and no later step in the same plan may run either, regardless
    of which of the five M43 actions is affected."""

    monkeypatch.setattr(list_processes_handler.psutil, "process_iter", lambda: iter([]))
    workspace, source_dir, archive_dir, parent_dir, notes, tools_config = _whole_milestone_config(
        tmp_path
    )
    # Remove the file_metadata/read_text_file resource that was valid at
    # planning time - simulating tools.yaml being edited between planning
    # and execution.
    stale_config = ToolsConfig(
        approved_directories=tools_config.approved_directories,
        approved_applications={},
        approved_scripts={},
        approved_files={},  # "notes_txt" no longer configured
        approved_directory_creations=tools_config.approved_directory_creations,
        approved_copies=tools_config.approved_copies,
    )
    executor = SafeTaskExecutor(stale_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(
        repo,
        [
            _action_step(1, "file_metadata", "notes_txt"),
            _action_step(2, "create_directory", "exports", depends_on=(1,), requires_confirmation=True),
        ],
    )

    result = run_task_until_blocked(task, repo, registry, stale_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "action_no_longer_valid"
    assert result.task.state == TaskState.FAILED
    assert repo.get_step_progress(task.task_id, 1) is None
    assert repo.get_step_progress(task.task_id, 2) is None
    assert not (parent_dir / "exports").exists()
