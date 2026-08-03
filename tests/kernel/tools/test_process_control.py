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
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from kernel.tools.process_control import (
    _reap_all_descendants,
    _reap_descendant,
    _terminate_and_reap,
    launch_detached,
    run_capturing_stdout,
    run_streaming_stdout_to_file,
    run_with_timeout,
)

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


# --- run_streaming_stdout_to_file (Milestone 35: repository_backup) ---


def test_run_streaming_stdout_to_file_writes_child_stdout_directly_to_the_file(tmp_path):
    output_path = tmp_path / "out.bin"
    with open(output_path, "wb") as f:
        result = run_streaming_stdout_to_file(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'hello world')"],
            str(tmp_path),
            timeout_seconds=10,
            output_file=f,
        )
        f.flush()

    assert result.success is True
    assert result.timed_out is False
    assert result.returncode == 0
    assert output_path.read_bytes() == b"hello world"


def test_run_streaming_stdout_to_file_reports_failure_for_a_nonzero_exit_process(tmp_path):
    output_path = tmp_path / "out.bin"
    with open(output_path, "wb") as f:
        result = run_streaming_stdout_to_file(
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            str(tmp_path),
            timeout_seconds=10,
            output_file=f,
        )

    assert result.success is False
    assert result.timed_out is False
    assert result.returncode == 1


def test_run_streaming_stdout_to_file_reports_failure_for_a_nonexistent_executable(tmp_path):
    output_path = tmp_path / "out.bin"
    with open(output_path, "wb") as f:
        result = run_streaming_stdout_to_file(
            [str(tmp_path / "does_not_exist.exe")],
            str(tmp_path),
            timeout_seconds=10,
            output_file=f,
        )

    assert result.success is False
    assert result.timed_out is False


def test_run_streaming_stdout_to_file_never_closes_the_callers_file_handle(tmp_path):
    output_path = tmp_path / "out.bin"
    f = open(output_path, "wb")
    try:
        run_streaming_stdout_to_file(
            [sys.executable, "-c", "print('done')"],
            str(tmp_path),
            timeout_seconds=10,
            output_file=f,
        )
        # Still usable by the caller after the call returns - proves this
        # function never closed the caller-owned file object.
        assert f.closed is False
        f.write(b"caller-owned-write-after-return")
        f.flush()
    finally:
        f.close()

    assert output_path.read_bytes().endswith(b"caller-owned-write-after-return")


def test_run_streaming_stdout_to_file_never_buffers_the_payload_in_memory(tmp_path):
    # Writes far more than any reasonable in-memory bound this process
    # would want to hold for a bundle payload, and asserts the resulting
    # file is complete and correctly sized - proving the data flowed
    # straight from the child's stdout handle to disk via the OS, never
    # through a Python-level buffer this function reads and re-writes.
    output_path = tmp_path / "large.bin"
    script = (
        "import sys\n"
        "for _ in range(50):\n"
        "    sys.stdout.buffer.write(b'x' * 100000)\n"  # 5,000,000 bytes
        "sys.stdout.flush()\n"
    )
    with open(output_path, "wb") as f:
        result = run_streaming_stdout_to_file(
            [sys.executable, "-c", script], str(tmp_path), timeout_seconds=15, output_file=f
        )
        f.flush()

    assert result.success is True
    assert output_path.stat().st_size == 5_000_000


def test_run_streaming_stdout_to_file_kills_a_process_that_exceeds_its_timeout(tmp_path):
    output_path = tmp_path / "out.bin"
    started = time.monotonic()
    with open(output_path, "wb") as f:
        result = run_streaming_stdout_to_file(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            str(tmp_path),
            timeout_seconds=0.5,
            output_file=f,
        )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.success is False
    assert elapsed < 10


def test_run_streaming_stdout_to_file_kills_the_full_process_tree_not_just_the_parent(tmp_path):
    child_marker = tmp_path / "child_pid.txt"
    parent_script = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open(r'{child_marker}', 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )
    output_path = tmp_path / "out.bin"
    with open(output_path, "wb") as f:
        result = run_streaming_stdout_to_file(
            [sys.executable, "-c", parent_script], str(tmp_path), timeout_seconds=1.0, output_file=f
        )

    assert result.timed_out is True

    child_pid = int(child_marker.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and psutil.pid_exists(child_pid):
        time.sleep(0.1)

    assert not psutil.pid_exists(child_pid)


def test_run_streaming_stdout_to_file_passes_through_a_custom_environment(tmp_path):
    env = os.environ.copy()
    env["AI_OS_TEST_MARKER"] = "present"
    output_path = tmp_path / "out.bin"

    with open(output_path, "wb") as f:
        run_streaming_stdout_to_file(
            [sys.executable, "-c", "import os,sys; sys.stdout.write(os.environ.get('AI_OS_TEST_MARKER', 'missing'))"],
            str(tmp_path),
            timeout_seconds=10,
            output_file=f,
            env=env,
        )

    assert output_path.read_text() == "present"


def test_run_streaming_stdout_to_file_uses_shell_false_and_list_argv(tmp_path):
    # A shell metacharacter in a single argv element must never be
    # interpreted - it should be passed to the child literally (here, as
    # a nonexistent filename argument) rather than executed.
    output_path = tmp_path / "out.bin"
    with open(output_path, "wb") as f:
        result = run_streaming_stdout_to_file(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", "$(echo injected)"],
            str(tmp_path),
            timeout_seconds=10,
            output_file=f,
        )
        f.flush()

    assert result.success is True
    assert output_path.read_text().strip() == "$(echo injected)"


# --- _terminate_and_reap() correctness (Milestone 35 fix: never return
# while the timed-out process remains alive) ---


def test_continuously_writing_process_is_timed_out_and_stops_writing(tmp_path):
    # Points 1-4 and 6 of the required regression coverage: a process that
    # never stops writing to stdout on its own is timed out, its activity
    # has genuinely stopped by the time the helper returns (not just "a
    # kill signal was sent at some point"), the output file's size is
    # stable afterward, and the caller-owned handle is still usable.
    output_path = tmp_path / "out.bin"
    parent_marker = tmp_path / "parent_pid.txt"
    script = (
        "import os, sys, time\n"
        f"open(r'{parent_marker}', 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'x' * 4096)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.01)\n"
    )

    with open(output_path, "wb") as f:
        result = run_streaming_stdout_to_file(
            [sys.executable, "-c", script], str(tmp_path), timeout_seconds=1.0, output_file=f
        )
        f.flush()
        size_immediately_after_return = output_path.stat().st_size

        assert result.timed_out is True
        assert result.success is False

        # The immediate child no longer exists - proc activity has
        # stopped, not merely been signaled.
        parent_pid = int(parent_marker.read_text().strip())
        assert not psutil.pid_exists(parent_pid)

        # No further bytes land in the file after the helper returns.
        time.sleep(1.2)
        assert output_path.stat().st_size == size_immediately_after_return

        # The caller-owned handle is still open and usable.
        assert f.closed is False
        f.write(b"still-usable-after-timeout")
        f.flush()

    assert output_path.read_bytes().endswith(b"still-usable-after-timeout")


def test_spawned_descendant_no_longer_exists_after_timeout(tmp_path):
    # Point 5: a descendant the timed-out process spawned (not just the
    # immediate child) is also confirmed gone, not merely signaled.
    output_path = tmp_path / "out.bin"
    child_marker = tmp_path / "child_pid.txt"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open(r'{child_marker}', 'w').write(str(child.pid))\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'x')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.01)\n"
    )

    with open(output_path, "wb") as f:
        result = run_streaming_stdout_to_file(
            [sys.executable, "-c", script], str(tmp_path), timeout_seconds=1.5, output_file=f
        )

    assert result.timed_out is True
    child_pid = int(child_marker.read_text().strip())
    assert not psutil.pid_exists(child_pid)


def test_terminate_and_reap_falls_back_to_direct_kill_when_first_wait_times_out(tmp_path):
    # Points 7-9: simulate the post-tree-kill proc.wait() itself timing
    # out on its first call (a real-world case the tree-kill path alone
    # cannot always guarantee against, e.g. OS scheduling delay) - proves
    # _terminate_and_reap() falls back to a direct proc.kill(), then
    # blocks until the process is actually reaped rather than returning
    # early. proc.poll() being non-None afterward is the cross-platform
    # proxy for "no zombie left behind": on POSIX it means waitpid()
    # actually reaped the child; on any platform it means this process's
    # own bookkeeping considers it finished.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    real_wait = proc.wait
    calls = {"n": 0}

    def fake_wait(timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return real_wait(timeout=timeout)

    proc.wait = fake_wait

    _terminate_and_reap(proc)

    # The simulated timeout on the first wait() forced the proc.kill()
    # fallback branch to run, which then called wait() again to reap.
    assert calls["n"] >= 2
    assert proc.poll() is not None
    assert not psutil.pid_exists(proc.pid)


def test_terminate_and_reap_returns_only_after_the_process_is_gone(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    _terminate_and_reap(proc)

    assert proc.poll() is not None
    assert not psutil.pid_exists(proc.pid)


# --- Descendant reaping correctness (Milestone 35 second fix: the
# `alive` list from psutil.wait_procs() must never be discarded) ---
#
# These use a controlled fake psutil.Process double rather than real
# subprocesses, so each exceptional branch (a zombie, a PID disappearing
# mid-sequence, a genuine non-NoSuchProcess error) is deterministic and
# fast - psutil.Process itself cannot be told to reliably reproduce these
# exact conditions on demand. wait() is what actually resolves the fake
# (is_running() becomes False only once wait() has been called enough
# times), mirroring the real psutil.Process.wait() contract: it blocks
# until the process is confirmed gone, so a caller normally only needs
# one kill()+wait() to fully resolve a process - kill() alone never
# confirms anything.


class _FakeDescendant:
    def __init__(
        self,
        pid,
        status=None,
        resolve_after_waits=1,
        wait_exc=None,
        kill_exc=None,
        is_running_exc=None,
    ):
        self.pid = pid
        self.kill_calls = 0
        self.wait_calls = 0
        self._status = status or psutil.STATUS_RUNNING
        self._resolve_after_waits = resolve_after_waits
        self._wait_exc = wait_exc
        self._kill_exc = kill_exc
        self._is_running_exc = is_running_exc

    def is_running(self):
        if self._is_running_exc is not None:
            raise self._is_running_exc
        return self.wait_calls < self._resolve_after_waits

    def status(self):
        return self._status

    def kill(self):
        self.kill_calls += 1
        if self._kill_exc is not None:
            raise self._kill_exc

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._wait_exc is not None:
            raise self._wait_exc


def test_reap_descendant_kills_and_blocks_until_confirmed_gone():
    descendant = _FakeDescendant(pid=9001)

    _reap_descendant(descendant)

    assert descendant.kill_calls == 1
    assert descendant.wait_calls == 1
    assert descendant.is_running() is False


def test_reap_descendant_skips_a_zombie_without_killing_it():
    # A zombie is not "still capable of writing anything" - it already
    # exited and is only waiting to be reaped by its own parent, so
    # killing it again would be pointless.
    zombie = _FakeDescendant(pid=9002, status=psutil.STATUS_ZOMBIE)

    _reap_descendant(zombie)

    assert zombie.kill_calls == 0
    assert zombie.wait_calls == 0


def test_reap_descendant_handles_kill_raising_no_such_process():
    vanished = _FakeDescendant(pid=9003, kill_exc=psutil.NoSuchProcess(9003))

    _reap_descendant(vanished)  # must not raise

    assert vanished.kill_calls == 1
    assert vanished.wait_calls == 0


def test_reap_descendant_handles_pid_disappearing_between_kill_and_wait():
    vanishing = _FakeDescendant(pid=9004, wait_exc=psutil.NoSuchProcess(9004))

    _reap_descendant(vanishing)  # must not raise

    assert vanishing.kill_calls == 1
    assert vanishing.wait_calls == 1


def test_reap_descendant_does_not_swallow_a_real_error_while_still_running():
    # psutil.AccessDenied (not NoSuchProcess) while the process is still
    # actively running is a genuine failure - it must propagate, never be
    # silently swallowed the way a disappeared PID is.
    stubborn = _FakeDescendant(pid=9005, kill_exc=psutil.AccessDenied(9005))

    with pytest.raises(psutil.AccessDenied):
        _reap_descendant(stubborn)


def test_reap_all_descendants_handles_a_nonempty_alive_list_from_wait_procs(monkeypatch):
    # A descendant that comes back in `alive` (not `gone`) from
    # psutil.wait_procs() must receive another direct kill and be waited
    # for individually, proving `alive` is inspected rather than
    # discarded.
    survivor = _FakeDescendant(pid=9006)

    def fake_wait_procs(procs, timeout=None):
        return [], list(procs)  # everything comes back "alive"

    monkeypatch.setattr(psutil, "wait_procs", fake_wait_procs)

    _reap_all_descendants([survivor])

    # One kill from the initial bulk pass, one more from handling the
    # `alive` list.
    assert survivor.kill_calls >= 2
    assert survivor.wait_calls >= 1
    assert survivor.is_running() is False


def test_reap_all_descendants_final_verification_pass_resolves_a_straggler(monkeypatch):
    # Needs two wait() calls to actually resolve - the alive-list
    # handling pass supplies the first (not enough on its own), so only
    # the final verification pass's extra kill+wait resolves it. Proves
    # that pass does real work rather than being a no-op re-check.
    straggler = _FakeDescendant(pid=9007, resolve_after_waits=2)

    def fake_wait_procs(procs, timeout=None):
        return [], list(procs)

    monkeypatch.setattr(psutil, "wait_procs", fake_wait_procs)

    _reap_all_descendants([straggler])

    assert straggler.wait_calls == 2
    assert straggler.kill_calls == 3
    assert straggler.is_running() is False


def test_reap_all_descendants_does_not_return_while_a_mocked_descendant_reports_running(
    monkeypatch,
):
    descendant = _FakeDescendant(pid=9008)

    def fake_wait_procs(procs, timeout=None):
        return [], list(procs)

    monkeypatch.setattr(psutil, "wait_procs", fake_wait_procs)

    _reap_all_descendants([descendant])

    # By the time the helper has returned, the known descendant must no
    # longer report itself as running.
    assert descendant.is_running() is False
    assert descendant.wait_calls >= 1
