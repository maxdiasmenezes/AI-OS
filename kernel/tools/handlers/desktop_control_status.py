"""
desktop_control_status action handler (Milestone 45 P1): reports whether
exactly one individually registered desktop control
(ToolsConfig.approved_desktop_controls) currently exists inside its
already-approved desktop target - never a caller/model-supplied control
selector, AutomationId, text, or index. resource_key is the ONLY input
this handler ever accepts, matching desktop_target_status.py's own
discipline exactly.

READ-ONLY / NON-SENSITIVE: see desktop_target_status.py's module docstring
for the full authority/sensitivity rationale, which applies identically
here - this handler never invokes, focuses, or otherwise mutates the
resolved control. Resolution (kernel/tools/desktop_safety.py's
resolve_control_status()) always performs a FRESH resolution of the
control's target first, and the control can only be reported available if
its target independently resolves to exactly one live, non-minimized
window owned by the configured process_executable - see that function's
own docstring.
"""

from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH
from kernel.tools.desktop_safety import (
    DesktopStatus,
    resolve_control_status,
    target_application_reference_valid,
)
from kernel.tools.types import ActionRequest, ActionResult

_CONTROL_UNREGISTERED = ActionResult(
    False, "That desktop control is not registered.", "rejected"
)
_TARGET_UNREGISTERED = ActionResult(
    False, "That desktop control is not registered.", "rejected"
)
# resolve_control_status() is documented to never raise - every internal
# failure maps to a DesktopStatus value instead, including its target's
# own status (AMBIGUOUS/CHECK_FAILED/AUTOMATION_UNAVAILABLE all propagate
# unchanged from the target - see that function's own docstring). The
# try/except below remains as pure defense in depth for a genuinely
# unexpected error only. See desktop_target_status.py's own identical
# comment.
_CHECK_FAILED = ActionResult(
    False, "That desktop control could not be checked.", "failed"
)
_AUTOMATION_UNAVAILABLE = ActionResult(
    False, "Desktop automation is unavailable.", "failed"
)

_MESSAGES = {
    DesktopStatus.AVAILABLE: "is available.",
    DesktopStatus.UNAVAILABLE: "is unavailable.",
    DesktopStatus.AMBIGUOUS: "is ambiguous.",
}


def run(request: ActionRequest, tools_config) -> ActionResult:
    resource_key = request.resource_key
    if resource_key is None or len(resource_key) > MAX_SYMBOLIC_NAME_LENGTH:
        return _CONTROL_UNREGISTERED

    control_spec = tools_config.approved_desktop_controls.get(resource_key)
    if control_spec is None:
        return _CONTROL_UNREGISTERED

    # Referential integrity is re-checked against the CURRENT config at
    # execution time, never assumed from config-load-time validation alone
    # - the target key this control referenced when tools.yaml was loaded
    # may have been removed from a since-reloaded config (this mirrors
    # kernel.task_execution.eligibility's own "current config is
    # authoritative" discipline, applied here one layer below it).
    target_spec = tools_config.approved_desktop_targets.get(control_spec.target_key)
    if target_spec is None:
        return _TARGET_UNREGISTERED

    if not target_application_reference_valid(target_spec, tools_config):
        return _TARGET_UNREGISTERED

    try:
        status = resolve_control_status(target_spec, control_spec)
    except Exception:
        return _CHECK_FAILED

    if status is DesktopStatus.CHECK_FAILED:
        return _CHECK_FAILED
    if status is DesktopStatus.AUTOMATION_UNAVAILABLE:
        return _AUTOMATION_UNAVAILABLE

    message = f"Control '{resource_key}' {_MESSAGES[status]}"
    return ActionResult(True, message, "executed")
