"""
run_registered_script action handler: runs one registered script to
completion, or until its configured timeout, by its symbolic key only.
The interpreter, script path, working directory, and timeout all come
only from kernel/config/tools.yaml - the sender never supplies script
arguments of any kind.
"""

from kernel.tools.process_control import run_with_timeout
from kernel.tools.types import ActionRequest, ActionResult


def run(request: ActionRequest, tools_config) -> ActionResult:
    key = request.resource_key
    spec = tools_config.approved_scripts.get(key)
    if spec is None:
        return ActionResult(False, "That script is not registered.", "rejected")

    result = run_with_timeout(
        [spec.interpreter, spec.script_path], spec.cwd, spec.timeout_seconds
    )

    if result.timed_out:
        return ActionResult(False, "That script timed out and was stopped.", "timed_out")
    if not result.success:
        return ActionResult(False, "That script did not complete successfully.", "failed")

    return ActionResult(True, f"'{key}' completed successfully.", "executed")
