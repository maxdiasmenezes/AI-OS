"""
Typed request/result objects for the safe task execution layer
(kernel/tools/). Nothing here executes anything - these are plain data
carried between capabilities/tasks/ and kernel/tools/executor.py.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionRequest:
    """One request to run a registered action.

    `action` is one of the names ActionRegistry knows about.
    `resource_key` is the single symbolic key the caller selected (a
    directory, an application, or a script name) - never a raw path or
    argument list. Actions that take no resource (system_status) leave it
    None.
    """

    action: str
    resource_key: str | None = None


@dataclass(frozen=True)
class ActionResult:
    """Result of executing (or refusing to execute) one ActionRequest.

    `message` is safe to relay to the caller as-is: it never contains a
    resolved filesystem path, an exception message, or a traceback.
    `outcome` is one of the stable, symbolic codes defined in
    kernel/tools/audit.py.
    """

    success: bool
    message: str
    outcome: str
