"""
repository_backup action handler: creates a verified, local-only Git
bundle of one registered repository's committed history (Milestone 35).

The caller never supplies a path, filename, ref, or option - only a key
already validated against kernel/config/tools.yaml's
repository_backup.approved_backups (which itself, at load time, is
validated to correspond to an existing repo_health.approved_repositories
entry - see kernel/tools/config.py). The repository path is looked up
through that repo_health entry rather than duplicated here; the backup
destination directory comes only from repository_backup.approved_backups.

Ref-inclusion policy: `git bundle create - HEAD --branches --tags`.
Included: the current HEAD (so a detached-HEAD checkout is still
captured), every local branch (refs/heads/*), every tag (refs/tags/*),
and every committed object those refs require. Excluded: refs/remotes/*
(remote-tracking refs mirror another machine's fetch history, not
anything committed here), refs/stash, refs/notes/*, refs/replace/* (also
neutralized globally by --no-replace-objects, part of the shared safety
prefix), refs/pull/* or any other custom ref, and - because a git bundle
can only ever contain committed objects reachable from the refs it
records - every uncommitted, untracked, and ignored file in the working
tree. A file that is untracked or gitignored *right now* is absent from
the bundle for that reason; a file that was ever actually committed to
selected history (even one later removed from HEAD, or one matching
today's .gitignore) is not filtered out - this handler does not inspect
filenames within Git history, only which refs are included. The
user-facing reply is careful to describe this precisely - see run()'s
message below.

Every git subprocess this handler runs is local-only: no fetch, pull,
push, checkout, reset, merge, commit, clone, remote, or network access of
any kind. It reuses kernel/tools/git_safety.py's GIT_SAFE_PREFIX
(`--no-optional-locks --no-pager --no-replace-objects -c
core.fsmonitor=false`) and sanitized_git_env() (full environment copy,
config-injection/repository-redirection/tracing/credential-helper
variables stripped case-insensitively, GIT_OPTIONAL_LOCKS=0,
GIT_CONFIG_NOSYSTEM=1, GIT_CONFIG_GLOBAL=os.devnull) - the exact same
protections repo_health.py applies to its own local git calls, extracted
in this milestone so the two handlers cannot silently drift apart. Unlike
repo_health.py, this handler has no remote-only overlay at all - it never
needs one.

Streaming, not buffering: `git bundle create -` writes the complete
bundle to *stdout*, which kernel/tools/process_control.py's
run_streaming_stdout_to_file() connects directly to an already-open,
exclusively-created (O_CREAT|O_EXCL) file descriptor in the approved
destination directory - the bundle payload is never held in this
process's memory, no matter its size. That exclusive-open closes the
exact time-of-check/time-of-use window a "generate a temp name, close it,
then have git re-open that path" sequence would leave open: nothing else
can ever have written to, replaced, or symlinked that path before git's
own writes land in it, because the path did not exist until this
process's own os.open() call atomically created it.

File-identity discipline: this handler records the partial file's device/
inode identity (`os.stat(..., follow_symlinks=False)`, requiring a
regular file - never a symlink, junction/reparse point, directory, or
other special file - directly inside the canonical destination, with a
hard-link count of 1) immediately after bundle creation completes, then
re-confirms that exact identity is unchanged after verification, after
hashing, and immediately before finalization. Any mismatch at any
checkpoint fails the whole operation closed and removes only the one
partial file this execution created - never anything else in the
destination directory, and never an existing completed backup.

Finalization never uses os.replace() and never relies on an informal
"rename doesn't overwrite" assumption - see
kernel/tools/atomic_finalize.py for the documented, platform-specific
no-replace guarantee it provides. An existing `.bundle` file is never
deleted, truncated, or overwritten, under any failure mode.

Nothing this handler returns or audits ever includes an absolute
filesystem path, the generated filename's containing directory, raw git
output, stderr, or a traceback - only the fixed, symbolic strings and
generated filename defined below.
"""

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kernel.tools.atomic_finalize import FinalizeCollisionError, atomic_finalize_no_replace
from kernel.tools.config import is_valid_backup_key
from kernel.tools.git_safety import GIT_SAFE_PREFIX, sanitized_git_env
from kernel.tools.process_control import run_capturing_stdout, run_streaming_stdout_to_file
from kernel.tools.types import ActionRequest, ActionResult

GIT_WORKTREE_CHECK_TIMEOUT_SECONDS = 5.0
GIT_BUNDLE_CREATE_TIMEOUT_SECONDS = 120.0
GIT_BUNDLE_VERIFY_TIMEOUT_SECONDS = 30.0
MAX_CAPTURED_BYTES = 4096
HASH_CHUNK_SIZE = 1_048_576
MAX_NAME_ATTEMPTS = 5
RANDOM_SUFFIX_BYTES = 8  # secrets.token_hex(8) -> 16 lowercase hex characters

# Only defined on Windows; a real, meaningful flag there (open the file in
# binary mode, no CRLF translation of the raw bundle bytes). Falls back to
# a no-op 0 everywhere else, where there is no text/binary distinction.
_O_BINARY = getattr(os, "O_BINARY", 0)

_BUNDLE_SUFFIX = ".bundle"
_PARTIAL_SUFFIX = ".partial"
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

_REPO_NOT_REGISTERED = ActionResult(False, "That repository is not registered.", "rejected")
_BACKUP_NOT_REGISTERED = ActionResult(
    False, "That repository is not registered for backups.", "rejected"
)
_REPO_UNAVAILABLE = ActionResult(False, "That repository is not available.", "failed")
_DESTINATION_UNAVAILABLE = ActionResult(
    False, "The backup destination is not available.", "failed"
)
_CREATION_FAILED = ActionResult(
    False, "The repository backup could not be created.", "failed"
)
_VERIFICATION_FAILED = ActionResult(
    False, "The repository backup could not be verified.", "failed"
)
_TIMED_OUT = ActionResult(
    False, "The repository backup timed out and was stopped.", "timed_out"
)

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


@dataclass(frozen=True)
class _FileIdentity:
    dev: int
    ino: int
    size: int


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _generate_names(key: str) -> tuple[str, str]:
    """Every component is either the validated symbolic key or trusted-
    code-generated (a fresh UTC timestamp, a fresh cryptographically
    random suffix) - never sender-controlled text beyond the key itself,
    and the key has already been validated as filename-safe by the time
    this is called (both at config-load time and redundantly by this
    module's own run()). Partial and final names share the exact same
    timestamp and suffix, differing only in the leading dot and
    extension."""

    timestamp = _utc_timestamp()
    suffix = secrets.token_hex(RANDOM_SUFFIX_BYTES)
    partial_name = f".{key}-{timestamp}-{suffix}{_PARTIAL_SUFFIX}"
    final_name = f"{key}-{timestamp}-{suffix}{_BUNDLE_SUFFIX}"
    return partial_name, final_name


def _format_size(num_bytes: int) -> str:
    value = float(num_bytes)
    unit_index = 0
    while value >= 1024.0 and unit_index < len(_SIZE_UNITS) - 1:
        value /= 1024.0
        unit_index += 1
    unit = _SIZE_UNITS[unit_index]
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def _cleanup_partial(path: Path) -> None:
    """Best-effort removal of exactly the one partial file this execution
    created - never a glob, never a directory sweep, never touches any
    other file. A cleanup failure is never raised or logged with detail;
    it must never mask the real outcome being reported."""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _stat_identity(path: Path, expected_parent: Path) -> _FileIdentity | None:
    """Returns this path's current identity iff, right now, it is a
    regular file - never a symlink, a junction/reparse point, a
    directory, or any other special file - with a hard-link count of 1,
    whose canonical parent is exactly expected_parent. Returns None for
    absolutely any other outcome; callers must treat None as "fail
    closed", never as a reason to retry."""

    if path.is_symlink():
        return None
    if hasattr(os.path, "isjunction") and os.path.isjunction(path):
        return None
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_nlink != 1:
        return None
    try:
        parent_canonical = path.parent.resolve(strict=True)
    except OSError:
        return None
    if parent_canonical != expected_parent:
        return None
    return _FileIdentity(dev=st.st_dev, ino=st.st_ino, size=st.st_size)


def _identity_matches(current: _FileIdentity | None, baseline: _FileIdentity) -> bool:
    return (
        current is not None
        and current.dev == baseline.dev
        and current.ino == baseline.ino
        and current.size == baseline.size
    )


def _is_worktree_root(canonical_repo: Path, env: dict) -> bool:
    inside = run_capturing_stdout(
        [*GIT_SAFE_PREFIX, "rev-parse", "--is-inside-work-tree"],
        str(canonical_repo),
        GIT_WORKTREE_CHECK_TIMEOUT_SECONDS,
        env=env,
        max_output_bytes=MAX_CAPTURED_BYTES,
    )
    if inside.timed_out or not inside.success:
        return False
    if (inside.stdout or b"").decode("utf-8", errors="replace").strip() != "true":
        return False

    toplevel = run_capturing_stdout(
        [*GIT_SAFE_PREFIX, "rev-parse", "--show-toplevel"],
        str(canonical_repo),
        GIT_WORKTREE_CHECK_TIMEOUT_SECONDS,
        env=env,
        max_output_bytes=MAX_CAPTURED_BYTES,
    )
    if toplevel.timed_out or not toplevel.success:
        return False
    raw_toplevel = (toplevel.stdout or b"").decode("utf-8", errors="replace").strip()
    if not raw_toplevel:
        return False
    try:
        toplevel_canonical = Path(raw_toplevel).resolve(strict=True)
    except OSError:
        return False
    return toplevel_canonical == canonical_repo


def _create_partial_file(destination: Path, key: str) -> tuple[Path, str, int] | None:
    """Exclusively creates a brand-new partial file directly in
    `destination` and returns (partial_path, final_name, open_fd) - the
    fd is left open, owned by the caller, ready to be handed to the git
    subprocess as its stdout sink. O_EXCL guarantees the path did not
    already exist (as a regular file, a symlink, or anything else) the
    instant before this call created it - there is no separate "generate
    a name, then create it" window. On a name collision, retries with an
    entirely fresh timestamp and random suffix, bounded by
    MAX_NAME_ATTEMPTS; a previously-collided suffix is never reused.

    Requests mode 0o600 (owner read/write only, no group/other access).
    On POSIX this is enforced directly by the OS at creation time. On
    Windows, os.open()'s mode argument only ever affects the file's
    read-only attribute bit - Windows has no POSIX permission-bit concept
    at the filesystem-call level, and the file's actual access control is
    governed entirely by inherited NTFS ACLs from the destination
    directory, which this handler makes no attempt to read, set, or
    otherwise modify. Requesting 0o600 here is therefore best-effort
    documentation of intent on Windows, not an access-control guarantee;
    restricting who can read the approved backup destination is a
    machine-configuration concern outside this milestone's scope."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY
    for _ in range(MAX_NAME_ATTEMPTS):
        partial_name, final_name = _generate_names(key)
        partial_path = destination / partial_name
        try:
            fd = os.open(partial_path, flags, 0o600)
        except FileExistsError:
            continue
        except OSError:
            return None
        return partial_path, final_name, fd
    return None


def _write_bundle(canonical_repo: Path, fd: int, env: dict):
    """Streams `git bundle create - HEAD --branches --tags`'s stdout
    directly into the already-open, exclusively-created fd - the bundle
    payload never passes through this process's own memory. Always
    flushes, fsyncs (where supported), and closes the wrapped file object
    before returning, regardless of outcome; the fd is not usable by the
    caller afterward either way."""

    file_obj = os.fdopen(fd, "wb")
    try:
        result = run_streaming_stdout_to_file(
            [*GIT_SAFE_PREFIX, "bundle", "create", "-", "HEAD", "--branches", "--tags"],
            str(canonical_repo),
            GIT_BUNDLE_CREATE_TIMEOUT_SECONDS,
            file_obj,
            env=env,
        )
    finally:
        try:
            file_obj.flush()
            os.fsync(file_obj.fileno())
        except OSError:
            pass
        file_obj.close()
    return result


def _verify_bundle(canonical_repo: Path, partial_path: Path, env: dict):
    return run_capturing_stdout(
        [*GIT_SAFE_PREFIX, "bundle", "verify", str(partial_path)],
        str(canonical_repo),
        GIT_BUNDLE_VERIFY_TIMEOUT_SECONDS,
        env=env,
        max_output_bytes=MAX_CAPTURED_BYTES,
    )


def _hash_and_size(path: Path) -> tuple[str, int] | None:
    """SHA-256 and byte count via bounded streaming reads - the complete
    file is never loaded into memory at once."""

    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except OSError:
        return None
    return digest.hexdigest(), total


def run(request: ActionRequest, tools_config) -> ActionResult:
    key = request.resource_key

    repo_spec = tools_config.approved_repositories.get(key)
    if repo_spec is None:
        return _REPO_NOT_REGISTERED

    backup_spec = tools_config.approved_backups.get(key)
    if backup_spec is None:
        return _BACKUP_NOT_REGISTERED

    if not is_valid_backup_key(key):
        # Defense in depth against a ToolsConfig ever built without going
        # through load_tools_config(), which already enforces this at
        # load time - see kernel/tools/config.py.
        return _CREATION_FAILED

    try:
        canonical_repo = Path(repo_spec.path).resolve(strict=True)
    except OSError:
        return _REPO_UNAVAILABLE
    if not canonical_repo.is_dir():
        return _REPO_UNAVAILABLE

    try:
        canonical_destination = Path(backup_spec.destination_directory).resolve(strict=True)
    except OSError:
        return _DESTINATION_UNAVAILABLE
    if not canonical_destination.is_dir():
        return _DESTINATION_UNAVAILABLE

    if canonical_repo == canonical_destination:
        return _DESTINATION_UNAVAILABLE
    if canonical_destination.is_relative_to(canonical_repo):
        return _DESTINATION_UNAVAILABLE
    if canonical_repo.is_relative_to(canonical_destination):
        return _DESTINATION_UNAVAILABLE

    env = sanitized_git_env()

    if not _is_worktree_root(canonical_repo, env):
        return _REPO_UNAVAILABLE

    created = _create_partial_file(canonical_destination, key)
    if created is None:
        return _CREATION_FAILED
    partial_path, final_name, fd = created

    write_result = _write_bundle(canonical_repo, fd, env)
    if write_result.timed_out:
        _cleanup_partial(partial_path)
        return _TIMED_OUT
    if not write_result.success:
        _cleanup_partial(partial_path)
        return _CREATION_FAILED

    baseline_identity = _stat_identity(partial_path, canonical_destination)
    if baseline_identity is None:
        _cleanup_partial(partial_path)
        return _CREATION_FAILED

    verify_result = _verify_bundle(canonical_repo, partial_path, env)
    if verify_result.timed_out:
        _cleanup_partial(partial_path)
        return _TIMED_OUT
    if not verify_result.success:
        _cleanup_partial(partial_path)
        return _VERIFICATION_FAILED

    post_verify_identity = _stat_identity(partial_path, canonical_destination)
    if not _identity_matches(post_verify_identity, baseline_identity):
        _cleanup_partial(partial_path)
        return _VERIFICATION_FAILED

    hashed = _hash_and_size(partial_path)
    if hashed is None:
        _cleanup_partial(partial_path)
        return _CREATION_FAILED
    digest, hashed_size = hashed

    post_hash_identity = _stat_identity(partial_path, canonical_destination)
    if not _identity_matches(post_hash_identity, baseline_identity) or post_hash_identity.size != hashed_size:
        _cleanup_partial(partial_path)
        return _CREATION_FAILED

    pre_finalize_identity = _stat_identity(partial_path, canonical_destination)
    if not _identity_matches(pre_finalize_identity, baseline_identity):
        _cleanup_partial(partial_path)
        return _CREATION_FAILED

    final_path = canonical_destination / final_name
    try:
        atomic_finalize_no_replace(partial_path, final_path)
    except (FinalizeCollisionError, OSError):
        _cleanup_partial(partial_path)
        return _CREATION_FAILED

    if partial_path.exists():
        # atomic_finalize_no_replace succeeded, which means the partial
        # name should already be gone (a same-directory rename/hardlink
        # move, not a copy) - if something now occupies that name anyway,
        # it is not the file this execution created, so it is never
        # touched here.
        return _CREATION_FAILED

    final_identity = _stat_identity(final_path, canonical_destination)
    if (
        final_identity is None
        or final_identity.size != hashed_size
        or final_identity.dev != baseline_identity.dev
        or final_identity.ino != baseline_identity.ino
    ):
        return _CREATION_FAILED

    message = (
        "Repository backup created.\n"
        f"Repository: {key}\n"
        f"File: {final_name}\n"
        f"Size: {_format_size(hashed_size)}\n"
        f"SHA-256: {digest}\n"
        "Included: committed HEAD, local branches, tags, and required Git history\n"
        "Not included: current uncommitted, untracked, or ignored files"
    )
    return ActionResult(True, message, "executed")
