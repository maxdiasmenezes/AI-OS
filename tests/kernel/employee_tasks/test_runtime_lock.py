"""
Tests for kernel/employee_tasks/runtime_lock.py (Milestone 47 P1).

Uses real, separate OS processes (subprocess.Popen against a small
`python -c "..."` helper script) for the cross-process contention/crash-
release proofs the design explicitly requires - a same-process, in-memory
simulation cannot prove an OS-level advisory lock is genuinely held across
process boundaries the way two independent Python interpreters can. No
live WhatsApp service, no network call, and no real task-database content
is ever touched - every lock targets a throwaway tmp_path.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kernel.employee_tasks.runtime_lock import (
    RuntimeOwnershipUnavailableError,
    acquire_runtime_ownership,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_HOLD_SECONDS = 30.0  # long enough that the holder is never killed by its own timeout


def _spawn_holder(db_path: Path) -> subprocess.Popen:
    """A real, separate process that acquires runtime ownership of
    `db_path`, prints ACQUIRED, then sleeps - the caller is responsible for
    killing it. Blocks until the ACQUIRED line is actually observed, so a
    caller never races the holder's own acquisition."""

    code = (
        "from pathlib import Path\n"
        "from kernel.employee_tasks.runtime_lock import acquire_runtime_ownership\n"
        # Assigned to a variable deliberately - an unassigned return value
        # is a bare temporary CPython destroys (via immediate refcounting)
        # at the end of this statement, which would close the underlying
        # file handle and silently release the lock again before the
        # sleep below ever runs.
        f"_lock = acquire_runtime_ownership(Path({str(db_path)!r}))\n"
        "print('ACQUIRED', flush=True)\n"
        f"import time; time.sleep({_HOLD_SECONDS!r})\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(_PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline().strip()
    assert line == "ACQUIRED", f"holder subprocess failed to acquire: stdout={line!r}"
    return proc


def _attempt_in_subprocess(db_path: Path) -> str:
    """A real, separate, one-shot process that attempts runtime ownership
    of `db_path` once and reports the outcome - never shares any Python
    state with this test process or with _spawn_holder()'s own process."""

    code = (
        "from pathlib import Path\n"
        "from kernel.employee_tasks.runtime_lock import ("
        "acquire_runtime_ownership, RuntimeOwnershipUnavailableError)\n"
        "try:\n"
        f"    acquire_runtime_ownership(Path({str(db_path)!r}))\n"
        "except RuntimeOwnershipUnavailableError:\n"
        "    print('DENIED', flush=True)\n"
        "else:\n"
        "    print('ACQUIRED', flush=True)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


# --- A. same DB: second acquisition fails while first process holds it -----


def test_second_process_same_db_fails_while_first_holds(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    holder = _spawn_holder(db_path)
    try:
        outcome = _attempt_in_subprocess(db_path)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert outcome == "DENIED"


# --- B. different DBs: both acquisitions succeed ----------------------------


def test_two_processes_different_db_both_succeed(tmp_path):
    db_a = tmp_path / "a" / "tasks.sqlite3"
    db_b = tmp_path / "b" / "tasks.sqlite3"
    db_a.parent.mkdir()
    db_b.parent.mkdir()

    holder = _spawn_holder(db_a)
    try:
        outcome = _attempt_in_subprocess(db_b)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert outcome == "ACQUIRED"


# --- C. owner process dies without cleanup: a new process can acquire ------


def test_owner_process_killed_without_cleanup_releases_lock(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    holder = _spawn_holder(db_path)

    # Kill abruptly - no chance for the holder's own interpreter to run
    # any cleanup/finally/atexit code of its own; only the OS's own
    # file-descriptor-table teardown on process death can release the
    # underlying flock()/msvcrt.locking() lock. This is exactly the crash
    # scenario database-scoped runtime ownership must survive.
    holder.kill()
    holder.wait(timeout=10)

    outcome = _attempt_in_subprocess(db_path)
    assert outcome == "ACQUIRED"


# --- D. stale lock file remains: content/existence is never authoritative --


def test_stale_lock_file_content_is_never_consulted(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    lock_path = tmp_path / "tasks.sqlite3.lock"
    # A lock file written directly, never through acquire_runtime_ownership(),
    # with content that would be nonsense if anything ever tried to read it
    # as a PID or any other meaningful value - proving neither the file's
    # existence nor its content is ever consulted, only the live OS lock.
    lock_path.write_bytes(b"not a pid, not json, not anything meaningful")

    lock = acquire_runtime_ownership(db_path)
    lock.release()


# --- E/F. normal same-process acquire/release behavior ---------------------


def test_same_process_reacquire_after_release_succeeds(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    lock = acquire_runtime_ownership(db_path)
    lock.release()

    lock2 = acquire_runtime_ownership(db_path)
    lock2.release()


def test_same_process_second_acquisition_while_first_still_held_fails(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    lock = acquire_runtime_ownership(db_path)
    try:
        with pytest.raises(RuntimeOwnershipUnavailableError):
            acquire_runtime_ownership(db_path)
    finally:
        lock.release()


def test_release_is_idempotent(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    lock = acquire_runtime_ownership(db_path)
    lock.release()
    lock.release()  # must not raise


def test_context_manager_releases_on_exit(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    with acquire_runtime_ownership(db_path):
        with pytest.raises(RuntimeOwnershipUnavailableError):
            acquire_runtime_ownership(db_path)
    # Released on context exit - a fresh acquisition now succeeds.
    lock = acquire_runtime_ownership(db_path)
    lock.release()


def test_lock_path_derived_deterministically_from_db_path(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    lock = acquire_runtime_ownership(db_path)
    try:
        assert (tmp_path / "tasks.sqlite3.lock").exists()
    finally:
        lock.release()


# --- G. path-spelling aliases (Milestone 47 P1 adversarial-review
#        correction MEDIUM-4): differently-spelled paths to the SAME
#        database must contend for the SAME lock, without every future
#        caller needing to already pass an identically-canonical path. ---


def test_relative_and_absolute_path_alias_to_the_same_lock(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    holder = _spawn_holder(db_path)
    try:
        # _attempt_in_subprocess() always runs with cwd=_PROJECT_ROOT, so a
        # path spelled relative to that root, on disk, is a different
        # spelling of the exact same file the holder already owns via its
        # absolute path.
        relative_db_path = Path(os.path.relpath(db_path, _PROJECT_ROOT))
        outcome = _attempt_in_subprocess(relative_db_path)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert outcome == "DENIED"


def test_dot_dot_segments_alias_to_the_same_lock(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    (tmp_path / "sibling").mkdir()

    holder = _spawn_holder(db_path)
    try:
        # Lexically distinct from db_path, but the exact same file once
        # the ".." segment is resolved.
        aliased_path = tmp_path / "sibling" / ".." / "tasks.sqlite3"
        outcome = _attempt_in_subprocess(aliased_path)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert outcome == "DENIED"


def test_symlinked_parent_directory_aliases_to_the_same_lock(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted in this environment")

    db_path_via_real = real_dir / "tasks.sqlite3"
    db_path_via_link = link_dir / "tasks.sqlite3"

    holder = _spawn_holder(db_path_via_real)
    try:
        outcome = _attempt_in_subprocess(db_path_via_link)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert outcome == "DENIED"


def test_failure_to_acquire_never_mutates_the_database_file(tmp_path):
    """No schema/task mutation occurs when ownership cannot be acquired -
    this module never opens, creates, or touches the task database file
    itself, only its own derived .lock file."""

    db_path = tmp_path / "tasks.sqlite3"
    holder = _spawn_holder(db_path)
    try:
        with pytest.raises(RuntimeOwnershipUnavailableError):
            acquire_runtime_ownership(db_path)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert not db_path.exists()
