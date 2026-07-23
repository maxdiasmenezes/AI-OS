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
from kernel.capabilities.base import Capability
from kernel.config.config import Config
from kernel.memory import MemoryManager
from kernel.models.base import ModelProvider, ModelResponse

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


def _model_response(text: str) -> ModelResponse:
    return ModelResponse(
        text=text, model="fake-model", input_tokens=1, output_tokens=1, latency_seconds=0.01
    )


class FakeOrchestrator:
    def __init__(self, text="a reply"):
        self._text = text
        self.received_prompts = []

    def handle(self, prompt):
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

    def handle(self, prompt):
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


# --- build_orchestrator(): the merged memory_manager seam -----------------


class FakeProvider(ModelProvider):
    def __init__(self, response: ModelResponse):
        self._response = response

    def send_prompt(self, prompt: str) -> ModelResponse:
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
