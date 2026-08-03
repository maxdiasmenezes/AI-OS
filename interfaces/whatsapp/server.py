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
"""

import hashlib
import hmac
import json
import logging
import queue
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from capabilities.loader import CapabilityLoader
from kernel.config.config import Config, load_config
from kernel.memory import MemoryManager
from kernel.orchestrator import Orchestrator

from interfaces.whatsapp.client import WhatsAppClient
from interfaces.whatsapp.config import WhatsAppConfig, load_whatsapp_config
from interfaces.whatsapp.dedup import SeenMessageCache
from interfaces.whatsapp.handler import MessageHandler, classify_message
from interfaces.whatsapp.memory import FixedNamespaceMemory
from interfaces.whatsapp.payload import parse_webhook_payload
from interfaces.whatsapp.signature import verify_signature

logger = logging.getLogger(__name__)

_WEBHOOK_PATH = "/webhook"

# AI-OS application limits - not claims about any Meta platform limit.
# All are constructor parameters so tests can inject different values.
DEFAULT_MAX_BODY_BYTES = 1_000_000
DEFAULT_QUEUE_CAPACITY = 16

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

                if not dedup.add_if_new(message.message_id):
                    logger.info(
                        "dropping message: duplicate (ref=%s)",
                        _message_reference(message.message_id),
                    )
                    continue

                task = classify_message(message)
                try:
                    work_queue.put_nowait(task)
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


class _ThreadingHTTPServerIPv6(ThreadingHTTPServer):
    """ThreadingHTTPServer defaults to AF_INET; this variant is selected
    whenever the validated host is an IPv6 literal (e.g. "::1"), since
    binding an IPv6 address on an AF_INET socket fails outright."""

    address_family = socket.AF_INET6


def _select_server_class(host: str) -> type[ThreadingHTTPServer]:
    # config.py only ever produces "localhost", an IPv4 loopback literal,
    # or "::1" - a literal colon is a reliable, simple IPv6 marker across
    # that whole set.
    return _ThreadingHTTPServerIPv6 if ":" in host else ThreadingHTTPServer


class WhatsAppServer:
    """Loopback-only HTTP server plus a single background worker for inbound tasks."""

    def __init__(
        self,
        whatsapp_config: WhatsAppConfig,
        message_handler: MessageHandler,
        dedup: SeenMessageCache | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
    ) -> None:
        self._dedup = dedup if dedup is not None else SeenMessageCache()
        self._queue: "queue.Queue" = queue.Queue(maxsize=queue_capacity)
        self._message_handler = message_handler
        handler_class = _make_handler_class(
            whatsapp_config, self._queue, self._dedup, max_body_bytes
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
        """Shut down the HTTP server and let the worker finish gracefully."""

        self._httpd.shutdown()
        self._httpd.server_close()
        self._queue.put(_STOP)
        self._worker_thread.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)

    def _run_worker(self) -> None:
        # No authorization or deduplication happens here - only pre-cleared
        # tasks ever reach this loop. Processes one task at a time,
        # preserving the queue's FIFO order, and never retries a failed
        # outbound send.
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    break
                try:
                    self._message_handler.handle_task(item)
                except Exception:
                    # No traceback, no exception message, no task detail -
                    # only a generic category, and the loop continues with
                    # the next queued task rather than dying or retrying
                    # this one.
                    logger.warning("worker_error")
            finally:
                self._queue.task_done()


def build_server() -> WhatsAppServer:
    """Composition root: build a fully-wired WhatsAppServer from environment configuration."""

    whatsapp_config = load_whatsapp_config()
    kernel_config = load_config()
    capability_loader = CapabilityLoader().load

    orchestrator = build_orchestrator(kernel_config, capability_loader)
    client = WhatsAppClient(
        whatsapp_config.access_token,
        whatsapp_config.phone_number_id,
        whatsapp_config.api_version,
    )
    message_handler = MessageHandler(orchestrator, client)

    return WhatsAppServer(whatsapp_config, message_handler)


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
