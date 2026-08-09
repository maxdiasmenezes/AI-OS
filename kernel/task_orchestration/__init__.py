"""
Public interface of kernel/task_orchestration/ (Milestone 41 P2 -
Planning Orchestration).

Callers outside this package must import from here, matching the
convention used by kernel/action_protocol/__init__.py,
kernel/employee_tasks/__init__.py, and kernel/task_planner/__init__.py.

This package connects the committed P1 pure planner (kernel.task_planner)
to the persistent task lifecycle (kernel.employee_tasks, Milestone 40),
without either of those depending on the other. It may depend on
kernel.employee_tasks, kernel.task_planner, and kernel.models. It must
never import a tool executor, a tool execution handler, process-control
execution, confirmation execution, or any Milestone 42 execution-loop
code - see tests/kernel/task_orchestration/test_service.py's import-
boundary test, which enforces this mechanically, not just by convention.

advance_task_planning() runs the full created -> planning -> {ready |
failed} sequence for one task and one invocation. It never executes a
plan step, never calls a tool, never loops autonomously, and never
retries. Execution of a ready plan's steps begins in a later milestone -
not this one.
"""

from kernel.task_orchestration.service import (
    TaskNotInCreatedStateError,
    advance_task_planning,
)

__all__ = [
    "advance_task_planning",
    "TaskNotInCreatedStateError",
]
