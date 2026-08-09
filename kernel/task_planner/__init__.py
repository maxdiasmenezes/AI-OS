"""
Public interface of the Milestone 41 Bounded Task Planner.

Callers outside this package must import from here, matching the
convention used by kernel/action_protocol/__init__.py,
kernel/employee_tasks/__init__.py, kernel/tools/__init__.py, and
kernel/knowledge/__init__.py.

kernel/task_planner/ takes one kernel/employee_tasks TaskRecord and a
deterministic action catalog (catalog.py:build_catalog(), built from the
real ActionRegistry + ToolsConfig) and produces a bounded, validated
PlanOutcome via exactly one structured-output model call
(planner.py:plan_task()). It never executes an action, never calls
kernel/tools/SafeTaskExecutor, never persists anything, and never mutates
a TaskRecord or transitions task state - this package has no dependency
on kernel/employee_tasks' db.py or repository.py, only on its plain,
I/O-free TaskRecord type. A higher layer that does not yet exist in this
milestone is responsible for calling kernel/employee_tasks/TaskRepository
to move a task through created -> planning -> ready/failed around a call
to plan_task().

This package also has no dependency on kernel/action_protocol/ - Milestone
39's resolve_action_candidates() is a per-request, natural-language,
single-action-or-nothing resolver (see its own module docstring); this
package's catalog is instead the full, request-independent cross product
of every registered action and its configured resource keys, and the model
picks zero or more catalog entries per bounded plan. Neither package
depends on the other.

Execution, tool calls, autonomous continuation from one step to another,
and plan persistence all begin in a later milestone - not this one.
"""

from kernel.task_planner.catalog import build_catalog
from kernel.task_planner.grounding import validate_capability_grounding
from kernel.task_planner.parser import parse_plan_response
from kernel.task_planner.planner import plan_task
from kernel.task_planner.prompt import build_prompt, build_schema
from kernel.task_planner.types import (
    MAX_DEPENDENCIES_PER_STEP,
    MAX_EXPECTED_RESULT_CHARS,
    MAX_JSON_NESTING_DEPTH,
    MAX_OBJECTIVE_CHARS,
    MAX_PLAN_STEPS,
    MAX_REASON_CHARS,
    MAX_RESPONSE_CHARS,
    MAX_STEP_DESCRIPTION_CHARS,
    PLAN_VERSION,
    PLANNER_TEMPERATURE_OVERRIDE,
    CannotPlan,
    CatalogEntry,
    ParsedPlan,
    ParseOutcome,
    PlannerErrorCode,
    PlannerFailure,
    PlanOutcome,
    PlanStep,
    RequiresClarification,
    StepKind,
    TaskPlan,
)

__all__ = [
    "PLAN_VERSION",
    "PLANNER_TEMPERATURE_OVERRIDE",
    "MAX_PLAN_STEPS",
    "MAX_OBJECTIVE_CHARS",
    "MAX_STEP_DESCRIPTION_CHARS",
    "MAX_EXPECTED_RESULT_CHARS",
    "MAX_REASON_CHARS",
    "MAX_DEPENDENCIES_PER_STEP",
    "MAX_RESPONSE_CHARS",
    "MAX_JSON_NESTING_DEPTH",
    "StepKind",
    "CatalogEntry",
    "PlanStep",
    "ParsedPlan",
    "TaskPlan",
    "CannotPlan",
    "RequiresClarification",
    "PlannerErrorCode",
    "PlannerFailure",
    "ParseOutcome",
    "PlanOutcome",
    "build_catalog",
    "build_prompt",
    "build_schema",
    "parse_plan_response",
    "validate_capability_grounding",
    "plan_task",
]
