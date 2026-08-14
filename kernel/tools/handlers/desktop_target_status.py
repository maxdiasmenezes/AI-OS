"""
desktop_target_status action handler (Milestone 45 P1 - Windows Desktop
Foundation and Exact Target/Control Status): reports whether exactly one
individually registered desktop target (ToolsConfig.approved_desktop_targets)
currently exists on the live Windows desktop - never a caller/model-supplied
window, class name, title, or executable. resource_key is the ONLY input
this handler ever accepts, exactly like read_text_file.py/
browser_read_page.py's own established discipline for approved_files/
approved_pages.

READ-ONLY / NON-SENSITIVE: this handler never focuses, activates, resizes,
moves, closes, or otherwise mutates anything - it only observes whether a
preconfigured target is currently resolvable (see
kernel/tools/desktop_safety.py's resolve_target_status() for the exact
resolution algorithm and its own module docstring for the full authority
model). Its output is one of exactly three fixed, code-owned strings, never
a match count, window title, PID, HWND, process path, class name, or
automation ID - see kernel/tools/desktop_safety.py's DesktopStatus and this
module's own _MESSAGES mapping. This is why it belongs in the same
non-sensitive, no-confirmation trust tier as system_status/repo_health/
browser_read_page (see kernel/tools/registry.py) even though its
implementation internally reads UIA properties to perform exact matching -
sensitivity is about output/authority/side effect, never about what a
handler reads internally to do its job correctly.
"""

from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH
from kernel.tools.desktop_safety import (
    DesktopStatus,
    resolve_target_status,
    target_application_reference_valid,
)
from kernel.tools.types import ActionRequest, ActionResult

_UNREGISTERED = ActionResult(False, "That desktop target is not registered.", "rejected")
# resolve_target_status() is documented to never raise - every internal
# failure maps to a DesktopStatus value instead (see
# kernel/tools/desktop_safety.py's module docstring). CHECK_FAILED and
# AUTOMATION_UNAVAILABLE are both real, reachable outcomes now (not dead
# code) - see that module's DesktopStatus docstring for the distinction
# between them and UNAVAILABLE. The try/except below remains as pure
# defense in depth for a genuinely unexpected error only.
_CHECK_FAILED = ActionResult(
    False, "That desktop target could not be checked.", "failed"
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
        return _UNREGISTERED

    spec = tools_config.approved_desktop_targets.get(resource_key)
    if spec is None:
        return _UNREGISTERED

    # Referential integrity re-checked against the CURRENT config, never
    # assumed from config-load-time validation alone - see
    # kernel/tools/desktop_safety.py's target_application_reference_valid()
    # docstring.
    if not target_application_reference_valid(spec, tools_config):
        return _UNREGISTERED

    try:
        status = resolve_target_status(spec, tools_config)
    except Exception:
        # No raw pywinauto/COM/Win32 exception, path, or metadata ever
        # surfaces - mirrors kernel/tools/executor.py's own
        # "never surface a raw exception" discipline one layer up, applied
        # here too since resolve_target_status() is documented to already
        # never raise; this is defense in depth only.
        return _CHECK_FAILED

    if status is DesktopStatus.CHECK_FAILED:
        return _CHECK_FAILED
    if status is DesktopStatus.AUTOMATION_UNAVAILABLE:
        return _AUTOMATION_UNAVAILABLE

    message = f"Target '{resource_key}' {_MESSAGES[status]}"
    return ActionResult(True, message, "executed")
