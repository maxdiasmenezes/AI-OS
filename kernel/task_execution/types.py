"""
Typed data for kernel/task_execution/ (Milestone 42 P1 - Durable Step
Progress + Deterministic Next-Step Eligibility; Milestone 42 P2 - Action
Execution and Durable Confirmation adds TASK_CONFIRMATION_TTL_SECONDS and
ExecutionAdvanceResult/ExecutionAdvanceStatus below): the closed outcome
union evaluate_next_step() returns instead of executing anything, plus the
result shape service.py's advance_task_execution()/
approve_task_confirmation()/deny_task_confirmation() return.

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

from kernel.employee_tasks import TaskRecord
from kernel.task_planner import MAX_PLAN_STEPS, PlanStep


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


# Milestone 42 P2: the task-execution confirmation safety window. Kept
# separate from kernel/tools/confirmation.py's ConfirmationStore's own
# CONFIRMATION_TTL_SECONDS (also 120) so this package never imports that
# module merely to share a numeric literal - the two confirmation systems
# are wholly independent (see kernel/employee_tasks/__init__.py). The
# value deliberately matches that store's own safety window today, but
# either may diverge later without coupling the two; M46 may revisit the
# UX/configuration of this value once confirmations are actually delivered
# over WhatsApp - this constant only establishes P2's safety semantics.
TASK_CONFIRMATION_TTL_SECONDS = 120.0

# Milestone 42 P3: an EARLY, raw-text bound on RESPOND's synthesized
# response, checked before any persistence attempt is even built (see
# respond.py's _validate_response_text()) - this alone rejects a response
# that is oversized in its own right, before it is ever wrapped in a
# StepObservation. IMPORTANT: this raw character count does NOT by itself
# guarantee the resulting StepObservation fits within
# MAX_STEP_RESULT_JSON_CHARS once serialized - json.dumps() escaping
# (quotes -> \", backslashes -> \\, and most control characters -> a
# 6-character \uXXXX sequence) can expand a string's serialized length
# substantially; a string of 3,800 quote characters, for example,
# serializes to roughly 7,600 characters, well past the 4,096-character
# result_json bound, even though every individual character was ASCII and
# within this bound. The AUTHORITATIVE persistence bound is always
# len(serialize_observation(observation)) <= MAX_STEP_RESULT_JSON_CHARS,
# checked explicitly and separately by service.py's RESPOND success path,
# which fails the step/task closed with respond_invalid_output if
# serialize_observation() raises ObservationSerializationError, rather
# than ever persisting a truncated or partial observation - see
# service.py's own RESPOND handling and test_service.py's escaping-bound
# tests. This raw bound remains a useful, cheap, early rejection for a
# response that is simply too long on its own; it is never treated as a
# substitute for the real serialized-size check.
MAX_RESPOND_TEXT_CHARS = 3800

# Milestone 42 P3: the bounded autonomous runner's hard step-advance limit
# (service.py's run_task_until_blocked()). A plan of MAX_PLAN_STEPS steps
# (kernel.task_planner.types.MAX_PLAN_STEPS - the planner's own maximum
# plan size) needs at most MAX_PLAN_STEPS successful advances (one per
# step) plus exactly one further advance to observe AllStepsComplete ->
# COMPLETED - so this bound is derived directly from, and stays in lock
# step with, that existing maximum rather than an arbitrary headroom
# number: every valid maximum-size plan can always finish inside this
# bound, and a logic bug cannot spin extra successful advances beyond what
# any valid plan could ever legitimately need before the bound trips and
# fails the task closed.
MAX_EXECUTION_ADVANCES = MAX_PLAN_STEPS + 1


class ExecutionAdvanceStatus(Enum):
    """What kernel/task_execution/service.py's advance_task_execution()/
    approve_task_confirmation()/deny_task_confirmation() actually did (or
    found), distinguishing every outcome a caller needs to tell apart."""

    STEP_SUCCEEDED = "step_succeeded"
    CONFIRMATION_REQUIRED = "confirmation_required"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"


@dataclass(frozen=True)
class ExecutionAdvanceResult:
    """The return shape for every kernel/task_execution/service.py public
    entrypoint. `task` is always the task's freshly reloaded, current
    TaskRecord after whatever this call did (never a stale echo of the
    caller's input). `detail` is an optional, fixed, code-authored short
    string (a failure/outcome code, e.g. "confirmation_expired") - never
    raw plan_json/model/request text."""

    task: TaskRecord
    status: ExecutionAdvanceStatus
    detail: str | None = None
