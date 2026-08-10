"""
Public interface of kernel/task_execution/ (Milestone 42 P1 - Durable Step
Progress + Deterministic Next-Step Eligibility; Milestone 42 P2 - Action
Execution and Durable Confirmation; Milestone 42 P3 - RESPOND Synthesis
and Bounded Autonomous Execution Loop).

Callers outside this package must import from here, matching the
convention used by kernel/task_planner/__init__.py,
kernel/employee_tasks/__init__.py, kernel/tools/__init__.py, and
kernel/action_protocol/__init__.py.

kernel/task_execution/ takes one kernel/employee_tasks TaskRecord (whose
plan_json is a Milestone 41 TaskPlan), its durable per-step progress
(kernel/employee_tasks' TaskStepProgress rows), and the CURRENT
ActionRegistry/ToolsConfig, and deterministically decides what - if
anything - is eligible to run next (eligibility.py:evaluate_next_step()).
service.py's advance_task_execution()/approve_task_confirmation()/
deny_task_confirmation() act on that decision: executing a non-sensitive
ACTION step through kernel.tools.executor.SafeTaskExecutor (the ONLY
action execution boundary this package ever calls - never a handler,
never kernel.tools.process_control, directly), synthesizing an eligible
RESPOND step's plain-text result through respond.py's
synthesize_response() using an injected kernel.models.base.ModelProvider
(Milestone 42 P3 - the ONLY place this package ever calls a model, and
never kernel.task_planner's structured-planner provider - see
service.py's and respond.py's own docstrings for the full model-role-
separation contract), proposing/consuming/denying/expiring a durable,
task-scoped confirmation (kernel.employee_tasks' task_pending_confirmation
- wholly independent of kernel.tools.confirmation.py's in-memory
ConfirmationStore, which this package never imports), and durably
recording each step's result as a StepObservation - the same durable
mechanism for both ACTION and RESPOND steps.
service.py's run_task_until_blocked() (Milestone 42 P3) is a bounded
driver over advance_task_execution() alone, repeatedly calling it until a
blocking/terminal condition (task completed/failed/cancelled, or a
confirmation is required/pending) - see that function's own docstring.
This package still never performs config/database I/O itself (every
dependency - TaskRepository, ActionRegistry, ToolsConfig,
SafeTaskExecutor, ModelProvider - is injected explicitly by the caller).

catalog_id and the persisted PlanStep.requires_confirmation carry no
execution authority anywhere in this package: a step's action_name/
resource_key are always revalidated directly against the CURRENT
ActionRegistry/ToolsConfig, and sensitivity is always re-derived from the
CURRENT ActionRegistry - never trusted from a recomputed catalog_id or
from what the planner believed was sensitive at plan time.

As of Milestone 42 P3, this package still has no runtime caller - nothing
in kernel/orchestrator/, any capabilities/, or interfaces/whatsapp/ calls
advance_task_execution()/run_task_until_blocked()/
approve_task_confirmation()/deny_task_confirmation() from a real request.
Any runtime/interface/WhatsApp wiring, real task submission, confirmation
delivery/approval routing, or crash-recovery reconciliation of an
uncertain in_progress step is Milestone 43 and later (see service.py's own
docstring for the exact scope boundary).
"""

from kernel.task_execution.eligibility import (
    evaluate_next_step,
    resolve_persisted_plan_step,
    revalidate_action,
)
from kernel.task_execution.observation import (
    OBSERVATION_VERSION,
    ObservationDeserializationError,
    ObservationSerializationError,
    StepObservation,
    build_action_observation,
    build_respond_observation,
    deserialize_observation,
    serialize_observation,
)
from kernel.task_execution.respond import (
    RespondContextTooLargeFailure,
    RespondDependencyFailure,
    RespondInvalidOutputFailure,
    RespondOutcome,
    RespondProviderFailure,
    RespondSuccess,
    synthesize_response,
)
from kernel.task_execution.service import (
    TaskNotReadyOrRunningError,
    TaskNotWaitingForConfirmationError,
    advance_task_execution,
    approve_task_confirmation,
    deny_task_confirmation,
    run_task_until_blocked,
)
from kernel.task_execution.types import (
    MAX_EXECUTION_ADVANCES,
    MAX_RESPOND_TEXT_CHARS,
    TASK_CONFIRMATION_TTL_SECONDS,
    ActionRevalidationFailure,
    AllStepsComplete,
    Blocked,
    EligibilityBlockReason,
    EligibilityOutcome,
    EligibleStep,
    ExecutionAdvanceResult,
    ExecutionAdvanceStatus,
    PlanDeserializationFailure,
    PlanIntegrityFailure,
)

__all__ = [
    "evaluate_next_step",
    "resolve_persisted_plan_step",
    "revalidate_action",
    "EligibilityOutcome",
    "EligibleStep",
    "AllStepsComplete",
    "Blocked",
    "EligibilityBlockReason",
    "PlanIntegrityFailure",
    "PlanDeserializationFailure",
    "ActionRevalidationFailure",
    "OBSERVATION_VERSION",
    "StepObservation",
    "build_action_observation",
    "build_respond_observation",
    "serialize_observation",
    "deserialize_observation",
    "ObservationSerializationError",
    "ObservationDeserializationError",
    "synthesize_response",
    "RespondOutcome",
    "RespondSuccess",
    "RespondDependencyFailure",
    "RespondContextTooLargeFailure",
    "RespondProviderFailure",
    "RespondInvalidOutputFailure",
    "TASK_CONFIRMATION_TTL_SECONDS",
    "MAX_RESPOND_TEXT_CHARS",
    "MAX_EXECUTION_ADVANCES",
    "ExecutionAdvanceResult",
    "ExecutionAdvanceStatus",
    "advance_task_execution",
    "run_task_until_blocked",
    "approve_task_confirmation",
    "deny_task_confirmation",
    "TaskNotReadyOrRunningError",
    "TaskNotWaitingForConfirmationError",
]
