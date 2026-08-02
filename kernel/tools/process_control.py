"""
Subprocess execution helpers shared by the open_application,
run_registered_script, and repo_health handlers.

Every call here is shell=False with a list-form argv and an explicit cwd,
both sourced entirely from local configuration (kernel/config/tools.yaml)
by the caller - nothing here accepts a raw string command, and nothing
here ever builds argv from a message sender's text.
"""

import subprocess
import threading

import psutil

_READER_THREAD_NAME = "process-control-stdout-reader"
_READER_CHUNK_SIZE = 65536


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


class CapturedResult:
    """Result of run_capturing_stdout(): whether the process exited zero
    within the timeout, plus its (bounded) stdout. stderr is never
    captured by that function - see its docstring - so there is nothing
    here to redact on the caller's behalf."""

    def __init__(
        self,
        success: bool,
        timed_out: bool,
        stdout: bytes | None = None,
        returncode: int | None = None,
    ):
        self.success = success
        self.timed_out = timed_out
        self.stdout = stdout
        self.returncode = returncode


def _drain_stdout(stream, max_output_bytes: int, buffer_holder: dict, done_event: threading.Event) -> None:
    """Runs on a dedicated background thread for the lifetime of one
    run_capturing_stdout() call: continuously reads the child's stdout so
    a chatty child can never block on a full pipe buffer, while never
    retaining more than max_output_bytes in memory - everything past that
    bound is read and immediately discarded, not buffered first and
    sliced afterward. Always sets done_event exactly once, even on a read
    error, so the caller's wait can never hang on this thread."""

    collected = bytearray()
    try:
        while True:
            chunk = stream.read(_READER_CHUNK_SIZE)
            if not chunk:
                break
            if len(collected) < max_output_bytes:
                collected.extend(chunk[: max_output_bytes - len(collected)])
    except (OSError, ValueError):
        pass
    finally:
        buffer_holder["data"] = bytes(collected)
        done_event.set()


def run_capturing_stdout(
    argv: list[str],
    cwd: str,
    timeout_seconds: float,
    env: dict | None = None,
    max_output_bytes: int = 4096,
) -> CapturedResult:
    """Run a process to completion, enforcing a hard timeout, and capture
    its stdout only, bounded to max_output_bytes at all times - a
    background reader thread drains the pipe continuously and discards
    anything past that bound as it arrives, so neither a slow consumer
    nor a multi-megabyte-or-larger child output can block the child or
    balloon this process's memory. stderr is always discarded (DEVNULL) -
    never captured, logged, or returned - since callers of this function
    (repo_health) only ever need a small, known stdout format from a
    trusted, fixed command. On timeout, kills the full process tree
    exactly like run_with_timeout does, then deterministically joins the
    reader thread and closes the pipe before returning - no reader thread
    or open handle survives this call.

    env=None inherits this process's environment unchanged, matching
    subprocess.Popen's own default. A caller that needs to add or
    override a variable must pass a full environment mapping (e.g. built
    from os.environ.copy()) - this function never fabricates a minimal
    environment of its own.
    """

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return CapturedResult(success=False, timed_out=False)

    buffer_holder: dict = {"data": b""}
    done_event = threading.Event()
    reader = threading.Thread(
        target=_drain_stdout,
        args=(proc.stdout, max_output_bytes, buffer_holder, done_event),
        name=_READER_THREAD_NAME,
        daemon=True,
    )
    reader.start()

    timed_out = False
    returncode = None
    try:
        returncode = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc.pid)
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            returncode = None
    finally:
        # The child's stdout pipe closes once it exits or is killed, so
        # the reader thread reaches EOF and sets done_event shortly after
        # either branch above returns - bounded waits here, not a bare
        # join(), so this function can never hang forever on a stuck
        # reader.
        done_event.wait(timeout=5)
        reader.join(timeout=5)
        try:
            proc.stdout.close()
        except OSError:
            pass

    if timed_out:
        return CapturedResult(success=False, timed_out=True, returncode=returncode)

    return CapturedResult(
        success=(returncode == 0),
        timed_out=False,
        stdout=buffer_holder["data"],
        returncode=returncode,
    )


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
