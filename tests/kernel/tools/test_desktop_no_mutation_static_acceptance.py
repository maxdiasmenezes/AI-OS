"""Milestone 45 P1 static no-mutation acceptance: production M45 code must
be MECHANICALLY read-only, not merely read-only by current behavior. Uses
AST inspection (never brittle substring matching over the raw source,
which would false-positive on this module's own docstrings/comments that
legitimately discuss the forbidden APIs by name, and would also
false-positive on legitimate, unrelated identifiers that happen to share a
substring - e.g. kernel/tools/desktop_safety.py's own
`handle.Close()` call, PyWin32's real method name for releasing a file
handle opened only to read its identity, which is an exact-casing
different identifier from the forbidden lowercase `close`) - mirrors
kernel/task_planner's and kernel/task_execution's own AST-based
import-boundary test precedent (see e.g.
tests/kernel/task_execution/test_eligibility.py's
test_task_execution_pure_modules_never_import_io_or_execution_modules).

Every identifier below is checked as an EXACT attribute/name/imported-module
match, never a substring - so this test does not need, and does not use, any
special-case allowance."""

import ast
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_M45_PRODUCTION_FILES = (
    _PROJECT_ROOT / "kernel" / "tools" / "desktop_safety.py",
    _PROJECT_ROOT / "kernel" / "tools" / "handlers" / "desktop_target_status.py",
    _PROJECT_ROOT / "kernel" / "tools" / "handlers" / "desktop_control_status.py",
)

# Exact identifiers (never a substring) that would indicate mouse/keyboard
# input synthesis, focus manipulation, clipboard access, screenshot/capture,
# semantic control invocation, or process/window termination - none of
# which any M45 P1 production code may ever use. Named exactly as they
# appear in pywinauto/pywin32/win32 APIs.
_FORBIDDEN_IDENTIFIERS = frozenset(
    {
        "click",
        "click_input",
        "invoke",
        "iface_invoke",
        "type_keys",
        "send_keys",
        "SendInput",
        "SetForegroundWindow",
        "set_focus",
        "set_edit_text",
        "set_value",
        "screenshot",
        "capture",
        "close",
        "kill",
        "terminate",
    }
)

# Modules whose mere import would indicate input-synthesis capability -
# checked independently of the attribute/name scan above, since an import
# alias could otherwise hide a forbidden call behind a renamed reference.
_FORBIDDEN_MODULES = frozenset({"keyboard", "mouse", "pyautogui"})


def _collect_used_identifiers(tree: ast.AST) -> set[str]:
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
    return identifiers


def _collect_imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


@pytest.mark.parametrize("path", _M45_PRODUCTION_FILES, ids=lambda p: p.name)
def test_no_forbidden_input_or_mutation_identifiers(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    used = _collect_used_identifiers(tree)
    forbidden_found = used & _FORBIDDEN_IDENTIFIERS

    assert forbidden_found == set(), (
        f"{path} references forbidden identifier(s) {sorted(forbidden_found)} - "
        "Milestone 45 P1 is mechanically read-only; see kernel/tools/desktop_safety.py's "
        "module docstring."
    )


@pytest.mark.parametrize("path", _M45_PRODUCTION_FILES, ids=lambda p: p.name)
def test_no_forbidden_input_module_imports(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    imported = _collect_imported_modules(tree)
    forbidden_found = imported & _FORBIDDEN_MODULES

    assert forbidden_found == set(), f"{path} imports forbidden module(s) {sorted(forbidden_found)}"


@pytest.mark.parametrize("path", _M45_PRODUCTION_FILES, ids=lambda p: p.name)
def test_no_coordinate_looking_parameter_names(path):
    """Defense in depth against a coordinate-based primitive being
    reintroduced under a different method name - a function/parameter
    literally named x/y/coordinates is the shape any such primitive would
    need, even if it didn't reuse one of the forbidden names above."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    suspicious = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.arg in {"x", "y", "x_coord", "y_coord", "coordinates"}:
            suspicious.add(node.arg)

    assert suspicious == set(), f"{path} has coordinate-shaped parameter(s) {sorted(suspicious)}"


# --- Positive API-surface boundary (pre-staging correction) ------------------
#
# The denylist tests above are useful but have known bypass classes an
# adversarial review identified: getattr(obj, "invoke"), a dynamic
# importlib import, or a raw numeric UIA pattern-ID call would all evade a
# name-based denylist entirely, since no forbidden IDENTIFIER would ever
# appear in the source. This section inverts the polarity: rather than
# denying a fixed set of known-dangerous attribute names, it asserts every
# ATTRIBUTE actually accessed anywhere in M45 production code is drawn
# from a small, explicit, code-owned allowlist of the exact external
# API surface P1 needs (pywinauto's find_elements, win32gui's IsIconic,
# win32file's metadata-only CreateFile/GetFileInformationByHandle/Close,
# psutil's Process/exe/NoSuchProcess, a handful of stdlib
# pathlib/stat/sys/platform accessors) plus this module's own internal
# dataclass fields and enum members. A future direct comtypes
# InvokePattern call, win32api.mouse_event, or any other UIA/Win32
# attribute this list does not already name would introduce a NEW
# attribute name and fail this test - it does not matter whether that new
# name happens to also appear in the denylist above. This is strictly
# broader coverage against accidental reintroduction than a denylist can
# ever provide, at the honestly-documented cost of NOT catching
# getattr()-based dynamic dispatch or a raw numeric pattern ID, neither of
# which is expressible as a named attribute access at all - no purely
# syntactic check can close that gap; see this module's own coverage
# limitations noted in the Milestone 45 correction pass.

_ALLOWED_ATTRIBUTE_NAMES = frozenset(
    {
        # pywinauto (UIA backend) - the one, low-level, exact-match entry
        # point this module ever calls.
        "find_elements",
        # win32gui - minimized-state query only.
        "IsIconic",
        # win32file - metadata-only file identity (never content read,
        # never write/delete access).
        "CreateFile",
        "GetFileInformationByHandle",
        "Close",
        "FILE_SHARE_READ",
        "FILE_SHARE_WRITE",
        "FILE_SHARE_DELETE",
        "OPEN_EXISTING",
        # psutil - process identity only.
        "Process",
        "exe",
        "NoSuchProcess",
        # pywinauto ElementInfo properties actually read.
        "handle",
        "process_id",
        # stdlib pathlib/stat/os-adjacent accessors used by config-load-time
        # AND runtime locator validation.
        "is_absolute",
        "lstat",
        "strip",
        "st_mode",
        "platform",
        # Plain container methods - never external authority.
        "get",
        "append",
        # This module's own dataclass fields (DesktopTargetSpec,
        # DesktopControlSpec, ActionRequest/ActionResult, ToolsConfig,
        # StatClassification) - internal data access, never external
        # UIA/Win32 API surface.
        "application_key",
        "process_executable",
        "window_class_name",
        "window_automation_id",
        "target_key",
        "control_automation_id",
        "control_type",
        "control_class_name",
        "resource_key",
        "approved_applications",
        "approved_desktop_targets",
        "approved_desktop_controls",
        "REGULAR_FILE",
        "SYMLINK_OR_REPARSE_POINT",
        # This module's own enum members (DesktopStatus, IdentityComparison,
        # _CandidateOutcome), accessed as class attributes.
        "AVAILABLE",
        "UNAVAILABLE",
        "AMBIGUOUS",
        "CHECK_FAILED",
        "AUTOMATION_UNAVAILABLE",
        "MATCH",
        "NO_MATCH",
        "QUALIFIED",
        "NON_QUALIFYING",
        "INSPECTION_FAILED",
    }
)


@pytest.mark.parametrize("path", _M45_PRODUCTION_FILES, ids=lambda p: p.name)
def test_only_the_approved_external_api_surface_is_used(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    used_attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    unexpected = used_attributes - _ALLOWED_ATTRIBUTE_NAMES

    assert unexpected == set(), (
        f"{path} uses attribute(s) {sorted(unexpected)} outside the approved M45 P1 "
        "external API surface - add to _ALLOWED_ATTRIBUTE_NAMES only after confirming "
        "the new attribute is genuinely read-only and does not introduce mutation/input "
        "authority."
    )


def test_desktop_safety_module_never_imports_a_confirmation_or_task_execution_module():
    """M45 P1 is entirely non-sensitive (see kernel/tools/registry.py) -
    it has no business importing kernel.tools.confirmation or
    kernel.task_execution at all; this is a structural cross-check that
    the read-only claim is not merely "no forbidden call today" but "no
    dependency on the machinery mutation/confirmation would need"."""

    path = _PROJECT_ROOT / "kernel" / "tools" / "desktop_safety.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)

    forbidden_prefixes = ("kernel.tools.confirmation", "kernel.task_execution")
    for module in imported_modules:
        for forbidden_prefix in forbidden_prefixes:
            assert not module.startswith(forbidden_prefix), (
                f"kernel/tools/desktop_safety.py must not import {module!r}"
            )
