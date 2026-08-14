"""
Shared Windows desktop-automation safety/identity logic for
kernel/tools/config.py (config-load-time validation of
approved_desktop_targets/approved_desktop_controls) and
kernel/tools/handlers/desktop_target_status.py and
desktop_control_status.py (the runtime exact-resolution gate) - mirrors
kernel/tools/browser_safety.py's and kernel/tools/file_safety.py's own
"shared, not duplicated" precedent. config.py depends on this module,
never the reverse; this module has no dependency on pywinauto/comtypes at
IMPORT time beyond what WINDOWS-gated code paths need (see PLATFORM
BOUNDARY below).

Milestone 45 P1 (Windows Desktop Foundation and Exact Target/Control
Status - see docs/architecture.md's Milestone 45 section for the full
design and its empirical validation history) is READ-ONLY: this module
resolves whether one preconfigured desktop target/control currently
exists, and nothing else. It never clicks, types, moves a cursor, sends a
keystroke, changes focus, or mutates any window/control state. Mutation
(desktop_invoke_control and similar) was empirically evaluated and
REJECTED for this milestone - see this module's own AUTHORITY MODEL
section and docs/architecture.md.

PLATFORM BOUNDARY: native Windows UI Automation is only meaningful on
Windows, in a normal, unlocked, interactive user session (see
docs/architecture.md's Milestone 45 "interactive session" discussion).
`pywinauto`/`win32file`/`win32gui` are imported only under `if WINDOWS:`,
so this module (and everything that imports it) collects cleanly under
pytest on a non-Windows machine - see tests/kernel/tools/
test_desktop_windows_integration.py for the real UIA integration coverage,
gated with `pytest.mark.skipif(sys.platform != "win32", ...)`. Every
resolution function below checks `WINDOWS` first and returns/raises the
same fixed "unavailable" outcome a real failed UIA resolution would, so no
caller needs a separate non-Windows code path.

AUTHORITY MODEL: an approved desktop target/control is identified ENTIRELY
by configuration (kernel/config/tools.yaml) - never by request text,
description, model output, or a prior step's observation (see
docs/architecture.md's "read-then-act" and "prompt-injection boundary"
discussion for Milestone 45). Window identity is
(window class name [+ optional window automation ID] AND the live
window's owning process image matching a configured process_executable);
control identity is (an automation ID, REQUIRED, plus a control type,
scoped to an already-resolved-and-validated single target window). Window
TITLE and any control's Name/displayed text are never part of this
authority model and are never returned in output - see WHY AUTOMATION_ID
IS REQUIRED and TITLE/TEXT EXCLUSION below.

WHY AUTOMATION_ID IS REQUIRED: empirical validation (see
docs/architecture.md) proved that a stock Tkinter application's sibling
controls of the same type share an EMPTY automation_id and an IDENTICAL
class_name/control_type - class/type alone cannot distinguish them.
AutomationId only became stable and distinguishing once the validation
fixture was modified to assign its own native Win32 control IDs, and that
identity was then proven stable across 5 independent launch/close cycles.
Because of this, `approved_desktop_controls[*].control_automation_id` is
REQUIRED and must be a non-empty string - an application that cannot
expose one for a given control is UNSUPPORTED by this milestone; this
module never falls back to visible Name/text, sibling index, or position.

TITLE/TEXT EXCLUSION: window title and any control's Name/displayed text
are untrusted, potentially private runtime data (a title can reflect an
open document's name; a control's text can reflect its content) - see
docs/architecture.md's Milestone 45 privacy discussion. This module never
reads a title as part of resolving identity, and the fixed status strings
`desktop_target_status`/`desktop_control_status` return never include one.

LAUNCH PATH IS NOT RUNTIME PROCESS IDENTITY: empirical validation proved
that a configured application LAUNCH path
(kernel.tools.config.ApplicationSpec.executable, e.g. a venv's
`Scripts\\python.exe`) can differ from the executable image Windows
reports as actually OWNING a live process's window
(`psutil.Process(pid).exe()`, e.g. the venv's base interpreter) - not
because of a symlink or a path-string quirk `os.path.realpath()`/
`Path.resolve()` would fix, but because the launch executable is a
distinct, real, separate file that starts a different one. Launch
authority (`approved_desktop_targets[*].application`, an
approved_applications reference - see kernel/tools/config.py) and runtime
window-process identity (`approved_desktop_targets[*].process_executable`)
are therefore two SEPARATE, both-required config fields; this module never
treats one as a proxy for the other, and `same_windows_executable()` below
proves file identity, never mere path equality, for the runtime check (see
its own docstring for why).

EXACT RESOLUTION / NO FUZZY MATCHING: every resolution here uses
`pywinauto.findwindows.find_elements()` with explicit, exact-equality
property filters (`class_name`, `control_type`, `auto_id`) - the
low-level, complete-enumeration API validation proved sufficient (see
docs/architecture.md). `best_match`, `title`/`title_re`, `ctrl_index`/
`found_index`, and every other pywinauto convenience API that performs
fuzzy/best-match/index-based resolution are never used. Cardinality is
always enforced explicitly by THIS module - never "pick the first" or
"closest match".

COMPLETE AUTHORITY BEFORE CARDINALITY (correction - an adversarial review
of the initial implementation found and reproduced this): the configured
target authority is the FULL combination of (exact UIA window locator) AND
(configured runtime executable identity) AND (usable state - visible, not
minimized). Cardinality must be computed over candidates that satisfy ALL
of that, never over raw UIA-locator matches alone. The initial
implementation computed cardinality on the raw locator match count BEFORE
ever checking process identity or minimized state - reproduced live: two
raw candidates sharing a configured window class, only one of which
belonged to the correct process (or only one of which was not minimized),
were both reported AMBIGUOUS without the process-identity/minimized check
ever running on either one. An unrelated process exposing the same window
class must never be able to suppress a legitimately-available target's
status merely by existing. `_resolve_target_internal()` below evaluates
EVERY raw candidate against the complete authority first (see
_evaluate_target_candidate()), then computes cardinality only over the
candidates that fully qualify.

INSPECTION FAILURE IS NOT ORDINARY ABSENCE (also a correction from the
same review): a raw candidate that could not be COMPLETELY evaluated
against the authority above (e.g. AccessDenied resolving its owning
process, an unexpected Win32/COM failure, a file-identity query that
itself could not be performed) must never be silently treated as
"non-qualifying" - doing so would let an infrastructure failure
masquerade as ordinary target absence ("the application isn't there"),
which is a materially different fact ("the system could not reliably
determine whether it's there"). If EVEN ONE raw candidate cannot be
completely evaluated, the overall result is DesktopStatus.CHECK_FAILED,
regardless of how many other candidates already qualified or didn't -
exact cardinality cannot be proven while any candidate remains unresolved
(see _evaluate_target_candidate()/_resolve_target_internal()'s own
docstrings). A raw candidate whose OWNING PROCESS has cleanly exited
(`psutil.NoSuchProcess`) is treated as ordinary non-qualification (the
window is simply gone), distinct from an unexpected inspection failure -
see PROCESS-DISAPPEARANCE TAXONOMY in _evaluate_target_candidate()'s
docstring.

FRESH RESOLUTION ONLY: every function here performs a complete, fresh
`find_elements()` call against the CURRENT live desktop - nothing here
returns, accepts, or persists a pywinauto wrapper, ElementInfo, HWND, PID,
runtime_id, or COM pointer for reuse by a caller. Empirical validation
proved a stale reference can silently return an empty-but-not-exception
result after its window has closed, so no caller may treat "I already
resolved this" as still true across any two calls.
"""

import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import psutil

from kernel.tools.file_safety import StatClassification, classify_stat_mode

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import win32file
    import win32gui
    from pywinauto import findwindows
else:  # pragma: no cover - exercised only on non-Windows CI/collection
    win32file = None
    win32gui = None
    findwindows = None


# Conservative, code-owned bounds on config-authored desktop-locator
# strings - mirrors kernel/tools/config.py's own MAX_SYMBOLIC_NAME_LENGTH/
# browser_safety.py's MAX_URL_LENGTH precedent: config-authored, never
# request/model-supplied, but still never allowed to be arbitrarily large.
# MAX_WINDOW_CLASS_NAME_LENGTH (256) mirrors the real Win32 constraint
# (RegisterClassEx documents a window class name as at most 256 characters
# including the terminating NUL) - not an arbitrary guess.
MAX_EXECUTABLE_PATH_LENGTH = 480
MAX_WINDOW_CLASS_NAME_LENGTH = 256
MAX_AUTOMATION_ID_LENGTH = 128
MAX_CONTROL_CLASS_NAME_LENGTH = 256

# The UIA ControlType values Milestone 45 P1 is prepared to validate
# against - a closed, code-owned allowlist, not the full set UIA defines.
# Extending this set is a deliberate decision for a later milestone, never
# implicit: an unsupported (but real) UIA ControlType is rejected at
# config-load time exactly like an invalid one, so a config author gets a
# clear, fail-closed error rather than a silently-never-matching control.
SUPPORTED_CONTROL_TYPES = frozenset(
    {
        "Button",
        "CheckBox",
        "RadioButton",
        "ComboBox",
        "Edit",
        "Text",
        "List",
        "ListItem",
        "Tab",
        "TabItem",
        "Group",
        "MenuItem",
        "Pane",
        "Window",
    }
)


def is_supported_control_type(value) -> bool:
    return isinstance(value, str) and value in SUPPORTED_CONTROL_TYPES


class DesktopStatus(Enum):
    """The five fixed, code-owned outcomes desktop_target_status/
    desktop_control_status ever report for a REGISTERED resource - never a
    match count, never raw metadata. See kernel/tools/handlers/
    desktop_target_status.py and desktop_control_status.py for how each
    maps to a fixed ActionResult message.

    AVAILABLE/UNAVAILABLE/AMBIGUOUS all mean the check itself SUCCEEDED -
    the system reliably determined the configured target/control's
    current state, whatever that state is (including "not currently
    present", which is UNAVAILABLE, not a failure).

    CHECK_FAILED means the system could NOT reliably perform the
    configured check (an unexpected Win32/COM/psutil failure, access
    denied, or an invalid/malformed spec that never reached a live UIA
    call at all - see _revalidate_target_spec()/_revalidate_control_spec()).
    This is never conflated with UNAVAILABLE - see module docstring's
    INSPECTION FAILURE IS NOT ORDINARY ABSENCE section.

    AUTOMATION_UNAVAILABLE means the platform/UIA foundation itself is not
    usable - today this is exactly "not running on Windows"; it is a
    structural, not an operational, determination (see WINDOWS at module
    scope) and is checked first, before any live call is attempted."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"
    CHECK_FAILED = "check_failed"
    AUTOMATION_UNAVAILABLE = "automation_unavailable"


class DesktopSafetyError(Exception):
    """Raised by the config-load-time validate_*() functions below, and
    caught internally by _revalidate_target_spec()/_revalidate_control_spec()
    at execution time (see their own docstrings) - never raised by, or
    allowed to escape, resolve_target_status()/resolve_control_status()
    themselves, which always return a DesktopStatus (AVAILABLE/
    UNAVAILABLE/AMBIGUOUS for a successful check, CHECK_FAILED/
    AUTOMATION_UNAVAILABLE for one that could not be performed) rather than
    propagate an exception a handler would need to translate."""


def validate_process_executable_path(value, *, field_name: str) -> str:
    """Config-load-time validation for
    approved_desktop_targets[*].process_executable - an absolute,
    bounded-length, currently-existing regular file that is not itself a
    symlink/reparse point (reusing kernel/tools/file_safety.py's own
    classify_stat_mode() rather than duplicating that logic - the same
    "reject outright, never follow" discipline approved_files already
    established). Windows executable-ness itself (unlike POSIX) has no
    single reliable filesystem bit to check; requiring a `.exe` extension
    would be either redundant (Popen/CreateProcess already need a real
    executable to launch anything) or too narrow (a configured
    process_executable is never itself launched by this module - only
    compared against a live process's own already-resolved image path) -
    so this function validates path shape/existence/kind only, exactly as
    kernel/tools/config.py's own _require_absolute_path()/
    resolve_approved_file() precedent does for other exact file paths.
    Raises DesktopSafetyError on any failure; never returns a partially
    validated value."""

    if not isinstance(value, str) or not value.strip():
        raise DesktopSafetyError(f"{field_name} must be a non-empty string")
    if "\x00" in value:
        raise DesktopSafetyError(f"{field_name} must not contain a NUL character")
    if len(value) > MAX_EXECUTABLE_PATH_LENGTH:
        raise DesktopSafetyError(
            f"{field_name} exceeds the maximum length ({MAX_EXECUTABLE_PATH_LENGTH})"
        )
    path = Path(value)
    if not path.is_absolute():
        raise DesktopSafetyError(f"{field_name} must be an absolute path, got: {value!r}")

    try:
        st = path.lstat()
    except OSError as exc:
        raise DesktopSafetyError(f"{field_name} does not exist: {value!r}") from exc

    classification = classify_stat_mode(st.st_mode, getattr(st, "st_file_attributes", None))
    if classification is StatClassification.SYMLINK_OR_REPARSE_POINT:
        raise DesktopSafetyError(f"{field_name} must not be a symlink or reparse point")
    if classification is not StatClassification.REGULAR_FILE:
        raise DesktopSafetyError(f"{field_name} must be a regular file, not a directory")

    return value


def validate_window_class_name(value, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesktopSafetyError(f"{field_name} must be a non-empty string")
    if "\x00" in value:
        raise DesktopSafetyError(f"{field_name} must not contain a NUL character")
    if len(value) > MAX_WINDOW_CLASS_NAME_LENGTH:
        raise DesktopSafetyError(
            f"{field_name} exceeds the maximum length ({MAX_WINDOW_CLASS_NAME_LENGTH})"
        )
    return value


def validate_automation_id(value, *, field_name: str) -> str:
    """Shared by a target's OPTIONAL window_automation_id and a control's
    REQUIRED control_automation_id - the caller enforces required-ness;
    this function only validates a VALUE that is present, and an explicit
    empty string is always rejected (never treated as 'not configured') -
    see this module's own docstring for why an empty automation_id can
    never be accepted as identity."""

    if not isinstance(value, str) or not value:
        raise DesktopSafetyError(f"{field_name} must be a non-empty string")
    if "\x00" in value:
        raise DesktopSafetyError(f"{field_name} must not contain a NUL character")
    if len(value) > MAX_AUTOMATION_ID_LENGTH:
        raise DesktopSafetyError(
            f"{field_name} exceeds the maximum length ({MAX_AUTOMATION_ID_LENGTH})"
        )
    return value


def validate_control_class_name(value, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DesktopSafetyError(f"{field_name} must be a non-empty string")
    if "\x00" in value:
        raise DesktopSafetyError(f"{field_name} must not contain a NUL character")
    if len(value) > MAX_CONTROL_CLASS_NAME_LENGTH:
        raise DesktopSafetyError(
            f"{field_name} exceeds the maximum length ({MAX_CONTROL_CLASS_NAME_LENGTH})"
        )
    return value


def validate_control_type(value, *, field_name: str) -> str:
    if not is_supported_control_type(value):
        raise DesktopSafetyError(
            f"{field_name} must be one of the supported control types: "
            f"{sorted(SUPPORTED_CONTROL_TYPES)}"
        )
    return value


@dataclass(frozen=True)
class _FileIdentity:
    """Windows' own notion of 'the same file' - a (volume serial number,
    file index high, file index low) triple from
    GetFileInformationByHandle(), robust to symlinks, hardlinks,
    junctions, drive-letter substitution, 8.3 short names, and case
    differences, none of which os.path.realpath()/Path.resolve() reliably
    normalize on this platform (see module docstring's LAUNCH PATH IS NOT
    RUNTIME PROCESS IDENTITY section). A hard link to the same NTFS file
    shares this identity (correct - it is, byte-for-byte, the same file
    under another name, not a substitution vector); a byte-identical COPY
    at a different path does not (a copy has its own, different file
    index) - this is an identity comparison, never a content hash."""

    volume_serial_number: int
    file_index_high: int
    file_index_low: int


class _FileIdentityCheckError(Exception):
    """Internal only - raised by _file_identity_strict() when a file's
    Windows identity could not be determined for ANY reason (missing,
    access denied, or any other OSError/pywintypes.error). Never allowed
    to propagate past compare_windows_executable_identity(), which
    converts it to IdentityComparison.CHECK_FAILED - the whole reason this
    exists separately from returning None/False is so a genuine inspection
    failure is never silently collapsed into 'proven not the same file'
    (see module docstring's INSPECTION FAILURE IS NOT ORDINARY ABSENCE
    section)."""


def _file_identity_strict(path: str) -> _FileIdentity:
    """Open `path` read-only/share-all (query metadata only - zero bytes
    of the file's own content are ever read, no write/delete access
    requested) and return its Windows file identity. Raises
    _FileIdentityCheckError (never returns a sentinel) if the file cannot
    be opened for ANY reason - callers must treat that as 'identity could
    not be determined', never as proof of a mismatch."""

    handle = None
    try:
        handle = win32file.CreateFile(
            path,
            0,  # query metadata only - no read or write access requested
            win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE | win32file.FILE_SHARE_DELETE,
            None,
            win32file.OPEN_EXISTING,
            0,
            None,
        )
        info = win32file.GetFileInformationByHandle(handle)
    except Exception as exc:
        raise _FileIdentityCheckError(str(exc)) from exc
    finally:
        if handle is not None:
            try:
                handle.Close()
            except Exception:
                pass

    return _FileIdentity(
        volume_serial_number=info[4],
        file_index_high=info[8],
        file_index_low=info[9],
    )


class IdentityComparison(Enum):
    """The three possible outcomes of comparing two Windows executable
    paths' file identity - deliberately NOT a bool, so a caller can never
    accidentally treat 'could not be checked' as 'proven different' (see
    module docstring's INSPECTION FAILURE IS NOT ORDINARY ABSENCE
    section)."""

    MATCH = "match"
    NO_MATCH = "no_match"
    CHECK_FAILED = "check_failed"


def compare_windows_executable_identity(
    configured_path: str, runtime_path: str
) -> IdentityComparison:
    """Strict, three-way comparison of `configured_path` (approved_
    desktop_targets[*].process_executable) and `runtime_path`
    (psutil.Process(pid).exe(), a RUNTIME OBSERVATION - never persisted as
    authority anywhere), proven via Windows file identity - NOT raw string
    equality, and NOT merely os.path.realpath()/Path.resolve() (neither
    reliably resolves a launcher/redirector's real target on this platform
    - a venv's Scripts/python.exe and its base interpreter are two
    distinct, real files; a launcher and what it starts must compare as
    NO_MATCH here, which is the whole point - see module docstring).
    Returns CHECK_FAILED (never NO_MATCH) if either path's identity could
    not be determined - an inability to verify identity must never be
    reported as a proven mismatch. This is the primitive
    _evaluate_target_candidate() uses; same_windows_executable() below is
    a convenience boolean wrapper for callers that only need yes/no."""

    try:
        a = _file_identity_strict(configured_path)
        b = _file_identity_strict(runtime_path)
    except _FileIdentityCheckError:
        return IdentityComparison.CHECK_FAILED
    return IdentityComparison.MATCH if a == b else IdentityComparison.NO_MATCH


def same_windows_executable(configured_path: str, runtime_path: str) -> bool:
    """Convenience boolean wrapper around compare_windows_executable_identity()
    for callers (tests, and any future simple caller) that only need a
    yes/no answer. NOTE: this collapses CHECK_FAILED into False, exactly
    like MATCH/NO_MATCH would be expected to - callers that must
    distinguish 'proven different' from 'could not be checked' (i.e. this
    module's own candidate evaluation, kernel/tools/handlers/
    desktop_target_status.py and desktop_control_status.py's error
    semantics) use compare_windows_executable_identity() directly instead,
    never this function."""

    return compare_windows_executable_identity(configured_path, runtime_path) is IdentityComparison.MATCH


def target_application_reference_valid(target_spec, tools_config) -> bool:
    """True only if `target_spec.application_key` still resolves against
    the CURRENT tools_config.approved_applications - re-checked at
    execution time, never assumed from config-load-time referential-
    integrity validation alone (kernel/tools/config.py's
    _parse_approved_desktop_targets() already checked this once, against
    whatever ToolsConfig was being built at that moment; a *different*,
    since-reloaded ToolsConfig at execution time is not guaranteed to
    still satisfy it). Callers must treat False exactly like "target not
    registered" - fail before any UIA call, mirroring
    kernel.task_execution.eligibility's own "current config is
    authoritative" discipline applied one layer below it, for the same
    reason resolve_control_status() re-resolves its target fresh rather
    than trusting a caller-held reference."""

    return target_spec.application_key in tools_config.approved_applications


def _find_exact_top_level_windows(class_name: str, automation_id: str | None):
    """Complete enumeration of currently-visible top-level windows with
    this EXACT class_name (and, if given, this exact automation_id) - the
    low-level, explicit-property pywinauto API validation proved
    sufficient (findwindows.find_elements(), never best_match/title/
    ctrl_index/found_index). visible_only=True (the default) already
    excludes a withdrawn/hidden window - validation proved a hidden Tk
    window is not enumerable through this path even with
    visible_only=False, so hidden targets fail closed here with no special
    case needed. Minimized-but-visible windows ARE still returned here (a
    minimized window is not "hidden" in Win32 terms) - see
    resolve_target_status() for the separate, explicit minimized check."""

    kwargs = {
        "class_name": class_name,
        "backend": "uia",
        "top_level_only": True,
        "visible_only": True,
    }
    if automation_id:
        kwargs["auto_id"] = automation_id
    return findwindows.find_elements(**kwargs)


def _revalidate_target_spec(spec) -> bool:
    """Defense-in-depth re-validation of a DesktopTargetSpec's locator
    fields, run again at EXECUTION time using the exact same validation
    kernel/tools/config.py already runs at config-load time - never trusts
    that a caller-supplied spec necessarily passed through
    load_tools_config() (a manually-constructed ToolsConfig - most
    plausible in a test, but not assumed impossible in production - could
    bypass it entirely). Returns False for any malformed field; every
    caller must fail CHECK_FAILED BEFORE find_elements() is ever called,
    never treat a malformed spec as an ordinary absence (see module
    docstring)."""

    try:
        validate_process_executable_path(spec.process_executable, field_name="process_executable")
        validate_window_class_name(spec.window_class_name, field_name="window_class_name")
        if spec.window_automation_id is not None:
            validate_automation_id(spec.window_automation_id, field_name="window_automation_id")
    except DesktopSafetyError:
        return False
    return True


def _revalidate_control_spec(spec) -> bool:
    """Control-side equivalent of _revalidate_target_spec() - see that
    function's own docstring. In particular closes the empty/invalid
    control_automation_id gap an adversarial review found: a
    manually-constructed DesktopControlSpec with control_automation_id=""
    would otherwise reach find_elements(auto_id="") directly, which was
    proven (live, against a real fixture) to match real controls that
    genuinely have an empty AutomationId - never reachable now, since
    validate_automation_id() rejects empty/oversized/malformed values here
    before any live call."""

    try:
        validate_automation_id(spec.control_automation_id, field_name="control_automation_id")
        validate_control_type(spec.control_type, field_name="control_type")
        if spec.control_class_name is not None:
            validate_control_class_name(spec.control_class_name, field_name="control_class_name")
    except DesktopSafetyError:
        return False
    return True


class _CandidateOutcome(Enum):
    """What _evaluate_target_candidate() decided about ONE raw UIA
    candidate - never exposed outside this module."""

    QUALIFIED = "qualified"
    NON_QUALIFYING = "non_qualifying"
    INSPECTION_FAILED = "inspection_failed"


def _evaluate_target_candidate(element, spec) -> _CandidateOutcome:
    """Evaluate ONE raw UIA locator match against the COMPLETE configured
    target authority: usable state (not minimized) AND owning-process
    executable identity. Never checks visibility itself - the raw
    candidate set already came from a visible_only=True enumeration (see
    _find_exact_top_level_windows()).

    PROCESS-DISAPPEARANCE TAXONOMY: `psutil.NoSuchProcess` (raised either
    constructing psutil.Process(pid) or calling .exe() on it) is treated
    as ordinary non-qualification - the window's owning process has
    cleanly exited, so the candidate simply no longer qualifies, exactly
    like a wrong-process or minimized candidate. Every OTHER exception
    (AccessDenied, an unexpected Win32/COM failure reading `.handle`/
    `.process_id`, win32gui.IsIconic() raising, or
    compare_windows_executable_identity() reporting CHECK_FAILED) is
    INSPECTION_FAILED - the system could not determine whether this
    candidate qualifies, which is a different fact from determining that
    it does not (see module docstring's INSPECTION FAILURE IS NOT ORDINARY
    ABSENCE section). This function never lets a bare `except Exception`
    silently map everything to one outcome - each step is its own
    narrowly-scoped try/except."""

    try:
        hwnd = element.handle
        pid = element.process_id
    except Exception:
        return _CandidateOutcome.INSPECTION_FAILED

    try:
        if win32gui.IsIconic(hwnd):
            return _CandidateOutcome.NON_QUALIFYING
    except Exception:
        return _CandidateOutcome.INSPECTION_FAILED

    try:
        runtime_exe = psutil.Process(pid).exe()
    except psutil.NoSuchProcess:
        return _CandidateOutcome.NON_QUALIFYING
    except Exception:
        return _CandidateOutcome.INSPECTION_FAILED

    comparison = compare_windows_executable_identity(spec.process_executable, runtime_exe)
    if comparison is IdentityComparison.CHECK_FAILED:
        return _CandidateOutcome.INSPECTION_FAILED
    if comparison is IdentityComparison.NO_MATCH:
        return _CandidateOutcome.NON_QUALIFYING
    return _CandidateOutcome.QUALIFIED


def _resolve_target_internal(spec) -> tuple[DesktopStatus, object | None]:
    """The one shared target-resolution implementation
    resolve_target_status() and resolve_control_status() both build on -
    never duplicated between them (a prior version of this module had two
    subtly-drifting copies of this same logic; an adversarial review
    proved the drift was real). Returns (status, unique_element) -
    `unique_element` is non-None ONLY when status is AVAILABLE, is the
    ephemeral pywinauto element for that one, already-fully-qualified
    candidate, and MUST NEVER be persisted, cached, or returned outside
    this module - resolve_target_status() below discards it immediately;
    resolve_control_status() uses it only as `parent=` for one further,
    immediate find_elements() call within the same call stack (see module
    docstring's FRESH RESOLUTION ONLY section).

    Every raw candidate is evaluated - this function never short-circuits
    on the first qualifying or non-qualifying candidate, so a later
    INSPECTION_FAILED candidate is never silently skipped merely because
    an earlier one already qualified (see module docstring's COMPLETE
    AUTHORITY BEFORE CARDINALITY / INSPECTION FAILURE IS NOT ORDINARY
    ABSENCE sections, and _evaluate_target_candidate()'s own docstring for
    the qualification rules). If ANY raw candidate could not be completely
    evaluated, the overall result is CHECK_FAILED unconditionally -
    regardless of how many other candidates already qualified - because
    exact cardinality cannot be proven while any candidate remains
    unresolved (the unresolved one might also have qualified)."""

    if not WINDOWS:
        return DesktopStatus.AUTOMATION_UNAVAILABLE, None

    if not _revalidate_target_spec(spec):
        return DesktopStatus.CHECK_FAILED, None

    try:
        raw_candidates = _find_exact_top_level_windows(
            spec.window_class_name, spec.window_automation_id
        )
    except Exception:
        return DesktopStatus.CHECK_FAILED, None

    qualified_elements = []
    any_inspection_failed = False
    for element in raw_candidates:
        outcome = _evaluate_target_candidate(element, spec)
        if outcome is _CandidateOutcome.INSPECTION_FAILED:
            any_inspection_failed = True
        elif outcome is _CandidateOutcome.QUALIFIED:
            qualified_elements.append(element)

    if any_inspection_failed:
        return DesktopStatus.CHECK_FAILED, None
    if len(qualified_elements) == 0:
        return DesktopStatus.UNAVAILABLE, None
    if len(qualified_elements) > 1:
        return DesktopStatus.AMBIGUOUS, None
    return DesktopStatus.AVAILABLE, qualified_elements[0]


def resolve_target_status(spec, tools_config=None) -> DesktopStatus:
    """Resolve one already-looked-up DesktopTargetSpec against the CURRENT
    live desktop. Always returns a DesktopStatus - never raises, and never
    persists anything about what it found. `tools_config` is unused and
    accepted only so callers can pass it uniformly with
    resolve_control_status() below; kept for a possible future need (e.g.
    a resource-key cross-check) rather than introduced speculatively as a
    dependency this function does not yet use for anything. See
    _resolve_target_internal() for the full algorithm - this function
    exists only to discard the ephemeral element that helper returns."""

    status, _element = _resolve_target_internal(spec)
    return status


def resolve_control_status(target_spec, control_spec) -> DesktopStatus:
    """Resolve one already-looked-up DesktopControlSpec, scoped to a FRESH
    resolution of its target via _resolve_target_internal() (never a
    caller-supplied/cached window reference - see module docstring's FRESH
    RESOLUTION ONLY section).

    TARGET STATUS PROPAGATES UNCHANGED unless the target is AVAILABLE: if
    the target is AMBIGUOUS, UNAVAILABLE, CHECK_FAILED, or
    AUTOMATION_UNAVAILABLE, that EXACT status is returned for the control
    too - a control scoped to an ambiguous target is itself ambiguous (it
    cannot be uniquely resolved, for a documented reason), not merely
    "unavailable"; a control whose target could not be checked has itself
    not been checked, not merely "not found". Only an AVAILABLE target
    (exactly one, fully-qualified, live window) proceeds to control-level
    enumeration, which reuses the exact same three-property exact-match
    query this module always has (automation_id + control_type + optional
    class_name, evaluated together in one find_elements() call, so
    control-level cardinality is never subject to the same raw-locator-vs-
    complete-authority bug target resolution had - there is no second,
    separate "usability" check for a control the way there is for a
    window)."""

    target_status, target_element = _resolve_target_internal(target_spec)
    if target_status is not DesktopStatus.AVAILABLE:
        return target_status

    if not _revalidate_control_spec(control_spec):
        return DesktopStatus.CHECK_FAILED

    try:
        kwargs = {
            "parent": target_element,
            "auto_id": control_spec.control_automation_id,
            "control_type": control_spec.control_type,
            "backend": "uia",
            "top_level_only": False,
            "visible_only": True,
        }
        if control_spec.control_class_name:
            kwargs["class_name"] = control_spec.control_class_name
        control_matches = findwindows.find_elements(**kwargs)
    except Exception:
        return DesktopStatus.CHECK_FAILED

    if len(control_matches) == 0:
        return DesktopStatus.UNAVAILABLE
    if len(control_matches) > 1:
        return DesktopStatus.AMBIGUOUS
    return DesktopStatus.AVAILABLE
