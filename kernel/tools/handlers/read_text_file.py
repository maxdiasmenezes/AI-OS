"""
read_text_file action handler (Milestone 43 P1): a bounded, exact read of
one individually registered file (ToolsConfig.approved_files) - never a
directory allowlist, and never a caller/model-supplied filename, wildcard,
relative path, or environment-variable expansion appended to an approved
root. resource_key is the ONLY input this handler ever accepts; the exact
file it names is decided entirely by configuration (kernel/config/
tools.yaml), never by request text, description, or expected_result. See
kernel/tools/file_safety.py's module docstring for the shared resolution/
validation logic this and file_metadata.py both use, and its residual-
TOCTOU-limitation discussion, which applies here too.

TEXT POLICY (Milestone 43 P1, deliberately narrow and fully deterministic):
  - UTF-8 strict decoding only - never errors="ignore"/"replace" and never
    encoding auto-detection. A file that is not valid UTF-8 fails closed
    with a fixed, generic message; the raw decoder exception is never
    logged or relayed.
  - A NUL byte, or any other C0 control byte other than the three ordinary
    text whitespace bytes (tab, newline, carriage return), anywhere in the
    raw content is treated as binary-like and rejected outright (see
    _DISALLOWED_CONTROL_BYTES_RE below) - even though most of these are
    technically valid (if highly unusual) UTF-8 codepoints. This is not
    only a content-policy choice: Python's json.dumps() (used by
    kernel.task_execution.observation.serialize_observation()) escapes any
    other C0 control byte as a six-character unicode escape sequence,
    rather than the two-character escape ordinary text, a backslash, or a
    quote character gets - a file within MAX_TEXT_FILE_BYTES built from
    such bytes could otherwise serialize to up to six times its raw size
    once wrapped in a StepObservation, silently exceeding
    kernel.employee_tasks.MAX_STEP_RESULT_JSON_CHARS (4,096) even though it
    passed this handler's own size bound - reproduced directly against
    build_action_observation()/serialize_observation() before this policy
    existed; see tests/kernel/tools/handlers/test_read_text_file.py's
    dedicated regression. Rejecting these bytes outright keeps the
    worst-case per-character JSON expansion at 2x (a quote, backslash,
    tab, newline, or carriage return), which MAX_TEXT_FILE_BYTES is sized
    against below.
  - A directory, or anything that is not currently a regular,
    non-symlink/non-reparse-point file, is rejected by
    resolve_approved_file() before this handler ever attempts to read
    anything.

SIZE BOUND: MAX_TEXT_FILE_BYTES is checked twice - once cheaply against the
already-known file size resolve_approved_file() already captured (no extra
stat call, and avoids ever opening a file already known to be oversized),
and again authoritatively via a BOUNDED read (at most
MAX_TEXT_FILE_BYTES + 1 bytes are ever actually read from disk, mirroring
kernel/tools/process_control.py's own bounded-capture discipline) so a
file that grew past the bound between the two checks (a TOCTOU race) can
never be loaded into memory in full either. There is no truncation: an
oversized file fails closed with a fixed, generic message - never a
partial read presented as complete.
"""

import re

from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH
from kernel.tools.file_safety import FileResourceError, resolve_approved_file
from kernel.tools.types import ActionRequest, ActionResult

# Sized so that the WORST-CASE serialized kernel.task_execution.observation.
# StepObservation - GIVEN the control-byte policy above, which caps every
# character's own JSON-escaping expansion at 2x (a quote, backslash, tab,
# newline, or carriage return; disallowed control bytes that would expand
# up to 6x are rejected before this bound is even relevant) - still fits
# comfortably within MAX_STEP_RESULT_JSON_CHARS (4,096) with several
# hundred characters of margin to spare, measured directly against the
# real observation.serialize_observation() shape, not picked arbitrarily;
# see tests/kernel/tools/handlers/test_read_text_file.py for the
# corroborating measurement, including the worst-case-quotes regression.
# Enforced against BYTE size (never decoded-character count), which for
# UTF-8 is always >= the decoded character count, so a file passing this
# check is guaranteed to also satisfy the same margin once decoded.
MAX_TEXT_FILE_BYTES = 1500

# Every C0 control byte (0x00-0x1F) except tab/newline/carriage-return,
# plus DEL (0x7F) - see the TEXT POLICY section of this module's docstring
# for why this is enforced (JSON-escaping cost, not just "looks binary").
_DISALLOWED_CONTROL_BYTES_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_TOO_LARGE_MESSAGE = "That file is too large to read."
_NOT_TEXT_MESSAGE = "That file's content could not be read as text."
_UNAVAILABLE_MESSAGE = "That file is not available."
# Same message/outcome resolve_approved_file() already returns for a key
# that is simply absent from approved_files - deliberately indistinguishable
# from an ordinary "not registered" rejection. Defense in depth against a
# ToolsConfig ever built without going through load_tools_config() (which
# already bounds every approved_files key - see kernel/tools/config.py's
# MAX_SYMBOLIC_NAME_LENGTH docstring): this handler echoes resolved.key
# verbatim into a successful ActionResult.message, which must always fit
# MAX_STEP_RESULT_JSON_CHARS.
_UNREGISTERED = ActionResult(False, "That file is not registered.", "rejected")


def run(request: ActionRequest, tools_config) -> ActionResult:
    if request.resource_key is not None and len(request.resource_key) > MAX_SYMBOLIC_NAME_LENGTH:
        return _UNREGISTERED

    try:
        resolved = resolve_approved_file(request.resource_key, tools_config)
    except FileResourceError as exc:
        return ActionResult(False, exc.message, exc.outcome)

    if resolved.size_bytes > MAX_TEXT_FILE_BYTES:
        return ActionResult(False, _TOO_LARGE_MESSAGE, "rejected")

    try:
        with resolved.path.open("rb") as f:
            raw = f.read(MAX_TEXT_FILE_BYTES + 1)
    except OSError:
        return ActionResult(False, _UNAVAILABLE_MESSAGE, "failed")

    if len(raw) > MAX_TEXT_FILE_BYTES:
        # The file grew past the bound between resolve_approved_file()'s
        # lstat() and this read (or the pre-check above was skipped by a
        # race) - fail closed rather than silently reading a truncated
        # prefix and presenting it as the whole file.
        return ActionResult(False, _TOO_LARGE_MESSAGE, "rejected")

    if _DISALLOWED_CONTROL_BYTES_RE.search(raw):
        return ActionResult(False, _NOT_TEXT_MESSAGE, "rejected")

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return ActionResult(False, _NOT_TEXT_MESSAGE, "rejected")

    message = f"File: '{resolved.key}'\nContent:\n{text}"
    return ActionResult(True, message, "executed")
