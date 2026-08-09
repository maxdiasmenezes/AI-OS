"""
Public interface of kernel/task_execution/ (Milestone 42 P1 - Durable Step
Progress + Deterministic Next-Step Eligibility; Milestone 42 P2 - Action
Execution and Durable Confirmation).

Callers outside this package must import from here, matching the
convention used by kernel/task_planner/__init__.py,
kernel/employee_tasks/__init__.py, kernel/tools/__init__.py, and
kernel/action_protocol/__init__.py.

kernel/task_execution/ takes one kernel/employee_tasks TaskRecord (whose
plan_json is a Milestone 41 TaskPlan), its durable per-step progress
(kernel/employee_tasks' TaskStepProgress rows), and the CURRENT
ActionRegistry/ToolsConfig, and deterministically decides what - if
anything - is eligible to run next (eligibility.py:evaluate_next_step()).
As of Milestone 42 P2, service.py's advance_task_execution()/
approve_task_confirmation()/deny_task_confirmation() act on that decision:
executing a non-sensitive ACTION step through kernel.tools.executor.
SafeTaskExecutor (the ONLY action execution boundary this package ever
calls - never a handler, never kernel.tools.process_control, directly),
proposing/consuming/denying/expiring a durable, task-scoped confirmation
(kernel.employee_tasks' task_pending_confirmation - wholly independent of
kernel.tools.confirmation.py's in-memory ConfirmationStore, which this
package never imports), and durably recording each ACTION step's result
as a StepObservation. It still never calls a model, still never performs
config/database I/O itself (every dependency - TaskRepository,
ActionRegistry, ToolsConfig, SafeTaskExecutor - is injected explicitly by
the caller), and still never supports a RESPOND step (fails closed with a
fixed code - see service.py's module docstring; RESPOND synthesis is
Milestone 42 P3).

catalog_id and the persisted PlanStep.requires_confirmation carry no
execution authority anywhere in this package: a step's action_name/
resource_key are always revalidated directly against the CURRENT
ActionRegistry/ToolsConfig, and sensitivity is always re-derived from the
CURRENT ActionRegistry - never trusted from a recomputed catalog_id or
from what the planner believed was sensitive at plan time.

As of Milestone 42 P2, this package still has no runtime caller - nothing
in kernel/orchestrator/, any capabilities/, or interfaces/whatsapp/ calls
advance_task_execution()/approve_task_confirmation()/
deny_task_confirmation() from a real request. A bounded execution-loop
primitive that repeatedly advances a task until a blocking/terminal
condition, RESPOND synthesis via the conversational ModelProvider, and any
runtime/interface/WhatsApp wiring are Milestone 42 P3 and later.
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
    deserialize_observation,
    serialize_observation,
)
from kernel.task_execution.service import (
    TaskNotReadyOrRunningError,
    TaskNotWaitingForConfirmationError,
    advance_task_execution,
    approve_task_confirmation,
    deny_task_confirmation,
)
from kernel.task_execution.types import (
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
    "serialize_observation",
    "deserialize_observation",
    "ObservationSerializationError",
    "ObservationDeserializationError",
    "TASK_CONFIRMATION_TTL_SECONDS",
    "ExecutionAdvanceResult",
    "ExecutionAdvanceStatus",
    "advance_task_execution",
    "approve_task_confirmation",
    "deny_task_confirmation",
    "TaskNotReadyOrRunningError",
    "TaskNotWaitingForConfirmationError",
]
