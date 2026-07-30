"""
Tests for kernel/tools/process_control.py.

Unlike the rest of this repository's test suites, these tests deliberately
spawn real, short-lived subprocesses (via sys.executable) - this module's
entire job is subprocess execution, timeout enforcement, and process-tree
termination, none of which can be meaningfully verified without a real
process. Every spawned process is trivial, fast, and either exits on its
own or is killed by the code under test within the test's own timeout.
"""

import sys
import time

import psutil

from kernel.tools.process_control import launch_detached, run_with_timeout


def test_launch_detached_returns_success_and_a_pid_immediately(tmp_path):
    started = time.monotonic()
    result = launch_detached(
        [sys.executable, "-c", "import time; time.sleep(2)"], str(tmp_path)
    )
    elapsed = time.monotonic() - started

    assert result.success is True
    assert result.pid is not None
    # Must return right after launch, not wait for the 2-second sleep.
    assert elapsed < 1.5

    # Cleanup: don't leave the sleeping process behind.
    try:
        psutil.Process(result.pid).kill()
    except psutil.NoSuchProcess:
        pass


def test_launch_detached_reports_failure_for_a_nonexistent_executable(tmp_path):
    result = launch_detached([str(tmp_path / "does_not_exist.exe")], str(tmp_path))

    assert result.success is False
    assert result.pid is None


def test_run_with_timeout_reports_success_for_a_zero_exit_process(tmp_path):
    result = run_with_timeout([sys.executable, "-c", "pass"], str(tmp_path), timeout_seconds=10)

    assert result.success is True
    assert result.timed_out is False
    assert result.returncode == 0


def test_run_with_timeout_reports_failure_for_a_nonzero_exit_process(tmp_path):
    result = run_with_timeout(
        [sys.executable, "-c", "import sys; sys.exit(1)"], str(tmp_path), timeout_seconds=10
    )

    assert result.success is False
    assert result.timed_out is False
    assert result.returncode == 1


def test_run_with_timeout_reports_failure_for_a_nonexistent_executable(tmp_path):
    result = run_with_timeout(
        [str(tmp_path / "does_not_exist.exe")], str(tmp_path), timeout_seconds=10
    )

    assert result.success is False
    assert result.timed_out is False


def test_run_with_timeout_kills_a_process_that_exceeds_its_timeout(tmp_path):
    started = time.monotonic()
    result = run_with_timeout(
        [sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path), timeout_seconds=0.5
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.success is False
    # Must return promptly after the timeout, not wait for the full sleep.
    assert elapsed < 10


def test_run_with_timeout_kills_the_full_process_tree_not_just_the_parent(tmp_path):
    # The parent script itself spawns a child that sleeps far longer than
    # the parent's own configured timeout - proving the child is reaped
    # too, not left orphaned and running.
    child_marker = tmp_path / "child_pid.txt"
    parent_script = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open(r'{child_marker}', 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )

    result = run_with_timeout(
        [sys.executable, "-c", parent_script], str(tmp_path), timeout_seconds=1.0
    )

    assert result.timed_out is True

    # Give the OS a brief moment to finish tearing the child down, then
    # confirm it's actually gone.
    child_pid = int(child_marker.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and psutil.pid_exists(child_pid):
        time.sleep(0.1)

    assert not psutil.pid_exists(child_pid)
