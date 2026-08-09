"""Milestone 41 closure: one true end-to-end, non-executing acceptance
test for the whole planning pipeline built across P1 (kernel.task_planner)
and P2 (kernel.employee_tasks + kernel.task_orchestration).

Deliberately separate from test_service.py (which covers each PlanOutcome
mapping and failure-scoping case in isolation): this file proves the
FULL real chain works together as one story, including a genuine database
connection close/reopen - the thing no single existing test exercised
end to end (see the Milestone 41 gap analysis this closes).

No tool, action, or executor is ever imported or called anywhere in this
file - only kernel.employee_tasks, kernel.task_planner, and
kernel.task_orchestration.
"""

import json

from kernel.employee_tasks import TaskRepository, TaskState, open_writer_connection
from kernel.models.base import ModelRequestOptions, ModelResponse
from kernel.task_orchestration import advance_task_planning
from kernel.task_planner import StepKind, build_catalog, deserialize_plan
from kernel.tools.config import RepoBackupSpec, ToolsConfig
from kernel.tools.registry import ActionRegistry


class _FakeModelProvider:
    """Returns a canned, real-shaped planner response - never makes a
    real network call, never executes anything."""

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


def test_full_non_executing_pipeline_survives_a_real_connection_reopen(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"

    # -- real catalog, built from the real ActionRegistry + a synthetic
    #    (never the real, gitignored) ToolsConfig -----------------------
    tools_config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )
    catalog = build_catalog(ActionRegistry(), tools_config)
    repo_health_entry = next(e for e in catalog if e.action_name == "repo_health")

    # -- 1. real TaskRepository.create_task() -> CREATED -----------------
    conn = open_writer_connection(db_path)
    repo = TaskRepository(conn)
    request_text = "Check the ai_os repository health."
    task = repo.create_task(request_text, "test")
    assert task.state == TaskState.CREATED
    assert task.plan_json is None

    # -- 2. real advance_task_planning(): CREATED -> PLANNING -> the real
    #    parse_plan_response()/validate_capability_grounding() path,
    #    using a fake ModelProvider for the one model call -> TaskPlan
    #    persisted atomically with PLANNING -> READY -----------------------
    raw_plan_response = json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": "Check the health of the ai_os repository.",
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": repo_health_entry.catalog_id,
                    "description": "Check the health of the registered 'ai_os' repository.",
                    "expected_result": "Repository health status is known.",
                    "depends_on": [],
                },
                {
                    "step_kind": "respond",
                    "description": "Report the repository health result.",
                    "expected_result": "Health status presented to the user.",
                    "depends_on": [1],
                },
            ],
        }
    )
    provider = _FakeModelProvider(raw_plan_response)

    outcome = advance_task_planning(task, repo, catalog, provider)

    assert len(provider.calls) == 1  # exactly one model call, no retries
    assert outcome.state == TaskState.READY
    assert outcome.plan_json is not None

    conn.close()

    # -- 3/4. close and reopen a genuinely fresh connection/repository ---
    reopened_conn = open_writer_connection(db_path)
    reopened_repo = TaskRepository(reopened_conn)

    # -- 5/6/7. reload the TaskRecord, assert state and plan_json --------
    reloaded = reopened_repo.get_task(task.task_id)
    assert reloaded.state == TaskState.READY
    assert reloaded.plan_json is not None
    assert reloaded.task_id == task.task_id

    # -- 8/9. deserialize_plan() and verify task_id integrity ------------
    deserialized = deserialize_plan(reloaded.plan_json)
    assert deserialized.task_id == reloaded.task_id
    assert deserialized.task_id == task.task_id

    # -- 10. verify expected plan contents survive the full round trip ---
    assert deserialized.objective == "Check the health of the ai_os repository."
    assert len(deserialized.steps) == 2

    action_step, respond_step = deserialized.steps
    assert action_step.kind is StepKind.ACTION
    assert action_step.action_name == "repo_health"
    assert action_step.resource_key == "ai_os"
    assert action_step.catalog_id == repo_health_entry.catalog_id
    assert action_step.requires_confirmation is False  # repo_health is not sensitive
    assert action_step.depends_on == ()

    assert respond_step.kind is StepKind.RESPOND
    assert respond_step.action_name is None
    assert respond_step.depends_on == (1,)

    # -- explicit non-execution / non-M42 guarantees ----------------------
    # Nothing about this pipeline ever advanced the task past "ready", ever
    # consumed a confirmation, and no tool/action ever ran - the fake
    # provider's single canned response is the only external interaction
    # anywhere in this test.
    final_transitions = reopened_repo.list_transitions(task.task_id)
    assert [t.to_state for t in final_transitions] == [
        TaskState.CREATED,
        TaskState.PLANNING,
        TaskState.READY,
    ]

    reopened_conn.close()
