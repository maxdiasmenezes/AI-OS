"""
WhatsApp interface composition root and HTTP server.

Wires WhatsApp-specific configuration, the kernel Orchestrator (scoped to
a fixed memory namespace via FixedNamespaceMemory), a Cloud API client,
and a message handler together, then exposes them over a loopback-only,
standard-library HTTP server. Exactly two endpoints exist - GET /webhook
for Meta's verification handshake, POST /webhook for inbound message
delivery - every other path is 404.

Destination authorization, sender authorization, and deduplication all
happen synchronously inside POST /webhook, before a message is ever
queued: the worker thread (interfaces/whatsapp/handler.py) receives only
pre-authorized, already-deduplicated tasks and performs neither. See
"HTTP POST processing order" below for the exact sequence and its status
codes.

HTTP POST processing order, per parsed message:
 1. verify the raw-body signature (once, for the whole request)
 2. parse the JSON payload (once, for the whole request)
 3. validate the destination phone_number_id
 4. validate the exact authorized sender ID
 5. reserve the message ID in the dedup cache
 6. classify the message into a work Task (text, or a fixed reply)
 7. submit that Task to the bounded queue

An unauthorized destination or sender is dropped before dedup is ever
consulted - it never occupies a dedup slot. A duplicate message ID is
dropped before a Task is ever created - it never reaches the queue or the
orchestrator. Either way the request still gets HTTP 200: from Meta's
perspective the delivery succeeded, since retrying would not change the
outcome.

Queue-full handling: the message ID is reserved *before* the queue
insertion is attempted, so a full queue means the reservation must be
released (SeenMessageCache.discard) rather than left stuck - otherwise a
later Meta redelivery of that same ID would be mistaken for a duplicate
and silently dropped forever. When queueing fails, this handler stops
processing the rest of the batch immediately (deterministic, so dedup
stays correct: messages already queued earlier in the same batch keep
their reservation and won't be reprocessed on redelivery; the message
that overflowed, and anything after it, were never reserved or were just
released, so a full redelivery of the batch reprocesses them correctly)
and responds HTTP 503 for the whole request - never 200 - so Meta knows
to retry.

Milestone 46 P1 - durable "/task <request>" ingress: a message classified
as interfaces.whatsapp.task_control.TaskRequestText (see
handler.py:classify_message()) never touches SeenMessageCache at all -
step 5 above applies only to ordinary conversational messages. Instead,
durable acceptance (interfaces.whatsapp.task_control.accept_task_message())
happens synchronously, right here in do_POST, before this handler can ever
respond with success for that message: the SQLite UNIQUE constraint on
kernel.employee_tasks' tasks.dedup_key - not this in-memory cache - is the
authoritative concurrent-dedup boundary for /task messages (see
task_control.py's own module docstring, and the Milestone 46 P1 design
report's empirical concurrency validation). A storage failure or a full
queue when a newly-CREATED task needs dispatch both produce the same
outcome as an ordinary queue-full: HTTP 503 for the whole request, the
durable row (if one exists) is never deleted or rolled back, and the rest
of the batch stops being processed - so a Meta retry resolves the same row
via DuplicateTaskError rather than creating a second one. P1 only ever
hands a bare task_id (never raw request text, the provider message ID, or
anything else) to the worker queue as a TaskExecutionWork - see handler.py
and task_control.py for the CREATED -> planning handoff this performs. P1
sends no outbound WhatsApp message of its own for a /task submission.

Milestone 46 P2A extends the same TaskExecutionWork dispatch (still queued
only here, still carrying only a bare task_id) to drive the task all the
way through the existing bounded execution runner
(kernel.task_execution.run_task_until_blocked()) and deliver exactly one
WhatsApp lifecycle message per newly-produced lifecycle event - a planning
failure, a terminal result/failure, or a confirmation request when a
sensitive step blocks. All of that still happens exclusively on the
background worker thread, never here in do_POST - see
task_control.py:dispatch_task_work()'s own docstring for the exact
transition-triggered delivery rule and the full execution/delivery
boundary.

Milestone 46 P2B: a "CONFIRM <id>"/"REJECT <id>" message needs NO new
handling in do_POST at all - interfaces.whatsapp.handler.classify_message()
now recognizes it and returns either a FixedReplyTask (a malformed command
shape) or a task_control.TaskConfirmationWork, and both fall through this
function's EXISTING generic-message branch below (SeenMessageCache
dedup, then work_queue.put_nowait()) exactly like ordinary chat always
has - TaskConfirmationWork is opaque to do_POST, which only ever
special-cases TaskRequestText above. Resolving, authorizing, approving,
denying, and delivering a confirmation decision all happen exclusively on
the worker thread, via task_control.py:dispatch_confirmation_work()'s own
docstring, for the same reason execution does: approve_task_confirmation()
may run a sensitive action synchronously and must never be reachable from
this request thread. HTTP 200 for a CONFIRM/REJECT message means the
command was accepted onto the in-memory worker queue - not a durable-
decision acknowledgement; see task_control.py's own module docstring for
why this is a deliberately accepted, documented boundary.
"""

import hashlib
import hmac
import json
import logging
import queue
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from capabilities.loader import CapabilityLoader
from kernel.config.config import Config, load_config
from kernel.employee_tasks import (
    TaskRepository,
    acquire_runtime_ownership,
    open_writer_connection,
    resolve_database_path,
)
from kernel.memory import MemoryManager
from kernel.models import get_planner_provider, get_provider
from kernel.orchestrator import Orchestrator
from kernel.task_planner import build_catalog
from kernel.tools import ActionRegistry, load_tools_config

from interfaces.whatsapp.client import WhatsAppClient
from interfaces.whatsapp.config import WhatsAppConfig, load_whatsapp_config
from interfaces.whatsapp.dedup import SeenMessageCache
from interfaces.whatsapp.handler import MessageHandler, classify_message
from interfaces.whatsapp.memory import FixedNamespaceMemory
from interfaces.whatsapp.payload import parse_webhook_payload
from interfaces.whatsapp.signature import verify_signature
from interfaces.whatsapp.task_control import (
    DurableAcceptanceFailed,
    TaskExecutionWork,
    TaskRequestText,
    accept_task_message,
    needs_dispatch,
)

logger = logging.getLogger(__name__)

_WEBHOOK_PATH = "/webhook"

# AI-OS application limits - not claims about any Meta platform limit.
# All are constructor parameters so tests can inject different values.
DEFAULT_MAX_BODY_BYTES = 1_000_000
DEFAULT_QUEUE_CAPACITY = 16
# Milestone 47 P1: how often the worker loop guarantees itself one bounded
# recovery checkpoint (see WhatsAppServer._run_worker()'s own docstring) -
# an application-level cadence choice, not a claim about any external
# system's own timing.
DEFAULT_RECOVERY_INTERVAL_SECONDS = 60.0

_WORKER_JOIN_TIMEOUT_SECONDS = 5.0

_STOP = object()  # sentinel used to unblock the worker's queue.get() on shutdown


def build_orchestrator(config: Config, capability_loader) -> Orchestrator:
    """Build the Orchestrator, scoping all memory through one fixed WhatsApp namespace."""

    real_memory = MemoryManager(config.memory_settings)
    scoped_memory = FixedNamespaceMemory(real_memory)

    return Orchestrator(
        config,
        capability_loader=capability_loader,
        memory_manager=scoped_memory,
    )


def _message_reference(message_id: str) -> str:
    """A short, non-reversible reference for log correlation - never the raw ID."""

    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:12]


def _make_handler_class(
    whatsapp_config: WhatsAppConfig,
    work_queue: "queue.Queue",
    dedup: SeenMessageCache,
    max_body_bytes: int,
    task_db_path=None,
):
    """Build a BaseHTTPRequestHandler subclass closed over this server's dependencies."""

    class WebhookRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args) -> None:
            # Privacy-safe: only method + path, with the query string (which
            # can carry hub.verify_token) stripped - never the raw request
            # line the default implementation would log.
            safe_path = urllib.parse.urlparse(self.path).path
            logger.info("%s %s", self.command, safe_path)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != _WEBHOOK_PATH:
                self._respond_empty(404)
                return

            query = urllib.parse.parse_qs(parsed.query)
            mode = query.get("hub.mode", [None])[0]
            token = query.get("hub.verify_token", [None])[0]
            challenge = query.get("hub.challenge", [None])[0]

            # Constant-time comparison, and only attempted once a token was
            # actually supplied - hmac.compare_digest requires two
            # same-typed arguments and would raise on None.
            token_matches = token is not None and hmac.compare_digest(
                token, whatsapp_config.verify_token
            )

            if mode == "subscribe" and challenge and token_matches:
                body = challenge.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                logger.warning("rejecting webhook verification request")
                self._respond_empty(403)

        def do_POST(self) -> None:
            if urllib.parse.urlparse(self.path).path != _WEBHOOK_PATH:
                self._drain_request_body()
                self._respond_empty(404)
                return

            content_length_header = self.headers.get("Content-Length")
            if content_length_header is None:
                self._respond_empty(411)
                return

            try:
                content_length = int(content_length_header)
            except ValueError:
                self._respond_empty(400)
                return

            if content_length < 0:
                self._respond_empty(400)
                return

            if content_length > max_body_bytes:
                # Rejected on the declared length alone - the body is never
                # read off the socket.
                self._respond_empty(413)
                return

            raw_body = self.rfile.read(content_length)
            if len(raw_body) != content_length:
                self._respond_empty(400)
                return

            signature_header = self.headers.get("X-Hub-Signature-256")
            if not verify_signature(whatsapp_config.app_secret, raw_body, signature_header):
                logger.warning("rejecting webhook POST with invalid signature")
                self._respond_empty(403)
                return

            try:
                parsed_body = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("rejecting webhook POST with malformed JSON body")
                self._respond_empty(400)
                return

            queue_overflowed = False
            for message in parse_webhook_payload(parsed_body):
                if queue_overflowed:
                    break

                if message.phone_number_id != whatsapp_config.phone_number_id:
                    logger.info("dropping message: unauthorized destination")
                    continue

                if message.sender != whatsapp_config.authorized_sender_id:
                    logger.info("dropping message: unauthorized sender")
                    continue

                classification = classify_message(message)

                if isinstance(classification, TaskRequestText):
                    # Milestone 46 P1: SeenMessageCache is never consulted
                    # for a /task message - see this module's own module
                    # docstring. task_db_path is None only if this server
                    # was built without durable-ingress wiring (e.g. an
                    # existing pre-Milestone-46 test construction) - never
                    # silently accepted as success in that case either.
                    if task_db_path is None:
                        logger.warning("rejecting request: task ingress unavailable")
                        queue_overflowed = True
                        continue

                    try:
                        task_record = accept_task_message(
                            open_writer_connection,
                            task_db_path,
                            classification.request_text,
                            message.message_id,
                        )
                    except DurableAcceptanceFailed:
                        logger.warning(
                            "rejecting request: task ingress storage failure (ref=%s)",
                            _message_reference(message.message_id),
                        )
                        queue_overflowed = True
                        continue

                    if needs_dispatch(task_record):
                        try:
                            work_queue.put_nowait(TaskExecutionWork(task_record.task_id))
                        except queue.Full:
                            logger.warning(
                                "rejecting request: queue full (ref=%s)",
                                _message_reference(message.message_id),
                            )
                            queue_overflowed = True
                    continue

                if not dedup.add_if_new(message.message_id):
                    logger.info(
                        "dropping message: duplicate (ref=%s)",
                        _message_reference(message.message_id),
                    )
                    continue

                try:
                    work_queue.put_nowait(classification)
                except queue.Full:
                    dedup.discard(message.message_id)
                    logger.warning(
                        "rejecting request: queue full (ref=%s)",
                        _message_reference(message.message_id),
                    )
                    queue_overflowed = True

            self._respond_empty(503 if queue_overflowed else 200)

        def do_PUT(self) -> None:
            self._handle_unsupported_method()

        def do_DELETE(self) -> None:
            self._handle_unsupported_method()

        def do_PATCH(self) -> None:
            self._handle_unsupported_method()

        def do_HEAD(self) -> None:
            self._handle_unsupported_method()

        def do_OPTIONS(self) -> None:
            self._handle_unsupported_method()

        def _handle_unsupported_method(self) -> None:
            self._drain_request_body()
            parsed_path = urllib.parse.urlparse(self.path).path
            self._respond_empty(405 if parsed_path == _WEBHOOK_PATH else 404)

        def _drain_request_body(self) -> None:
            """Reads and discards any request body the client already
            declared via Content-Length, bounded to max_body_bytes, before
            this handler responds and the connection is torn down.

            Every response path here that has already read the declared
            body (successfully or not) is unaffected by this; this exists
            for the early-return paths that respond without ever touching
            the body at all (a POST/PUT/DELETE/PATCH to the wrong path, or
            an unsupported method on /webhook). Leaving a body the client
            already sent unread when the connection subsequently closes
            has a real, reproducible failure mode: TCP sends a RST instead
            of a clean FIN once a socket is closed with unread bytes still
            sitting in its receive buffer, which the client observes as
            ConnectionAbortedError/ConnectionResetError rather than
            receiving the response that was actually sent - the race is
            timing-dependent (worse under scheduler/thread contention),
            not merely a test artifact, so this is a real production
            correctness fix, not test-only cleanup.

            Deliberately bounded to max_body_bytes (matching the size cap
            already enforced for a normal /webhook POST) rather than
            draining an arbitrary declared length - never reads more than
            that regardless of what Content-Length claims, so this cannot
            be used to force a large read. This is separate from, and does
            not change, the deliberate choice in do_POST() to never read
            an oversized declared body before responding 413 - that
            early-reject-on-the-declared-length-alone behavior stays
            exactly as it was.

            Best effort only: a missing, non-integer, or non-positive
            Content-Length, or any read error, is never allowed to prevent
            the response that follows.
            """

            content_length_header = self.headers.get("Content-Length")
            if content_length_header is None:
                return
            try:
                content_length = int(content_length_header)
            except ValueError:
                return
            if content_length <= 0:
                return
            try:
                self.rfile.read(min(content_length, max_body_bytes))
            except OSError:
                pass

        def send_error(self, code, message=None, explain=None) -> None:
            # Never let http.server's default error page - which reflects
            # the request method/path back into an HTML body - reach the
            # caller. Covers any HTTP method this class doesn't define a
            # do_* handler for, and any other internal error path.
            self._respond_empty(code)

        def _respond_empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return WebhookRequestHandler


class _QuiescentThreadingHTTPServer(ThreadingHTTPServer):
    """Milestone 47 P1 adversarial-review correction (MEDIUM-3): makes the
    request-thread quiescence guarantee WhatsAppServer.stop()'s own
    runtime-lock release ordering depends on EXPLICIT, never an
    accidentally-inherited stdlib default.

    socketserver.ThreadingMixIn's own class defaults - daemon_threads=False,
    block_on_close=True - already happen to provide exactly what is
    needed: with both at their stdlib default, server_close() genuinely
    blocks (via ThreadingMixIn._threads.join()) until every currently
    in-flight, non-daemon request-handling thread has fully finished -
    including one still inside accept_task_message()'s durable `/task`
    write. That is precisely the property required before this process's
    runtime-ownership lock is ever safe to release (see
    WhatsAppServer.stop()'s own docstring for the full 5-step release
    sequence this makes possible). Leaving that as an unstated, merely-
    inherited default is fragile: a future contributor adding
    `daemon_threads = True` to some subclass, for a "faster shutdown"
    optimization, would silently remove this exact guarantee, with
    nothing here to catch it. Restating both attributes explicitly, right
    here, turns an accident into a documented, load-bearing invariant -
    verified by
    tests/interfaces/whatsapp/test_server.py::test_stop_does_not_release_runtime_ownership_while_a_request_thread_is_still_active."""

    daemon_threads = False
    block_on_close = True


class _ThreadingHTTPServerIPv6(_QuiescentThreadingHTTPServer):
    """ThreadingHTTPServer defaults to AF_INET; this variant is selected
    whenever the validated host is an IPv6 literal (e.g. "::1"), since
    binding an IPv6 address on an AF_INET socket fails outright. Inherits
    the same explicit daemon_threads/block_on_close invariant from
    _QuiescentThreadingHTTPServer - the IPv6 path must never be a weaker
    quiescence guarantee than the IPv4 one."""

    address_family = socket.AF_INET6


def _select_server_class(host: str) -> type[ThreadingHTTPServer]:
    # config.py only ever produces "localhost", an IPv4 loopback literal,
    # or "::1" - a literal colon is a reliable, simple IPv6 marker across
    # that whole set. Both branches now return a server class with the
    # SAME explicit quiescence guarantee (_QuiescentThreadingHTTPServer's
    # own daemon_threads=False/block_on_close=True) - never plain,
    # unmodified ThreadingHTTPServer, whose own class defaults happen to
    # match today but are never asserted here as this codebase's own
    # stated contract.
    return _ThreadingHTTPServerIPv6 if ":" in host else _QuiescentThreadingHTTPServer


class WhatsAppServer:
    """Loopback-only HTTP server plus a single background worker for inbound tasks."""

    def __init__(
        self,
        whatsapp_config: WhatsAppConfig,
        message_handler: MessageHandler,
        dedup: SeenMessageCache | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        *,
        task_db_path=None,
        worker_task_connection=None,
        runtime_lock=None,
        recovery_interval_seconds: float = DEFAULT_RECOVERY_INTERVAL_SECONDS,
    ) -> None:
        self._dedup = dedup if dedup is not None else SeenMessageCache()
        self._queue: "queue.Queue" = queue.Queue(maxsize=queue_capacity)
        self._message_handler = message_handler
        # Milestone 46 P1: the worker's own long-lived task-DB connection
        # (if any). Only ever used, and only ever closed, by the worker
        # thread itself (see _run_worker()'s own finally block and stop()'s
        # own docstring) - never shared with a request thread's own
        # connection (opened per-request inside accept_task_message(),
        # never held here), and never closed by any other thread.
        self._worker_task_connection = worker_task_connection
        # Milestone 47 P1: the database-scoped runtime-ownership lock (if
        # any - see kernel/employee_tasks/runtime_lock.py), acquired by
        # build_server() BEFORE this object was ever constructed. Released
        # only by stop() below, and only once every component that could
        # still touch the task database (the worker thread, and whatever
        # it may still be running - see this milestone's own recovery
        # checkpoint) is confirmed quiescent - never released merely
        # because the worker thread was asked to stop.
        self._runtime_lock = runtime_lock
        self._recovery_interval_seconds = recovery_interval_seconds
        handler_class = _make_handler_class(
            whatsapp_config, self._queue, self._dedup, max_body_bytes, task_db_path
        )
        server_class = _select_server_class(whatsapp_config.host)
        self._httpd = server_class(
            (whatsapp_config.host, whatsapp_config.port), handler_class
        )
        self._worker_thread = threading.Thread(target=self._run_worker, daemon=False)

    @property
    def server_address(self):
        return self._httpd.server_address

    def start(self) -> None:
        """Start the worker thread and serve requests until stop() is called. Blocks."""

        self._worker_thread.start()
        self._httpd.serve_forever()

    def stop(self) -> None:
        """Shut down the HTTP server and signal the worker to stop.

        Milestone 47 P1 adversarial-review correction (MEDIUM-3): the full
        release sequence, stated explicitly, in order - every step below
        must complete before the next is safe to rely on:

          1. self._httpd.shutdown() - stops serve_forever() from accepting
             any further connection.
          2. self._httpd.server_close() - closes the listening socket AND
             (see _QuiescentThreadingHTTPServer's own docstring for why
             this is now an explicit, asserted class attribute rather than
             a merely-inherited stdlib default) BLOCKS until every
             currently in-flight, non-daemon request-handling thread has
             fully finished - including one still inside
             accept_task_message()'s durable `/task` write. By the time
             this call returns, no request thread capable of a durable
             task-DB write can still be running.
          3. self._queue.put(_STOP) + self._worker_thread.join(timeout=...) -
             signals and waits (up to the existing bounded timeout) for the
             worker thread to finish.
          4. If the join succeeds: _run_worker()'s own finally block (which
             closes the worker's task-DB connection) has, by construction,
             already run by the time join() returns with is_alive() False -
             so "worker joined" and "worker's DB resources are quiescent"
             are the same moment.
          5. Runtime ownership is released LAST, only after steps 2-4 have
             all completed - i.e. only once BOTH every request thread and
             the worker thread are provably done touching the task
             database.

        Milestone 46 P1 correction (unchanged by the above): this method
        never closes the worker's own task-DB connection itself, whether
        or not the join completed in time - only the worker thread may
        ever close a connection it might still be using (kernel/
        employee_tasks/db.py's own module docstring is explicit that
        sqlite3 does not serialize concurrent calls on the same connection
        object across threads; the original version of this method
        violated that by closing the connection here regardless of
        whether join() actually returned because the worker finished, or
        merely timed out while the worker was still active inside
        dispatch_planning() - empirically proven to leave a task
        permanently stuck in TaskState.PLANNING).

        If the worker is still alive once the join timeout elapses (step 3
        fails), this method still returns - it never blocks indefinitely
        and never forces the worker to stop (a worker still finishing its
        current item, then draining the already-enqueued _STOP sentinel on
        its own schedule, is a normal bounded-shutdown outcome, not a
        failure this method needs to correct). In that case, step 5
        deliberately does NOT run: the runtime-ownership lock remains held
        by this still-live process (correctly - the worker may still be
        mid-write), and is released, automatically, by the OS whenever
        this process actually terminates (see
        kernel/employee_tasks/runtime_lock.py's own docstring for why that
        is always safe, never a stale-lock risk). Releasing early here
        would let a second runtime start reconciling the same database
        while this process's worker might still be touching it - exactly
        the hazard database-scoped ownership exists to prevent."""

        self._httpd.shutdown()
        self._httpd.server_close()
        self._queue.put(_STOP)
        self._worker_thread.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
        if self._worker_thread.is_alive():
            # Never claim the worker has stopped when it has not - a
            # generic, bounded log line only, never request/task/SQL/
            # provider detail. The worker keeps running; it will still
            # close its own connection when _run_worker() actually exits.
            logger.warning("worker_shutdown_pending")
            return
        if self._runtime_lock is not None:
            self._runtime_lock.release()

    def _run_worker(self) -> None:
        # No authorization or deduplication happens here - only pre-cleared
        # tasks ever reach this loop. Processes one task at a time,
        # preserving the queue's FIFO order, and never retries a failed
        # outbound send for an ordinary task.
        #
        # Milestone 47 P1: a monotonic recovery deadline is interleaved with
        # normal queue consumption - queue.get() only ever blocks up to that
        # deadline (never indefinitely), and the deadline is re-checked
        # after EVERY normal item too, not only when the queue happens to go
        # idle - so an unbounded SEQUENCE of short queued items can never
        # starve recovery indefinitely (a naive "only check when idle"
        # design could not make even that guarantee). This is single-worker
        # serialization, not preemption: this loop processes one item at a
        # time to completion, so a single long-running handle_task() call
        # can still delay the next recovery checkpoint past
        # self._recovery_interval_seconds - the deadline is only ever
        # re-checked BETWEEN items, never used to interrupt one already in
        # progress. Each recovery checkpoint is itself bounded to at most
        # one external send (see
        # task_control.run_outbound_lifecycle_recovery_checkpoint()'s own
        # docstring) - this is what keeps recovery from ever starving
        # normal queue responsiveness in the other direction. _STOP is
        # still dequeued and honored with the same priority as any other
        # item - a recovery checkpoint never delays shutdown, since it only
        # ever runs strictly between two item-processing steps, never
        # instead of consuming _STOP.
        next_recovery_deadline = time.monotonic() + self._recovery_interval_seconds
        try:
            while True:
                remaining = max(0.0, next_recovery_deadline - time.monotonic())
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    item = None

                if item is not None:
                    try:
                        if item is _STOP:
                            break
                        try:
                            self._message_handler.handle_task(item)
                        except Exception:
                            # No traceback, no exception message, no task
                            # detail - only a generic category, and the loop
                            # continues with the next queued task rather
                            # than dying or retrying this one.
                            logger.warning("worker_error")
                    finally:
                        self._queue.task_done()

                if time.monotonic() >= next_recovery_deadline:
                    try:
                        self._message_handler.run_recovery_checkpoint()
                    except Exception:
                        # Same no-detail discipline as worker_error above -
                        # a recovery-checkpoint defect must never crash the
                        # worker thread or block normal queue processing.
                        logger.warning("worker_recovery_error")
                    next_recovery_deadline = time.monotonic() + self._recovery_interval_seconds
        finally:
            # Milestone 46 P1: only this thread - the one that actually used
            # this connection - ever closes it, and only once this loop has
            # genuinely finished (normal _STOP exit, or any exception
            # escaping the loop above, though none currently does since
            # handle_task()/run_recovery_checkpoint() are already caught
            # inside it). See stop()'s own docstring for the invariant this
            # preserves.
            if self._worker_task_connection is not None:
                self._worker_task_connection.close()


def build_server() -> WhatsAppServer:
    """Composition root: build a fully-wired WhatsAppServer from environment configuration."""

    whatsapp_config = load_whatsapp_config()
    kernel_config = load_config()
    capability_loader = CapabilityLoader().load

    # Milestone 46 P1: durable task-ingress wiring.
    task_db_path = resolve_database_path()

    # Milestone 47 P1: database-scoped runtime ownership, acquired BEFORE
    # schema initialization, before any TaskRepository is constructed,
    # before any worker or recovery activity of any kind - see
    # kernel/employee_tasks/runtime_lock.py's own module docstring for the
    # exact OS-level guarantee this provides and why it is scoped to the
    # database file itself rather than this interface's own HTTP port.
    # RuntimeOwnershipUnavailableError propagates uncaught: a second live
    # runtime against the same database must fail this process's startup
    # closed, before touching any durable task state, never silently
    # continue without recovery ownership.
    runtime_lock = acquire_runtime_ownership(task_db_path)
    try:
        orchestrator = build_orchestrator(kernel_config, capability_loader)
        client = WhatsAppClient(
            whatsapp_config.access_token,
            whatsapp_config.phone_number_id,
            whatsapp_config.api_version,
        )

        # Ensure/verify the schema exactly once, synchronously, before
        # ThreadingHTTPServer ever begins accepting concurrent requests -
        # this is what makes every later per-request
        # open_writer_connection() call hit the cheap, already-current-
        # schema path rather than racing another connection through
        # first-ever schema creation (see task_control.py's own module
        # docstring and the Milestone 46 P1 design report's empirical
        # concurrency validation for why concurrent first-ever schema
        # creation can otherwise leak a raw sqlite3.OperationalError). Safe
        # to run exactly once, exclusively, precisely because runtime
        # ownership is already held by this point.
        schema_init_connection = open_writer_connection(task_db_path)
        schema_init_connection.close()

        task_registry = ActionRegistry()
        tools_config = load_tools_config()
        task_catalog = build_catalog(task_registry, tools_config)
        planner_provider = get_planner_provider(kernel_config)

        # Milestone 46 P2A: a second, independent conversational ModelProvider
        # instance, dedicated to RESPOND synthesis - deliberately never the
        # planner_provider above (see kernel/task_execution/__init__.py's
        # model-role-separation contract). Orchestrator constructs its own
        # provider internally with no injection seam, so this is a second
        # instance rather than a shared one; verified harmless (a ModelProvider
        # is a stateless config wrapper - kernel/models/ollama.py's
        # OllamaProvider holds only four plain attributes, no connection pool,
        # no persistent socket) - not worth adding an Orchestrator seam for.
        respond_provider = get_provider(kernel_config)

        # The single WhatsApp worker's own long-lived connection/repository -
        # opened once here, reused for the life of the process. Ownership
        # transfers to the returned WhatsAppServer (whose worker thread closes
        # it - see _run_worker()'s own finally block) only once construction
        # below actually succeeds; never shared with a request thread's own
        # per-request connection (see task_control.py's own module docstring).
        worker_task_connection = open_writer_connection(task_db_path)
        try:
            worker_task_repository = TaskRepository(worker_task_connection)

            message_handler = MessageHandler(
                orchestrator,
                client,
                task_repository=worker_task_repository,
                task_catalog=task_catalog,
                planner_provider=planner_provider,
                # Milestone 46 P2A execution-time dependencies. action_registry
                # reuses the same stateless task_registry instance built above
                # for planning - ActionRegistry has no config dependency, so
                # there is no freshness concern in sharing it. tools_config_loader
                # is the bare load_tools_config function, called fresh by
                # dispatch_task_work() before every execution-layer operation -
                # never the startup-time `tools_config` snapshot planning uses
                # (see task_control.py's own module docstring for why that
                # distinction is security-relevant).
                action_registry=task_registry,
                tools_config_loader=load_tools_config,
                respond_provider=respond_provider,
                authorized_sender=whatsapp_config.authorized_sender_id,
            )

            return WhatsAppServer(
                whatsapp_config,
                message_handler,
                task_db_path=task_db_path,
                worker_task_connection=worker_task_connection,
                runtime_lock=runtime_lock,
            )
        except Exception:
            # Construction failed after the connection was already opened, so
            # WhatsAppServer never took ownership of it (its worker thread will
            # never run to close it) - close it here instead of leaking it.
            worker_task_connection.close()
            raise
    except Exception:
        # Construction failed after runtime ownership was already acquired,
        # so no WhatsAppServer exists to release it via stop() - release it
        # here instead of leaking it (see RuntimeLock.release()'s own
        # idempotency guarantee).
        runtime_lock.release()
        raise


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = build_server()
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
