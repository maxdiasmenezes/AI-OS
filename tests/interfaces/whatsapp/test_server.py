"""
Tests for the WhatsApp interface's composition root and HTTP server.

All HTTP exercised here is real but loopback-only (127.0.0.1, an
OS-assigned ephemeral port) - never Meta's actual Cloud API. Several
tests use raw sockets rather than urllib, since urllib cannot express a
missing/malformed Content-Length header or a body shorter than declared.
"""

import hashlib
import hmac
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from interfaces.whatsapp.client import WhatsAppClientError
from interfaces.whatsapp.config import WhatsAppConfig
from interfaces.whatsapp.dedup import DEFAULT_CAPACITY as DEFAULT_DEDUP_CAPACITY
from interfaces.whatsapp.dedup import SeenMessageCache
from interfaces.whatsapp.handler import MessageHandler
from interfaces.whatsapp.memory import FixedNamespaceMemory
from interfaces.whatsapp.server import (
    DEFAULT_QUEUE_CAPACITY,
    WhatsAppServer,
    build_orchestrator,
)
from interfaces.whatsapp.task_control import TaskExecutionWork, compute_dedup_key
from kernel.capabilities.base import Capability
from kernel.config.config import Config
from kernel.employee_tasks import (
    TaskRepository,
    TaskState,
    open_writer_connection,
)
from kernel.memory import MemoryManager
from kernel.models.base import ModelProvider, ModelRequestOptions, ModelResponse
from kernel.task_planner import PlanStep, StepKind, TaskPlan, build_catalog, serialize_plan
from kernel.tools.config import RepoBackupSpec, ToolsConfig
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionResult

_APP_SECRET = "test-app-secret"
_VERIFY_TOKEN = "test-verify-token"
_PHONE_NUMBER_ID = "1234567890"
_AUTHORIZED_SENDER = "15551234567"
_UNAUTHORIZED_SENDER = "19998887777"


def _make_whatsapp_config() -> WhatsAppConfig:
    return WhatsAppConfig(
        verify_token=_VERIFY_TOKEN,
        app_secret=_APP_SECRET,
        access_token="test-access-token",
        phone_number_id=_PHONE_NUMBER_ID,
        authorized_sender_id=_AUTHORIZED_SENDER,
        host="127.0.0.1",
        port=0,  # OS-assigned ephemeral port
        api_version="v23.0",
    )


class RecordingClient:
    def __init__(self):
        self.sent = []

    def send_text_message(self, to, body):
        self.sent.append((to, body))
        return "wamid.OUT1"


class FailFirstSendClient:
    """Raises WhatsAppClientError on exactly its first send_text_message()
    call, then behaves exactly like RecordingClient for every call after -
    used to prove the worker survives a lifecycle-delivery send failure and
    goes on to correctly process later, unrelated queue items (Milestone 46
    adversarial review, M2/worker-survival)."""

    def __init__(self):
        self.sent = []
        self._calls = 0

    def send_text_message(self, to, body):
        self._calls += 1
        if self._calls == 1:
            raise WhatsAppClientError("simulated outbound failure")
        self.sent.append((to, body))
        return "wamid.OUT1"


def _model_response(text: str) -> ModelResponse:
    return ModelResponse(
        text=text, model="fake-model", input_tokens=1, output_tokens=1, latency_seconds=0.01
    )


class FakeOrchestrator:
    def __init__(self, text="a reply"):
        self._text = text
        self.received_prompts = []

    def handle(self, prompt, context=None):
        self.received_prompts.append(prompt)
        return _model_response(self._text)


class BlockingOrchestrator:
    """Blocks on handle() until release() is called; a repeat call returns
    immediately, since `release` stays set once triggered."""

    def __init__(self, text="done"):
        self._text = text
        self.started = threading.Event()
        self._release = threading.Event()
        self.received_prompts = []

    def handle(self, prompt, context=None):
        self.received_prompts.append(prompt)
        self.started.set()
        self._release.wait(timeout=5)
        return _model_response(self._text)

    def release(self):
        self._release.set()


def _sign(body: bytes) -> str:
    digest = hmac.new(_APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _url(server: WhatsAppServer, path: str) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}{path}"


def _envelope(message_id, sender, phone_number_id=_PHONE_NUMBER_ID, text="hello"):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": phone_number_id},
                    "messages": [{
                        "id": message_id,
                        "from": sender,
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
            }],
        }],
    }


def _multi_message_envelope(message_specs, phone_number_id=_PHONE_NUMBER_ID):
    """Build one webhook envelope containing several messages in a single
    `value.messages` batch, as Meta's webhook can deliver."""

    return {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": phone_number_id},
                    "messages": [
                        {"id": message_id, "from": sender, "type": "text", "text": {"body": text}}
                        for (message_id, sender, text) in message_specs
                    ],
                },
            }],
        }],
    }


def _post(server: WhatsAppServer, payload: dict) -> int:
    """POST a signed, well-formed webhook body via urllib. Returns the status
    code (urllib raises HTTPError for >=400, so that's unwrapped here)."""

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _url(server, "/webhook"),
        data=body,
        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _raw_request(host, port, method, path, headers, body_bytes, shutdown_write=False):
    """Send a hand-built HTTP/1.1 request over a raw socket and return the
    raw response bytes. Used for edge cases urllib cannot express."""

    sock = socket.create_connection((host, port), timeout=5)
    try:
        header_lines = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}"]
        for key, value in headers.items():
            header_lines.append(f"{key}: {value}")
        request = ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8") + body_bytes
        sock.sendall(request)
        if shutdown_write:
            sock.shutdown(socket.SHUT_WR)
        sock.settimeout(5)
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        return b"".join(chunks)
    finally:
        sock.close()


def _status_code(raw_response: bytes) -> int:
    status_line = raw_response.split(b"\r\n", 1)[0]
    return int(status_line.split(b" ")[1])


@pytest.fixture
def running_server():
    whatsapp_config = _make_whatsapp_config()
    orchestrator = FakeOrchestrator("a bold Malbec would work well")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)
    server = WhatsAppServer(whatsapp_config, handler)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)  # let the listener bind before tests connect

    yield server, orchestrator, client

    server.stop()
    thread.join(timeout=5)


# --- Routing: only GET/POST /webhook exist ---------------------------


def test_get_verification_succeeds_with_correct_token(running_server):
    server, _, _ = running_server
    url = _url(
        server,
        f"/webhook?hub.mode=subscribe&hub.verify_token={_VERIFY_TOKEN}&hub.challenge=abc123",
    )

    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200
        assert response.read() == b"abc123"


def test_get_verification_fails_with_wrong_token(running_server):
    server, _, _ = running_server
    url = _url(
        server, f"/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=abc123"
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)
    assert exc_info.value.code == 403


def test_get_verification_uses_hmac_compare_digest(running_server, monkeypatch):
    # Spies on (rather than replaces the outcome of) the real
    # hmac.compare_digest, proving the GET token check goes through the
    # constant-time comparison rather than a plain `==`.
    import interfaces.whatsapp.server as server_module

    server, _, _ = running_server
    calls = []
    real_compare_digest = hmac.compare_digest

    def spy_compare_digest(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(server_module.hmac, "compare_digest", spy_compare_digest)

    url = _url(
        server,
        f"/webhook?hub.mode=subscribe&hub.verify_token={_VERIFY_TOKEN}&hub.challenge=abc123",
    )
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200

    assert len(calls) == 1
    assert calls[0] == (_VERIFY_TOKEN, _VERIFY_TOKEN)


def test_get_on_a_different_path_returns_404(running_server):
    server, _, _ = running_server
    url = _url(
        server, f"/?hub.mode=subscribe&hub.verify_token={_VERIFY_TOKEN}&hub.challenge=abc123"
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)
    assert exc_info.value.code == 404


def test_post_on_a_different_path_returns_404(running_server):
    server, orchestrator, _ = running_server
    body = json.dumps({"entry": []}).encode("utf-8")
    request = urllib.request.Request(
        _url(server, "/not-webhook"),
        data=body,
        headers={"X-Hub-Signature-256": _sign(body)},
        method="POST",
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=5)
    assert exc_info.value.code == 404
    assert orchestrator.received_prompts == []


# --- Signature and body validation -------------------------------------


def test_post_with_valid_signature_is_processed(running_server):
    server, orchestrator, client = running_server
    payload = _envelope("wamid.1", _AUTHORIZED_SENDER, text="what wine goes with steak?")

    assert _post(server, payload) == 200

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not client.sent:
        time.sleep(0.02)

    assert orchestrator.received_prompts == ["what wine goes with steak?"]
    assert client.sent == [(_AUTHORIZED_SENDER, "a bold Malbec would work well")]


def test_post_with_invalid_signature_is_rejected(running_server):
    server, orchestrator, client = running_server
    body = json.dumps({"entry": []}).encode("utf-8")
    request = urllib.request.Request(
        _url(server, "/webhook"),
        data=body,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        method="POST",
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=5)
    assert exc_info.value.code == 403
    assert orchestrator.received_prompts == []
    assert client.sent == []


def test_post_missing_signature_is_rejected(running_server):
    server, orchestrator, _ = running_server
    body = json.dumps({"entry": []}).encode("utf-8")
    request = urllib.request.Request(_url(server, "/webhook"), data=body, method="POST")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=5)
    assert exc_info.value.code == 403
    assert orchestrator.received_prompts == []


def test_post_with_valid_signature_but_malformed_json_returns_400(running_server):
    server, orchestrator, _ = running_server
    body = b"not valid json"
    request = urllib.request.Request(
        _url(server, "/webhook"), data=body, headers={"X-Hub-Signature-256": _sign(body)},
        method="POST",
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=5)
    assert exc_info.value.code == 400
    assert orchestrator.received_prompts == []


def test_post_missing_content_length_returns_411(running_server):
    server, orchestrator, _ = running_server
    host, port = server.server_address[0], server.server_address[1]

    response = _raw_request(host, port, "POST", "/webhook", headers={}, body_bytes=b"")

    assert _status_code(response) == 411
    assert orchestrator.received_prompts == []


def test_post_malformed_content_length_returns_400(running_server):
    server, orchestrator, _ = running_server
    host, port = server.server_address[0], server.server_address[1]

    response = _raw_request(
        host, port, "POST", "/webhook",
        headers={"Content-Length": "not-a-number"}, body_bytes=b"",
    )

    assert _status_code(response) == 400
    assert orchestrator.received_prompts == []


def test_post_over_body_limit_returns_413_without_reading_the_body():
    whatsapp_config = _make_whatsapp_config()
    handler = MessageHandler(FakeOrchestrator(), RecordingClient())
    server = WhatsAppServer(whatsapp_config, handler, max_body_bytes=100)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        host, port = server.server_address[0], server.server_address[1]
        # Declares a body far over the 100-byte limit but sends none of it -
        # if the server tried to read it first, this would hang until the
        # socket read times out rather than returning promptly.
        response = _raw_request(
            host, port, "POST", "/webhook",
            headers={"Content-Length": "999999"}, body_bytes=b"",
        )
        assert _status_code(response) == 413
    finally:
        server.stop()
        thread.join(timeout=5)


def test_post_incomplete_body_returns_400(running_server):
    server, orchestrator, _ = running_server
    host, port = server.server_address[0], server.server_address[1]
    declared_length = 100
    actual_body = b"x" * 10  # fewer bytes than declared

    response = _raw_request(
        host, port, "POST", "/webhook",
        headers={"Content-Length": str(declared_length)},
        body_bytes=actual_body,
        shutdown_write=True,
    )

    assert _status_code(response) == 400
    assert orchestrator.received_prompts == []


def test_post_negative_content_length_returns_400_without_processing(running_server):
    server, orchestrator, client = running_server
    host, port = server.server_address[0], server.server_address[1]

    response = _raw_request(
        host, port, "POST", "/webhook",
        headers={"Content-Length": "-1", "X-Hub-Signature-256": _sign(b"")},
        body_bytes=b"",
    )

    assert _status_code(response) == 400
    time.sleep(0.1)
    assert orchestrator.received_prompts == []
    assert client.sent == []


# --- Unsupported HTTP methods --------------------------------------------


def test_put_on_webhook_returns_405(running_server):
    server, orchestrator, _ = running_server
    host, port = server.server_address[0], server.server_address[1]

    response = _raw_request(host, port, "PUT", "/webhook", headers={}, body_bytes=b"")

    assert _status_code(response) == 405
    assert orchestrator.received_prompts == []


def test_delete_on_webhook_returns_405(running_server):
    server, orchestrator, _ = running_server
    host, port = server.server_address[0], server.server_address[1]

    response = _raw_request(host, port, "DELETE", "/webhook", headers={}, body_bytes=b"")

    assert _status_code(response) == 405
    assert orchestrator.received_prompts == []


def test_put_on_a_different_path_is_not_405_and_reveals_nothing(running_server):
    server, _, _ = running_server
    host, port = server.server_address[0], server.server_address[1]

    response = _raw_request(host, port, "PUT", "/not-webhook", headers={}, body_bytes=b"")

    # Either 404 or 405 is acceptable per spec - what matters is that the
    # response body never echoes back the method or path, unlike
    # BaseHTTPRequestHandler's default error page.
    assert _status_code(response) in (404, 405)
    body = response.split(b"\r\n\r\n", 1)[1]
    assert body == b""


# --- Request-body draining before an early-return response ---------------
#
# Regression coverage for a real connection-lifecycle defect: do_POST()'s
# wrong-path branch used to respond 404 without ever reading the request
# body the client had already sent. If the connection is then closed
# while unread bytes are still sitting in the OS receive buffer, TCP
# sends a RST instead of a clean FIN - the client sees
# ConnectionAbortedError/ConnectionResetError instead of the response
# that was actually sent. The race is timing-dependent (far more likely
# under scheduler/thread contention from other work happening around the
# same time), which is why it was intermittent rather than constant. The
# fix is WebhookRequestHandler._drain_request_body() in
# interfaces/whatsapp/server.py, called before every early-return response
# that hasn't already consumed the body.


def test_post_to_wrong_path_with_a_body_reliably_returns_404(running_server):
    server, orchestrator, _ = running_server
    body = json.dumps({"entry": []}).encode("utf-8")

    for attempt in range(20):
        request = urllib.request.Request(
            _url(server, "/not-webhook"),
            data=body,
            headers={"X-Hub-Signature-256": _sign(body)},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request, timeout=5)
        assert exc_info.value.code == 404, f"attempt {attempt}"

    assert orchestrator.received_prompts == []


def test_post_to_wrong_path_with_a_body_via_raw_socket_gets_a_clean_404(running_server):
    # A raw-socket-level proof, distinct from urllib's exception-based
    # check above: confirms the connection itself tears down cleanly (a
    # fully readable HTTP response, not a reset) when a POST body is sent
    # to a path the server never reads that body for.
    server, _, _ = running_server
    host, port = server.server_address[0], server.server_address[1]
    body = json.dumps({"entry": []}).encode("utf-8")

    response = _raw_request(
        host, port, "POST", "/not-webhook",
        headers={
            "Content-Length": str(len(body)),
            "X-Hub-Signature-256": _sign(body),
        },
        body_bytes=body,
    )

    assert _status_code(response) == 404


def test_wrong_path_post_succeeds_immediately_after_a_processed_webhook_request(running_server):
    # The exact ordering that reproduced the race during investigation: a
    # fully-processed, worker-queued webhook POST immediately followed by
    # a wrong-path POST carrying a body, on the same live server.
    server, orchestrator, _ = running_server
    assert _post(server, _envelope("wamid.precede1", _AUTHORIZED_SENDER)) == 200

    body = json.dumps({"entry": []}).encode("utf-8")
    request = urllib.request.Request(
        _url(server, "/not-webhook"),
        data=body,
        headers={"X-Hub-Signature-256": _sign(body)},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=5)
    assert exc_info.value.code == 404


def test_repeated_valid_then_wrong_path_post_sequence_succeeds(running_server):
    # Repeats the exact problematic sequence (a processed webhook POST,
    # then a wrong-path POST carrying a body) several times in a row on
    # one server, to make a reintroduced race extremely likely to surface
    # rather than relying on a single lucky pass.
    server, orchestrator, _ = running_server
    body = json.dumps({"entry": []}).encode("utf-8")

    for i in range(10):
        assert _post(server, _envelope(f"wamid.seq{i}", _AUTHORIZED_SENDER)) == 200

        request = urllib.request.Request(
            _url(server, "/not-webhook"),
            data=body,
            headers={"X-Hub-Signature-256": _sign(body)},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request, timeout=5)
        assert exc_info.value.code == 404, f"iteration {i}"


# --- Authorization and dedup happen before queueing --------------------


def test_unauthorized_sender_is_dropped_with_200_and_no_reply(running_server):
    server, orchestrator, client = running_server

    status = _post(server, _envelope("wamid.unauth", _UNAUTHORIZED_SENDER))

    assert status == 200
    time.sleep(0.1)
    assert orchestrator.received_prompts == []
    assert client.sent == []


def test_wrong_destination_is_dropped_with_200_and_no_reply(running_server):
    server, orchestrator, client = running_server

    status = _post(server, _envelope("wamid.wrongdest", _AUTHORIZED_SENDER, phone_number_id="0000000000"))

    assert status == 200
    time.sleep(0.1)
    assert orchestrator.received_prompts == []
    assert client.sent == []


def test_unauthorized_sender_never_reserves_the_dedup_slot(running_server):
    server, orchestrator, client = running_server
    shared_id = "wamid.shared-unauth"

    # First: same ID, wrong sender - must be dropped without touching dedup.
    assert _post(server, _envelope(shared_id, _UNAUTHORIZED_SENDER)) == 200
    # Second: same ID, correct sender - must NOT be treated as a duplicate.
    assert _post(server, _envelope(shared_id, _AUTHORIZED_SENDER, text="hi")) == 200

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not client.sent:
        time.sleep(0.02)

    assert orchestrator.received_prompts == ["hi"]
    assert len(client.sent) == 1


def test_wrong_destination_never_reserves_the_dedup_slot(running_server):
    server, orchestrator, client = running_server
    shared_id = "wamid.shared-dest"

    assert _post(server, _envelope(shared_id, _AUTHORIZED_SENDER, phone_number_id="0000000000")) == 200
    assert _post(server, _envelope(shared_id, _AUTHORIZED_SENDER, text="hi")) == 200

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not client.sent:
        time.sleep(0.02)

    assert orchestrator.received_prompts == ["hi"]
    assert len(client.sent) == 1


def test_duplicate_message_id_is_dropped_with_200_and_no_second_reply(running_server):
    server, orchestrator, client = running_server
    payload = _envelope("wamid.dup", _AUTHORIZED_SENDER, text="hi")

    assert _post(server, payload) == 200

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not client.sent:
        time.sleep(0.02)

    assert _post(server, payload) == 200
    time.sleep(0.1)

    assert orchestrator.received_prompts == ["hi"]
    assert len(client.sent) == 1


# --- Queue-full behavior ------------------------------------------------


def test_queue_full_returns_503_discards_reservation_and_allows_redelivery():
    whatsapp_config = _make_whatsapp_config()
    orchestrator = BlockingOrchestrator("done")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)
    server = WhatsAppServer(whatsapp_config, handler, queue_capacity=1)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        # A: the worker dequeues it immediately and blocks inside handle().
        assert _post(server, _envelope("wamid.A", _AUTHORIZED_SENDER, text="A")) == 200
        assert orchestrator.started.wait(timeout=2)

        # B: queue_capacity=1, so this occupies the one free slot.
        assert _post(server, _envelope("wamid.B", _AUTHORIZED_SENDER, text="B")) == 200

        # C: the queue is now full - must be rejected with 503, and its
        # dedup reservation must be released.
        assert _post(server, _envelope("wamid.C", _AUTHORIZED_SENDER, text="C")) == 503

        # Nothing has completed yet - A is still blocked.
        assert client.sent == []

        # Let A finish, which lets the worker drain B too (BlockingOrchestrator
        # no longer blocks once released).
        orchestrator.release()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(client.sent) < 2:
            time.sleep(0.02)
        assert len(client.sent) == 2

        # C's ID was discarded on queue-full, so redelivering it now (queue
        # has room, worker idle) must succeed rather than being treated as
        # a duplicate.
        assert _post(server, _envelope("wamid.C", _AUTHORIZED_SENDER, text="C")) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(client.sent) < 3:
            time.sleep(0.02)
        assert len(client.sent) == 3
        assert orchestrator.received_prompts == ["A", "B", "C"]
    finally:
        server.stop()
        thread.join(timeout=5)


def test_queue_full_does_not_call_orchestrator_for_the_rejected_message():
    whatsapp_config = _make_whatsapp_config()
    orchestrator = BlockingOrchestrator("done")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)
    server = WhatsAppServer(whatsapp_config, handler, queue_capacity=1)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        _post(server, _envelope("wamid.A2", _AUTHORIZED_SENDER, text="A2"))
        orchestrator.started.wait(timeout=2)
        _post(server, _envelope("wamid.B2", _AUTHORIZED_SENDER, text="B2"))

        status = _post(server, _envelope("wamid.C2", _AUTHORIZED_SENDER, text="REJECTED"))

        assert status == 503
        assert "REJECTED" not in orchestrator.received_prompts
        assert client.sent == []  # A2 still blocked, nothing sent yet
    finally:
        orchestrator.release()
        server.stop()
        thread.join(timeout=5)


def test_queue_full_mid_batch_stops_processing_the_rest_of_that_batch():
    # Documents and tests the chosen deterministic policy for a single
    # webhook POST that batches multiple messages in one
    # value.messages[] array: processing stops at the first message that
    # overflows the queue, its reservation is released, and the whole
    # request gets one 503 - messages already queued earlier in the same
    # batch are not rolled back (they keep their dedup reservation, so a
    # full redelivery of the batch won't reprocess them).
    whatsapp_config = _make_whatsapp_config()
    orchestrator = BlockingOrchestrator("done")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)
    server = WhatsAppServer(whatsapp_config, handler, queue_capacity=1)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        # Occupy the worker so the queue's single slot stays free for the
        # batch below to fill by itself.
        assert _post(server, _envelope("wamid.occupy", _AUTHORIZED_SENDER, text="occupy")) == 200
        assert orchestrator.started.wait(timeout=2)

        # One POST, two messages: the first fills the queue's one free
        # slot, the second must overflow it within the same request.
        payload = _multi_message_envelope([
            ("wamid.batch1", _AUTHORIZED_SENDER, "batch1"),
            ("wamid.batch2", _AUTHORIZED_SENDER, "batch2"),
        ])
        status = _post(server, payload)

        assert status == 503
        assert client.sent == []  # "occupy" is still blocked - nothing delivered yet

        orchestrator.release()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(client.sent) < 2:
            time.sleep(0.02)

        # Only "occupy" and "batch1" were ever queued; "batch2" overflowed
        # and its reservation was released rather than left stuck.
        assert orchestrator.received_prompts == ["occupy", "batch1"]
        assert len(client.sent) == 2

        # batch2's ID was released on overflow, so redelivering it must
        # succeed rather than being treated as a duplicate.
        redelivered = _envelope("wamid.batch2", _AUTHORIZED_SENDER, text="batch2-redelivered")
        assert _post(server, redelivered) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(client.sent) < 3:
            time.sleep(0.02)
        assert len(client.sent) == 3
        assert orchestrator.received_prompts == ["occupy", "batch1", "batch2-redelivered"]
    finally:
        server.stop()
        thread.join(timeout=5)


# --- Default capacities -------------------------------------------------


def test_default_queue_capacity_is_16():
    assert DEFAULT_QUEUE_CAPACITY == 16


def test_default_dedup_capacity_is_256():
    assert DEFAULT_DEDUP_CAPACITY == 256


# --- Privacy-safe logging ------------------------------------------------


def test_no_log_line_contains_any_portion_of_the_sender_id(running_server, caplog):
    server, orchestrator, client = running_server

    with caplog.at_level(logging.DEBUG):
        _post(server, _envelope("wamid.log1", _UNAUTHORIZED_SENDER, text="hi"))
        _post(server, _envelope("wamid.log2", _AUTHORIZED_SENDER, text="secret request text"))
        # Duplicate delivery, to also exercise the dedup log line.
        _post(server, _envelope("wamid.log2", _AUTHORIZED_SENDER, text="secret request text"))

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not client.sent:
        time.sleep(0.02)

    all_messages = "\n".join(record.getMessage() for record in caplog.records)

    assert _AUTHORIZED_SENDER not in all_messages
    assert _UNAUTHORIZED_SENDER not in all_messages
    assert _AUTHORIZED_SENDER[-4:] not in all_messages
    assert "secret request text" not in all_messages
    assert _VERIFY_TOKEN not in all_messages


def test_get_verification_query_string_is_not_logged(running_server, caplog):
    server, _, _ = running_server
    url = _url(
        server,
        f"/webhook?hub.mode=subscribe&hub.verify_token={_VERIFY_TOKEN}&hub.challenge=abc123",
    )

    with caplog.at_level(logging.DEBUG):
        with urllib.request.urlopen(url, timeout=5) as response:
            response.read()

    all_messages = "\n".join(record.getMessage() for record in caplog.records)
    assert _VERIFY_TOKEN not in all_messages
    assert "hub.verify_token" not in all_messages


# --- Graceful shutdown ---------------------------------------------------


def test_server_starts_and_stops_gracefully():
    whatsapp_config = _make_whatsapp_config()
    handler = MessageHandler(FakeOrchestrator(), RecordingClient())
    server = WhatsAppServer(whatsapp_config, handler)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    server.stop()
    thread.join(timeout=5)

    assert not thread.is_alive()


def _start_server_for_lifecycle_test():
    whatsapp_config = _make_whatsapp_config()
    handler = MessageHandler(FakeOrchestrator(), RecordingClient())
    server = WhatsAppServer(whatsapp_config, handler)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server, thread


def test_two_sequential_server_instances_start_and_stop_cleanly_with_no_cross_talk():
    server_a, thread_a = _start_server_for_lifecycle_test()
    host_a, port_a = server_a.server_address[0], server_a.server_address[1]

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"http://{host_a}:{port_a}/nope", timeout=5)
    assert exc_info.value.code == 404

    server_a.stop()
    thread_a.join(timeout=5)

    # Shutdown leaves the server thread terminated...
    assert not thread_a.is_alive()
    # ...and the listening socket itself closed, not merely unresponsive.
    assert server_a._httpd.socket.fileno() == -1

    # Immediately after stop(), the prior instance's port refuses new
    # connections - no subsequent request can reach it, since it is
    # genuinely gone rather than just quiet.
    with pytest.raises(OSError):
        socket.create_connection((host_a, port_a), timeout=1)

    # A second, fully independent server instance starts and stops
    # cleanly too - proves no lingering thread, socket, or fixture state
    # from the first instance interferes with the second.
    server_b, thread_b = _start_server_for_lifecycle_test()
    host_b, port_b = server_b.server_address[0], server_b.server_address[1]

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"http://{host_b}:{port_b}/nope", timeout=5)
    assert exc_info.value.code == 404

    server_b.stop()
    thread_b.join(timeout=5)

    assert not thread_b.is_alive()
    assert server_b._httpd.socket.fileno() == -1


def test_queued_tasks_ahead_of_the_sentinel_are_processed_before_shutdown_completes():
    whatsapp_config = _make_whatsapp_config()
    orchestrator = FakeOrchestrator("done")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)
    server = WhatsAppServer(whatsapp_config, handler, queue_capacity=16)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    for i in range(5):
        assert _post(server, _envelope(f"wamid.drain{i}", _AUTHORIZED_SENDER, text=f"msg{i}")) == 200

    server.stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(client.sent) == 5
    # Reaching into the queue's own bookkeeping here (rather than a public
    # API) is deliberate: it is the most direct proof that every queued
    # task was actually drained (task_done() called for each) rather than
    # merely that the thread exited.
    assert server._queue.unfinished_tasks == 0


def test_stop_does_not_hang_when_the_queue_is_temporarily_full():
    whatsapp_config = _make_whatsapp_config()
    orchestrator = BlockingOrchestrator("done")
    client = RecordingClient()
    handler = MessageHandler(orchestrator, client)
    server = WhatsAppServer(whatsapp_config, handler, queue_capacity=1)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    assert _post(server, _envelope("wamid.stopA", _AUTHORIZED_SENDER, text="A")) == 200
    assert orchestrator.started.wait(timeout=2)
    # Fills the queue's single slot while the worker is still blocked on A.
    assert _post(server, _envelope("wamid.stopB", _AUTHORIZED_SENDER, text="B")) == 200

    stop_thread = threading.Thread(target=server.stop, daemon=True)
    stop_thread.start()

    # stop() calls queue.put(_STOP), which must block right now - the
    # queue is genuinely full and nothing is draining it yet.
    time.sleep(0.2)
    assert stop_thread.is_alive()

    orchestrator.release()  # lets the worker drain A, then B, then accept _STOP

    stop_thread.join(timeout=5)
    thread.join(timeout=5)

    assert not stop_thread.is_alive()
    assert not thread.is_alive()
    assert len(client.sent) == 2


# --- Worker resilience ----------------------------------------------------


class FlakyMessageHandler:
    """Raises on its first call, regardless of the task; succeeds silently
    on every call after that. Used to prove the worker survives an
    unexpected exception escaping handle_task() itself (as opposed to one
    MessageHandler already catches internally) and keeps processing."""

    def __init__(self):
        self.calls = []

    def handle_task(self, task):
        self.calls.append(task)
        if len(self.calls) == 1:
            raise RuntimeError("sender=15551234567 text='should never be logged'")


def test_worker_survives_a_task_exception_and_processes_the_next_task(caplog):
    whatsapp_config = _make_whatsapp_config()
    flaky_handler = FlakyMessageHandler()
    server = WhatsAppServer(whatsapp_config, flaky_handler, queue_capacity=16)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        with caplog.at_level(logging.DEBUG):
            assert _post(server, _envelope("wamid.flaky1", _AUTHORIZED_SENDER, text="first")) == 200
            assert _post(server, _envelope("wamid.flaky2", _AUTHORIZED_SENDER, text="second")) == 200

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and len(flaky_handler.calls) < 2:
                time.sleep(0.02)
    finally:
        server.stop()
        thread.join(timeout=5)

    assert len(flaky_handler.calls) == 2  # the second task still ran
    assert not thread.is_alive()

    messages = [record.getMessage() for record in caplog.records]
    assert "worker_error" in messages
    for message in messages:
        assert "15551234567" not in message
        assert "should never be logged" not in message
        assert "Traceback" not in message


# --- IPv6 loopback ---------------------------------------------------------


def test_server_can_bind_to_ipv6_loopback():
    whatsapp_config = WhatsAppConfig(
        verify_token=_VERIFY_TOKEN,
        app_secret=_APP_SECRET,
        access_token="test-access-token",
        phone_number_id=_PHONE_NUMBER_ID,
        authorized_sender_id=_AUTHORIZED_SENDER,
        host="::1",
        port=0,
        api_version="v23.0",
    )
    handler = MessageHandler(FakeOrchestrator(), RecordingClient())

    try:
        server = WhatsAppServer(whatsapp_config, handler)
    except OSError as error:
        pytest.skip(f"IPv6 loopback not available on this system: {error}")

    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        host = server.server_address[0]
        port = server.server_address[1]
        url = (
            f"http://[{host}]:{port}/webhook?hub.mode=subscribe"
            f"&hub.verify_token={_VERIFY_TOKEN}&hub.challenge=ipv6ok"
        )
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"ipv6ok"
    finally:
        server.stop()
        thread.join(timeout=5)


# --- Milestone 46 P1: durable "/task" ingress ---------------------------


def _task_catalog():
    tools_config = ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": object()},
        approved_scripts={},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )
    return build_catalog(ActionRegistry(), tools_config)


# Milestone 46 P2A: dispatch_task_work() now continues straight from a
# fresh plan into REAL execution (P1's dispatch_planning() stopped at
# READY; that boundary no longer exists). The default plan used by most of
# this file's existing tests therefore references open_application
# ("notepad") - a SENSITIVE action, so execution deterministically stops at
# WAITING_FOR_CONFIRMATION (propose_confirmation() is called BEFORE
# SafeTaskExecutor ever would be - see kernel/task_execution/service.py) -
# rather than a non-sensitive action that would actually execute (and would
# need a real, safe target to execute against). Tests that specifically
# need a completed/non-sensitive result use their own dedicated
# list_files-based plan against a tmp_path directory instead - see
# _non_sensitive_plan_raw() below.
def _valid_plan_raw(catalog, objective="Open notepad."):
    entry = next(e for e in catalog if e.action_name == "open_application")
    return json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": objective,
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": entry.catalog_id,
                    "description": "Open notepad.",
                    "expected_result": "Notepad is open.",
                    "depends_on": [],
                }
            ],
        }
    )


def _non_sensitive_plan_raw(catalog, objective="List the downloads folder."):
    entry = next(e for e in catalog if e.action_name == "list_files" and e.resource_key == "downloads")
    return json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": objective,
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": entry.catalog_id,
                    "description": "List the downloads folder.",
                    "expected_result": "Files known.",
                    "depends_on": [],
                }
            ],
        }
    )


class _FakePlannerProvider:
    """Records every prompt it received and returns canned responses in
    order - never a real network call."""

    def __init__(self, responses=None):
        self._responses = list(responses) if responses is not None else []
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        text = self._responses.pop(0) if self._responses else _valid_plan_raw(_task_catalog())
        return ModelResponse(
            text=text, model="fake-planner", input_tokens=0, output_tokens=0, latency_seconds=0.0
        )


class _BlockingPlannerProvider:
    """Blocks send_prompt() until release() is called - mirrors
    BlockingOrchestrator above, used to prove the planner is only ever
    called asynchronously, off the webhook HTTP thread (see
    test_task_planning_never_happens_on_the_webhook_http_thread below)."""

    def __init__(self, response_text=None):
        self._response_text = response_text
        self.started = threading.Event()
        self._release = threading.Event()
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        self.started.set()
        self._release.wait(timeout=5)
        text = self._response_text or _valid_plan_raw(_task_catalog())
        return ModelResponse(
            text=text, model="fake-planner", input_tokens=0, output_tokens=0, latency_seconds=0.0
        )

    def release(self):
        self._release.set()


class _BlockingRespondProvider:
    """Blocks send_prompt() until release() is called - the P2A analogue
    of _BlockingPlannerProvider, used to prove RESPOND synthesis (and, by
    extension, the whole execution phase around it) also never happens on
    the webhook HTTP thread."""

    def __init__(self, response_text="ok"):
        self._response_text = response_text
        self.started = threading.Event()
        self._release = threading.Event()
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        self.started.set()
        self._release.wait(timeout=5)
        return ModelResponse(
            text=self._response_text, model="fake-respond", input_tokens=0, output_tokens=0, latency_seconds=0.0
        )

    def release(self):
        self._release.set()


def _task_catalog_with_downloads(downloads_dir):
    # A planning-time catalog that also registers a "downloads" directory -
    # used only by tests that specifically need a non-sensitive, genuinely
    # completing task at the real HTTP+worker level (list_files requires
    # no grounding - catalog.py:_requires_grounding() - so the request text
    # need not name the directory).
    tools_config = ToolsConfig(
        approved_directories={"downloads": str(downloads_dir)},
        approved_applications={"notepad": object()},
        approved_scripts={},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )
    return build_catalog(ActionRegistry(), tools_config)


def _execution_tools_config_with_downloads(downloads_dir):
    return ToolsConfig(
        approved_directories={"downloads": str(downloads_dir)},
        approved_applications={"notepad": object()},
        approved_scripts={},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )


def _default_execution_tools_config():
    # Deliberately identical to _task_catalog()'s own ToolsConfig - by
    # default, execution-time authority agrees with planning-time
    # authority, so the default open_application/"notepad" plan reaches
    # WAITING_FOR_CONFIRMATION rather than an unrelated revalidation
    # failure. Tests that specifically want them to disagree (fresh-config
    # revalidation) pass their own tools_config_loader instead.
    return ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": object()},
        approved_scripts={},
        approved_repositories={"ai_os": object()},
        approved_backups={"ai_os": RepoBackupSpec(destination_directory="/x")},
    )


class _FakeRespondProvider:
    """Records every prompt it received and returns canned responses in
    order - never a real network call. Distinct type from
    _FakePlannerProvider purely for readability at call sites; behavior is
    identical."""

    def __init__(self, responses=None):
        self._responses = list(responses) if responses is not None else []
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        text = self._responses.pop(0) if self._responses else "ok"
        return ModelResponse(
            text=text, model="fake-respond", input_tokens=0, output_tokens=0, latency_seconds=0.0
        )


def _make_server_with_tasks(
    tmp_path,
    planner_provider,
    *,
    orchestrator=None,
    queue_capacity=DEFAULT_QUEUE_CAPACITY,
    tools_config_loader=None,
    respond_provider=None,
    task_catalog=None,
    client=None,
):
    """Builds a real WhatsAppServer with Milestone 46 P1 durable-ingress
    and P2A execution/delivery wiring - a real tmp_path SQLite database
    (schema pre-initialized, like build_server()'s own composition root), a
    real worker-owned TaskRepository, a real ActionRegistry/SafeTaskExecutor,
    and the given (fake) planner/respond providers. Returns
    (server, client, db_path) - the caller is responsible for
    starting/stopping the server, matching every other inline server
    construction in this file."""

    whatsapp_config = _make_whatsapp_config()
    db_path = tmp_path / "tasks.sqlite3"

    schema_init_connection = open_writer_connection(db_path)
    schema_init_connection.close()

    worker_connection = open_writer_connection(db_path)
    worker_repository = TaskRepository(worker_connection)

    client = client if client is not None else RecordingClient()
    handler = MessageHandler(
        orchestrator if orchestrator is not None else FakeOrchestrator("ordinary chat reply"),
        client,
        task_repository=worker_repository,
        task_catalog=task_catalog if task_catalog is not None else _task_catalog(),
        planner_provider=planner_provider,
        action_registry=ActionRegistry(),
        tools_config_loader=tools_config_loader or _default_execution_tools_config,
        respond_provider=respond_provider if respond_provider is not None else _FakeRespondProvider(),
        authorized_sender=_AUTHORIZED_SENDER,
    )
    server = WhatsAppServer(
        whatsapp_config,
        handler,
        queue_capacity=queue_capacity,
        task_db_path=db_path,
        worker_task_connection=worker_connection,
    )
    return server, client, db_path


def _list_tasks(db_path):
    conn = open_writer_connection(db_path)
    try:
        return TaskRepository(conn).list_tasks(limit=100)
    finally:
        conn.close()


def test_task_request_is_durably_accepted_planned_and_executed(tmp_path):
    # Milestone 46 P2A: a single TaskExecutionWork dispatch now carries a
    # freshly-planned task all the way through planning and execution in
    # one continuous worker turn. The default plan references a sensitive
    # action (open_application), so it deterministically stops at
    # WAITING_FOR_CONFIRMATION - propose_confirmation() is reached before
    # SafeTaskExecutor ever would be, so no real action executes here.
    planner = _FakePlannerProvider([_valid_plan_raw(_task_catalog())])
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        status = _post(server, _envelope("wamid.task1", _AUTHORIZED_SENDER, text="/task open notepad"))
        assert status == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not client.sent:
            time.sleep(0.02)

        tasks = _list_tasks(db_path)
        assert len(tasks) == 1
        assert tasks[0].source == "whatsapp"
        assert tasks[0].request_text == "open notepad"
        assert tasks[0].state == TaskState.WAITING_FOR_CONFIRMATION

        # P2A sends exactly one confirmation-request message - never a
        # separate "Task accepted" notice (that was never added).
        assert len(client.sent) == 1
        assert client.sent[0][0] == _AUTHORIZED_SENDER
        assert "Action: open_application" in client.sent[0][1]
    finally:
        server.stop()
        thread.join(timeout=5)


def test_task_planning_never_happens_on_the_webhook_http_thread(tmp_path):
    planner = _BlockingPlannerProvider()
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        # The webhook POST must complete and return 200 WITHOUT ever
        # waiting on the (currently blocked) planner call - this is a
        # call-path proof, not a timing race: urlopen() only returns once
        # the HTTP response has actually been sent, so reaching this
        # assertion already proves do_POST returned while the planner
        # provider was still parked on its own Event.
        status = _post(server, _envelope("wamid.block1", _AUTHORIZED_SENDER, text="/task check repo health"))
        assert status == 200

        # The durable row already exists even though the model call has not
        # returned yet - proving durable acceptance itself also never waited
        # on the model. advance_task_planning() moves CREATED -> PLANNING
        # before ever calling the model, so PLANNING (not CREATED) is the
        # state observed here while the fake provider is still blocked.
        tasks = _list_tasks(db_path)
        assert len(tasks) == 1
        assert tasks[0].state == TaskState.PLANNING

        # Only now does the worker thread reach the (still-blocked) model
        # call - proving planning genuinely happens, just asynchronously.
        assert planner.started.wait(timeout=2)
    finally:
        planner.release()
        server.stop()
        thread.join(timeout=5)


def test_task_execution_never_happens_on_the_webhook_http_thread(tmp_path):
    # P2A extension of test_task_planning_never_happens_on_the_webhook_http_thread
    # above: planning here uses a fast, non-blocking fake, but RESPOND
    # synthesis blocks - proving the whole execution phase (SafeTaskExecutor
    # invocation, then RESPOND) also runs exclusively on the worker thread,
    # never on the webhook HTTP thread.
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    catalog = _task_catalog_with_downloads(downloads_dir)
    entry = next(e for e in catalog if e.action_name == "list_files" and e.resource_key == "downloads")
    plan_raw = json.dumps(
        {
            "plan_version": 1,
            "result": "plan",
            "objective": "List the downloads folder and summarize it.",
            "steps": [
                {
                    "step_kind": "action",
                    "catalog_id": entry.catalog_id,
                    "description": "List the downloads folder.",
                    "expected_result": "Files known.",
                    "depends_on": [],
                },
                {
                    "step_kind": "respond",
                    "description": "Summarize the files.",
                    "expected_result": "A summary.",
                    "depends_on": [1],
                },
            ],
        }
    )
    planner = _FakePlannerProvider([plan_raw])
    respond_provider = _BlockingRespondProvider()
    server, client, db_path = _make_server_with_tasks(
        tmp_path,
        planner,
        task_catalog=catalog,
        tools_config_loader=lambda: _execution_tools_config_with_downloads(downloads_dir),
        respond_provider=respond_provider,
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        status = _post(server, _envelope("wamid.execblock1", _AUTHORIZED_SENDER, text="/task list downloads"))
        assert status == 200

        # Only now does the worker thread reach the (still-blocked) RESPOND
        # call - proving the ACTION step already executed, and the RESPOND
        # step was reached, entirely asynchronously.
        assert respond_provider.started.wait(timeout=2)

        # The task is durably RUNNING (past READY, mid-execution) while the
        # webhook HTTP thread has already returned - the HTTP response
        # cannot have waited on this in-progress execution.
        tasks = _list_tasks(db_path)
        assert len(tasks) == 1
        assert tasks[0].state == TaskState.RUNNING
        assert client.sent == []
    finally:
        respond_provider.release()
        server.stop()
        thread.join(timeout=5)


def test_non_sensitive_task_completes_with_one_result_message_via_real_http(tmp_path):
    # A full, real HTTP + worker + composition-root E2E: a non-sensitive
    # task actually executes and completes, delivering exactly one result
    # message - not just the confirmation path most of this file's other
    # tests exercise.
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    (downloads_dir / "report.txt").write_text("hello")
    catalog = _task_catalog_with_downloads(downloads_dir)
    planner = _FakePlannerProvider([_non_sensitive_plan_raw(catalog)])
    server, client, db_path = _make_server_with_tasks(
        tmp_path,
        planner,
        task_catalog=catalog,
        tools_config_loader=lambda: _execution_tools_config_with_downloads(downloads_dir),
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        assert _post(server, _envelope("wamid.nonsensitive1", _AUTHORIZED_SENDER, text="/task list downloads")) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not client.sent:
            time.sleep(0.02)

        tasks = _list_tasks(db_path)
        assert len(tasks) == 1
        assert tasks[0].state == TaskState.COMPLETED
        assert len(client.sent) == 1
        assert "report.txt" in client.sent[0][1]

        # A retried delivery of the exact same provider message must not
        # resend the already-delivered result - transition-triggered
        # delivery, proven here at the real HTTP+worker level.
        assert _post(server, _envelope("wamid.nonsensitive1", _AUTHORIZED_SENDER, text="/task list downloads")) == 200
        time.sleep(0.1)
        assert len(client.sent) == 1
    finally:
        server.stop()
        thread.join(timeout=5)


def test_sensitive_task_duplicate_dispatch_sends_no_second_confirmation_via_real_http(tmp_path):
    planner = _FakePlannerProvider([_valid_plan_raw(_task_catalog())])
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        assert _post(server, _envelope("wamid.dupconf1", _AUTHORIZED_SENDER, text="/task open notepad")) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not client.sent:
            time.sleep(0.02)

        tasks = _list_tasks(db_path)
        assert len(tasks) == 1
        assert tasks[0].state == TaskState.WAITING_FOR_CONFIRMATION
        assert len(client.sent) == 1

        # Same provider message ID redelivered after the task is already
        # WAITING_FOR_CONFIRMATION - dispatch_task_work() must no-op (P2A
        # transition-triggered delivery), never resend the confirmation.
        assert _post(server, _envelope("wamid.dupconf1", _AUTHORIZED_SENDER, text="/task open notepad")) == 200
        time.sleep(0.1)
        assert len(client.sent) == 1
        assert _list_tasks(db_path)[0].state == TaskState.WAITING_FOR_CONFIRMATION
    finally:
        server.stop()
        thread.join(timeout=5)


def test_planning_failure_is_delivered_via_real_http(tmp_path):
    # A plan whose action is real but whose resource_key is not textually
    # grounded in the request text (kernel/task_planner/grounding.py) fails
    # closed at planning - P2A must still deliver exactly one failure
    # message for it, unlike P1 (which only ever produced a silent FAILED
    # row for this case).
    planner = _FakePlannerProvider([_valid_plan_raw(_task_catalog())])
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        # Deliberately ungrounded: the canned plan always selects
        # open_application/"notepad", but this request text never mentions
        # "notepad".
        assert _post(server, _envelope("wamid.planfail1", _AUTHORIZED_SENDER, text="/task check repo health")) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not client.sent:
            time.sleep(0.02)

        tasks = _list_tasks(db_path)
        assert len(tasks) == 1
        assert tasks[0].state == TaskState.FAILED
        assert len(client.sent) == 1
        assert client.sent[0][1].startswith("Task failed:")

        # Redelivery must not resend the failure message a second time.
        assert _post(server, _envelope("wamid.planfail1", _AUTHORIZED_SENDER, text="/task check repo health")) == 200
        time.sleep(0.1)
        assert len(client.sent) == 1
    finally:
        server.stop()
        thread.join(timeout=5)


def test_worker_survives_a_lifecycle_send_failure_and_processes_the_next_task(tmp_path, caplog):
    # Milestone 46 adversarial review, M2/worker-survival: a real HTTP +
    # worker-queue-level proof that a lifecycle-delivery send failure on
    # one task does not kill the worker or corrupt that task's durable
    # state, and that a second, later-queued task is still processed (and
    # delivered) correctly afterward.
    planner = _FakePlannerProvider()  # always falls back to the default open_application/notepad plan
    client = FailFirstSendClient()
    server, _client_unused, db_path = _make_server_with_tasks(tmp_path, planner, client=client)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        with caplog.at_level(logging.WARNING):
            # Task A: its confirmation-request send fails (the client's
            # first call).
            assert _post(server, _envelope("wamid.survivea", _AUTHORIZED_SENDER, text="/task open notepad")) == 200
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not _list_tasks(db_path):
                time.sleep(0.02)

            tasks_after_a = _list_tasks(db_path)
            assert len(tasks_after_a) == 1
            # The durable WAITING_FOR_CONFIRMATION transition already
            # committed before the send was even attempted - a send
            # failure never rolls it back.
            assert tasks_after_a[0].state == TaskState.WAITING_FOR_CONFIRMATION
            assert client.sent == []  # the one send attempt so far failed

            # Task B: a distinct request, still grounded to "notepad" -
            # its confirmation-request send succeeds (the client's second
            # call), proving the worker is still alive and processing.
            assert _post(server, _envelope("wamid.surviveb", _AUTHORIZED_SENDER, text="/task open notepad again")) == 200
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and len(client.sent) == 0:
                time.sleep(0.02)

        assert len(client.sent) == 1
        assert "Action: open_application" in client.sent[0][1]

        tasks_final = _list_tasks(db_path)
        assert len(tasks_final) == 2
        assert all(t.state == TaskState.WAITING_FOR_CONFIRMATION for t in tasks_final)

        # No raw exception detail (the fake's message text) ever reached
        # the logs - only the generic, bounded outbound-failure category.
        for record in caplog.records:
            assert "simulated outbound failure" not in record.getMessage()
    finally:
        server.stop()
        thread.join(timeout=5)


def test_task_storage_failure_returns_retryable_status_and_creates_nothing(tmp_path, monkeypatch):
    import kernel.employee_tasks.repository as repository_module

    def raise_storage_error(self, *args, **kwargs):
        from kernel.employee_tasks import TaskStorageUnavailableError
        raise TaskStorageUnavailableError("simulated failure")

    monkeypatch.setattr(repository_module.TaskRepository, "create_task", raise_storage_error)

    planner = _FakePlannerProvider()
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        status = _post(server, _envelope("wamid.fail1", _AUTHORIZED_SENDER, text="/task open notepad"))
        assert status == 503

        time.sleep(0.1)
        assert _list_tasks(db_path) == []
        assert planner.calls == []  # never even reached the worker
        assert client.sent == []
    finally:
        server.stop()
        thread.join(timeout=5)


def test_duplicate_task_message_resolves_same_task_no_second_row(tmp_path):
    planner = _FakePlannerProvider([_valid_plan_raw(_task_catalog())])
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        envelope = _envelope("wamid.dup_task", _AUTHORIZED_SENDER, text="/task check repo health")
        assert _post(server, envelope) == 200
        assert _post(server, envelope) == 200  # exact redelivery, same message id

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(_list_tasks(db_path)) == 0:
            time.sleep(0.02)

        tasks = _list_tasks(db_path)
        assert len(tasks) == 1
    finally:
        server.stop()
        thread.join(timeout=5)


def test_duplicate_message_id_with_different_content_does_not_mutate_original(tmp_path):
    # Milestone 46 adversarial review, §13: the same provider message ID
    # redelivered with DIFFERENT /task text must never replace the
    # original task's intent, never create a second TaskRecord, and never
    # trigger a second plan from the changed body - exercised through the
    # real server path, not only task_control.py directly.
    planner = _FakePlannerProvider([_valid_plan_raw(_task_catalog())])
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        first_envelope = _envelope("wamid.samekey", _AUTHORIZED_SENDER, text="/task check repo health")
        assert _post(server, first_envelope) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(_list_tasks(db_path)) == 0:
            time.sleep(0.02)
        original_task_id = _list_tasks(db_path)[0].task_id

        # Same message ID, deliberately different /task text.
        second_envelope = _envelope("wamid.samekey", _AUTHORIZED_SENDER, text="/task open notepad")
        assert _post(server, second_envelope) == 200

        time.sleep(0.1)  # give any (incorrect) second dispatch a chance to happen

        tasks = _list_tasks(db_path)
        assert len(tasks) == 1, "a redelivered message ID with different content must not create a second row"
        assert tasks[0].task_id == original_task_id
        assert tasks[0].request_text == "check repo health", "original request text must remain authoritative"
        # Only the original request was ever planned - a second, different
        # plan was never generated from the changed body.
        assert len(planner.calls) <= 1
    finally:
        server.stop()
        thread.join(timeout=5)


def test_task_queue_full_returns_503_keeps_durable_row_and_retries_via_dedup(tmp_path):
    planner = _BlockingPlannerProvider()
    server, client, db_path = _make_server_with_tasks(tmp_path, planner, queue_capacity=1)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        # A, B, and C all mention "notepad" - the _BlockingPlannerProvider's
        # default response (P2A) is a fixed open_application/"notepad" plan
        # regardless of which task it is planning for, and P2A's
        # dispatch_task_work() now continues straight from planning into
        # capability-grounding-validated execution, so every request text
        # reaching this default plan must textually ground "notepad" (see
        # kernel/task_planner/grounding.py) or planning itself fails
        # closed - a request text mismatch here is a real planner rejection,
        # not a queue/dedup concern, which is what this test is actually
        # about. A/B/C stay textually distinct from each other only via
        # their surrounding words.

        # A: dequeued immediately, blocks the worker inside the planner call.
        assert _post(server, _envelope("wamid.qa", _AUTHORIZED_SENDER, text="/task open notepad for health check")) == 200
        assert planner.started.wait(timeout=2)

        # B: occupies the queue's one free slot (never dequeued while A blocks).
        assert _post(server, _envelope("wamid.qb", _AUTHORIZED_SENDER, text="/task open notepad")) == 200

        # C: a NEW /task - the queue is full, so this must be 503, and its
        # durable row must still exist (never deleted merely because the
        # queue overflowed).
        status = _post(server, _envelope("wamid.qc", _AUTHORIZED_SENDER, text="/task open notepad also please"))
        assert status == 503

        tasks_after_overflow = _list_tasks(db_path)
        assert len(tasks_after_overflow) == 3  # A, B, and the durably-created-but-unqueued C
        c_task = next(t for t in tasks_after_overflow if t.request_text == "open notepad also please")
        assert c_task.state == TaskState.CREATED

        planner.release()

        # Let A and B both fully drain (the worker dequeues and processes
        # one at a time) before retrying C, so the queue is deterministically
        # empty for the retry - never a wall-clock guess.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(
            t.state == TaskState.CREATED for t in _list_tasks(db_path) if t.request_text != "open notepad also please"
        ):
            time.sleep(0.02)
        # Both A and B use the default sensitive open_application plan, so
        # each deterministically stops at WAITING_FOR_CONFIRMATION once
        # planning completes and execution runs (P2A: planning no longer
        # ends the flow at READY; that boundary no longer exists).
        assert all(
            t.state == TaskState.WAITING_FOR_CONFIRMATION
            for t in _list_tasks(db_path)
            if t.request_text != "open notepad also please"
        )

        # A retry of the exact same provider message (C) must resolve the
        # SAME durable row via dedup_key, never create a second one - and
        # the queue is now empty, so it deterministically succeeds. Must
        # still be recognized as the SAME /task message on redelivery - a
        # webhook retry resends the identical message body, so this keeps
        # the "/task " prefix (an earlier version of this test used
        # non-"/task" text here, which silently classified the redelivery
        # as ordinary chat instead of exercising the durable dedup path at
        # all - a real bug the L5 assertion-tightening in the Milestone 46
        # adversarial review correction pass exposed).
        retry_status = _post(
            server, _envelope("wamid.qc", _AUTHORIZED_SENDER, text="/task open notepad also please")
        )
        assert retry_status == 200

        # Wait deterministically for the retry to actually restore dispatch
        # and finish planning - proving the retry worked, not merely that
        # it might have (Milestone 46 adversarial review, L5).
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and next(
            t for t in _list_tasks(db_path) if t.request_text == "open notepad also please"
        ).state == TaskState.CREATED:
            time.sleep(0.02)

        tasks_final = _list_tasks(db_path)
        assert len(tasks_final) == 3  # still exactly 3 - no new row from the retry
        c_task_final = next(t for t in tasks_final if t.request_text == "open notepad also please")
        assert c_task_final.state == TaskState.WAITING_FOR_CONFIRMATION
    finally:
        server.stop()
        thread.join(timeout=5)


# --- H1 regression: worker owns and closes its own DB connection --------


class _ClosingSpyConnection:
    """Wraps a real sqlite3.Connection, recording every close() call -
    everything else is delegated unchanged. sqlite3.Connection instances
    do not support arbitrary attribute assignment (no __dict__), so a
    wrapper is required rather than monkeypatching .close on the instance
    itself."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_stop_does_not_close_worker_connection_while_worker_still_active(tmp_path, monkeypatch):
    """Milestone 46 adversarial review, H1 regression test.

    Reproduces the exact original defect if the fix is reverted: the
    worker is deliberately kept blocked inside a planner call well past
    stop()'s join timeout, proving stop() returns without closing the
    worker's connection, and that the worker - once released - completes
    normally, closes its OWN connection exactly once, and leaves the task
    in its correct durable state (never orphaned in PLANNING, never a
    worker_error from a closed database)."""

    import interfaces.whatsapp.server as server_module

    # Shortened so this test is fast and deterministic - the worker is
    # kept blocked well past this timeout, on purpose.
    monkeypatch.setattr(server_module, "_WORKER_JOIN_TIMEOUT_SECONDS", 0.3)

    whatsapp_config = _make_whatsapp_config()
    db_path = tmp_path / "tasks.sqlite3"

    schema_init_connection = open_writer_connection(db_path)
    schema_init_connection.close()

    real_worker_connection = open_writer_connection(db_path)
    spy_connection = _ClosingSpyConnection(real_worker_connection)
    worker_repository = TaskRepository(spy_connection)

    planner = _BlockingPlannerProvider()
    client = RecordingClient()
    handler = MessageHandler(
        FakeOrchestrator("ordinary chat reply"),
        client,
        task_repository=worker_repository,
        task_catalog=_task_catalog(),
        planner_provider=planner,
        action_registry=ActionRegistry(),
        tools_config_loader=_default_execution_tools_config,
        respond_provider=_FakeRespondProvider(),
        authorized_sender=_AUTHORIZED_SENDER,
    )
    server = server_module.WhatsAppServer(
        whatsapp_config,
        handler,
        task_db_path=db_path,
        worker_task_connection=spy_connection,
    )

    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    # Seed a durable CREATED task directly and dispatch it into the worker
    # queue - this test is about shutdown lifecycle, not ingress, so HTTP
    # is not needed to get a task in flight. Request text must textually
    # ground "notepad" (kernel/task_planner/grounding.py) since the default
    # _BlockingPlannerProvider response is the open_application/"notepad"
    # plan - an ungrounded request text would fail planning validation
    # itself, unrelated to the connection-lifecycle behavior under test.
    task = worker_repository.create_task(
        "open notepad", "whatsapp", dedup_key="whatsapp:h1regression"
    )
    server._queue.put(TaskExecutionWork(task_id=task.task_id))

    try:
        assert planner.started.wait(timeout=2), "worker never reached the planner call"

        # stop()'s bounded join times out (0.3s) while the worker is still
        # blocked inside the planner call - it must still return, and it
        # must NOT close the worker's connection while doing so.
        server.stop()

        assert spy_connection.close_calls == 0, (
            "stop() closed the worker's connection while the worker was still active"
        )

        # Release the blocked planner call - the worker finishes planning,
        # then drains the already-enqueued _STOP sentinel and exits, closing
        # its own connection only at that point.
        planner.release()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and spy_connection.close_calls == 0:
            time.sleep(0.02)

        assert spy_connection.close_calls == 1, "worker connection was never closed after the worker exited"

        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        planner.release()
        thread.join(timeout=5)

    # The task must have finished planning normally through the exact same
    # connection the worker used throughout - never orphaned in PLANNING,
    # never requiring a worker_error to "recover" from a closed database.
    verify_connection = open_writer_connection(db_path)
    try:
        final_task = TaskRepository(verify_connection).get_task(task.task_id)
        # The seeded task planned via the default _BlockingPlannerProvider
        # response, which is the sensitive open_application plan - so once
        # planning finishes, execution (still on this same worker/connection)
        # deterministically stops at WAITING_FOR_CONFIRMATION rather than
        # READY (P2A: planning no longer ends the flow at READY).
        assert final_task.state == TaskState.WAITING_FOR_CONFIRMATION
    finally:
        verify_connection.close()


class _BlockingExecutor:
    """Blocks execute() until release() is called - the H1 shutdown test's
    analogue of _BlockingPlannerProvider/_BlockingRespondProvider, now
    blocking inside the real execution boundary itself (SafeTaskExecutor's
    replacement), since P2A execution can now take real, possibly slow
    action just as easily as planning/RESPOND can."""

    def __init__(self):
        self.started = threading.Event()
        self._release = threading.Event()
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        self.started.set()
        self._release.wait(timeout=5)
        return ActionResult(True, "done", "executed")

    def release(self):
        self._release.set()


def test_stop_does_not_close_worker_connection_while_executor_still_active(tmp_path, monkeypatch):
    """Milestone 46 adversarial review, §20: the same H1 connection-
    ownership guarantee as the planner-blocking test above, now proven
    while the worker is blocked inside the real execution boundary
    (SafeTaskExecutor) instead of the planner call - the mechanism in
    WhatsAppServer.stop()/_run_worker() is generic to WHERE inside
    handle_task() the worker is blocked, and this pins that down
    explicitly for the specific phase P2A newly introduced."""

    import interfaces.whatsapp.server as server_module
    import interfaces.whatsapp.task_control as task_control_module

    monkeypatch.setattr(server_module, "_WORKER_JOIN_TIMEOUT_SECONDS", 0.3)

    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()

    blocking_executor = _BlockingExecutor()
    monkeypatch.setattr(task_control_module, "SafeTaskExecutor", lambda *a, **k: blocking_executor)

    whatsapp_config = _make_whatsapp_config()
    db_path = tmp_path / "tasks.sqlite3"

    schema_init_connection = open_writer_connection(db_path)
    schema_init_connection.close()

    real_worker_connection = open_writer_connection(db_path)
    spy_connection = _ClosingSpyConnection(real_worker_connection)
    worker_repository = TaskRepository(spy_connection)

    tools_config = ToolsConfig(
        approved_directories={"downloads": str(downloads_dir)},
        approved_applications={}, approved_scripts={}, approved_repositories={}, approved_backups={},
    )
    client = RecordingClient()
    handler = MessageHandler(
        FakeOrchestrator("ordinary chat reply"),
        client,
        task_repository=worker_repository,
        task_catalog=_task_catalog(),
        planner_provider=_FakePlannerProvider([]),
        action_registry=ActionRegistry(),
        tools_config_loader=lambda: tools_config,
        respond_provider=_FakeRespondProvider(),
        authorized_sender=_AUTHORIZED_SENDER,
    )
    server = server_module.WhatsAppServer(
        whatsapp_config,
        handler,
        task_db_path=db_path,
        worker_task_connection=spy_connection,
    )

    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    # Seed a task straight into READY with a single non-sensitive ACTION
    # step - this test is about shutdown lifecycle during execution, not
    # ingress or planning, so neither HTTP nor a real planner call is
    # needed to get the worker blocked inside the executor.
    record = worker_repository.create_task("list downloads", "whatsapp", dedup_key="whatsapp:executorblock")
    worker_repository.transition_task(record.task_id, TaskState.CREATED, TaskState.PLANNING)
    plan = TaskPlan(
        plan_version=1,
        task_id=record.task_id,
        objective="List downloads.",
        steps=(
            PlanStep(
                step_id="step_1", position=1, kind=StepKind.ACTION,
                action_name="list_files", resource_key="downloads",
                catalog_id="action_1", description="list", expected_result="files",
                depends_on=(), requires_confirmation=False,
            ),
        ),
        created_at="2026-08-08T00:00:00+00:00",
    )
    task = worker_repository.persist_plan_and_ready(record.task_id, TaskState.PLANNING, serialize_plan(plan))
    server._queue.put(TaskExecutionWork(task_id=task.task_id))

    try:
        assert blocking_executor.started.wait(timeout=2), "worker never reached the executor call"

        # stop()'s bounded join times out (0.3s) while the worker is still
        # blocked inside execute() - it must still return, and it must NOT
        # close the worker's connection while doing so.
        server.stop()

        assert spy_connection.close_calls == 0, (
            "stop() closed the worker's connection while the worker was still active"
        )

        # Release the blocked executor call - the worker finishes
        # execution, then drains the already-enqueued _STOP sentinel and
        # exits, closing its own connection only at that point.
        blocking_executor.release()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and spy_connection.close_calls == 0:
            time.sleep(0.02)

        assert spy_connection.close_calls == 1, "worker connection was never closed after the worker exited"

        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        blocking_executor.release()
        thread.join(timeout=5)

    verify_connection = open_writer_connection(db_path)
    try:
        final_task = TaskRepository(verify_connection).get_task(task.task_id)
        assert final_task.state == TaskState.COMPLETED
    finally:
        verify_connection.close()


def test_stop_logs_pending_not_stopped_when_worker_still_active(tmp_path, monkeypatch, caplog):
    """stop() must never claim the worker has stopped when it has not -
    only a generic, bounded log line, never request/task/SQL/provider
    detail."""

    import logging

    import interfaces.whatsapp.server as server_module

    monkeypatch.setattr(server_module, "_WORKER_JOIN_TIMEOUT_SECONDS", 0.3)

    planner = _BlockingPlannerProvider()
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        assert _post(server, _envelope("wamid.pending1", _AUTHORIZED_SENDER, text="/task check repo health")) == 200
        assert planner.started.wait(timeout=2)

        with caplog.at_level(logging.WARNING, logger="interfaces.whatsapp.server"):
            server.stop()

        assert "worker_shutdown_pending" in caplog.text
        assert "check repo health" not in caplog.text
    finally:
        planner.release()
        thread.join(timeout=5)


def test_concurrent_duplicate_task_messages_create_at_most_one_task(tmp_path):
    planner = _FakePlannerProvider([_valid_plan_raw(_task_catalog()) for _ in range(8)])
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        message_count = 6
        envelope = _envelope("wamid.concurrent1", _AUTHORIZED_SENDER, text="/task check repo health")
        barrier = threading.Barrier(message_count)
        results = [None] * message_count

        def worker(i):
            barrier.wait()
            results[i] = _post(server, envelope)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(message_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Every concurrent duplicate must be accepted (200) - the SQLite
        # UNIQUE constraint on dedup_key resolves the race, never a 5xx
        # merely because of the concurrency itself.
        assert all(status == 200 for status in results), results

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(_list_tasks(db_path)) == 0:
            time.sleep(0.02)

        tasks = _list_tasks(db_path)
        assert len(tasks) == 1, "at most one TaskRecord for one provider message"
        assert tasks[0].dedup_key == compute_dedup_key("wamid.concurrent1")
    finally:
        server.stop()
        thread.join(timeout=5)


def test_plain_chat_unaffected_by_task_wiring_no_task_record_created(tmp_path):
    orchestrator = FakeOrchestrator("a bold Malbec would work well")
    planner = _FakePlannerProvider()
    server, client, db_path = _make_server_with_tasks(tmp_path, planner, orchestrator=orchestrator)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        status = _post(server, _envelope("wamid.chat1", _AUTHORIZED_SENDER, text="what wine goes with steak?"))
        assert status == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not client.sent:
            time.sleep(0.02)

        assert orchestrator.received_prompts == ["what wine goes with steak?"]
        assert client.sent == [(_AUTHORIZED_SENDER, "a bold Malbec would work well")]
        assert _list_tasks(db_path) == []
        assert planner.calls == []
    finally:
        server.stop()
        thread.join(timeout=5)


def test_legacy_task_router_and_confirmation_store_never_reached_for_whatsapp_task(tmp_path):
    # The old M33 path is only reachable via Orchestrator.handle() - proving
    # the orchestrator never sees the /task text is sufficient proof
    # TasksCapability/kernel.tools.confirmation.py's ConfirmationStore were
    # never reached either, since neither is reachable any other way.
    orchestrator = FakeOrchestrator("should never be produced for /task")
    planner = _FakePlannerProvider([_valid_plan_raw(_task_catalog())])
    server, client, db_path = _make_server_with_tasks(tmp_path, planner, orchestrator=orchestrator)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        assert _post(server, _envelope("wamid.legacy1", _AUTHORIZED_SENDER, text="/task open notepad")) == 200

        # P2A: dispatch_task_work() continues straight from planning into
        # execution and sends exactly one confirmation message for this
        # sensitive open_application plan - wait for that real delivery
        # deterministically, rather than asserting nothing was ever sent
        # (that would no longer be true under P2A, and would not be
        # evidence of anything - see below for what this test actually
        # proves instead).
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not client.sent:
            time.sleep(0.02)

        assert orchestrator.received_prompts == []
        # The one message sent is task_control.py's own confirmation
        # delivery, never anything the legacy orchestrator/M33 router could
        # have produced (the orchestrator was never even called - proven
        # above) - this is the actual proof this test is about.
        assert len(client.sent) == 1
        assert "Action: open_application" in client.sent[0][1]

        # Ordinary chat, on the SAME server, still reaches the orchestrator
        # exactly as before - this is a migration, not a removal.
        assert _post(server, _envelope("wamid.legacy2", _AUTHORIZED_SENDER, text="hello there")) == 200
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not orchestrator.received_prompts:
            time.sleep(0.02)
        assert orchestrator.received_prompts == ["hello there"]
    finally:
        server.stop()
        thread.join(timeout=5)


def test_standalone_confirm_and_reject_remain_ordinary_chat_in_p1(tmp_path):
    # Milestone 46 adversarial review, §14: bare "CONFIRM <id>"/"REJECT <id>"
    # (not "/task confirm"/"/task cancel") are NOT intercepted in P1 - they
    # must reach the orchestrator as ordinary conversational text, never
    # enter durable task-confirmation handling, and never touch the legacy
    # ConfirmationStore either. P2 will intercept these; P1 must not.
    orchestrator = FakeOrchestrator("ordinary chat response")
    planner = _FakePlannerProvider()
    server, client, db_path = _make_server_with_tasks(tmp_path, planner, orchestrator=orchestrator)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        assert _post(server, _envelope("wamid.confirm1", _AUTHORIZED_SENDER, text="CONFIRM abc-123")) == 200
        assert _post(server, _envelope("wamid.reject1", _AUTHORIZED_SENDER, text="REJECT abc-123")) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(orchestrator.received_prompts) < 2:
            time.sleep(0.02)

        # Reached the orchestrator as plain text, verbatim - never
        # specially parsed, never diverted into a durable TaskRecord.
        assert orchestrator.received_prompts == ["CONFIRM abc-123", "REJECT abc-123"]
        assert client.sent == [
            (_AUTHORIZED_SENDER, "ordinary chat response"),
            (_AUTHORIZED_SENDER, "ordinary chat response"),
        ]
        assert _list_tasks(db_path) == []
        assert planner.calls == []
    finally:
        server.stop()
        thread.join(timeout=5)


def test_mixed_batch_chat_and_task_both_handled(tmp_path):
    orchestrator = FakeOrchestrator("chat reply")
    planner = _FakePlannerProvider([_valid_plan_raw(_task_catalog())])
    server, client, db_path = _make_server_with_tasks(tmp_path, planner, orchestrator=orchestrator)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        payload = _multi_message_envelope([
            ("wamid.mix1", _AUTHORIZED_SENDER, "hello"),
            ("wamid.mix2", _AUTHORIZED_SENDER, "/task check repo health"),
        ])
        assert _post(server, payload) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (not client.sent or len(_list_tasks(db_path)) == 0):
            time.sleep(0.02)

        assert orchestrator.received_prompts == ["hello"]
        tasks = _list_tasks(db_path)
        assert len(tasks) == 1
        assert tasks[0].request_text == "check repo health"
    finally:
        server.stop()
        thread.join(timeout=5)


def test_two_different_task_messages_create_two_independent_tasks(tmp_path):
    planner = _FakePlannerProvider(
        [_valid_plan_raw(_task_catalog()), _valid_plan_raw(_task_catalog())]
    )
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        payload = _multi_message_envelope([
            ("wamid.two1", _AUTHORIZED_SENDER, "/task check repo health"),
            ("wamid.two2", _AUTHORIZED_SENDER, "/task open notepad"),
        ])
        assert _post(server, payload) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(_list_tasks(db_path)) < 2:
            time.sleep(0.02)

        tasks = _list_tasks(db_path)
        assert len(tasks) == 2
        assert {t.dedup_key for t in tasks} == {
            compute_dedup_key("wamid.two1"),
            compute_dedup_key("wamid.two2"),
        }
    finally:
        server.stop()
        thread.join(timeout=5)


def test_task_ingress_unavailable_without_wiring_returns_503(running_server):
    # running_server builds a WhatsAppServer with no task_db_path - a /task
    # message must never be silently accepted as success in that case.
    server, orchestrator, client = running_server
    status = _post(server, _envelope("wamid.nowiring", _AUTHORIZED_SENDER, text="/task open notepad"))
    assert status == 503
    assert orchestrator.received_prompts == []
    assert client.sent == []


def test_request_thread_never_shares_the_workers_connection(tmp_path, monkeypatch):
    # Structural proof (not merely empirical) that a request thread's
    # durable-ingress connection is never the worker's own long-lived
    # connection: server.py's do_POST always calls the real
    # kernel.employee_tasks.open_writer_connection() function fresh, per
    # /task message, and never touches the MessageHandler's
    # worker-owned TaskRepository/connection at all - see
    # task_control.py's own module docstring for why sharing one
    # connection across concurrently-active threads is unsafe (proven in
    # the Milestone 46 P1 design report's empirical concurrency
    # validation).
    import interfaces.whatsapp.server as server_module

    planner = _FakePlannerProvider([_valid_plan_raw(_task_catalog())])
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    worker_connection_id = id(server._worker_task_connection)

    seen_connections = []
    real_open_writer_connection = server_module.open_writer_connection

    def spying_open_writer_connection(path):
        conn = real_open_writer_connection(path)
        seen_connections.append(conn)
        return conn

    # Only patched AFTER server construction - build_server()'s own
    # schema-pre-init and worker-connection opens (already done above)
    # must not be counted here; only per-request opens are under test.
    monkeypatch.setattr(server_module, "open_writer_connection", spying_open_writer_connection)

    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        assert _post(server, _envelope("wamid.conn1", _AUTHORIZED_SENDER, text="/task check repo health")) == 200
        assert _post(server, _envelope("wamid.conn2", _AUTHORIZED_SENDER, text="/task open notepad")) == 200
    finally:
        server.stop()
        thread.join(timeout=5)

    # Each /task request opened its own connection object - none of them
    # is the same object as another, and none is the worker's own
    # long-lived connection.
    assert len(seen_connections) == 2
    assert seen_connections[0] is not seen_connections[1]
    assert all(id(conn) != worker_connection_id for conn in seen_connections)


def test_bare_task_and_help_produce_fixed_replies_via_worker(tmp_path):
    planner = _FakePlannerProvider()
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        assert _post(server, _envelope("wamid.help1", _AUTHORIZED_SENDER, text="/task help")) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not client.sent:
            time.sleep(0.02)

        assert len(client.sent) == 1
        assert client.sent[0][0] == _AUTHORIZED_SENDER
        assert _list_tasks(db_path) == []  # help never touches TaskRepository
        assert planner.calls == []
    finally:
        server.stop()
        thread.join(timeout=5)


def test_legacy_task_confirm_produces_migration_reply_no_task_created(tmp_path):
    planner = _FakePlannerProvider()
    server, client, db_path = _make_server_with_tasks(tmp_path, planner)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    time.sleep(0.05)

    try:
        assert _post(server, _envelope("wamid.legconfirm1", _AUTHORIZED_SENDER, text="/task confirm")) == 200

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not client.sent:
            time.sleep(0.02)

        assert len(client.sent) == 1
        assert "CONFIRM" in client.sent[0][1]
        assert _list_tasks(db_path) == []
    finally:
        server.stop()
        thread.join(timeout=5)


# --- build_orchestrator(): the merged memory_manager seam -----------------


class FakeProvider(ModelProvider):
    def __init__(self, response: ModelResponse):
        self._response = response

    def send_prompt(
        self, prompt: str, *, options: ModelRequestOptions | None = None
    ) -> ModelResponse:
        return self._response


class FakeCapability(Capability):
    def __init__(self, capability_id, memory_manager):
        self._id = capability_id
        self._memory = memory_manager

    @property
    def id(self):
        return self._id

    def handle(self, prompt: str) -> str:
        return "ok"


def _make_kernel_config(tmp_path) -> Config:
    return Config(
        provider="fake",
        provider_settings={},
        log_path=tmp_path / "logs" / "interactions.jsonl",
        memory_settings={"storage_dir": str(tmp_path / "memory")},
        knowledge_storage_dir=tmp_path / "knowledge",
    )


def test_build_orchestrator_scopes_all_memory_to_a_single_fixed_namespace(tmp_path, monkeypatch):
    config = _make_kernel_config(tmp_path)
    fake_response = ModelResponse(
        text="it's sunny tomorrow", model="fake-model",
        input_tokens=1, output_tokens=1, latency_seconds=0.01,
    )
    monkeypatch.setattr(
        "kernel.orchestrator.orchestrator.get_provider",
        lambda cfg: FakeProvider(fake_response),
    )

    def unreachable_loader(*args):
        raise AssertionError("this prompt should not route to a capability")

    orchestrator = build_orchestrator(config, capability_loader=unreachable_loader)
    orchestrator.handle("What's the weather tomorrow?")

    verification_memory = MemoryManager(config.memory_settings)
    assert [e.content for e in verification_memory.recall("whatsapp")] == [
        "What's the weather tomorrow?",
        "it's sunny tomorrow",
    ]
    assert verification_memory.recall("conversation") == []


def test_build_orchestrator_passes_the_same_scoped_memory_to_the_capability_loader(
    tmp_path, monkeypatch
):
    config = _make_kernel_config(tmp_path)
    fake_response = ModelResponse(
        text="fallback", model="fake-model",
        input_tokens=1, output_tokens=1, latency_seconds=0.01,
    )
    monkeypatch.setattr(
        "kernel.orchestrator.orchestrator.get_provider",
        lambda cfg: FakeProvider(fake_response),
    )

    captured = {}

    def capability_loader(capability_id, provider, memory, knowledge):
        captured["memory"] = memory
        return FakeCapability(capability_id, memory)

    orchestrator = build_orchestrator(config, capability_loader=capability_loader)
    orchestrator.handle("What wine goes with steak?")

    assert isinstance(captured["memory"], FixedNamespaceMemory)
