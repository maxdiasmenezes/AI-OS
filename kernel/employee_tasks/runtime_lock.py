"""
Database-scoped runtime ownership (Milestone 47 P1).

A stdlib-only, OS-level advisory lock proving "only one live employee-task
runtime may own/reconcile a given durable task database at a time" -
deliberately scoped to the database file itself, never to an interface's
HTTP port (a fixed port bind is a WhatsApp-specific incidental, not a
database-scoped guarantee - see the Milestone 47 design history for why
that was rejected). This module has no dependency on sqlite3, kernel.config,
or any interface package - it only ever takes an already-resolved database
Path and derives its own lock file path from it.

Mechanism, by platform:

  - POSIX: fcntl.flock(fd, LOCK_EX | LOCK_NB) - a whole-file advisory lock
    tied to the OPEN FILE DESCRIPTION. The kernel releases it unconditionally
    when every file descriptor referencing that open file description
    closes - including on process crash, since the OS always closes a
    dead process's file descriptors. No PID bookkeeping, no staleness
    polling: the OS's own lock table is authoritative.
  - Windows: msvcrt.locking(fd, LK_NBLCK, 1) - Windows exposes no whole-file
    advisory primitive via msvcrt, so this locks a fixed, 1-byte range at
    offset 0 (the conventional portable technique) instead. Windows
    likewise releases file locks automatically when the owning process
    terminates, for any reason, and its handles are closed by the OS - the
    same crash-safety property as POSIX, with no PID file needed.

CRITICAL INVARIANT: the lock FILE's existence or content is never
authoritative and never consulted - only the OS-level lock acquisition
attempt's success or failure is. A stale lock file left behind by a crashed
process is harmless; the next process locks the very same file immediately,
without needing to notice or clean up anything. This is deliberate: a
PID-liveness-polling design would need to distinguish a genuinely-stale PID
from a reused one (a PID-reuse race) - the OS-level lock has no such
ambiguity to resolve in the first place.

Non-blocking only: acquisition either succeeds immediately or fails
immediately (RuntimeOwnershipUnavailableError) - this module never waits for
a lock to free up. A caller that cannot acquire ownership must fail its own
startup closed, before touching any durable task state - see this package's
own composition-root caller (interfaces/whatsapp/server.py:build_server())
for the required ordering: acquire before schema init, before any
TaskRepository is constructed, before any worker/reconciliation activity.
"""

import os
import sys
from pathlib import Path

_LOCK_FILE_SUFFIX = ".lock"
_LOCK_BYTE_RANGE = 1  # Windows byte-range lock size; irrelevant on POSIX, which locks the whole file.


class RuntimeOwnershipUnavailableError(Exception):
    """Runtime ownership of a task database could not be acquired -
    another live process already owns it (or, far less likely, some other
    OS-level failure occurred trying to open/lock the lock file). Never
    carries a raw OSError message or filesystem path detail meant for a
    caller to inspect via str() - callers must treat this as a fixed,
    generic, code-owned signal only, matching every other bounded-error
    convention in kernel/employee_tasks/."""


def _lock_path_for(db_path: Path) -> Path:
    """Deterministic, 1:1 derivation from the database path - never a
    second, independent configuration value. Two servers configured with
    different database paths always get independent lock files; two
    servers configured with the SAME path always contend for the SAME
    lock file, EVEN if the two callers spelled that path differently
    (relative vs. absolute, `.`/`..` segments, or a symlinked parent
    directory) - see acquire_runtime_ownership()'s own docstring for why
    this must not rely on every future caller already passing an
    identically-spelled path.

    Milestone 47 P1 adversarial-review correction (MEDIUM-4): the
    database file itself may not exist yet (runtime ownership is
    intentionally acquired before schema initialization/database
    creation - see this module's own docstring), so this canonicalizes
    only the PARENT directory strictly (kernel/employee_tasks/db.py's own
    resolve_database_path() already guarantees that directory exists and
    is never created automatically), then re-appends the database's own
    filename - never db_path.resolve(strict=True) on the database file
    itself, which would fail for a not-yet-created database."""

    canonical_parent = db_path.parent.resolve(strict=True)
    canonical_db_path = canonical_parent / db_path.name
    return canonical_db_path.with_name(canonical_db_path.name + _LOCK_FILE_SUFFIX)


class RuntimeLock:
    """An acquired, held-open runtime-ownership lock. Holds one open file
    handle for its entire lifetime; release() (or use as a context manager)
    closes it, which is what actually releases the underlying OS-level
    lock. Never inspect or trust this object's own file's content - it is
    written once, for human debugging convenience only, and never read
    back by any code path in this module."""

    def __init__(self, path: Path, handle) -> None:
        self._path = path
        self._handle = handle
        self._released = False

    def release(self) -> None:
        """Idempotent: calling this more than once, or after the handle
        was already closed some other way, is safe and a no-op on the
        second call onward."""

        if self._released:
            return
        self._released = True
        try:
            self._handle.close()
        except OSError:
            pass

    def __enter__(self) -> "RuntimeLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _acquire_posix(handle) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _acquire_windows(handle) -> None:
    import msvcrt

    # msvcrt.locking() locks bytes starting at the file's CURRENT position -
    # the file must actually contain at least _LOCK_BYTE_RANGE bytes to lock,
    # or the call fails; write a single placeholder byte (content is never
    # meaningful - see module docstring) if the file is currently empty,
    # then seek back to the fixed offset this lock always uses.
    handle.seek(0, os.SEEK_END)
    if handle.tell() < _LOCK_BYTE_RANGE:
        handle.write(b"\0" * _LOCK_BYTE_RANGE)
        handle.flush()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTE_RANGE)


def acquire_runtime_ownership(db_path: Path) -> RuntimeLock:
    """Attempt to acquire exclusive runtime ownership of the task database
    at `db_path`, immediately, non-blocking. Raises
    RuntimeOwnershipUnavailableError if another live process already owns
    it. Returns a RuntimeLock the caller must hold open (never close, never
    let go out of scope) for as long as anything in this process may still
    read or write the task database - see this module's own docstring for
    the required acquire-before/release-after ordering.

    CALLER RESPONSIBILITY, STATED EXPLICITLY (a real, silent-failure-shaped
    footgun found and fixed in this milestone's own test suite): the
    returned RuntimeLock MUST be assigned to a variable/attribute that
    outlives the ownership window - `acquire_runtime_ownership(db_path)`
    with the return value discarded is destroyed immediately (CPython's
    own reference counting, not a bug in this function), which closes the
    underlying file handle and silently releases the lock again right
    away, with no exception raised anywhere to indicate anything went
    wrong. Always bind the result: `lock = acquire_runtime_ownership(...)`,
    and keep `lock` alive (e.g. as an attribute on the long-lived server
    object - see interfaces/whatsapp/server.py:build_server()) for as long
    as ownership must be held."""

    try:
        lock_path = _lock_path_for(db_path)
        handle = open(lock_path, "a+b")
    except OSError as exc:
        raise RuntimeOwnershipUnavailableError(
            "could not open the task database runtime lock file"
        ) from exc

    try:
        if sys.platform == "win32":
            _acquire_windows(handle)
        else:
            _acquire_posix(handle)
    except OSError as exc:
        handle.close()
        raise RuntimeOwnershipUnavailableError(
            "another runtime already owns this task database"
        ) from exc

    return RuntimeLock(lock_path, handle)
