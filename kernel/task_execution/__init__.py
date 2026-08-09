"""
Public interface of kernel/task_execution/ (Milestone 42 P1 - Durable Step
Progress + Deterministic Next-Step Eligibility).

Callers outside this package must import from here, matching the
convention used by kernel/task_planner/__init__.py,
kernel/employee_tasks/__init__.py, kernel/tools/__init__.py, and
kernel/action_protocol/__init__.py.

kernel/task_execution/ takes one kernel/employee_tasks TaskRecord (whose
plan_json is a Milestone 41 TaskPlan), its durable per-step progress
(kernel/employee_tasks' TaskStepProgress rows), and the CURRENT
ActionRegistry/ToolsConfig, and deterministically decides what - if
anything - is eligible to run next (eligibility.py:evaluate_next_step()).
It never executes an action, never calls kernel/tools/SafeTaskExecutor,
never creates or consumes a confirmation, never calls a model, and never
performs I/O itself - no database read/write, no tools.yaml read. This
package has no dependency on kernel/employee_tasks' db.py or
repository.py, only on its plain, I/O-free top-level exports; no
dependency on kernel/tools/executor.py, kernel/tools/confirmation.py, or
kernel/tools/process_control.py; and no dependency on
kernel/action_protocol/ or kernel/task_planner's model-calling modules
(planner.py, prompt.py, parser.py) - only on its plain, I/O-free
serialization and type exports (deserialize_plan(), PlanStep, StepKind,
PlanDeserializationError).

catalog_id carries no execution authority anywhere in this package: a
step's action_name/resource_key are revalidated directly against the
CURRENT ActionRegistry/ToolsConfig supplied by the caller, never by
rebuilding a kernel.task_planner.catalog.build_catalog() catalog and
authorizing execution from a recomputed catalog_id (which is not even
guaranteed stable across two catalog builds - see eligibility.py's module
docstring).

As of Milestone 42 P1, this package has no service.py and no runtime
caller - claiming a step, executing an ACTION step through
SafeTaskExecutor, the durable confirmation flow, RESPOND synthesis via a
conversational ModelProvider, and any bounded execution-loop primitive
that repeatedly advances a task are all later Milestone 42 phases (P2/P3),
not this one.
"""

from kernel.task_execution.eligibility import evaluate_next_step
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

__all__ = [
    "evaluate_next_step",
    "EligibilityOutcome",
    "EligibleStep",
    "AllStepsComplete",
    "Blocked",
    "EligibilityBlockReason",
    "PlanIntegrityFailure",
    "PlanDeserializationFailure",
    "ActionRevalidationFailure",
]
