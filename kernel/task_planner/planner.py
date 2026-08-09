"""
plan_task(): the one function in kernel/task_planner/ that calls a model.

This is P1's "pure planner" seam: plan_task(task, catalog, model_provider)
builds the request-scoped prompt/schema (prompt.py), makes exactly one
structured-output call through the injected ModelProvider (never
constructing or configuring one itself - the same explicit-injection
discipline capabilities/tasks/TasksCapability and
capabilities/wine/WineCapability already use), strictly parses the result
(parser.py), and - only for a structurally valid plan - runs one further
deterministic check (grounding.py:validate_capability_grounding()) before
returning it as a trustworthy TaskPlan. A real, correctly-referenced
catalog action is necessary but not sufficient: a structurally valid plan
that selects a named-capability action (e.g. run_registered_script,
open_application) not actually referenced by the request text fails
closed here as an UNGROUNDED_CAPABILITY PlannerFailure, never a TaskPlan -
see grounding.py's module docstring for the full reasoning.

"Pure" here means: no database read or write of any kind (kernel/task_planner
has no dependency on kernel/employee_tasks' db.py or repository.py - only
on its plain, I/O-free TaskRecord type), no tool execution, no
confirmation-store access, and no mutation of anything outside this
function's own return value. A TaskRecord goes in; a PlanOutcome comes out.
Task-state transitions (created -> planning -> ready, or planning ->
failed) are the responsibility of a higher-layer caller that does not yet
exist in this milestone - see docs/architecture.md's Milestone 41 section
and kernel/employee_tasks/__init__.py for why that coupling is deliberately
not made here.

Provider-availability and timeout failures (e.g. a socket timeout from
kernel/models/ollama.py) are NOT caught here - they propagate to the
caller unchanged, exactly like kernel/action_protocol/'s Stage B never
catches them either. This module never turns a provider failure into a
PlanOutcome.

No tool is ever imported or called from this module.
"""

from datetime import datetime, timezone

from kernel.employee_tasks import TaskRecord
from kernel.models.base import ModelProvider, ModelRequestOptions
from kernel.task_planner.grounding import validate_capability_grounding
from kernel.task_planner.parser import parse_plan_response
from kernel.task_planner.prompt import build_prompt, build_schema
from kernel.task_planner.types import (
    CatalogEntry,
    ParsedPlan,
    PLANNER_TEMPERATURE_OVERRIDE,
    PlanOutcome,
    TaskPlan,
)


def plan_task(
    task: TaskRecord,
    catalog: tuple[CatalogEntry, ...],
    model_provider: ModelProvider,
) -> PlanOutcome:
    """Produce a bounded, validated plan for one task's request text.

    `catalog` is expected to be built once (kernel/task_planner/catalog.py:
    build_catalog()) from the real ActionRegistry + ToolsConfig, and reused
    across calls - it is not derived from `task` itself. `model_provider`
    is the same injected ModelProvider abstraction every other model-calling
    component in this codebase already takes as an explicit constructor/
    call argument.

    Always makes exactly one model call, with require_json=True, the
    request-scoped dynamic schema, and the empirically-validated
    temperature_override - matching Milestone 39's proven structured-
    output request pattern (kernel/models/base.py:ModelRequestOptions).
    No retries.
    """

    prompt = build_prompt(task.request_text, catalog)
    schema = build_schema(catalog)
    options = ModelRequestOptions(
        require_json=True,
        json_schema=schema,
        temperature_override=PLANNER_TEMPERATURE_OVERRIDE,
    )

    response = model_provider.send_prompt(prompt, options=options)
    outcome = parse_plan_response(response.text, catalog)

    if isinstance(outcome, ParsedPlan):
        grounding_failure = validate_capability_grounding(outcome, task.request_text, catalog)
        if grounding_failure is not None:
            return grounding_failure

        return TaskPlan(
            plan_version=outcome.plan_version,
            task_id=task.task_id,
            objective=outcome.objective,
            steps=outcome.steps,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    # CannotPlan | RequiresClarification | PlannerFailure - passed through
    # unchanged; none of these carry or need task identity.
    return outcome
