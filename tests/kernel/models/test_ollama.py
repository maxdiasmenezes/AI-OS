"""Tests for kernel/models/ollama.py: request shape and the Milestone 38
fixed request timeout, plus Milestone 39's ModelRequestOptions (require_json,
json_schema, temperature_override). Every test mocks urllib.request.urlopen -
none contacts a real Ollama server or any network."""

import json
import socket

import pytest

from kernel.models.base import ModelRequestOptions
from kernel.models.ollama import (
    MAX_TEMPERATURE_OVERRIDE,
    MIN_TEMPERATURE_OVERRIDE,
    OLLAMA_REQUEST_TIMEOUT_SECONDS,
    OllamaProvider,
)


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


# --- Milestone 39: ModelRequestOptions ---------------------------------------


def _capture_body(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeHTTPResponse(_success_body())

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", fake_urlopen)
    return captured


def test_no_options_payload_is_byte_identical_to_pre_milestone_39(monkeypatch):
    captured = _capture_body(monkeypatch)

    OllamaProvider(_settings(temperature=0.7, max_tokens=512)).send_prompt("a prompt")

    assert captured["body"] == {
        "model": "llama3.1:8b",
        "prompt": "a prompt",
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 512},
    }
    assert "format" not in captured["body"]


def test_options_with_require_json_false_matches_no_options(monkeypatch):
    captured = _capture_body(monkeypatch)

    OllamaProvider(_settings()).send_prompt(
        "hi", options=ModelRequestOptions(require_json=False)
    )

    assert "format" not in captured["body"]


def test_require_json_true_without_schema_sets_format_json(monkeypatch):
    captured = _capture_body(monkeypatch)

    OllamaProvider(_settings()).send_prompt("hi", options=ModelRequestOptions(require_json=True))

    assert captured["body"]["format"] == "json"


def test_require_json_true_with_schema_sets_format_to_schema(monkeypatch):
    captured = _capture_body(monkeypatch)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}

    OllamaProvider(_settings()).send_prompt(
        "hi", options=ModelRequestOptions(require_json=True, json_schema=schema)
    )

    assert captured["body"]["format"] == schema


def test_temperature_override_zero_used_only_for_that_request(monkeypatch):
    captured = _capture_body(monkeypatch)

    OllamaProvider(_settings(temperature=1.0)).send_prompt(
        "hi", options=ModelRequestOptions(temperature_override=0.0)
    )

    assert captured["body"]["options"]["temperature"] == 0.0


def test_temperature_override_none_keeps_configured_temperature(monkeypatch):
    captured = _capture_body(monkeypatch)

    OllamaProvider(_settings(temperature=0.42)).send_prompt(
        "hi", options=ModelRequestOptions(temperature_override=None)
    )

    assert captured["body"]["options"]["temperature"] == 0.42


def test_require_json_false_with_schema_is_rejected_locally():
    with pytest.raises(ValueError):
        ModelRequestOptions(require_json=False, json_schema={"type": "object"})


def test_temperature_override_bool_is_rejected_locally():
    with pytest.raises(ValueError):
        ModelRequestOptions(temperature_override=True)


def test_temperature_override_nan_is_rejected_locally():
    with pytest.raises(ValueError):
        ModelRequestOptions(temperature_override=float("nan"))


def test_temperature_override_infinity_is_rejected_locally():
    with pytest.raises(ValueError):
        ModelRequestOptions(temperature_override=float("inf"))


def test_temperature_override_below_provider_range_is_rejected(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("no HTTP request must be made after local validation fails")

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", explode)

    below = MIN_TEMPERATURE_OVERRIDE - 0.5
    with pytest.raises(ValueError):
        OllamaProvider(_settings()).send_prompt(
            "hi", options=ModelRequestOptions(temperature_override=below)
        )


def test_temperature_override_above_provider_range_is_rejected(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("no HTTP request must be made after local validation fails")

    monkeypatch.setattr("kernel.models.ollama.urllib.request.urlopen", explode)

    above = MAX_TEMPERATURE_OVERRIDE + 0.5
    with pytest.raises(ValueError):
        OllamaProvider(_settings()).send_prompt(
            "hi", options=ModelRequestOptions(temperature_override=above)
        )


def test_invalid_options_never_reach_urlopen():
    # ModelRequestOptions itself raises at construction time for a
    # nonsensical combination - never even reaching send_prompt(), let
    # alone urlopen(). No monkeypatch is installed here on purpose: if
    # this test somehow did reach urlopen(), it would attempt a real
    # network call and fail loudly rather than silently pass.
    with pytest.raises(ValueError):
        ModelRequestOptions(require_json=False, json_schema={"type": "object"})


def test_provider_state_unchanged_after_a_structured_request(monkeypatch):
    _capture_body(monkeypatch)
    provider = OllamaProvider(_settings(model="llama3.1:8b", temperature=1.0, max_tokens=1024))

    provider.send_prompt(
        "structured",
        options=ModelRequestOptions(require_json=True, temperature_override=0.0),
    )

    assert provider.temperature == 1.0
    assert provider.model == "llama3.1:8b"
    assert provider.max_tokens == 1024


def test_structured_request_followed_by_ordinary_request_is_unaffected(monkeypatch):
    captured = _capture_body(monkeypatch)
    provider = OllamaProvider(_settings(temperature=1.0))

    provider.send_prompt(
        "structured", options=ModelRequestOptions(require_json=True, temperature_override=0.0)
    )
    assert captured["body"]["format"] == "json"
    assert captured["body"]["options"]["temperature"] == 0.0

    provider.send_prompt("ordinary")
    assert "format" not in captured["body"]
    assert captured["body"]["options"]["temperature"] == 1.0


def test_default_options_none_keyword_matches_omitting_it_entirely(monkeypatch):
    captured_a = _capture_body(monkeypatch)
    OllamaProvider(_settings()).send_prompt("hi")
    body_a = captured_a["body"]

    captured_b = _capture_body(monkeypatch)
    OllamaProvider(_settings()).send_prompt("hi", options=None)
    body_b = captured_b["body"]

    assert body_a == body_b
