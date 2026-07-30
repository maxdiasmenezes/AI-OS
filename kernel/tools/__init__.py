"""
Public interface of the safe task execution layer (Milestone 33).

Callers outside this package (e.g. capabilities/tasks/) must import from
here, matching the convention used by kernel/memory/__init__.py and
kernel/knowledge/__init__.py. Modules inside kernel/tools/ may still
import each other's submodules directly - that's internal wiring, not the
public surface.
"""

from kernel.tools.config import (
    ApplicationSpec,
    ScriptSpec,
    ToolsConfig,
    ToolsConfigError,
    load_tools_config,
)
from kernel.tools.confirmation import ConfirmationStore, PendingAction, default_store
from kernel.tools.executor import SafeTaskExecutor
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest, ActionResult

__all__ = [
    "ApplicationSpec",
    "ScriptSpec",
    "ToolsConfig",
    "ToolsConfigError",
    "load_tools_config",
    "ConfirmationStore",
    "PendingAction",
    "default_store",
    "SafeTaskExecutor",
    "ActionRegistry",
    "ActionRequest",
    "ActionResult",
]
