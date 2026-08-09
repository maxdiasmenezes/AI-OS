"""
evaluate_next_step(): the pure, deterministic next-step-eligibility
function for kernel/task_execution/ (Milestone 42 P1).

This is the P1 "pure eligibility" seam, mirroring kernel/task_planner/'s
own "pure planner" discipline: no database read or write of any kind (this
module has no dependency on kernel/employee_tasks' db.py or repository.py -
only on its plain, I/O-free top-level exports: TaskRecord, TaskStepProgress,
StepStatus), no tools.yaml read (ToolsConfig is always supplied by the
caller, already loaded), no tool execution, no confirmation-store access,
and no model call of any kind - a TaskRecord plus its durable step
progress plus the CURRENT ActionRegistry/ToolsConfig go in; a typed
EligibilityOutcome comes out. Nothing is claimed, executed, or mutated by
this function - it only decides what step, if any, is currently eligible.

CURRENT REGISTRY/CONFIG REVALIDATION: `registry`/`tools_config` are always
injected by the caller, never constructed or loaded here (see
kernel/tools/registry.py's ActionRegistry and kernel/tools/config.py's
ToolsConfig - both are already-loaded, in-memory objects; loading
ToolsConfig from kernel/config/tools.yaml is a later orchestration layer's
responsibility, not this module's). For an ACTION step, action_name and
resource_key are revalidated EXACTLY as persisted on the PlanStep - no
substitution, no fuzzy resolution, no fallback to a "similar" resource.
catalog_id has NO execution authority here: this module never imports
kernel.task_planner.catalog and never rebuilds a planner catalog to
authorize anything - the durable execution identity is action_name plus
resource_key, checked directly against the registry/config's own shape
(kernel.tools.registry.ActionRegistry.descriptors()'s
resource_key_requirement, and the matching field on ToolsConfig).

DEPENDENCY DEFENSE IN DEPTH: because kernel.task_planner.parser.py enforces
that a freshly-parsed step's depends_on may only reference strictly
earlier positions, and because this function scans positions in ascending
order and returns at the very first non-succeeded step it finds, a
not-started step's dependencies are, in a freshly-planned and
never-corrupted plan, always already satisfied by construction (every
earlier position was necessarily succeeded, or the scan would have
returned earlier). This function does not rely on that emergent property:
kernel.task_planner.serialization.deserialize_plan() does NOT re-validate
the backward-only depends_on constraint against a persisted row (see its
own docstring) - it only checks structural JSON shape - so a corrupted or
hand-edited plan_json could in principle contain a forward reference. Every
not-started step's declared dependencies are therefore re-checked
explicitly, directly against durable step progress, independent of scan
order.

Not implemented here (later milestones): claiming a step, calling
kernel.tools.SafeTaskExecutor, creating or consuming a confirmation,
calling a model for a RESPOND step, or advancing task state at all.
"""

from collections.abc import Sequence

from kernel.employee_tasks import StepStatus, TaskRecord, TaskStepProgress
from kernel.task_planner import PlanDeserializationError, PlanStep, StepKind, deserialize_plan
from kernel.task_execution.types import (
    ActionRevalidationFailure,
    AllStepsComplete,
    Blocked,
    EligibilityBlockReason,
    EligibilityOutcome,
    EligibleStep,
    PlanDeserializationFailure,
    PlanIntegrityFailure,
)
from kernel.tools.config import ToolsConfig
from kernel.tools.registry import ActionRegistry, ResourceKeyRequirement

# Maps each action whose resource_key requirement is REQUIRED to the
# ToolsConfig field holding its real, configured resource keys. A small,
# independent copy - not imported from kernel/task_planner/catalog.py or
# kernel/action_protocol/candidates.py, matching this codebase's own
# established convention of keeping this mapping private to each module
# that needs it rather than introducing a shared module for two-or-three
# callers (see catalog.py's own comment on this same choice). This module
# never imports kernel.task_planner.catalog - see module docstring.
_RESOURCE_FIELD_BY_ACTION = {
    "list_files": "approved_directories",
    "open_application": "approved_applications",
    "run_registered_script": "approved_scripts",
    "repo_health": "approved_repositories",
    "repository_backup": "approved_backups",
}


def revalidate_action(
    action_name: str,
    resource_key: str | None,
    registry: ActionRegistry,
    tools_config: ToolsConfig,
) -> bool:
    """True only if `action_name`/`resource_key` still resolve exactly
    against the CURRENT registry/config - no substitution, no fuzzy
    matching, no fallback to any other configured resource. A step's
    catalog_id is never consulted here or by any caller of this function.

    Public (not module-private) because kernel/task_execution/service.py
    (Milestone 42 P2) reuses this exact check at confirmation-approval
    time (see approve_task_confirmation()) - the durable pending
    confirmation row only carries action_name/resource_key, not a full
    PlanStep, so this function takes the two raw values directly rather
    than a PlanStep, decoupling it from evaluate_next_step()'s own
    call site below (which still calls it with a real step's fields)."""

    if not registry.is_known(action_name):
        return False

    descriptor = next(
        (d for d in registry.descriptors() if d.name == action_name), None
    )
    if descriptor is None:
        return False

    if descriptor.resource_key_requirement is ResourceKeyRequirement.FORBIDDEN:
        return resource_key is None

    if resource_key is None:
        return False

    resource_field = _RESOURCE_FIELD_BY_ACTION.get(action_name)
    if resource_field is None:
        return False

    configured_resources = getattr(tools_config, resource_field)
    return resource_key in configured_resources


def resolve_persisted_plan_step(
    task: TaskRecord, position: int
) -> PlanStep | PlanIntegrityFailure | PlanDeserializationFailure:
    """Deserialize task.plan_json, verify plan.task_id == task.task_id,
    and locate exactly the PlanStep at `position` in the persisted plan -
    the smallest pure "resolve one step from the persisted plan"
    operation, factored out for reuse by
    kernel.task_execution.service.approve_task_confirmation() (Milestone
    42 P2 correction): a durable task_pending_confirmation row is a record
    of a confirmation PROPOSAL, never execution authority by itself -
    approval must re-bind it to the IMMUTABLE persisted TaskPlan before
    treating any of its fields as authoritative, exactly like
    evaluate_next_step() already does for the non-sensitive path below.

    Deliberately not used internally by evaluate_next_step() itself - that
    function's own inline plan-resolution logic performs the identical
    deserialize/task_id checks as one step of its larger eligibility scan
    (which also needs the FULL step list, not just one position); sharing
    this helper there would not simplify anything and is out of scope for
    this correction (no behavior change, no broad refactor for aesthetics
    alone - see this package's own "small, focused changes" doctrine)."""

    if task.plan_json is None:
        return PlanIntegrityFailure(detail="task has no persisted plan")

    try:
        plan = deserialize_plan(task.plan_json)
    except PlanDeserializationError:
        return PlanDeserializationFailure(
            detail="task.plan_json is not a valid serialized plan"
        )

    if plan.task_id != task.task_id:
        return PlanIntegrityFailure(
            detail="persisted plan's task_id does not match the task record"
        )

    for step in plan.steps:
        if step.position == position:
            return step

    return PlanIntegrityFailure(
        detail=f"persisted plan has no step at position {position}"
    )


def evaluate_next_step(
    task: TaskRecord,
    step_progress: Sequence[TaskStepProgress],
    registry: ActionRegistry,
    tools_config: ToolsConfig,
) -> EligibilityOutcome:
    """Deterministically decide what, if anything, is currently eligible
    to run next for `task`. See this module's own docstring and
    kernel/task_execution/types.py's EligibilityOutcome docstring for the
    full contract. Considers only steps from the persisted TaskPlan
    (task.plan_json) in ascending PlanStep.position order; the first
    eligible position wins - independent steps are never reordered for
    convenience."""

    if task.plan_json is None:
        return PlanIntegrityFailure(detail="task has no persisted plan")

    try:
        plan = deserialize_plan(task.plan_json)
    except PlanDeserializationError:
        return PlanDeserializationFailure(
            detail="task.plan_json is not a valid serialized plan"
        )

    if plan.task_id != task.task_id:
        return PlanIntegrityFailure(
            detail="persisted plan's task_id does not match the task record"
        )

    progress_by_position = {record.step_position: record for record in step_progress}

    for step in sorted(plan.steps, key=lambda s: s.position):
        progress = progress_by_position.get(step.position)

        if progress is not None:
            if progress.status == StepStatus.SUCCEEDED:
                continue
            if progress.status == StepStatus.FAILED:
                return Blocked(EligibilityBlockReason.STEP_FAILED, step.position)
            # StepStatus.IN_PROGRESS: an UNCERTAIN execution state. Never
            # retried, never skipped, never treated as success or
            # failure by this layer - see kernel/task_execution/types.py's
            # Blocked docstring and the module docstring above.
            return Blocked(EligibilityBlockReason.STEP_IN_PROGRESS, step.position)

        # Not started. Re-verify every declared dependency is durably
        # succeeded explicitly - see module docstring's "dependency
        # defense in depth" section for why this is not assumed from scan
        # order alone.
        unmet_dependencies = [
            dependency
            for dependency in step.depends_on
            if progress_by_position.get(dependency) is None
            or progress_by_position[dependency].status != StepStatus.SUCCEEDED
        ]
        if unmet_dependencies:
            return Blocked(
                EligibilityBlockReason.DEPENDENCIES_NOT_SATISFIED, step.position
            )

        if step.kind is StepKind.ACTION:
            if not revalidate_action(step.action_name, step.resource_key, registry, tools_config):
                return ActionRevalidationFailure(
                    step_position=step.position,
                    action_name=step.action_name,
                    resource_key=step.resource_key,
                )
            return EligibleStep(
                step=step,
                currently_sensitive=registry.is_sensitive(step.action_name),
            )

        # StepKind.RESPOND - no action/resource revalidation applies, and
        # no model is ever called from this module.
        return EligibleStep(step=step, currently_sensitive=False)

    return AllStepsComplete()
