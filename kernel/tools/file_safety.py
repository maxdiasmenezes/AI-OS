"""
Shared exact-file/directory resolution and validation for
kernel/tools/handlers/file_metadata.py, read_text_file.py (Milestone 43
P1), create_directory.py, and copy_file.py (Milestone 43 P2) - the local
hardening logic these handlers need for resolving one
ToolsConfig.approved_files or ToolsConfig.approved_directories entry into
a safe, freshly-checked filesystem target, extracted so the handlers
cannot silently drift apart (mirrors kernel/tools/git_safety.py's own
reason for existing, and kernel/tools/handlers/list_files.py's
canonical-root re-check discipline, adapted here to a single exact file or
directory rather than a listed directory tree).

approved_files/approved_directories are deliberately NOT the same thing as
"permission to touch everything inside a directory": approving a
directory as a parent for create_directory or a destination for copy_file
authorizes exactly the one pre-configured child name a composite spec
names - never an arbitrary name inside it. There is no relative-path,
filename, or pattern accepted from a caller, a model, or request text
anywhere in this module.

FRESH CHECKS ONLY: configuration validity at load time
(kernel/tools/config.py) is never treated as a guarantee that a target
still exists, is still the right kind of filesystem object, or is still
not a symlink/reparse point. resolve_approved_file()/
resolve_approved_directory() each perform exactly one lstat() call against
the actual filesystem at the moment of the call and reject outright
(never follow) a configured path that is itself currently a symlink or
reparse point, or that is not currently the expected kind of object.

THREAT MODEL / RESIDUAL TOCTOU LIMITATION: this is a personal, single-user
machine, not a hostile multi-user filesystem security product (see
docs/architecture.md). This module rejects a configured target that is a
symlink/reparse point at check time and requires the expected kind of
object - but it does not implement atomic, race-free open-with-no-follow
semantics, which are not available in a simple, portable form on this
Windows stack. A small window remains, in principle, between this
module's lstat() and a caller's own subsequent open()/stat()/mkdir() call
(e.g. read_text_file.py opening the file immediately afterward, or
create_directory.py/copy_file.py creating something inside a resolved
directory immediately afterward) in which the path could be replaced.
This is an accepted, documented limitation given the threat model, not an
oversight - see kernel/tools/handlers/file_metadata.py, read_text_file.py,
create_directory.py, and copy_file.py's own docstrings, and this
package's tests. For create_directory.py/copy_file.py specifically, the
FINAL no-clobber guarantee never rests on this module's pre-check alone -
it rests on os.mkdir()'s or kernel/tools/atomic_finalize.py's own atomic,
race-free "fail if it already exists" primitive, which this module's
checks only usefully narrow the window before, never replace.
"""

from __future__ import annotations

import stat as stat_module
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

# TYPE_CHECKING-only import (never at runtime): kernel/tools/config.py now
# imports kernel/tools/desktop_safety.py, which imports THIS module for
# its shared classify_stat_mode()/StatClassification - a real, runtime,
# top-level "from kernel.tools.config import ToolsConfig" here would
# create a circular import (config -> desktop_safety -> file_safety ->
# config). ToolsConfig is only ever used as a type annotation below.
if TYPE_CHECKING:
    from kernel.tools.config import ToolsConfig

# Symbolic outcome codes shared by both handlers via FileResourceError -
# kernel/tools/audit.py's own fixed, bounded outcome vocabulary.
_OUTCOME_REJECTED = "rejected"
_OUTCOME_FAILED = "failed"

_UNAVAILABLE_MESSAGE = "That file is not available."
_DIRECTORY_UNAVAILABLE_MESSAGE = "That directory is not available."


class StatClassification(Enum):
    """What classify_stat_mode() decided about one lstat() result - a
    closed, three-way outcome so a caller never has to re-derive the
    symlink/reparse-point/regular-file distinction itself."""

    REGULAR_FILE = "regular_file"
    SYMLINK_OR_REPARSE_POINT = "symlink_or_reparse_point"
    OTHER = "other"


def classify_stat_mode(
    st_mode: int, st_file_attributes: int | None = None
) -> StatClassification:
    """Pure classification of one lstat() result's st_mode (and, on
    Windows, st_file_attributes) - no filesystem access, so the
    safety-critical symlink/reparse-point rejection decision has
    unit-level coverage that never depends on this machine's account
    having permission to actually CREATE a real symlink/junction (which
    local Windows accounts often lack without Developer Mode or
    elevation - unlike a real-symlink integration test, this can never be
    skipped).

    stat.S_ISLNK(st_mode) alone reliably detects an NTFS symbolic link
    (what os.symlink() creates) but NOT every kind of Windows reparse
    point: CPython's stat implementation only sets the S_IFLNK bit for the
    IO_REPARSE_TAG_SYMLINK/IO_REPARSE_TAG_MOUNT_POINT (junction) reparse
    tags - a different reparse point (e.g. a OneDrive cloud-placeholder
    file, or a deduplication reparse point) still reports an ordinary
    S_IFREG/S_IFDIR mode and would NOT be caught by S_ISLNK alone. This
    function additionally checks the raw FILE_ATTRIBUTE_REPARSE_POINT bit
    (st_file_attributes, populated by os.lstat() on Windows since Python
    3.5 - None on a non-Windows stat() result, where it is simply not
    checked), so ANY reparse point is rejected - matching this module's
    own documented "reject outright, never follow" policy, not only the
    subset CPython happens to map onto S_IFLNK."""

    if stat_module.S_ISLNK(st_mode):
        return StatClassification.SYMLINK_OR_REPARSE_POINT
    if st_file_attributes is not None and (
        st_file_attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        return StatClassification.SYMLINK_OR_REPARSE_POINT
    if stat_module.S_ISREG(st_mode):
        return StatClassification.REGULAR_FILE
    return StatClassification.OTHER


class FileResourceError(Exception):
    """Raised by resolve_approved_file() when resource_key does not
    resolve to a safe, currently-valid regular file. `message` is already
    safe to relay as an ActionResult.message (never a resolved path, an
    exception's own text, or a traceback); `outcome` is one of
    kernel/tools/audit.py's fixed outcome codes."""

    def __init__(self, message: str, outcome: str):
        super().__init__(message)
        self.message = message
        self.outcome = outcome


@dataclass(frozen=True)
class ResolvedFile:
    """The result of successfully resolving one approved_files entry.
    `path` is the exact configured Path - handlers open/stat it directly,
    never re-deriving or accepting a different path from anywhere else.
    `size_bytes`/`modified_at` are captured from the SAME lstat() call
    resolve_approved_file() already made, so file_metadata.py never needs
    a second filesystem call (and a second TOCTOU window) just to read
    back the metadata this module already has in hand."""

    key: str
    path: Path
    size_bytes: int
    modified_at: str


def resolve_approved_file(resource_key: str | None, tools_config: ToolsConfig) -> ResolvedFile:
    """Resolve `resource_key` against tools_config.approved_files and
    verify, via one fresh lstat() call, that it currently names an
    existing regular file that is not itself a symlink/reparse point.
    Never follows a symlink to find "the real target" - a configured
    symlink is rejected outright, exactly like an unregistered key. Raises
    FileResourceError on any failure; never returns a partially-resolved
    result."""

    if resource_key is None:
        raise FileResourceError("A file must be specified.", _OUTCOME_REJECTED)

    spec = tools_config.approved_files.get(resource_key)
    if spec is None:
        raise FileResourceError("That file is not registered.", _OUTCOME_REJECTED)

    configured_path = Path(spec.path)

    try:
        st = configured_path.lstat()
    except OSError:
        raise FileResourceError(_UNAVAILABLE_MESSAGE, _OUTCOME_FAILED)

    classification = classify_stat_mode(st.st_mode, getattr(st, "st_file_attributes", None))

    if classification is StatClassification.SYMLINK_OR_REPARSE_POINT:
        # A symlink/reparse point at the configured path itself - rejected
        # outright, never followed to "the real" target.
        raise FileResourceError(_UNAVAILABLE_MESSAGE, _OUTCOME_FAILED)

    if classification is not StatClassification.REGULAR_FILE:
        # A directory or other special file - never a regular file.
        raise FileResourceError(_UNAVAILABLE_MESSAGE, _OUTCOME_FAILED)

    modified_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()

    return ResolvedFile(
        key=resource_key,
        path=configured_path,
        size_bytes=st.st_size,
        modified_at=modified_at,
    )


@dataclass(frozen=True)
class ResolvedDirectory:
    """The result of successfully resolving one approved_directories entry
    as a safe parent/destination directory (Milestone 43 P2). `path` is
    the exact configured Path - handlers create inside it directly, never
    re-deriving or accepting a different directory from anywhere else."""

    key: str
    path: Path


def resolve_approved_directory(
    directory_key: str | None, tools_config: ToolsConfig
) -> ResolvedDirectory:
    """Resolve `directory_key` against tools_config.approved_directories
    and verify, via one fresh lstat() call, that it currently names an
    existing directory that is not itself a symlink/reparse point. Never
    follows a symlink to find "the real target" - a configured symlink is
    rejected outright, exactly like resolve_approved_file() above. Raises
    FileResourceError on any failure; never returns a partially-resolved
    result. Shared by create_directory.py (as the creation parent) and
    copy_file.py (as the destination directory) - the exact same
    discipline resolve_approved_file() already established, adapted to a
    directory rather than a file."""

    if directory_key is None:
        raise FileResourceError("A directory must be specified.", _OUTCOME_REJECTED)

    configured_path_str = tools_config.approved_directories.get(directory_key)
    if configured_path_str is None:
        raise FileResourceError("That directory is not registered.", _OUTCOME_REJECTED)

    configured_path = Path(configured_path_str)

    try:
        st = configured_path.lstat()
    except OSError:
        raise FileResourceError(_DIRECTORY_UNAVAILABLE_MESSAGE, _OUTCOME_FAILED)

    classification = classify_stat_mode(st.st_mode, getattr(st, "st_file_attributes", None))

    if classification is StatClassification.SYMLINK_OR_REPARSE_POINT:
        # A symlink/reparse point at the configured path itself - rejected
        # outright, never followed to "the real" target.
        raise FileResourceError(_DIRECTORY_UNAVAILABLE_MESSAGE, _OUTCOME_FAILED)

    if not stat_module.S_ISDIR(st.st_mode):
        raise FileResourceError(_DIRECTORY_UNAVAILABLE_MESSAGE, _OUTCOME_FAILED)

    return ResolvedDirectory(key=directory_key, path=configured_path)


def path_exists_including_dangling_links(path: Path) -> bool:
    """True if anything currently occupies `path` - including a broken/
    dangling symlink or reparse point, which Path.exists() would
    incorrectly report as absent (it follows the final path component and
    returns False when the target of a broken link is missing). Uses
    lstat(), which never follows the final component, so a dangling link
    is correctly reported as "something is there."

    This is a pre-check only, used by create_directory.py/copy_file.py to
    fail fast and cleanly before attempting anything - it narrows, but
    does not by itself close, the race between this check and the actual
    creation call. Final no-clobber correctness always rests on the
    atomic, race-free primitive used afterward (os.mkdir()'s own
    FileExistsError, or kernel/tools/atomic_finalize.py's no-replace
    finalize) - never on this function alone."""

    try:
        path.lstat()
    except OSError:
        return False
    return True
