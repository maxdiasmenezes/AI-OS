"""
Subprocess execution helpers shared by the open_application and
run_registered_script handlers.

Every call here is shell=False with a list-form argv and an explicit cwd,
both sourced entirely from local configuration (kernel/config/tools.yaml)
by the caller - nothing here accepts a raw string command, and nothing
here ever builds argv from a message sender's text.
"""

import subprocess

import psutil


class LaunchResult:
    """Result of launch_detached(): whether the process actually started -
    nothing about whether or how it later exits."""

    def __init__(self, success: bool, pid: int | None = None):
        self.success = success
        self.pid = pid


class RunResult:
    """Result of run_with_timeout(): whether the process ran to completion
    with a zero exit code, or was killed for exceeding its timeout."""

    def __init__(self, success: bool, timed_out: bool, returncode: int | None = None):
        self.success = success
        self.timed_out = timed_out
        self.returncode = returncode


def launch_detached(argv: list[str], cwd: str) -> LaunchResult:
    """Start a process and return immediately - never waits for it to
    exit. Used by open_application, which must return after a successful
    launch rather than waiting for the application to close."""

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return LaunchResult(success=False)
    return LaunchResult(success=True, pid=proc.pid)


def run_with_timeout(argv: list[str], cwd: str, timeout_seconds: float) -> RunResult:
    """Run a process to completion, enforcing a hard timeout. On timeout,
    kills the full process tree - the process and every descendant it
    spawned - not just the immediate child, using psutil."""

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return RunResult(success=False, timed_out=False)

    try:
        returncode = proc.wait(timeout=timeout_seconds)
        return RunResult(success=(returncode == 0), timed_out=False, returncode=returncode)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return RunResult(success=False, timed_out=True)


def _kill_process_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    try:
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []

    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass

    try:
        parent.kill()
    except psutil.NoSuchProcess:
        pass

    psutil.wait_procs(children + [parent], timeout=5)
