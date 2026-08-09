"""Tests for kernel/task_execution/eligibility.py: evaluate_next_step().
Every test builds a synthetic TaskRecord/TaskPlan/TaskStepProgress
directly, in memory - never a real kernel/employee_tasks database, never a
real ActionRegistry/ToolsConfig loaded from disk, and never a model call
(evaluate_next_step()'s signature has no model parameter at all)."""

import ast
from pathlib import Path

import pytest

from kernel.employee_tasks import StepStatus, TaskRecord, TaskState, TaskStepProgress
from kernel.task_planner import PlanStep, StepKind, TaskPlan, serialize_plan
from kernel.task_execution import (
    ActionRevalidationFailure,
    AllStepsComplete,
    Blocked,
    EligibilityBlockReason,
    EligibleStep,
    PlanDeserializationFailure,
    PlanIntegrityFailure,
    evaluate_next_step,
)
from kernel.tools.config import RepoBackupSpec, ToolsConfig
from kernel.tools.registry import ActionRegistry

_TASK_ID = "0198c1e0-0000-7000-8000-000000000000"


def _task_record(plan_json: str | None, task_id: str = _TASK_ID) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        display_id="TASK-ABCD2345",
        state=TaskState.RUNNING,
        request_text="do something",
        source="test",
        dedup_key=None,
        created_at="2026-08-08T00:00:00+00:00",
        updated_at="2026-08-08T00:00:00+00:00",
        started_at="2026-08-08T00:00:00+00:00",
        completed_at=None,
        failure_code=None,
        failure_summary=None,
        metadata_json="{}",
        protocol_version=1,
        version=1,
        plan_json=plan_json,
    )


def _action_step(
    position: int,
    action_name: str,
    resource_key: str | None,
    *,
    catalog_id: str = "action_1",
    depends_on: tuple = (),
    requires_confirmation: bool = False,
) -> PlanStep:
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
        requires_confirmation=requires_confirmation,
    )


def _respond_step(position: int, *, depends_on: tuple = ()) -> PlanStep:
    return PlanStep(
        step_id=f"step_{position}",
        position=position,
        kind=StepKind.RESPOND,
        action_name=None,
        resource_key=None,
        catalog_id=None,
        description="summarize the results",
        expected_result="a summary is produced",
        depends_on=depends_on,
        requires_confirmation=False,
    )


def _plan_json(task_id: str, steps: tuple) -> str:
    plan = TaskPlan(
        plan_version=1,
        task_id=task_id,
        objective="do something",
        steps=steps,
        created_at="2026-08-08T00:00:00+00:00",
    )
    return serialize_plan(plan)


def _progress(position: int, status: StepStatus, task_id: str = _TASK_ID) -> TaskStepProgress:
    return TaskStepProgress(
        task_id=task_id,
        step_position=position,
        status=status,
        started_at="2026-08-08T00:00:00+00:00",
        completed_at="2026-08-08T00:01:00+00:00" if status != StepStatus.IN_PROGRESS else None,
        result_json=None,
        failure_code="tool_error" if status == StepStatus.FAILED else None,
        failure_summary="it failed" if status == StepStatus.FAILED else None,
        task_version=1,
    )


@pytest.fixture
def tools_config() -> ToolsConfig:
    return ToolsConfig(
        approved_directories={"downloads": object()},
        approved_applications={"notepad": object()},
        approved_scripts={"whatsapp_test": object()},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )


@pytest.fixture
def registry() -> ActionRegistry:
    return ActionRegistry()


# --- plan/task integrity ------------------------------------------------------


def test_missing_plan_json_fails_closed(registry, tools_config):
    task = _task_record(plan_json=None)
    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, PlanIntegrityFailure)


def test_task_id_mismatch_fails_closed(registry, tools_config):
    plan_json = _plan_json("some-other-task-id", (_action_step(1, "system_status", None),))
    task = _task_record(plan_json=plan_json)
    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, PlanIntegrityFailure)


def test_malformed_plan_json_fails_closed(registry, tools_config):
    task = _task_record(plan_json="not valid json at all")
    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, PlanDeserializationFailure)


def test_plan_json_valid_json_but_wrong_shape_fails_closed(registry, tools_config):
    task = _task_record(plan_json='{"unexpected": "shape"}')
    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, PlanDeserializationFailure)


# --- deterministic first-eligible-position ------------------------------------


def test_first_eligible_position_wins(registry, tools_config):
    steps = (
        _action_step(1, "system_status", None),
        _action_step(2, "list_files", "downloads"),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))
    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, EligibleStep)
    assert outcome.step.position == 1


def test_succeeded_steps_are_skipped(registry, tools_config):
    steps = (
        _action_step(1, "system_status", None),
        _action_step(2, "list_files", "downloads"),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))
    progress = [_progress(1, StepStatus.SUCCEEDED)]
    outcome = evaluate_next_step(task, progress, registry, tools_config)
    assert isinstance(outcome, EligibleStep)
    assert outcome.step.position == 2


def test_all_steps_succeeded_returns_all_complete(registry, tools_config):
    steps = (
        _action_step(1, "system_status", None),
        _action_step(2, "list_files", "downloads"),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))
    progress = [_progress(1, StepStatus.SUCCEEDED), _progress(2, StepStatus.SUCCEEDED)]
    outcome = evaluate_next_step(task, progress, registry, tools_config)
    assert isinstance(outcome, AllStepsComplete)


# --- dependency enforcement ----------------------------------------------------


def test_step_only_eligible_once_dependency_durably_succeeded(registry, tools_config):
    steps = (
        _action_step(1, "system_status", None),
        _respond_step(2, depends_on=(1,)),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    # Before step 1 succeeds, step 1 itself is the eligible step - step 2
    # is never reachable while its dependency is unresolved.
    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, EligibleStep)
    assert outcome.step.position == 1

    # Once step 1 durably succeeds, step 2 becomes eligible.
    progress = [_progress(1, StepStatus.SUCCEEDED)]
    outcome = evaluate_next_step(task, progress, registry, tools_config)
    assert isinstance(outcome, EligibleStep)
    assert outcome.step.position == 2


def test_forward_referencing_dependency_blocks_closed(registry, tools_config):
    """kernel.task_planner.parser.py structurally forbids a forward
    dependency reference in any freshly-parsed plan, but
    deserialize_plan() does not re-validate that constraint against a
    persisted row (see its own docstring) - so this exercises the
    explicit, independent dependency re-check eligibility.py performs
    rather than relying on scan-order alone (see that module's own
    "dependency defense in depth" docstring section)."""

    steps = (
        _action_step(1, "system_status", None),
        _respond_step(2, depends_on=(3,)),  # forward reference - never producible by parser.py
        _respond_step(3),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))
    progress = [_progress(1, StepStatus.SUCCEEDED)]

    outcome = evaluate_next_step(task, progress, registry, tools_config)
    assert outcome == Blocked(EligibilityBlockReason.DEPENDENCIES_NOT_SATISFIED, 2)


# --- failed / in_progress blocking ---------------------------------------------


def test_failed_step_blocks_continuation(registry, tools_config):
    steps = (
        _action_step(1, "system_status", None),
        _action_step(2, "list_files", "downloads"),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))
    progress = [_progress(1, StepStatus.FAILED)]

    outcome = evaluate_next_step(task, progress, registry, tools_config)
    assert outcome == Blocked(EligibilityBlockReason.STEP_FAILED, 1)


def test_in_progress_step_blocks_continuation_and_is_never_retried(registry, tools_config):
    steps = (
        _action_step(1, "system_status", None),
        _action_step(2, "list_files", "downloads"),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))
    progress = [_progress(1, StepStatus.IN_PROGRESS)]

    outcome_1 = evaluate_next_step(task, progress, registry, tools_config)
    outcome_2 = evaluate_next_step(task, progress, registry, tools_config)

    assert outcome_1 == Blocked(EligibilityBlockReason.STEP_IN_PROGRESS, 1)
    # Calling again with the exact same durable state must be idempotent -
    # never silently advance to step 2, never re-select step 1 as eligible.
    assert outcome_2 == Blocked(EligibilityBlockReason.STEP_IN_PROGRESS, 1)


# --- current registry/config revalidation --------------------------------------


def test_unknown_action_fails_closed(registry, tools_config):
    steps = (_action_step(1, "delete_everything", "downloads"),)
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, ActionRevalidationFailure)
    assert outcome.step_position == 1
    assert outcome.action_name == "delete_everything"


def test_removed_resource_key_fails_closed(registry, tools_config):
    steps = (_action_step(1, "run_registered_script", "whatsapp_test"),)
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    empty_scripts_config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},  # "whatsapp_test" no longer configured
        approved_repositories={},
        approved_backups={},
    )

    outcome = evaluate_next_step(task, [], registry, empty_scripts_config)
    assert isinstance(outcome, ActionRevalidationFailure)
    assert outcome.step_position == 1
    assert outcome.resource_key == "whatsapp_test"


def test_wrong_currently_unauthorized_resource_fails_closed(registry, tools_config):
    steps = (_action_step(1, "open_application", "some_other_app"),)
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, ActionRevalidationFailure)
    assert outcome.resource_key == "some_other_app"


def test_action_step_missing_required_resource_key_fails_closed(registry, tools_config):
    steps = (_action_step(1, "list_files", None),)
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, ActionRevalidationFailure)


def test_system_status_eligible_regardless_of_tools_config(registry):
    steps = (_action_step(1, "system_status", None),)
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))
    empty_config = ToolsConfig(
        approved_directories={}, approved_applications={}, approved_scripts={}
    )

    outcome = evaluate_next_step(task, [], registry, empty_config)
    assert isinstance(outcome, EligibleStep)


def test_catalog_id_changes_have_no_execution_authority(registry, tools_config):
    """A stale/nonsensical catalog_id must never block, nor be required
    to match, a step whose action_name/resource_key are still valid -
    catalog_id is a planner-facing label only, never consulted here."""

    steps = (
        _action_step(1, "list_files", "downloads", catalog_id="action_999_stale"),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, EligibleStep)
    assert outcome.step.catalog_id == "action_999_stale"


# --- sensitivity re-derivation -------------------------------------------------


def test_current_sensitivity_re_derived_from_registry_for_sensitive_action(
    registry, tools_config
):
    # requires_confirmation persisted as False (stale/wrong) - the current
    # registry must still be consulted, not this field.
    steps = (
        _action_step(
            1, "open_application", "notepad", requires_confirmation=False
        ),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, EligibleStep)
    assert outcome.currently_sensitive is True


def test_current_sensitivity_re_derived_from_registry_for_non_sensitive_action(
    registry, tools_config
):
    steps = (
        _action_step(1, "list_files", "downloads", requires_confirmation=True),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, EligibleStep)
    assert outcome.currently_sensitive is False


# --- respond steps --------------------------------------------------------------


def test_respond_step_selected_without_action_revalidation_or_model(registry, tools_config):
    steps = (_respond_step(1),)
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, EligibleStep)
    assert outcome.step.kind == StepKind.RESPOND
    assert outcome.currently_sensitive is False


def test_respond_step_still_subject_to_dependency_enforcement(registry, tools_config):
    steps = (
        _action_step(1, "system_status", None),
        _respond_step(2, depends_on=(1,)),
    )
    task = _task_record(plan_json=_plan_json(_TASK_ID, steps))

    outcome = evaluate_next_step(task, [], registry, tools_config)
    assert isinstance(outcome, EligibleStep)
    assert outcome.step.position == 1  # not the RESPOND step yet


# --- import boundary (no I/O, no execution, no model) --------------------------

_PACKAGE_DIR = Path(__file__).resolve().parents[3] / "kernel" / "task_execution"

_FORBIDDEN_IMPORT_PREFIXES = (
    "kernel.employee_tasks.db",
    "kernel.employee_tasks.repository",
    "kernel.tools.executor",
    "kernel.tools.confirmation",
    "kernel.tools.process_control",
    "kernel.tools.handlers",
    "kernel.task_planner.catalog",
    "kernel.task_planner.planner",
    "kernel.task_planner.prompt",
    "kernel.task_planner.parser",
    "kernel.models",
    "kernel.action_protocol",
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


def test_task_execution_package_never_imports_io_or_execution_modules():
    for py_file in _PACKAGE_DIR.glob("*.py"):
        imported = _imported_module_names(py_file)
        for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
            matches = {
                name
                for name in imported
                if name == forbidden or name.startswith(forbidden + ".")
            }
            assert not matches, f"{py_file.name} imports forbidden module(s): {matches}"


def test_task_execution_package_only_imports_the_plain_types_from_employee_tasks():
    for py_file in _PACKAGE_DIR.glob("*.py"):
        imported = _imported_module_names(py_file)
        employee_tasks_imports = {
            name for name in imported if name.startswith("kernel.employee_tasks")
        }
        assert employee_tasks_imports <= {"kernel.employee_tasks"}, (
            f"{py_file.name} imports from kernel.employee_tasks submodules directly: "
            f"{employee_tasks_imports}"
        )
