"""
list_processes action handler (Milestone 43 P1): a bounded, read-only
snapshot of currently-running processes - PID, process name, and status
only. Needs no local configuration (like system_status) and takes no
resource_key at all - there is nothing here for a caller/model to select
or narrow; a resource_key is rejected outright (see run() below) rather
than silently ignored, so this action's declared no-resource contract
cannot be violated even by a direct SafeTaskExecutor caller that bypasses
kernel.task_execution.eligibility.revalidate_action()'s own, separate
enforcement of the same contract.

Uses psutil (already an existing project dependency - see
kernel/tools/handlers/system_status.py and kernel/tools/process_control.py)
purely for read-only process enumeration - never to send a signal, open a
handle for anything beyond the three safe attributes below, or shell out
to tasklist/PowerShell.

Deliberately excludes command-line arguments, environment variables,
working directories, open files, network connections, memory contents,
and username - none of that is needed to answer "is X running?" or "what
is currently running?", and all of it is either potentially sensitive or
simply out of scope for this bounded snapshot.

Each process is inspected independently and defensively: a process that
disappears mid-enumeration, or one this process lacks permission to
inspect, is silently skipped rather than raising a raw psutil/OS
exception through this handler - matching kernel/tools/executor.py's own
"never surface a raw exception" discipline one layer up.

OUTPUT-SIZE CONTRACT (Milestone 43 P1 correction): MAX_PROCESSES alone (a
plain item-COUNT ceiling) does not bound the serialized size of the
resulting kernel.task_execution.observation.StepObservation - one hundred
long process names can produce a raw ActionResult.message, and therefore a
serialized observation, far past
kernel.employee_tasks.MAX_STEP_RESULT_JSON_CHARS (4,096); reproduced
directly against the real build_action_observation()/serialize_observation()
pipeline before this correction (see
tests/kernel/tools/handlers/test_list_processes.py). Two further,
independent bounds close this:

  1. MAX_PROCESS_NAME_CHARS/MAX_PROCESS_STATUS_CHARS - a process whose
     reported name OR status exceeds the relevant bound is SKIPPED
     entirely, never truncated - showing a truncated identifier could be
     mistaken for a different, shorter-named process, which is worse than
     omitting it (see this module's own "avoid misleading partial names"
     doctrine). A name or status containing any control character, a
     double quote, or a backslash is skipped for the same reason -
     legitimate Windows executable names can never contain either a
     double quote or a backslash (both are invalid NTFS filename
     characters), and psutil's own status values are always one of a
     small, fixed set of plain-ASCII words - so this costs nothing in
     practice for either field and, by construction, guarantees a shown
     entry can never trigger JSON-string-escaping expansion at all once
     serialized, as a structural property rather than an assumption
     resting on psutil's current behavior.
  2. MAX_RESULT_CHARS - a code-owned bound on the total raw
     ActionResult.message. Entries are added, in the same deterministic
     (casefolded-name, then PID) order every other part of this handler
     already uses, until the NEXT entry would exceed the remaining
     budget - at which point addition stops deterministically and a
     fixed, bounded "(showing N of TOTAL)" note is appended instead of
     ever emitting a serialized result that could exceed the M42
     persistence bound. This is a graceful size-based fallback on top of
     (never a replacement for) the pre-existing MAX_PROCESSES count
     ceiling.

Both bounds are sized (see the constants' own comments below) so that the
worst case this handler can ever actually produce - now that every shown
name is guaranteed free of JSON-escaping-expensive characters - still
serializes comfortably under MAX_STEP_RESULT_JSON_CHARS, proven directly
against the real observation pipeline in this module's test suite rather
than an estimated overhead formula.
"""

import re

import psutil

from kernel.tools.types import ActionRequest, ActionResult

MAX_PROCESSES = 100

# A process name this long, or containing a control character/quote/
# backslash, is skipped outright rather than truncated - see module
# docstring's OUTPUT-SIZE CONTRACT section. 100 characters is already far
# beyond any real Windows executable base name.
MAX_PROCESS_NAME_CHARS = 100

# A process status this long, or containing a control character/quote/
# backslash, is skipped outright (the whole entry, never just the status
# field replaced or truncated) - the same "skip, never sanitize into a
# misleading value" policy MAX_PROCESS_NAME_CHARS already applies to name.
# psutil.Process.status() only ever returns one of its own small, fixed
# internal STATUS_* constants ("running", "sleeping", "disk-sleep",
# "stopped", "tracing-stop", "zombie", "dead", "wake-kernel", "waking",
# "idle", "locked", "waiting", "suspended", "parked", ...) - the longest of
# which is 12 characters - so this bound is never expected to reject a real
# status. It exists so the module's own "every shown field is guaranteed
# JSON-escape-free, so the size budget maps to serialized size at
# essentially 1:1" claim (see MAX_RESULT_CHARS below) is a structural
# property of this handler, never merely an assumption resting on psutil's
# current behavior never changing.
MAX_PROCESS_STATUS_CHARS = 32

# Shared between name and status - both are fixed-shape identifiers this
# handler never truncates or sanitizes, only accepts verbatim or skips
# entirely (see the two constants above).
_DISALLOWED_CHARS_RE = re.compile(r'[\x00-\x1f\x7f"\\]')

# The total raw ActionResult.message budget - see module docstring. Sized
# (and proven, in tests/kernel/tools/handlers/test_list_processes.py,
# directly against the real build_action_observation()/
# serialize_observation() pipeline) so that even MAX_PROCESSES entries at
# MAX_PROCESS_NAME_CHARS each - which would exceed this budget and so be
# gracefully cut off deterministically before that point - never produce a
# serialized StepObservation anywhere near MAX_STEP_RESULT_JSON_CHARS
# (4,096). Every character a shown entry can contain is guaranteed
# JSON-escape-free (see MAX_PROCESS_NAME_CHARS/MAX_PROCESS_STATUS_CHARS
# above and the fixed literal line template below, which contains no
# quote/backslash characters of its own), so this budget maps to
# serialized size at essentially 1:1, not the 2x this module would
# otherwise need to assume.
MAX_RESULT_CHARS = 3000

# A conservative, fixed upper bound on the header/trailing-note line's own
# length, reserved out of MAX_RESULT_CHARS before any process line is
# added - see the "header reserve" test proving this holds even for an
# absurdly large fake total count.
_HEADER_RESERVE_CHARS = 200


def _snapshot() -> list[tuple[int, str, str]]:
    """Every currently-inspectable, safely-displayable (pid, name, status)
    triple - never raises: a process that disappears mid-enumeration or
    cannot be inspected (NoSuchProcess/AccessDenied/ZombieProcess) is
    simply skipped, a process reporting a missing/empty name is skipped
    too rather than shown as a blank entry, and a name that is too long or
    contains a control/quote/backslash character is skipped rather than
    truncated or sanitized (see module docstring). A missing/empty status
    is normalized to "unknown" (the entry is still shown - the pid/name are
    still valid and displayable); a NON-empty status that is too long or
    contains a control/quote/backslash character causes the WHOLE entry to
    be skipped, exactly like an unsafe name - never truncated, sanitized,
    or silently replaced with "unknown" (which would misrepresent a real,
    reported-but-unsafe status as if psutil had reported nothing at all)."""

    entries: list[tuple[int, str, str]] = []
    for proc in psutil.process_iter():
        try:
            pid = proc.pid
            name = proc.name()
            status = proc.status()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        if not isinstance(pid, int):
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        if len(name) > MAX_PROCESS_NAME_CHARS:
            continue
        if _DISALLOWED_CHARS_RE.search(name):
            continue

        if not isinstance(status, str) or not status.strip():
            status = "unknown"
        elif len(status) > MAX_PROCESS_STATUS_CHARS or _DISALLOWED_CHARS_RE.search(status):
            continue

        entries.append((pid, name, status))

    return entries


def run(request: ActionRequest, tools_config) -> ActionResult:
    if request.resource_key is not None:
        return ActionResult(False, "list_processes does not take a resource.", "rejected")

    entries = _snapshot()

    if not entries:
        return ActionResult(True, "No processes found.", "executed")

    # Deterministic ordering: normalized (casefolded) process name, then
    # PID as the tie-break - never enumeration/OS-scheduler order, which
    # is neither stable nor reproducible across calls or test runs.
    entries.sort(key=lambda entry: (entry[1].casefold(), entry[0]))

    total_found = len(entries)
    count_capped = entries[:MAX_PROCESSES]

    lines: list[str] = []
    running_len = 0
    budget_for_lines = MAX_RESULT_CHARS - _HEADER_RESERVE_CHARS
    for pid, name, status in count_capped:
        line = f"{pid} {name} ({status})"
        added_len = len(line) + 1  # +1 for the joining newline
        if running_len + added_len > budget_for_lines:
            break
        lines.append(line)
        running_len += added_len

    shown_count = len(lines)
    truncated = shown_count < total_found

    header = f"{shown_count} process(es)"
    if truncated:
        header += f" (showing {shown_count} of {total_found})"

    return ActionResult(True, "\n".join([header, *lines]), "executed")
