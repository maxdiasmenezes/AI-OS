"""
SafeTaskExecutor: the single choke point every computer action passes
through. Looks the action up in ActionRegistry, calls its handler with the
local ToolsConfig, and unconditionally audit-logs the outcome - success,
failure, or rejection - regardless of what the handler itself does.

Per-action timeout enforcement (and, for run_registered_script, killing
the full process tree on timeout) lives inside the handler that actually
spawns a process (kernel/tools/process_control.py), not here - handlers
that don't spawn a process bound their own duration directly (a capped
directory listing, short HTTP timeouts).
"""

import logging

from kernel.tools import audit
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest, ActionResult

logger = logging.getLogger(__name__)


class SafeTaskExecutor:
    def __init__(self, tools_config, registry: ActionRegistry | None = None):
        self._config = tools_config
        self._registry = registry or ActionRegistry()

    def execute(self, request: ActionRequest) -> ActionResult:
        if not self._registry.is_known(request.action):
            result = ActionResult(False, "Unknown action.", "rejected")
            audit.record(request.action, request.resource_key, result.outcome)
            return result

        handler = self._registry.handler_for(request.action)
        try:
            result = handler(request, self._config)
        except Exception:
            # No exception object, message, or traceback ever logged - it
            # could carry a resolved path or other machine detail.
            logger.warning("task_execution_error action=%s", request.action)
            result = ActionResult(False, "That action could not be completed.", "failed")

        audit.record(request.action, request.resource_key, result.outcome)
        return result
