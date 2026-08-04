"""Tests for kernel/models/ollama.py: request shape and the Milestone 38
fixed request timeout. Every test mocks urllib.request.urlopen - none
contacts a real Ollama server or any network."""

import json
import socket

import pytest

from kernel.models.ollama import OLLAMA_REQUEST_TIMEOUT_SECONDS, OllamaProvider


def _settings(**overrides):
    base = {
        "model": "llama3.1:8b",
        "base_url": "http://localhost:11434",
        "max_tokens": 1024,
        "temperature": 1.0,
    }
    base.update(overrides)
    return base


class _FakeHTTPResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def _success_body(text="hello", model="llama3.1:8b"):
    return {"response": text, "model": model, "prompt_eval_count": 5, "eval_count": 7}


def test_fixed_timeout_constant_is_120_seconds():
    assert OLLAMA_REQUEST_TIMEOUT_SECONDS == 120


def test_fixed_timeout_is_passed_to_urlopen(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["timeout"] = timeout
        return _FakeHTTPResponse(_success_body())

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", fake_urlopen)

    provider = OllamaProvider(_settings())
    provider.send_prompt("hello")

    assert captured["timeout"] == OLLAMA_REQUEST_TIMEOUT_SECONDS


def test_request_body_unchanged(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse(_success_body())

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", fake_urlopen)

    provider = OllamaProvider(_settings(model="llama3.1:8b", temperature=0.7, max_tokens=512))
    provider.send_prompt("a prompt")

    assert captured["url"] == "http://localhost:11434/api/generate"
    assert captured["method"] == "POST"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["body"] == {
        "model": "llama3.1:8b",
        "prompt": "a prompt",
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 512},
    }


def test_stream_remains_false(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse(_success_body())

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", fake_urlopen)

    OllamaProvider(_settings()).send_prompt("hi")

    assert captured["body"]["stream"] is False


def test_successful_response_parsed_into_model_response(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_success_body(text="a generated answer"))

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", fake_urlopen)

    response = OllamaProvider(_settings()).send_prompt("hi")

    assert response.text == "a generated answer"
    assert response.model == "llama3.1:8b"
    assert response.input_tokens == 5
    assert response.output_tokens == 7


def test_timeout_exception_propagates_as_provider_exception(monkeypatch):
    def timing_out(request, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", timing_out)

    with pytest.raises(socket.timeout):
        OllamaProvider(_settings()).send_prompt("hi")


def test_connection_failure_propagates_as_provider_exception(monkeypatch):
    import urllib.error

    def failing(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", failing)

    with pytest.raises(urllib.error.URLError):
        OllamaProvider(_settings()).send_prompt("hi")


def test_no_retry_on_failure(monkeypatch):
    call_count = 0

    def failing(request, timeout=None):
        nonlocal call_count
        call_count += 1
        raise ConnectionError("refused")

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", failing)

    with pytest.raises(ConnectionError):
        OllamaProvider(_settings()).send_prompt("hi")

    assert call_count == 1


def test_no_real_network_contact(monkeypatch):
    # Any attempt to actually open a socket during this test would raise,
    # proving urlopen is fully intercepted rather than merely fast.
    def explode(*args, **kwargs):
        raise AssertionError("a real network call must never happen in this test")

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", explode)

    with pytest.raises(AssertionError):
        OllamaProvider(_settings()).send_prompt("hi")


def test_timeout_exception_not_logged_with_prompt_data(monkeypatch, caplog):
    import logging

    def timing_out(request, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", timing_out)

    secret_prompt = "a-very-specific-secret-prompt-token"
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(socket.timeout):
            OllamaProvider(_settings()).send_prompt(secret_prompt)

    for record in caplog.records:
        assert secret_prompt not in record.getMessage()
