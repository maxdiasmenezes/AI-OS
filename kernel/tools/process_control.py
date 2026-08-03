"""
Subprocess execution helpers shared by the open_application,
run_registered_script, repo_health, and repository_backup handlers.

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
_KILL_TREE_WAIT_SECONDS = 5
_FINAL_REAP_WAIT_SECONDS = 5


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
    spawned, not just the immediate child - and does not return until
    every one of them has actually been reaped (see
    _terminate_and_reap())."""

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
        _terminate_and_reap(proc)
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
    exactly like run_with_timeout does and does not return until every
    process in it has actually been reaped (see _terminate_and_reap()),
    then deterministically joins the reader thread and closes the pipe
    before returning - no reader thread, running process, or open handle
    survives this call.

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
        _terminate_and_reap(proc)
        returncode = proc.returncode
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


def run_streaming_stdout_to_file(
    argv: list[str],
    cwd: str,
    timeout_seconds: float,
    output_file,
    env: dict | None = None,
) -> RunResult:
    """Run a process to completion, enforcing a hard timeout, with its
    stdout connected *directly* to `output_file` - an already-open,
    writable, binary file object the caller owns - via a real OS-level
    pipe/handle. Used by repository_backup, whose whole reason for
    existing is to never hold a multi-megabyte-or-larger git bundle in
    this process's memory: subprocess.Popen(..., stdout=output_file)
    hands the child the file's own file descriptor, so every byte the
    child writes goes straight to disk without ever passing through a
    pipe this process reads from, unlike run_capturing_stdout's
    (deliberately bounded, in-memory) capture.

    This function never writes to output_file itself and never closes
    it, seeks it, or otherwise touches its position - the caller retains
    full ownership of its lifecycle (flushing, fsync, closing) before and
    after this call. stdin is always DEVNULL and stderr is always
    DEVNULL - never captured, logged, or returned, matching every other
    helper in this module.

    On timeout, kills the full process tree exactly like run_with_timeout
    and run_capturing_stdout do, and does not return timed_out=True until
    every process in that tree has actually been reaped (see
    _terminate_and_reap()) - so a caller can trust that nothing is still
    capable of writing to output_file by the time this function returns.
    This function never closes output_file itself, on any path, including
    the timeout path.

    Returns only success/timed_out/returncode - there is no stdout to
    return here, by design.
    """

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=output_file,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return RunResult(success=False, timed_out=False)

    try:
        returncode = proc.wait(timeout=timeout_seconds)
        return RunResult(success=(returncode == 0), timed_out=False, returncode=returncode)
    except subprocess.TimeoutExpired:
        _terminate_and_reap(proc)
        return RunResult(success=False, timed_out=True)


def _kill_process_tree(pid: int) -> list:
    """Kills the process tree rooted at `pid` - the process itself and
    every descendant psutil can discover - and waits up to
    _KILL_TREE_WAIT_SECONDS for all of them to actually exit. Returns the
    list of descendant psutil.Process objects found (possibly empty), so
    a caller that needs a final confirmation pass (_terminate_and_reap())
    knows exactly which processes to re-check without re-discovering the
    tree from scratch - by the time it would do that, the parent may
    already be gone and parent.children() would find nothing."""

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []

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

    psutil.wait_procs(children + [parent], timeout=_KILL_TREE_WAIT_SECONDS)
    return children


def _is_active_writer(process: psutil.Process) -> bool:
    """True only if `process` is still capable of writing anything: it is
    running *and* not a zombie. A zombie has already exited - it is only
    waiting for its own parent to reap it and can never write another
    byte - so treating one as "still needs killing" would be pointless
    and would make a caller wait on a condition that can never resolve by
    itself. psutil.NoSuchProcess (the process is entirely gone) is always
    treated as "not an active writer", never propagated from here."""

    try:
        if not process.is_running():
            return False
        return process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _reap_descendant(descendant: psutil.Process) -> None:
    """Ensures one specific, already-known descendant is gone or a
    non-running zombie before returning. Kills it (if it's still an
    active writer) and then blocks - no timeout - until psutil confirms
    it is no longer running, so this never returns while that specific
    process might still be writing. psutil.NoSuchProcess is tolerated at
    any point (the process disappearing on its own between checks, e.g.
    between the kill() call and the wait() call) - handled safely rather
    than propagated. Any other exception raised while the process is
    still confirmed active is never silently swallowed."""

    if not _is_active_writer(descendant):
        return
    try:
        descendant.kill()
    except psutil.NoSuchProcess:
        return
    try:
        descendant.wait()
    except psutil.NoSuchProcess:
        return


def _reap_all_descendants(descendants: list) -> None:
    """Ensures every process in `descendants` (as originally discovered by
    _kill_process_tree()) is gone or a non-running zombie before
    returning - the second half of _terminate_and_reap()'s contract.

    1. Kills every descendant still an active writer right now.
    2. Calls psutil.wait_procs() on exactly those and inspects *both*
       returned lists - `alive` is never discarded. Anything still in
       `alive` gets a direct, individual kill-then-blocking-wait via
       _reap_descendant(), one at a time, so this can never silently
       return while a known descendant psutil still reports as running.
    3. A final verification pass re-checks every *originally* known
       descendant (not just the ones that were in `alive`) - anything
       still an active writer at this point is resolved the same way,
       rather than being logged, raised on, or accepted as-is. This
       function only returns once every originally known descendant is
       confirmed gone or a non-running zombie.
    """

    active = [d for d in descendants if _is_active_writer(d)]
    for descendant in active:
        try:
            descendant.kill()
        except psutil.NoSuchProcess:
            continue

    if active:
        _gone, alive = psutil.wait_procs(active, timeout=_FINAL_REAP_WAIT_SECONDS)
        for descendant in alive:
            _reap_descendant(descendant)

    for descendant in descendants:
        if _is_active_writer(descendant):
            _reap_descendant(descendant)


def _terminate_and_reap(proc: subprocess.Popen) -> None:
    """Ensures `proc` - and every descendant _kill_process_tree() can
    discover - is no longer running before returning. Never returns while
    proc.poll() is still None, or while a known descendant could still be
    writing anything. Shared by every timeout branch in this module so a
    caller can trust that timed_out=True means the process (and anything
    it might still be writing to, e.g. a caller-owned output file passed
    to run_streaming_stdout_to_file()) has actually stopped, not merely
    that a kill signal was sent at some point.

    1. _kill_process_tree(proc.pid) - the existing full-tree termination
       (parent + every descendant psutil can discover), which already
       waits up to _KILL_TREE_WAIT_SECONDS via psutil.wait_procs().
    2. A second, bounded wait (via the subprocess module's own proc.wait,
       not psutil) for the parent to exit - needed so proc.returncode is
       actually set through Python's own process-reaping bookkeeping, not
       only observed as "gone" from the OS's process table by psutil.
    3. If the parent is still alive after that wait, proc.kill() as a
       direct fallback, independent of the psutil-based kill above.
       ProcessLookupError/OSError from that call is tolerated only when
       the process has, in the meantime, already exited (proc.poll() is
       no longer None) - a real failure while the process is still
       running is never silently swallowed.
    4. proc.wait() unconditionally after the fallback kill, with no
       timeout - blocks until the parent is actually reaped rather than
       returning while it might still be alive.
    5. _reap_all_descendants() - a confirmation pass over every
       descendant _kill_process_tree() found that never discards
       psutil.wait_procs()'s `alive` result and never returns while one
       of those known descendants is still an active writer.
    """

    descendants = _kill_process_tree(proc.pid)

    try:
        proc.wait(timeout=_KILL_TREE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            if proc.poll() is None:
                raise
        proc.wait()

    _reap_all_descendants(descendants)
