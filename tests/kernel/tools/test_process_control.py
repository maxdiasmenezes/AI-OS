"""
Tests for kernel/tools/process_control.py.

Unlike the rest of this repository's test suites, these tests deliberately
spawn real, short-lived subprocesses (via sys.executable) - this module's
entire job is subprocess execution, timeout enforcement, and process-tree
termination, none of which can be meaningfully verified without a real
process. Every spawned process is trivial, fast, and either exits on its
own or is killed by the code under test within the test's own timeout.
"""

import os
import sys
import threading
import time
from pathlib import Path

import psutil

from kernel.tools.process_control import launch_detached, run_capturing_stdout, run_with_timeout

_READER_THREAD_NAME = "process-control-stdout-reader"


def _reader_threads_alive():
    return [t for t in threading.enumerate() if t.name == _READER_THREAD_NAME and t.is_alive()]


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


def test_run_capturing_stdout_returns_captured_output_for_a_zero_exit_process(tmp_path):
    result = run_capturing_stdout(
        [sys.executable, "-c", "print('hello')"], str(tmp_path), timeout_seconds=10
    )

    assert result.success is True
    assert result.timed_out is False
    assert result.returncode == 0
    assert result.stdout.strip() == b"hello"


def test_run_capturing_stdout_reports_failure_for_a_nonzero_exit_process(tmp_path):
    result = run_capturing_stdout(
        [sys.executable, "-c", "import sys; sys.exit(1)"], str(tmp_path), timeout_seconds=10
    )

    assert result.success is False
    assert result.timed_out is False
    assert result.returncode == 1


def test_run_capturing_stdout_reports_failure_for_a_nonexistent_executable(tmp_path):
    result = run_capturing_stdout(
        [str(tmp_path / "does_not_exist.exe")], str(tmp_path), timeout_seconds=10
    )

    assert result.success is False
    assert result.timed_out is False


def test_run_capturing_stdout_bounds_output_to_max_output_bytes(tmp_path):
    result = run_capturing_stdout(
        [sys.executable, "-c", "print('x' * 10000)"],
        str(tmp_path),
        timeout_seconds=10,
        max_output_bytes=100,
    )

    assert result.success is True
    assert len(result.stdout) == 100


def test_run_capturing_stdout_handles_several_megabytes_without_deadlocking(tmp_path):
    # A child writing far more than max_output_bytes must never block on a
    # full pipe buffer waiting for a reader that has stopped consuming -
    # the old communicate()-then-slice implementation would still drain
    # everything (communicate() itself is deadlock-safe), but only after
    # buffering all of it in memory first. This proves the new streaming
    # reader finishes promptly and never retains more than the bound.
    script = (
        "import sys\n"
        "for _ in range(50):\n"
        "    sys.stdout.buffer.write(b'x' * 100000)\n"  # 50 * 100_000 = 5,000,000 bytes
        "sys.stdout.flush()\n"
    )

    started = time.monotonic()
    result = run_capturing_stdout(
        [sys.executable, "-c", script],
        str(tmp_path),
        timeout_seconds=15,
        max_output_bytes=1024,
    )
    elapsed = time.monotonic() - started

    assert result.success is True
    assert result.timed_out is False
    assert len(result.stdout) == 1024
    assert result.stdout == b"x" * 1024
    # Well under the 15s timeout - proves the child was never stalled
    # waiting on us to keep draining its stdout.
    assert elapsed < 10


def test_run_capturing_stdout_leaves_no_reader_thread_alive_after_normal_completion(tmp_path):
    assert _reader_threads_alive() == []

    run_capturing_stdout([sys.executable, "-c", "print('done')"], str(tmp_path), timeout_seconds=10)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _reader_threads_alive():
        time.sleep(0.05)
    assert _reader_threads_alive() == []


def test_run_capturing_stdout_leaves_no_reader_thread_alive_after_timeout(tmp_path):
    assert _reader_threads_alive() == []

    run_capturing_stdout(
        [sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path), timeout_seconds=0.5
    )

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _reader_threads_alive():
        time.sleep(0.05)
    assert _reader_threads_alive() == []


def test_run_capturing_stdout_honors_the_configured_cwd(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    result = run_capturing_stdout(
        [sys.executable, "-c", "import os; print(os.getcwd())"], str(workdir), timeout_seconds=10
    )

    assert result.success is True
    reported_cwd = Path(result.stdout.decode("utf-8").strip())
    assert reported_cwd.resolve() == workdir.resolve()


def test_run_capturing_stdout_kills_a_process_that_exceeds_its_timeout(tmp_path):
    started = time.monotonic()
    result = run_capturing_stdout(
        [sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path), timeout_seconds=0.5
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.success is False
    assert elapsed < 10


def test_run_capturing_stdout_kills_the_full_process_tree_not_just_the_parent(tmp_path):
    # Same shape as test_run_with_timeout_kills_the_full_process_tree_not_just_the_parent
    # above - proves run_capturing_stdout's timeout path reuses the same
    # full-tree kill, not just run_with_timeout's.
    child_marker = tmp_path / "child_pid.txt"
    parent_script = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open(r'{child_marker}', 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )

    result = run_capturing_stdout(
        [sys.executable, "-c", parent_script], str(tmp_path), timeout_seconds=1.0
    )

    assert result.timed_out is True

    child_pid = int(child_marker.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and psutil.pid_exists(child_pid):
        time.sleep(0.1)

    assert not psutil.pid_exists(child_pid)


def test_run_capturing_stdout_passes_through_a_custom_environment(tmp_path):
    env = os.environ.copy()
    env["AI_OS_TEST_MARKER"] = "present"

    result = run_capturing_stdout(
        [sys.executable, "-c", "import os; print(os.environ.get('AI_OS_TEST_MARKER', 'missing'))"],
        str(tmp_path),
        timeout_seconds=10,
        env=env,
    )

    assert result.stdout.strip() == b"present"
