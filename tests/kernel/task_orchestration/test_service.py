"""Tests for kernel/task_orchestration/service.py: advance_task_planning().
Every test uses a tmp_path SQLite database (never storage/tasks/) and a
fake ModelProvider (never a real Ollama call)."""

import ast
import json
from pathlib import Path

import pytest

from kernel.employee_tasks import (
    TaskAlreadyTerminalError,
    TaskRepository,
    TaskState,
    open_writer_connection,
)
from kernel.models.base import ModelRequestOptions, ModelResponse
from kernel.task_orchestration.service import TaskNotInCreatedStateError, advance_task_planning
from kernel.task_planner import RequiresClarification, build_catalog
from kernel.tools.config import RepoBackupSpec, ToolsConfig
from kernel.tools.registry import ActionRegistry


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
        approved_applications={},
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


# --- PlanOutcome -> lifecycle mapping ---------------------------------------


def test_task_plan_outcome_produces_ready_task_with_durable_plan(repo, catalog):
    task = repo.create_task("Check repository health.", "test")
    provider = _FakeModelProvider(_valid_plan_raw(catalog))

    result = advance_task_planning(task, repo, catalog, provider)

    assert result.state == TaskState.READY
    assert result.plan_json is not None
    payload = json.loads(result.plan_json)
    assert payload["objective"] == "Check repository health."

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.READY
    assert reloaded.plan_json == result.plan_json


def test_cannot_plan_outcome_fails_task_with_code_authored_summary(repo, catalog):
    task = repo.create_task("Do the usual maintenance.", "test")
    raw = json.dumps(
        {
            "plan_version": 1,
            "result": "cannot_plan",
            "reason": "MODEL SUPPLIED TEXT THAT MUST NOT BE PERSISTED VERBATIM",
        }
    )
    provider = _FakeModelProvider(raw)

    result = advance_task_planning(task, repo, catalog, provider)

    assert result.state == TaskState.FAILED
    assert result.failure_code == "cannot_plan"
    assert result.failure_summary == "Planner could not produce a supported plan."
    assert "MODEL SUPPLIED TEXT" not in result.failure_summary
    assert result.plan_json is None


def test_requires_clarification_outcome_fails_task(monkeypatch, repo, catalog):
    task = repo.create_task("some ambiguous request", "test")

    def _fake_plan_task(task_arg, catalog_arg, provider_arg):
        return RequiresClarification(plan_version=1, question="MODEL SUPPLIED QUESTION TEXT")

    monkeypatch.setattr("kernel.task_orchestration.service.plan_task", _fake_plan_task)

    result = advance_task_planning(task, repo, catalog, _FakeModelProvider("unused"))

    assert result.state == TaskState.FAILED
    assert result.failure_code == "clarification_required"
    assert result.failure_summary == (
        "Planner requires clarification; interactive clarification is not supported."
    )
    assert "MODEL SUPPLIED QUESTION" not in result.failure_summary
    assert result.plan_json is None


def test_planner_failure_outcome_fails_task_with_error_code(repo, catalog):
    task = repo.create_task("request", "test")
    provider = _FakeModelProvider("not json at all")

    result = advance_task_planning(task, repo, catalog, provider)

    assert result.state == TaskState.FAILED
    assert result.failure_code == "malformed_plan"
    assert result.failure_summary
    assert result.plan_json is None


def test_planner_failure_summary_never_embeds_a_model_supplied_value(repo, catalog):
    # PlannerErrorCode.INVALID_DEPENDENCY's own detail string embeds a
    # model-chosen integer (see kernel/task_planner/parser.py). Prove the
    # PERSISTED failure_summary is the fixed, code-authored mapping, never
    # that raw detail string with the model's value inside it.
    entry = next(e for e in catalog if e.action_name == "repo_health")
    raw = json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": "x",
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": entry.catalog_id,
                    "description": "x",
                    "expected_result": "y",
                    "depends_on": [987654321],
                }
            ],
        }
    )
    task = repo.create_task("request", "test")
    provider = _FakeModelProvider(raw)

    result = advance_task_planning(task, repo, catalog, provider)

    assert result.state == TaskState.FAILED
    assert result.failure_code == "invalid_dependency"
    assert "987654321" not in result.failure_summary


def test_provider_exception_fails_task(repo, catalog):
    class _RaisingProvider:
        def send_prompt(self, prompt, *, options=None):
            raise TimeoutError("simulated timeout")

    task = repo.create_task("request", "test")

    result = advance_task_planning(task, repo, catalog, _RaisingProvider())

    assert result.state == TaskState.FAILED
    assert result.failure_code == "planner_provider_unavailable"
    assert result.plan_json is None


# --- exception-scoping: the broad `except Exception` around plan_task() ----
# --- must never leak into, or conceal, anything outside that one call ------


def test_serialize_plan_error_other_than_oversized_propagates_uncaught(monkeypatch, repo, catalog):
    # serialize_plan()'s own catch is narrow (PlanSerializationError only -
    # see service.py). Prove a DIFFERENT exception from serialize_plan()
    # (standing in for a genuine internal defect, not an oversized-plan
    # case) is never caught here and never misreported as a plan-related
    # PlanOutcome failure.
    task = repo.create_task("Check repository health.", "test")
    provider = _FakeModelProvider(_valid_plan_raw(catalog))

    def _broken_serialize_plan(plan):
        raise RuntimeError("simulated internal defect, not an oversized plan")

    monkeypatch.setattr(
        "kernel.task_orchestration.service.serialize_plan", _broken_serialize_plan
    )

    with pytest.raises(RuntimeError):
        advance_task_planning(task, repo, catalog, provider)

    # The task must be left exactly where plan_task() left it (planning) -
    # not silently marked failed, since this was never a PlanOutcome at all.
    assert repo.get_task(task.task_id).state == TaskState.PLANNING


def test_stale_created_check_still_fails_closed_at_the_repository_race_guard(repo, catalog):
    # Complements test_non_created_task_rejected_before_any_model_call
    # (the FAST, in-hand-record check): this proves the REAL safety net -
    # TaskRepository.transition_task()'s own database-level race check -
    # also fails closed and propagates, for a task whose in-hand
    # TaskRecord still (staleness) claims CREATED but whose actual DB row
    # has already moved on. No model call must happen in this case either.
    from kernel.employee_tasks import InvalidTransitionError

    task = repo.create_task("request", "test")
    stale_task = task  # captured before the out-of-band transition below
    repo.transition_task(task.task_id, "created", "planning")  # DB moves on

    provider = _FakeModelProvider("should never be reached")

    with pytest.raises(InvalidTransitionError):
        advance_task_planning(stale_task, repo, catalog, provider)

    assert provider.calls == []


def test_persist_plan_and_ready_failure_on_the_success_path_propagates_uncaught(
    repo, catalog, db_path
):
    # A concurrency conflict during the FINAL write (TaskPlan ->
    # persist_plan_and_ready) must propagate too - not just on the
    # failure path (already covered by
    # test_concurrent_conflict_during_failure_transition_propagates).
    # Uses a second real writer connection, exactly like that test.
    task = repo.create_task("Check repository health.", "test")

    concurrent_conn = open_writer_connection(db_path)
    concurrent_repo = TaskRepository(concurrent_conn)

    class _ProviderThatTriggersARaceThenSucceeds:
        def __init__(self, raw_response: str):
            self._raw_response = raw_response

        def send_prompt(self, prompt, *, options=None):
            concurrent_repo.transition_task(task.task_id, "planning", "cancelled")
            return ModelResponse(
                text=self._raw_response,
                model="fake",
                input_tokens=0,
                output_tokens=0,
                latency_seconds=0.0,
            )

    provider = _ProviderThatTriggersARaceThenSucceeds(_valid_plan_raw(catalog))

    try:
        with pytest.raises(TaskAlreadyTerminalError):
            advance_task_planning(task, repo, catalog, provider)
    finally:
        concurrent_conn.close()

    # The task was genuinely cancelled by the "concurrent" writer - never
    # silently overwritten back to ready.
    assert repo.get_task(task.task_id).state == TaskState.CANCELLED
    assert repo.get_task(task.task_id).plan_json is None


# --- durable plan / task identity -------------------------------------------


def test_persisted_plan_json_task_id_matches_the_advanced_task(repo, catalog):
    task = repo.create_task("Check repository health.", "test")
    provider = _FakeModelProvider(_valid_plan_raw(catalog))

    result = advance_task_planning(task, repo, catalog, provider)

    payload = json.loads(result.plan_json)
    assert payload["task_id"] == task.task_id


# --- entry-point contract ----------------------------------------------------


def test_non_created_task_rejected_before_any_model_call(repo, catalog):
    task = repo.create_task("request", "test")
    repo.transition_task(task.task_id, "created", "planning")
    already_planning = repo.get_task(task.task_id)

    provider = _FakeModelProvider("should never be reached")

    with pytest.raises(TaskNotInCreatedStateError):
        advance_task_planning(already_planning, repo, catalog, provider)

    assert provider.calls == []
    assert repo.get_task(task.task_id).state == TaskState.PLANNING


def test_exactly_one_model_call_per_invocation(repo, catalog):
    task = repo.create_task("Check repository health.", "test")
    provider = _FakeModelProvider(_valid_plan_raw(catalog))

    advance_task_planning(task, repo, catalog, provider)

    assert len(provider.calls) == 1


def test_no_retry_on_provider_exception(repo, catalog):
    class _CountingRaisingProvider:
        def __init__(self):
            self.call_count = 0

        def send_prompt(self, prompt, *, options=None):
            self.call_count += 1
            raise TimeoutError("simulated timeout")

    provider = _CountingRaisingProvider()
    task = repo.create_task("request", "test")

    advance_task_planning(task, repo, catalog, provider)

    assert provider.call_count == 1


def test_concurrent_conflict_during_failure_transition_propagates(repo, catalog, db_path):
    # If another process moves the task to a terminal state during the
    # window between this call's created->planning transition and its own
    # attempt to mark the task failed, that conflict must propagate -
    # never be concealed or silently overwritten. Uses a genuinely
    # separate writer connection to the same database, matching
    # tests/kernel/employee_tasks/test_repository.py's own concurrency
    # test pattern.
    task = repo.create_task("request", "test")

    concurrent_conn = open_writer_connection(db_path)
    concurrent_repo = TaskRepository(concurrent_conn)

    class _RaisingProviderThatTriggersARace:
        def send_prompt(self, prompt, *, options=None):
            concurrent_repo.transition_task(task.task_id, "planning", "cancelled")
            raise TimeoutError("simulated timeout")

    try:
        with pytest.raises(TaskAlreadyTerminalError):
            advance_task_planning(task, repo, catalog, _RaisingProviderThatTriggersARace())
    finally:
        concurrent_conn.close()


# --- import-boundary regression: no execution/tool dependency --------------

_PACKAGE_DIR = Path(__file__).resolve().parents[3] / "kernel" / "task_orchestration"

_FORBIDDEN_IMPORT_PREFIXES = (
    "kernel.tools.executor",
    "kernel.tools.handlers",
    "kernel.tools.process_control",
    "kernel.tools.confirmation",
    "kernel.employee_tasks.db",
    "kernel.employee_tasks.repository",
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


def test_task_orchestration_package_never_imports_execution_or_tool_modules():
    for py_file in _PACKAGE_DIR.glob("*.py"):
        imported = _imported_module_names(py_file)
        for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
            matches = {
                name
                for name in imported
                if name == forbidden or name.startswith(forbidden + ".")
            }
            assert not matches, f"{py_file.name} imports forbidden module(s): {matches}"


def test_task_orchestration_package_never_imports_kernel_tools_at_all():
    # Stricter than the prefix check above: this package needs no
    # kernel.tools dependency of any kind (not even a currently-safe one) -
    # its only allowed dependencies are kernel.employee_tasks,
    # kernel.task_planner, and kernel.models.
    for py_file in _PACKAGE_DIR.glob("*.py"):
        imported = _imported_module_names(py_file)
        tools_imports = {name for name in imported if name.startswith("kernel.tools")}
        assert not tools_imports, f"{py_file.name} imports kernel.tools: {tools_imports}"
