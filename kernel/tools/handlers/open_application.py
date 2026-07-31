"""
open_application action handler: launches one registered application by
its symbolic key and returns immediately after a successful launch -
never waits for the application to exit. The executable and its working
directory both come only from kernel/config/tools.yaml; nothing about
either is sender-supplied.
"""

from kernel.tools.process_control import launch_detached
from kernel.tools.types import ActionRequest, ActionResult


def run(request: ActionRequest, tools_config) -> ActionResult:
    key = request.resource_key
    spec = tools_config.approved_applications.get(key)
    if spec is None:
        return ActionResult(False, "That application is not registered.", "rejected")

    result = launch_detached([spec.executable], spec.cwd)
    if not result.success:
        return ActionResult(False, "That application could not be launched.", "failed")

    return ActionResult(True, f"'{key}' launched.", "executed")
