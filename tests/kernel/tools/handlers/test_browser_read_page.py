"""Pure/cheap tests for kernel/tools/handlers/browser_read_page.py that do
not require launching a real browser: resource_key resolution, error
mapping, output-bound helpers, and the authoritative worst-case
serialization proof against the REAL build_action_observation()/
serialize_observation() pipeline (mirrors
tests/kernel/tools/handlers/test_read_text_file.py's own
test_worst_case_permitted_content_serializes_within_the_real_observation_bound).

Real-browser behavior (navigation, the network gate, redirects, resource
blocking, etc.) is covered separately in
tests/kernel/tools/test_milestone_44_p1_integration.py via the module's
private _execute_read() test seam."""

from datetime import datetime, timezone

import pytest

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_execution.observation import (
    ObservationSerializationError,
    build_action_observation,
    serialize_observation,
)
from kernel.tools.browser_safety import Origin, PageAuthority
from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH, ApprovedPageSpec, ToolsConfig
from kernel.tools.handlers import browser_read_page
from kernel.tools.types import ActionRequest, ActionResult


def _config(approved_pages):
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_pages=approved_pages,
    )


def _request(resource_key):
    return ActionRequest(action="browser_read_page", resource_key=resource_key)


# --- resource_key resolution (no browser needed) ------------------------------


def test_unregistered_key_is_rejected():
    config = _config({})

    result = browser_read_page.run(_request("example_docs"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert result.message == "That page is not registered."


def test_none_resource_key_is_rejected():
    config = _config({"example_docs": ApprovedPageSpec(url="https://example.com:443/docs")})

    result = browser_read_page.run(_request(None), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_oversized_resource_key_fails_closed_before_lookup():
    long_key = "k" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    config = _config({long_key: ApprovedPageSpec(url="https://example.com:443/docs")})

    result = browser_read_page.run(_request(long_key), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert result.message == "That page is not registered."


def test_manually_built_private_host_spec_is_rejected_at_execution_time():
    # A ToolsConfig built by hand, bypassing load_tools_config()'s own
    # config-load-time validation entirely - _build_authority()'s fresh
    # re-validation must still catch this before any browser is launched.
    config = _config({"bad": ApprovedPageSpec(url="https://127.0.0.1:443/docs")})

    result = browser_read_page.run(_request("bad"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert result.message == "That page is not permitted."
    assert "127.0.0.1" not in result.message


def test_manually_built_http_spec_is_rejected_at_execution_time():
    config = _config({"bad": ApprovedPageSpec(url="http://example.com:80/docs")})

    result = browser_read_page.run(_request("bad"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert result.message == "That page is not permitted."


# --- _bounded() / _safe_location() ---------------------------------------------


def test_bounded_rejects_empty_string_with_no_placeholder():
    value, reason = browser_read_page._bounded("", 100)
    assert value is None
    assert reason == "too_large"


def test_bounded_rejects_oversized_value():
    value, reason = browser_read_page._bounded("x" * 101, 100)
    assert value is None
    assert reason == "too_large"


def test_bounded_accepts_value_at_exact_bound():
    expected = "x" * 100
    value, reason = browser_read_page._bounded(expected, 100)
    assert value == expected
    assert reason is None


def test_bounded_rejects_disallowed_control_character():
    value, reason = browser_read_page._bounded("before\x01after", 100)
    assert value is None
    assert reason == "unreadable"


def test_bounded_allows_ordinary_whitespace():
    expected = "line one\tcol\nline two"
    value, reason = browser_read_page._bounded(expected, 100)
    assert value == expected
    assert reason is None


def test_bounded_empty_with_placeholder_substitutes_placeholder():
    value, reason = browser_read_page._bounded("", 100, placeholder="(none)")
    assert value == "(none)"
    assert reason is None


def test_bounded_non_empty_with_placeholder_still_validated_normally():
    value, reason = browser_read_page._bounded("x" * 101, 100, placeholder="(none)")
    assert value is None
    assert reason == "too_large"


def test_safe_location_strips_query_and_fragment():
    location = browser_read_page._safe_location("https://example.com:443/docs?token=secret#frag")
    assert location == "https://example.com:443/docs"
    assert "token" not in location
    assert "secret" not in location
    assert "frag" not in location


def test_safe_location_defaults_to_root_path():
    location = browser_read_page._safe_location("https://example.com:443")
    assert location == "https://example.com:443/"


# --- error privacy: no raw exception/URL/DNS detail ever leaks ----------------


def test_document_outcome_messages_are_fixed_and_generic():
    for code, message in browser_read_page._MESSAGE_BY_DOCUMENT_OUTCOME.items():
        assert isinstance(message, str) and message
        # None of the fixed messages ever mention a scheme, host, or path.
        assert "://" not in message
        assert "http" not in message.lower()


# --- worst-case output-bound / serialization proof (design section 31) -------


def test_worst_case_permitted_output_serializes_within_the_real_observation_bound():
    """The authoritative proof required for M44 P1: the largest, worst-
    case-for-JSON-escaping successful output browser_read_page.run() can
    ever construct (every bounded field at its exact maximum length, every
    character a double quote - the single most expensive ordinary
    character to escape) survives the REAL build_action_observation()/
    serialize_observation() pipeline within MAX_STEP_RESULT_JSON_CHARS -
    not an estimated overhead formula."""

    page_key = "k" * MAX_SYMBOLIC_NAME_LENGTH
    location = '"' * browser_read_page.MAX_LOCATION_CHARS
    title = '"' * browser_read_page.MAX_TITLE_CHARS
    text = '"' * browser_read_page.MAX_VISIBLE_TEXT_CHARS

    message = f"Page: '{page_key}'\nLocation: {location}\nTitle: {title}\nContent:\n{text}"
    result = ActionResult(True, message, "executed")

    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())
    serialized = serialize_observation(observation)

    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS


def test_title_over_bound_is_rejected_without_leaking_partial_content():
    # Exercise the actual rejection path via _bounded() directly - the
    # over-bound value itself must never appear in what a caller could
    # relay.
    oversized_title = "T" * (browser_read_page.MAX_TITLE_CHARS + 1)
    value, reason = browser_read_page._bounded(oversized_title, browser_read_page.MAX_TITLE_CHARS)
    assert value is None
    assert reason == "too_large"


def test_visible_text_over_bound_is_rejected():
    oversized_text = "x" * (browser_read_page.MAX_VISIBLE_TEXT_CHARS + 1)
    value, reason = browser_read_page._bounded(
        oversized_text, browser_read_page.MAX_VISIBLE_TEXT_CHARS
    )
    assert value is None
    assert reason == "too_large"


def test_empty_title_gets_a_placeholder_not_a_failure():
    value, reason = browser_read_page._bounded(
        "", browser_read_page.MAX_TITLE_CHARS, placeholder=browser_read_page._NO_TITLE_PLACEHOLDER
    )
    assert value == "(no title)"
    assert reason is None


def test_empty_visible_text_gets_a_placeholder_not_a_failure():
    value, reason = browser_read_page._bounded(
        "", browser_read_page.MAX_VISIBLE_TEXT_CHARS, placeholder=browser_read_page._NO_TEXT_PLACEHOLDER
    )
    assert value == "(no visible text)"
    assert reason is None


# --- _fetch_and_validate() reason-code shape (no real network) ---------------


class _FakeRoute:
    def __init__(self, response=None, raise_on_fetch=False):
        self._response = response
        self._raise_on_fetch = raise_on_fetch
        self.fetch_calls = []

    def fetch(self, max_redirects=None, timeout=None):
        self.fetch_calls.append({"max_redirects": max_redirects, "timeout": timeout})
        if self._raise_on_fetch:
            raise RuntimeError("simulated network failure detail that must never leak")
        return self._response


class _FakeResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self._headers = headers
        self._body = body

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def headers(self):
        return self._headers

    def body(self):
        return self._body


def test_fetch_and_validate_passes_an_explicit_timeout_to_route_fetch():
    route = _FakeRoute(_FakeResponse(200, {"content-type": "text/html"}, b"<html></html>"))
    browser_read_page._fetch_and_validate(
        route, max_bytes=1000, allowed_content_types=frozenset({"text/html"})
    )
    assert route.fetch_calls == [
        {"max_redirects": 0, "timeout": browser_read_page._FETCH_TIMEOUT_MS}
    ]


def test_fetch_and_validate_rejects_redirect_status():
    route = _FakeRoute(_FakeResponse(302, {"content-type": "text/html"}, b"<html></html>"))
    response, body_len, reason = browser_read_page._fetch_and_validate(
        route, max_bytes=1000, allowed_content_types=frozenset({"text/html"})
    )
    assert response is None
    assert body_len is None
    assert reason == "redirect"


def test_fetch_and_validate_rejects_http_error_status():
    route = _FakeRoute(_FakeResponse(404, {"content-type": "text/html"}, b"not found"))
    response, body_len, reason = browser_read_page._fetch_and_validate(
        route, max_bytes=1000, allowed_content_types=frozenset({"text/html"})
    )
    assert response is None
    assert body_len is None
    assert reason == "http_error"


def test_fetch_and_validate_rejects_wrong_content_type():
    route = _FakeRoute(
        _FakeResponse(200, {"content-type": "application/octet-stream"}, b"binary")
    )
    response, body_len, reason = browser_read_page._fetch_and_validate(
        route, max_bytes=1000, allowed_content_types=frozenset({"text/html"})
    )
    assert response is None
    assert body_len is None
    assert reason == "bad_content_type"


def test_fetch_and_validate_rejects_oversized_content_length_precondition():
    route = _FakeRoute(
        _FakeResponse(
            200, {"content-type": "text/html", "content-length": "999999"}, b"short body"
        )
    )
    response, body_len, reason = browser_read_page._fetch_and_validate(
        route, max_bytes=100, allowed_content_types=frozenset({"text/html"})
    )
    assert response is None
    assert body_len is None
    assert reason == "too_large"


def test_fetch_and_validate_content_length_precondition_is_advisory_only():
    """A Content-Length that UNDERSTATES the actual (decompressed) body -
    the compression-amplification shape confirmed empirically - must still
    be caught by the authoritative body-length check, not waved through
    because the (wrong) header passed the cheap precondition."""

    route = _FakeRoute(
        _FakeResponse(
            200, {"content-type": "text/html", "content-length": "10"}, b"x" * 200
        )
    )
    response, body_len, reason = browser_read_page._fetch_and_validate(
        route, max_bytes=100, allowed_content_types=frozenset({"text/html"})
    )
    assert response is None
    assert body_len is None
    assert reason == "too_large"


def test_fetch_and_validate_rejects_oversized_actual_body_when_content_length_absent():
    route = _FakeRoute(_FakeResponse(200, {"content-type": "text/html"}, b"x" * 200))
    response, body_len, reason = browser_read_page._fetch_and_validate(
        route, max_bytes=100, allowed_content_types=frozenset({"text/html"})
    )
    assert response is None
    assert body_len is None
    assert reason == "too_large"


def test_fetch_and_validate_accepts_valid_small_response():
    route = _FakeRoute(_FakeResponse(200, {"content-type": "text/html"}, b"<html></html>"))
    response, body_len, reason = browser_read_page._fetch_and_validate(
        route, max_bytes=1000, allowed_content_types=frozenset({"text/html"})
    )
    assert response is not None
    assert body_len == len(b"<html></html>")
    assert reason == "ok"


def test_fetch_and_validate_maps_fetch_exception_without_leaking_detail():
    route = _FakeRoute(raise_on_fetch=True)
    response, body_len, reason = browser_read_page._fetch_and_validate(
        route, max_bytes=1000, allowed_content_types=frozenset({"text/html"})
    )
    assert response is None
    assert body_len is None
    assert reason == "fetch_error"
