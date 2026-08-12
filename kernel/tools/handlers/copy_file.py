"""
copy_file action handler (Milestone 43 P2): a bounded, exact, no-clobber
copy of one pre-authorized composite operation
(ToolsConfig.approved_copies). resource_key selects the ENTIRE operation -
a (source file key, destination directory key, destination name) triple
fixed by configuration - nothing here accepts a caller/model-supplied
source, destination, filename, or overwrite flag. No path is ever derived
from request text, description, expected_result, or model output; the
only input this handler ever reads is request.resource_key.

MECHANICS deliberately reuse kernel/tools/handlers/repository_backup.py's
established pattern rather than introducing shutil (no precedent for it
anywhere in kernel/tools/, and it offers neither exclusive-create/
no-clobber semantics nor a byte cap without being wrapped anyway):

  1. Resolve the source through kernel/tools/file_safety.py's
     resolve_approved_file() - the exact same symlink/reparse-point/
     regular-file check file_metadata.py and read_text_file.py already
     rely on.
  2. Resolve the destination directory through file_safety.py's
     resolve_approved_directory() (Milestone 43 P2).
  3. Reject a source larger than MAX_COPY_SIZE_BYTES BEFORE any
     destination-side work begins (see that constant's own docstring).
  4. Pre-check the exact destination path for anything already there
     (including a dangling symlink) - a fast, clean rejection, though not
     by itself race-free; see step 7.
  5. Create a temporary file with os.O_CREAT | os.O_EXCL directly inside
     the destination directory (required: kernel/tools/atomic_finalize.py
     only guarantees same-directory atomicity), then stream the source
     into it in bounded chunks - the source is never read into memory in
     full, and neither is the destination.
  6. Re-check the source's identity (device, inode, size, mtime_ns)
     immediately before finalizing; any change since the pre-copy
     baseline aborts without publishing anything at the final name.
  7. Finalize with kernel/tools/atomic_finalize.py's
     atomic_finalize_no_replace() - the same no-clobber primitive
     repository_backup.py already relies on, and the ACTUAL no-clobber
     authority (never the step-4 pre-check alone).

No content transformation of any kind - binary mode throughout.
"""

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from kernel.tools.atomic_finalize import FinalizeCollisionError, atomic_finalize_no_replace
from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH, is_valid_child_name
from kernel.tools.file_safety import (
    FileResourceError,
    path_exists_including_dangling_links,
    resolve_approved_directory,
    resolve_approved_file,
)
from kernel.tools.types import ActionRequest, ActionResult

# Bounds a single copy's disk I/O and execution duration - NOT
# ActionResult.message/StepObservation size, which stays a handful of
# fixed-shape lines regardless of file size (see run()'s own success
# message, which never grows with the copied content). This is a
# resource/time bound only, matching run_registered_script.py's own
# per-action timeout in spirit: on a personal, single-threaded workstation
# where kernel/task_execution/ processes one plan step at a time
# synchronously, an unbounded copy would block the whole execution loop
# for however long the underlying storage takes. 256 MiB is comfortably
# enough for real documents, spreadsheets, PDFs, and small archives - the
# realistic use case for a pre-authorized personal-file copy - while even
# on conservatively slow local storage (tens of MB/s) it bounds one
# action's blocking duration to at most a few seconds.
MAX_COPY_SIZE_BYTES = 256 * 1024 * 1024

# Matches kernel/tools/handlers/repository_backup.py's own HASH_CHUNK_SIZE -
# large enough to be efficient, small enough that memory use never scales
# with file size.
_COPY_CHUNK_SIZE = 1_048_576

_TEMP_SUFFIX = ".copytmp"
RANDOM_SUFFIX_BYTES = 8
MAX_NAME_ATTEMPTS = 5

# Only meaningful on Windows (binary mode, no CRLF translation) - a no-op
# 0 elsewhere, matching repository_backup.py's own _O_BINARY.
_O_BINARY = getattr(os, "O_BINARY", 0)

_UNKNOWN_OPERATION = ActionResult(False, "That copy operation is not registered.", "rejected")
_SOURCE_TOO_LARGE = ActionResult(False, "That file is too large to copy.", "rejected")
_SOURCE_UNAVAILABLE = ActionResult(False, "The source file is not available.", "failed")
_DESTINATION_UNAVAILABLE = ActionResult(
    False, "The destination directory is not available.", "failed"
)
# Deliberately the SAME message/outcome for both the step-4 pre-check and
# a step-7 finalize-time collision - so a caller can never distinguish,
# from this handler's output alone, which of the two actually caught it.
_DESTINATION_EXISTS = ActionResult(False, "The destination already exists.", "failed")
_SOURCE_CHANGED = ActionResult(False, "The source file changed during the copy.", "failed")
_COPY_FAILED = ActionResult(False, "The file could not be copied.", "failed")


@dataclass(frozen=True)
class _SourceIdentity:
    dev: int
    ino: int
    size: int
    mtime_ns: int


def _capture_source_identity(path: Path) -> "_SourceIdentity | None":
    try:
        st = path.lstat()
    except OSError:
        return None
    return _SourceIdentity(dev=st.st_dev, ino=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns)


def _generate_temp_name(key: str) -> str:
    # The key is already a validated symbolic identifier (casefolded,
    # config-load validated); the random suffix is the sole source of
    # uniqueness - never model/request text of any kind.
    suffix = secrets.token_hex(RANDOM_SUFFIX_BYTES)
    return f".{key}-{suffix}{_TEMP_SUFFIX}"


def _create_temp_file(destination_dir: Path, key: str) -> "tuple[Path, int] | None":
    """Exclusively creates a brand-new temp file directly in
    destination_dir. On a name collision, retries with an entirely fresh
    random suffix, bounded by MAX_NAME_ATTEMPTS - a previously-collided
    suffix is never reused."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY
    for _ in range(MAX_NAME_ATTEMPTS):
        temp_path = destination_dir / _generate_temp_name(key)
        try:
            fd = os.open(temp_path, flags, 0o600)
        except FileExistsError:
            continue
        except OSError:
            return None
        return temp_path, fd
    return None


def _cleanup_temp(path: Path) -> None:
    """Best-effort removal of exactly the one temp file this invocation
    created - never a glob, never a directory sweep, never touches any
    other file. A cleanup failure is never raised or logged with detail."""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _stream_copy(source_path: Path, fd: int, max_bytes: int) -> "int | None":
    """Streams source_path into the already-open fd in bounded chunks -
    neither file is ever held fully in memory. Returns the byte count
    copied, or None on any failure, including exceeding max_bytes
    mid-copy (independently re-enforced here even though run() already
    checked the source's known size before calling this, mirroring
    read_text_file.py's own belt-and-suspenders discipline against a
    source that grew since that check).

    NEVER raises - every failure, including one from flush()/fsync()/
    close() itself (e.g. a deferred write error surfaced only at close
    time, possible on a full disk), is reported through the return value,
    never as an escaping exception. This matters beyond tidiness: a Python
    `finally` block's own exception silently REPLACES a pending `return`
    from the `try` block above it - an unguarded close() failure here
    would discard an already-decided return None/return total and
    propagate uncaught out of this function, past run()'s own
    `if copied_bytes is None: _cleanup_temp(...)` check entirely, leaking
    the owned temp file. Guarding close() the same way flush()/fsync()
    already are closes that gap: run() always gets a clean, well-formed
    answer and can always attempt its own cleanup."""

    file_obj = os.fdopen(fd, "wb")
    total = 0
    failed = False
    try:
        with open(source_path, "rb") as src:
            while True:
                chunk = src.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    failed = True
                    break
                file_obj.write(chunk)
    except OSError:
        failed = True
    finally:
        # Two SEPARATE try/except blocks, deliberately - flush()/fsync()
        # failing must never skip the close() attempt below (a leaked
        # open handle would be worse than a failed flush), and close()
        # failing must never mask a flush()/fsync() failure that already
        # happened.
        try:
            file_obj.flush()
            os.fsync(file_obj.fileno())
        except OSError:
            failed = True
        try:
            file_obj.close()
        except OSError:
            failed = True

    if failed:
        return None
    return total


def run(request: ActionRequest, tools_config) -> ActionResult:
    key = request.resource_key
    if key is None:
        return _UNKNOWN_OPERATION

    spec = tools_config.approved_copies.get(key)
    if spec is None:
        return _UNKNOWN_OPERATION

    if not is_valid_child_name(spec.destination_name):
        # Defense in depth against a ToolsConfig ever built without going
        # through load_tools_config(), which already enforces this at
        # config-load time - see kernel/tools/config.py's own
        # _parse_copy_file(), and repository_backup.py's identical
        # is_valid_backup_key() re-check precedent.
        return _COPY_FAILED

    if (
        len(key) > MAX_SYMBOLIC_NAME_LENGTH
        or len(spec.source_file_key) > MAX_SYMBOLIC_NAME_LENGTH
        or len(spec.destination_directory_key) > MAX_SYMBOLIC_NAME_LENGTH
    ):
        # Defense in depth, same reasoning as above: load_tools_config()
        # already bounds every symbolic identifier's length (see
        # kernel/tools/config.py's MAX_SYMBOLIC_NAME_LENGTH). Without
        # this, a hand-built ToolsConfig with an absurdly long key/
        # reference could still execute successfully - the real copy only
        # ever touches the short, validated source/destination paths -
        # while producing an ActionResult.message too large to fit
        # MAX_STEP_RESULT_JSON_CHARS once wrapped in a StepObservation,
        # discovered only AFTER the real copy already happened
        # (kernel/task_execution/service.py's ACTION-step finalize path
        # does not catch that the way the RESPOND path does).
        return _COPY_FAILED

    try:
        resolved_source = resolve_approved_file(spec.source_file_key, tools_config)
    except FileResourceError:
        return _SOURCE_UNAVAILABLE

    try:
        resolved_dest_dir = resolve_approved_directory(spec.destination_directory_key, tools_config)
    except FileResourceError:
        return _DESTINATION_UNAVAILABLE

    if resolved_source.size_bytes > MAX_COPY_SIZE_BYTES:
        return _SOURCE_TOO_LARGE

    final_path = resolved_dest_dir.path / spec.destination_name
    if path_exists_including_dangling_links(final_path):
        return _DESTINATION_EXISTS

    baseline_identity = _capture_source_identity(resolved_source.path)
    if baseline_identity is None:
        return _SOURCE_UNAVAILABLE

    created = _create_temp_file(resolved_dest_dir.path, key)
    if created is None:
        return _COPY_FAILED
    temp_path, fd = created

    copied_bytes = _stream_copy(resolved_source.path, fd, MAX_COPY_SIZE_BYTES)
    if copied_bytes is None:
        _cleanup_temp(temp_path)
        return _COPY_FAILED

    current_identity = _capture_source_identity(resolved_source.path)
    if current_identity is None or current_identity != baseline_identity:
        _cleanup_temp(temp_path)
        return _SOURCE_CHANGED

    try:
        atomic_finalize_no_replace(temp_path, final_path)
    except (FinalizeCollisionError, OSError):
        _cleanup_temp(temp_path)
        return _DESTINATION_EXISTS

    message = (
        "File copied.\n"
        f"Operation: '{key}'\n"
        f"Source: '{spec.source_file_key}'\n"
        f"Destination directory: '{spec.destination_directory_key}'\n"
        f"Destination name: '{spec.destination_name}'\n"
        f"Bytes copied: {copied_bytes}"
    )
    return ActionResult(True, message, "executed")
