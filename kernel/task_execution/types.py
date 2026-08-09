"""
Typed data for kernel/task_execution/ (Milestone 42 P1 - Durable Step
Progress + Deterministic Next-Step Eligibility): the closed outcome union
evaluate_next_step() returns instead of executing anything.

Nothing here performs I/O, calls a model, executes an action, or mutates
kernel/employee_tasks - these are plain, frozen data, matching
kernel/task_planner/types.py's and kernel/action_protocol/types.py's own
convention. EligibilityOutcome is deliberately a closed, six-way union so a
caller can always distinguish exactly what evaluate_next_step() found,
without inspecting exception types or sentinel values:

  - EligibleStep              - a step from the persisted TaskPlan is ready
                                 to run right now: every earlier dependency
                                 has durably succeeded, and - for an ACTION
                                 step - the action/resource selection still
                                 revalidates against the CURRENT
                                 ActionRegistry/ToolsConfig supplied by the
                                 caller. Nothing has been claimed or
                                 executed yet; this is a pure decision.
  - AllStepsComplete           - every step in the plan has durably
                                 succeeded. The task is done.
  - Blocked                    - the plan cannot advance right now for a
                                 reason distinguishable via
                                 EligibilityBlockReason: an earlier step
                                 durably failed (the task cannot continue
                                 normally), an earlier step is durably
                                 in_progress (an UNCERTAIN execution state -
                                 never retried, never skipped, never
                                 treated as either success or failure by
                                 this layer), or a not-started step's
                                 declared dependencies are not all durably
                                 succeeded (defense in depth - see
                                 eligibility.py's own module docstring for
                                 why this is checked explicitly rather than
                                 assumed from scan order alone).
  - PlanIntegrityFailure        - the persisted plan's own task_id does not
                                 match the TaskRecord it was loaded
                                 alongside, or the task has no persisted
                                 plan at all. Never repaired automatically,
                                 never treated as a planning-time concern
                                 (kernel.task_planner.PlannerFailure) -
                                 this is a storage/execution-identity
                                 concern.
  - PlanDeserializationFailure  - task.plan_json is not valid, well-formed
                                 serialized-TaskPlan JSON (see
                                 kernel.task_planner.serialization's own
                                 PlanDeserializationError). A
                                 storage/corruption concern, never a
                                 planning-time concern either.
  - ActionRevalidationFailure   - a step's own action_name is no longer
                                 known to the current ActionRegistry, or
                                 its resource_key is no longer configured
                                 for that action in the current
                                 ToolsConfig. Never repaired, substituted,
                                 or silently skipped.

Every outcome except EligibleStep carries only fixed, code-authored or
already-safe (task_id, action_name, resource_key, step_position - all
already-persisted, non-secret identifiers) fields - never raw plan_json
content, a deserialization library's exception text, or anything else
that might carry unsafe detail.
"""

from dataclasses import dataclass
from enum import Enum

from kernel.task_planner import PlanStep


class EligibilityBlockReason(Enum):
    """Why evaluate_next_step() returned Blocked instead of an eligible
    step or completion. Mirrors kernel.task_planner.types.PlannerErrorCode's
    "one enum, one wrapping dataclass" convention."""

    DEPENDENCIES_NOT_SATISFIED = "dependencies_not_satisfied"
    STEP_FAILED = "step_failed"
    STEP_IN_PROGRESS = "step_in_progress"


@dataclass(frozen=True)
class EligibleStep:
    """The one step evaluate_next_step() selected. `step` is the exact,
    immutable PlanStep from the persisted TaskPlan - never reconstructed
    or modified. `currently_sensitive` is re-derived from the CURRENT
    ActionRegistry.is_sensitive(step.action_name) at evaluation time
    (always False for a RESPOND step, which has no action_name) - never
    trusted from the persisted PlanStep.requires_confirmation, which
    reflects the registry's state at planning time, not now."""

    step: PlanStep
    currently_sensitive: bool


@dataclass(frozen=True)
class AllStepsComplete:
    """Every step in the persisted plan has durably succeeded."""


@dataclass(frozen=True)
class Blocked:
    """The plan cannot advance past `step_position` right now. See
    EligibilityBlockReason for what, specifically, is blocking it."""

    reason: EligibilityBlockReason
    step_position: int


@dataclass(frozen=True)
class PlanIntegrityFailure:
    """The task's persisted plan identity could not be trusted - either
    there is no persisted plan, or its embedded task_id does not match
    the TaskRecord it was loaded alongside. `detail` is a short, fixed,
    code-authored phrase - never raw plan_json content."""

    detail: str


@dataclass(frozen=True)
class PlanDeserializationFailure:
    """task.plan_json is not valid, well-formed serialized-TaskPlan JSON.
    `detail` is a short, fixed, code-authored phrase - never raw
    plan_json content or a library exception's own text."""

    detail: str


@dataclass(frozen=True)
class ActionRevalidationFailure:
    """The selected step's action_name/resource_key no longer resolves
    against the CURRENT ActionRegistry/ToolsConfig supplied to
    evaluate_next_step() - e.g. a resource key removed from tools.yaml
    since the plan was created. Carries only already-persisted, non-secret
    identifiers - never a resolved path or other machine detail."""

    step_position: int
    action_name: str
    resource_key: str | None


# What eligibility.py:evaluate_next_step() returns - a closed, six-way
# union. See this module's own docstring for what each branch means.
EligibilityOutcome = (
    EligibleStep
    | AllStepsComplete
    | Blocked
    | PlanIntegrityFailure
    | PlanDeserializationFailure
    | ActionRevalidationFailure
)
