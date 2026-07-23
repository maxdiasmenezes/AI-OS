"""Tests for the urllib-based WhatsApp Cloud API client. No real network call."""

import json
import urllib.error

import pytest

from interfaces.whatsapp.client import DEFAULT_TIMEOUT_SECONDS, WhatsAppClient, WhatsAppClientError


class _FakeResponse:
    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _success_body(message_id: str = "wamid.OUT1") -> bytes:
    return json.dumps({"messages": [{"id": message_id}]}).encode("utf-8")


def test_send_text_message_posts_expected_request():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(_success_body())

    client = WhatsAppClient("test-token", "1234567890", "v23.0", urlopen=fake_urlopen)
    client.send_text_message("15551234567", "hello there")

    assert captured["url"] == "https://graph.facebook.com/v23.0/1234567890/messages"
    assert captured["method"] == "POST"
    assert captured["headers"]["authorization"] == "Bearer test-token"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["body"] == {
        "messaging_product": "whatsapp",
        "to": "15551234567",
        "type": "text",
        "text": {"body": "hello there"},
    }
    assert captured["timeout"] == DEFAULT_TIMEOUT_SECONDS


def test_default_timeout_is_10_seconds():
    assert DEFAULT_TIMEOUT_SECONDS == 10


def test_timeout_is_injectable():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["timeout"] = timeout
        return _FakeResponse(_success_body())

    client = WhatsAppClient(
        "test-token", "1234567890", "v23.0", timeout_seconds=2.5, urlopen=fake_urlopen
    )
    client.send_text_message("15551234567", "hi")

    assert captured["timeout"] == 2.5


def test_successful_send_returns_the_outbound_message_id():
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(_success_body("wamid.ABC123"))

    client = WhatsAppClient("test-token", "1234567890", "v23.0", urlopen=fake_urlopen)

    message_id = client.send_text_message("15551234567", "hello")

    assert message_id == "wamid.ABC123"


def test_malformed_json_response_raises():
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(b"not json")

    client = WhatsAppClient("test-token", "1234567890", "v23.0", urlopen=fake_urlopen)

    with pytest.raises(WhatsAppClientError):
        client.send_text_message("15551234567", "hello")


def test_response_missing_message_id_raises():
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(json.dumps({"messages": [{}]}).encode("utf-8"))

    client = WhatsAppClient("test-token", "1234567890", "v23.0", urlopen=fake_urlopen)

    with pytest.raises(WhatsAppClientError):
        client.send_text_message("15551234567", "hello")


def test_response_with_empty_messages_list_raises():
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(json.dumps({"messages": []}).encode("utf-8"))

    client = WhatsAppClient("test-token", "1234567890", "v23.0", urlopen=fake_urlopen)

    with pytest.raises(WhatsAppClientError):
        client.send_text_message("15551234567", "hello")


def test_transport_failure_raises_whatsapp_client_error():
    def failing_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    client = WhatsAppClient("test-token", "1234567890", "v23.0", urlopen=failing_urlopen)

    with pytest.raises(WhatsAppClientError):
        client.send_text_message("15551234567", "hello")


def test_client_never_retries_after_a_failure():
    call_count = 0

    def failing_urlopen(request, timeout=None):
        nonlocal call_count
        call_count += 1
        raise urllib.error.URLError("connection refused")

    client = WhatsAppClient("test-token", "1234567890", "v23.0", urlopen=failing_urlopen)

    with pytest.raises(WhatsAppClientError):
        client.send_text_message("15551234567", "hello")

    assert call_count == 1


def test_transport_failure_message_contains_no_sensitive_data():
    def failing_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    client = WhatsAppClient("super-secret-token", "1234567890", "v23.0", urlopen=failing_urlopen)

    with pytest.raises(WhatsAppClientError) as exc_info:
        client.send_text_message("15551234567", "sensitive user text")

    message = str(exc_info.value)
    assert "super-secret-token" not in message
    assert "15551234567" not in message
    assert "sensitive user text" not in message
