"""
Deterministic TaskPlan serialization for kernel/task_planner/ (Milestone
41 P2). serialize_plan()/deserialize_plan() convert between an immutable
TaskPlan and the durable string kernel.employee_tasks persists opaquely as
its plan_json column (kernel/employee_tasks/repository.py:
persist_plan_and_ready()) - this is the ONE place in the codebase that
knows both TaskPlan's shape and the fact that it gets turned into text for
storage; kernel/employee_tasks/ itself never imports TaskPlan or this
module (see that package's own docstring), and this module never imports
anything from kernel.employee_tasks beyond the one size bound it shares
(MAX_PLAN_JSON_CHARS - the single source of truth for that bound lives in
kernel.employee_tasks, since it is fundamentally a database-column limit;
importing it here is the allowed direction - task_planner may depend on
employee_tasks' plain, I/O-free top-level exports, never the reverse).

Deliberately two distinct failure concerns, kept apart on purpose:

  - PlanSerializationError - serialize_plan() could not turn an
    already-validated TaskPlan into a string that fits MAX_PLAN_JSON_CHARS.
    Should be effectively unreachable in practice (parser.py's own field
    bounds already keep a valid TaskPlan well under this limit - see the
    bound's own comment in kernel/employee_tasks/types.py) but is still
    real defense in depth, not assumed away.

  - PlanDeserializationError - deserialize_plan() was given a string that
    is not valid, well-formed serialized-TaskPlan JSON. This is a
    STORAGE/CORRUPTION concern (a database row that doesn't match what
    this module itself would ever have written), never a model-planning
    concern - it must NEVER be represented as, or converted into, a
    PlannerFailure/PlannerErrorCode. Those types describe a problem with a
    *model's response*; a corrupt persisted row was never a model
    response at all. Milestone 42, the first real reader of a persisted
    plan, is expected to treat this as a distinct, storage-level failure
    category from every planning-time failure this package already
    defines.

Both functions perform no I/O and call no model.

TASK IDENTITY (read before Milestone 42 executes anything): TaskPlan.task_id
is always the id of the TaskRecord kernel.task_planner.plan_task() was
called with (see planner.py - it is stamped from `task.task_id`, never
model text), and serialize_plan()/deserialize_plan() round-trip it exactly
like every other field (see test_serialization.py's
test_round_trip_preserves_task_id). kernel/employee_tasks/ never reads or
checks this embedded value - persist_plan_and_ready() addresses the
*row* to write to entirely by the task_id argument the orchestration layer
passes it, and plan_json's own content (including its embedded task_id) is
opaque to that layer, exactly like metadata_json. Today, in
kernel/task_orchestration/service.py, both of those task_id values are
always the same variable, so they cannot diverge - but that is a property
of THIS milestone's single, linear call path, not something
kernel.employee_tasks enforces at the storage layer. Milestone 42, or any
future caller that loads a TaskRecord and its plan_json independently (for
example after a restart, via two separate reads instead of one linear
call), MUST verify
`deserialize_plan(task_record.plan_json).task_id == task_record.task_id`
before treating any step of that plan as executable, and must treat a
mismatch as a corruption/integrity failure (a PlanDeserializationError-style
concern, not a PlannerFailure) rather than executing it anyway.
"""

import json

from kernel.employee_tasks import MAX_PLAN_JSON_CHARS
from kernel.task_planner.types import PlanStep, StepKind, TaskPlan


class PlanSerializationError(Exception):
    """serialize_plan() could not produce a string within
    MAX_PLAN_JSON_CHARS. Distinct from PlannerErrorCode/PlannerFailure,
    which describe a problem with a model's response - this describes a
    problem turning an already-validated TaskPlan into durable bytes."""


class PlanDeserializationError(Exception):
    """deserialize_plan() was given text that is not valid, well-formed
    serialized-TaskPlan JSON - a storage/corruption concern, never a model
    planning concern. Never convert this into a PlannerFailure - see this
    module's own docstring."""


def serialize_plan(plan: TaskPlan) -> str:
    """Deterministic, canonical JSON serialization of `plan`: the exact
    same TaskPlan always produces the exact same string (sorted object
    keys, compact separators, no whitespace-dependent ordering). Every
    field of TaskPlan and PlanStep is represented - nothing is dropped,
    nothing is added. Raises PlanSerializationError if the result would
    exceed MAX_PLAN_JSON_CHARS; never truncates."""

    payload = {
        "plan_version": plan.plan_version,
        "task_id": plan.task_id,
        "objective": plan.objective,
        "created_at": plan.created_at,
        "steps": [
            {
                "step_id": step.step_id,
                "position": step.position,
                "kind": step.kind.value,
                "action_name": step.action_name,
                "resource_key": step.resource_key,
                "catalog_id": step.catalog_id,
                "description": step.description,
                "expected_result": step.expected_result,
                "depends_on": list(step.depends_on),
                "requires_confirmation": step.requires_confirmation,
            }
            for step in plan.steps
        ],
    }

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    if len(serialized) > MAX_PLAN_JSON_CHARS:
        raise PlanSerializationError(
            f"serialized plan exceeds MAX_PLAN_JSON_CHARS ({MAX_PLAN_JSON_CHARS})"
        )

    return serialized


def _step_from_dict(raw_step) -> PlanStep:
    return PlanStep(
        step_id=raw_step["step_id"],
        position=raw_step["position"],
        kind=StepKind(raw_step["kind"]),
        action_name=raw_step["action_name"],
        resource_key=raw_step["resource_key"],
        catalog_id=raw_step["catalog_id"],
        description=raw_step["description"],
        expected_result=raw_step["expected_result"],
        depends_on=tuple(raw_step["depends_on"]),
        requires_confirmation=raw_step["requires_confirmation"],
    )


def deserialize_plan(raw: str) -> TaskPlan:
    """The inverse of serialize_plan(). Never raises anything other than
    PlanDeserializationError - every malformed-input path (invalid JSON,
    wrong top-level shape, a missing/mistyped field, an unrecognized
    StepKind value) is caught and re-raised as that one type, so a caller
    never needs to catch a grab-bag of json/KeyError/TypeError/ValueError
    exceptions individually."""

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanDeserializationError("plan_json is not valid JSON") from exc

    if not isinstance(data, dict):
        raise PlanDeserializationError("plan_json is not a JSON object")

    try:
        steps = tuple(_step_from_dict(raw_step) for raw_step in data["steps"])
        return TaskPlan(
            plan_version=data["plan_version"],
            task_id=data["task_id"],
            objective=data["objective"],
            steps=steps,
            created_at=data["created_at"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanDeserializationError(
            "plan_json does not match the expected TaskPlan shape"
        ) from exc
