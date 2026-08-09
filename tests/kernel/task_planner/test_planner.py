"""Tests for kernel/task_planner/planner.py: plan_task(). Every test uses
a fake ModelProvider - never a real Ollama call - and a synthetic
TaskRecord built directly, never a real kernel/employee_tasks database."""

import ast
import json
from pathlib import Path

import pytest

from kernel.employee_tasks.types import TaskRecord, TaskState
from kernel.models.base import ModelRequestOptions, ModelResponse
from kernel.task_planner.catalog import build_catalog
from kernel.task_planner.planner import plan_task
from kernel.task_planner.types import (
    CannotPlan,
    CatalogEntry,
    PLANNER_TEMPERATURE_OVERRIDE,
    PlannerErrorCode,
    PlannerFailure,
    TaskPlan,
)
from kernel.tools.config import RepoBackupSpec, ToolsConfig
from kernel.tools.registry import ActionRegistry


def _task_record(request_text: str = "Check the system status.") -> TaskRecord:
    return TaskRecord(
        task_id="0198c1e0-0000-7000-8000-000000000000",
        display_id="TASK-ABCD2345",
        state=TaskState.PLANNING,
        request_text=request_text,
        source="test",
        dedup_key=None,
        created_at="2026-08-08T00:00:00+00:00",
        updated_at="2026-08-08T00:00:00+00:00",
        started_at=None,
        completed_at=None,
        failure_code=None,
        failure_summary=None,
        metadata_json="{}",
        protocol_version=1,
        version=1,
    )


class _FakeModelProvider:
    """Records the exact call it received and returns a canned response -
    never makes a real network call."""

    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list[tuple[str, ModelRequestOptions | None]] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append((prompt, options))
        return ModelResponse(
            text=self._response_text,
            model="fake",
            input_tokens=0,
            output_tokens=0,
            latency_seconds=0.0,
        )


@pytest.fixture
def catalog() -> tuple[CatalogEntry, ...]:
    tools_config = ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": object()},
        approved_scripts={"whatsapp_test": object()},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )
    return build_catalog(ActionRegistry(), tools_config)


def test_plan_task_calls_provider_exactly_once_with_structured_output_options(catalog):
    raw = json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": "Check the system status.",
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": catalog[0].catalog_id,
                    "description": "Check system status.",
                    "expected_result": "Status known.",
                    "depends_on": [],
                }
            ],
        }
    )
    provider = _FakeModelProvider(raw)

    plan_task(_task_record(), catalog, provider)

    assert len(provider.calls) == 1
    prompt, options = provider.calls[0]
    assert "Check the system status." in prompt
    assert isinstance(options, ModelRequestOptions)
    assert options.require_json is True
    assert options.json_schema is not None
    assert options.temperature_override == PLANNER_TEMPERATURE_OVERRIDE


def test_plan_task_wraps_a_valid_response_into_a_task_stamped_taskplan(catalog):
    raw = json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": "Check the system status.",
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": catalog[0].catalog_id,
                    "description": "Check system status.",
                    "expected_result": "Status known.",
                    "depends_on": [],
                }
            ],
        }
    )
    task = _task_record()
    provider = _FakeModelProvider(raw)

    outcome = plan_task(task, catalog, provider)

    assert isinstance(outcome, TaskPlan)
    assert outcome.task_id == task.task_id
    assert outcome.plan_version == 1
    assert isinstance(outcome.created_at, str) and outcome.created_at
    # task_id is code-generated from the TaskRecord, never derivable from
    # the model's raw response (which contains no task_id field at all).
    assert "task_id" not in raw


def test_plan_task_passes_through_cannot_plan_unchanged(catalog):
    raw = json.dumps({"plan_version": 1, "result": "cannot_plan", "reason": "Ambiguous request."})
    provider = _FakeModelProvider(raw)

    outcome = plan_task(_task_record("Do the usual maintenance."), catalog, provider)

    assert outcome == CannotPlan(plan_version=1, reason="Ambiguous request.")


def test_plan_task_passes_through_a_parser_failure_unchanged(catalog):
    provider = _FakeModelProvider("not json at all")

    outcome = plan_task(_task_record(), catalog, provider)

    assert isinstance(outcome, PlannerFailure)


# --- pipeline-level M1 regression: full plan_task() end to end -------------


def test_plan_task_rejects_the_exact_gemma3_m1_response_as_ungrounded(catalog):
    """The EXACT raw response gemma3:12b produced for corpus item M1 during
    the Milestone 41 model evaluation - see
    tests/kernel/task_planner/test_parser.py's own M1 regression test for
    parser.py's (unchanged) structural-validity guarantee on this same raw
    text. This test proves the ADDITIONAL grounding boundary added in
    planner.py now rejects it end to end: plan_task() must return an
    UNGROUNDED_CAPABILITY PlannerFailure, never a TaskPlan, for a task
    whose request text says generic "run the tests" while the model
    substituted the specifically-scoped whatsapp_test capability."""

    script_entry = next(e for e in catalog if e.action_name == "run_registered_script")

    raw_gemma3_response = (
        '{"plan_version": 1, "result": "plan", "objective": "Check the AI-OS repository health, '
        'run WhatsApp tests, back up the repository if tests succeed, and provide a summary.", '
        f'"steps": [{{"step_kind": "action", "catalog_id": '
        f'"{next(e for e in catalog if e.action_name == "repo_health").catalog_id}", '
        '"description": "Check the health of the AI-OS repository.", "expected_result": '
        '"Repository health status is known.", "depends_on": []}, {"step_kind": "action", '
        f'"catalog_id": "{script_entry.catalog_id}", '
        '"description": "Run the WhatsApp tests.", "expected_result": "WhatsApp test results are '
        'available.", "depends_on": [1]}, {"step_kind": "action", "catalog_id": '
        f'"{next(e for e in catalog if e.action_name == "repository_backup").catalog_id}", '
        '"description": "Back up the AI-OS repository if tests passed.", "expected_result": '
        '"Repository backup is created (conditionally).", "depends_on": [2]}, {"step_kind": '
        '"respond", "description": "Summarize the results of the health check, test run, and '
        'conditional backup.", "expected_result": "A concise summary of all actions performed and '
        'their outcomes is presented to the user.", "depends_on": [1, 2]}]}'
    )
    task = _task_record(
        "Check the AI-OS repository, run the tests, create a backup if the tests pass, and "
        "summarize the result."
    )
    provider = _FakeModelProvider(raw_gemma3_response)

    outcome = plan_task(task, catalog, provider)

    assert isinstance(outcome, PlannerFailure)
    assert outcome.error is PlannerErrorCode.UNGROUNDED_CAPABILITY


def test_plan_task_accepts_an_explicit_whatsapp_test_request(catalog):
    """The legitimate counterpart to the M1 regression above: when the
    request text explicitly names the WhatsApp test, plan_task() must
    still produce a valid TaskPlan - the grounding boundary must not
    reject every use of run_registered_script, only ungrounded ones."""

    script_entry = next(e for e in catalog if e.action_name == "run_registered_script")
    raw = json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": "Run the WhatsApp test script.",
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": script_entry.catalog_id,
                    "description": "Run the registered whatsapp_test script.",
                    "expected_result": "Test script executed.",
                    "depends_on": [],
                }
            ],
        }
    )
    task = _task_record("Please run the configured WhatsApp test script.")
    provider = _FakeModelProvider(raw)

    outcome = plan_task(task, catalog, provider)

    assert isinstance(outcome, TaskPlan)
    assert outcome.steps[0].action_name == "run_registered_script"
    assert outcome.steps[0].resource_key == "whatsapp_test"


def test_plan_task_lets_a_provider_exception_propagate(catalog):
    class _RaisingProvider:
        def send_prompt(self, prompt, *, options=None):
            raise TimeoutError("provider unavailable")

    with pytest.raises(TimeoutError):
        plan_task(_task_record(), catalog, _RaisingProvider())


def test_plan_task_never_mutates_the_task_record(catalog):
    task = _task_record()
    raw = json.dumps({"plan_version": 1, "result": "cannot_plan", "reason": "x"})
    plan_task(task, catalog, _FakeModelProvider(raw))
    # TaskRecord is frozen - any attempted mutation would raise on its own,
    # but this also documents the intent: plan_task() only ever reads
    # task.request_text/task.task_id, never writes to task at all.
    assert task.state == TaskState.PLANNING


# --- import-boundary regression: P1's "no DB, no execution" guarantee -----

_PACKAGE_DIR = Path(__file__).resolve().parents[3] / "kernel" / "task_planner"

_FORBIDDEN_IMPORT_PREFIXES = (
    "kernel.employee_tasks.db",
    "kernel.employee_tasks.repository",
    "kernel.tools.executor",
    "kernel.tools.confirmation",
    "kernel.tools.process_control",
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


def test_task_planner_package_never_imports_persistence_or_execution_modules():
    for py_file in _PACKAGE_DIR.glob("*.py"):
        imported = _imported_module_names(py_file)
        for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
            matches = {name for name in imported if name == forbidden or name.startswith(forbidden + ".")}
            assert not matches, f"{py_file.name} imports forbidden module(s): {matches}"


def test_task_planner_package_only_imports_the_plain_taskrecord_type_from_employee_tasks():
    for py_file in _PACKAGE_DIR.glob("*.py"):
        imported = _imported_module_names(py_file)
        employee_tasks_imports = {name for name in imported if name.startswith("kernel.employee_tasks")}
        assert employee_tasks_imports <= {"kernel.employee_tasks"}, (
            f"{py_file.name} imports from kernel.employee_tasks submodules directly: "
            f"{employee_tasks_imports}"
        )
