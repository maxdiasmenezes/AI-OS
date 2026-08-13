"""End-to-end acceptance tests for Milestone 45 P1 (desktop_target_status/
desktop_control_status) driven through the REAL M42 pipeline: real
TaskRepository (tmp_path SQLite), real ActionRegistry/ToolsConfig, real
SafeTaskExecutor, and the real kernel.tools.handlers.desktop_target_status/
desktop_control_status run() -> kernel.tools.desktop_safety chain against a
real, local, deterministic Tkinter fixture - mirrors
tests/kernel/task_execution/test_milestone_44_p1_e2e.py's own discipline
exactly (real pipeline, only the one external system - the fixture window -
is a real, but disposable, local process).

Proves desktop_target_status/desktop_control_status needed ZERO changes to
kernel/task_execution/service.py or respond.py - each is reachable purely
because it is a registered, non-sensitive action with a configured
resource, exactly like every read-only action since Milestone 33.

Windows-only (skipped elsewhere - see
kernel/tools/desktop_safety.py's PLATFORM BOUNDARY docstring section)."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Milestone 45 P1 is Windows-only (UIA)"
)

from kernel.employee_tasks import TaskRepository, TaskState, open_writer_connection
from kernel.models.base import ModelResponse
from kernel.task_execution.observation import deserialize_observation
from kernel.task_execution.service import run_task_until_blocked
from kernel.task_execution.types import ExecutionAdvanceStatus
from kernel.task_planner import PlanStep, StepKind, TaskPlan, serialize_plan
from kernel.tools.config import (
    ApplicationSpec,
    DesktopControlSpec,
    DesktopTargetSpec,
    ToolsConfig,
)
from kernel.tools.executor import SafeTaskExecutor
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest, ActionResult

_FIXTURE_SCRIPT = (
    Path(__file__).resolve().parents[2] / "kernel" / "tools" / "fixtures" / "desktop_fixture_app.py"
)
_TITLE = "AIOS-M45-Fixture-Window"


class _FakeExecutor:
    """Records every ActionRequest it receives - used only for the
    stale-resource scenarios, where the point is proving the real handler
    (and therefore any real UIA call) is never reached at all."""

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
        description="check desktop status",
        expected_result="the status is known",
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


def _config(targets=None, controls=None):
    return ToolsConfig(
        approved_directories={},
        approved_applications={
            "fixture_app": ApplicationSpec(executable=sys._base_executable, cwd="C:/")
        },
        approved_scripts={},
        approved_desktop_targets=targets or {},
        approved_desktop_controls=controls or {},
    )


def _target_spec():
    return DesktopTargetSpec(
        application_key="fixture_app",
        process_executable=sys._base_executable,
        window_class_name="TkTopLevel",
    )


def _control_spec():
    return DesktopControlSpec(
        target_key="fixture_window", control_automation_id="5001", control_type="Button"
    )


def _ready_task(repo, steps):
    record = repo.create_task("check the desktop target", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")
    plan = TaskPlan(
        plan_version=1,
        task_id=record.task_id,
        objective="check the desktop target",
        steps=tuple(steps),
        created_at="2026-08-13T00:00:00+00:00",
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


@pytest.fixture
def running_fixture(tmp_path):
    status_path = tmp_path / "status.txt"
    proc = subprocess.Popen([sys._base_executable, str(_FIXTURE_SCRIPT), str(status_path)])
    try:
        from pywinauto import findwindows

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if findwindows.find_elements(title=_TITLE, backend="uia"):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("fixture window did not appear in time")
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                proc.wait(timeout=5)


# --- A: desktop_target_status action alone completes the task ---------------


def test_a_desktop_target_status_completes_via_the_real_pipeline(repo, registry, running_fixture):
    tools_config = _config(targets={"fixture_window": _target_spec()})
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "desktop_target_status", "fixture_window")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert model.calls == []

    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert observation.success is True
    assert observation.step_kind == StepKind.ACTION
    assert observation.safe_summary == "Target 'fixture_window' is available."


def test_a_desktop_target_status_requires_no_confirmation(repo, registry, running_fixture):
    tools_config = _config(targets={"fixture_window": _target_spec()})
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "desktop_target_status", "fixture_window")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    # Never CONFIRMATION_REQUIRED/WAITING_FOR_CONFIRMATION - non-sensitive,
    # exactly like read_text_file/browser_read_page.
    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED


def test_a_desktop_control_status_completes_via_the_real_pipeline(repo, registry, running_fixture):
    tools_config = _config(
        targets={"fixture_window": _target_spec()},
        controls={"fixture_refresh": _control_spec()},
    )
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "desktop_control_status", "fixture_refresh")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert observation.safe_summary == "Control 'fixture_refresh' is available."


# --- B: desktop_target_status -> RESPOND depends on the durable observation --


def test_b_desktop_target_status_then_respond_synthesizes_from_durable_observation(
    repo, registry, running_fixture
):
    tools_config = _config(targets={"fixture_window": _target_spec()})
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(
        responses=[ModelResponse("The fixture window is currently available.", "fake", 0, 0, 0.0)]
    )

    task = _ready_task(
        repo,
        [
            _action_step(1, "desktop_target_status", "fixture_window"),
            _respond_step(2, depends_on=(1,)),
        ],
    )

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert len(model.calls) == 1
    assert "Target 'fixture_window' is available." in model.calls[0]

    step_2 = repo.get_step_progress(task.task_id, 2)
    respond_observation = deserialize_observation(step_2.result_json)
    assert respond_observation.safe_summary == "The fixture window is currently available."


# --- C: stale desktop resource fails closed, never reaches the executor -----


def test_c_stale_desktop_target_key_fails_closed_without_ever_calling_the_executor(repo, registry):
    tools_config = _config(targets={})  # "fixture_window" is not configured
    executor = _FakeExecutor()
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "desktop_target_status", "fixture_window")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "action_no_longer_valid"
    assert result.task.state == TaskState.FAILED
    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1) is None


def test_c_stale_desktop_control_key_fails_closed_without_ever_calling_the_executor(repo, registry):
    tools_config = _config(
        targets={"fixture_window": _target_spec()}, controls={}
    )  # "fixture_refresh" is not configured
    executor = _FakeExecutor()
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "desktop_control_status", "fixture_refresh")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "action_no_longer_valid"
    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1) is None


def test_c_control_whose_target_disappeared_from_current_config_fails_closed(repo, registry):
    # The control key itself is still configured, but the TARGET it
    # references is not - a deeper referential-integrity break than a
    # missing control key alone. eligibility.py's revalidate_action() only
    # checks the control key itself exists in approved_desktop_controls
    # (it does) - so this step IS eligible and reaches the REAL
    # SafeTaskExecutor -> the real handler, which is exactly where the
    # deeper target-reference check (desktop_control_status.py's own
    # `tools_config.approved_desktop_targets.get(control_spec.target_key)`
    # lookup) catches it, before any UIA call is ever made.
    tools_config = _config(
        targets={},  # "fixture_window" removed
        controls={"fixture_refresh": _control_spec()},
    )
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "desktop_control_status", "fixture_refresh")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    # The step IS claimed and actually executed (unlike the eligibility-
    # revalidation-failure scenarios above, where no row is ever created)
    # - but the handler itself reports failure (the referenced target is
    # gone), which durably fails the step and therefore the whole task,
    # exactly like any other failed ACTION step under M42.
    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.task.state == TaskState.FAILED
    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert observation.success is False
    assert observation.safe_summary == "That desktop control is not registered."
