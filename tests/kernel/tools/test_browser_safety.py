"""Tests for kernel/tools/browser_safety.py: URL/origin parsing,
normalization, private-network rejection, DNS defense-in-depth, and the
pure request-allowlist function shared by production and by network-policy
unit tests that never launch a browser."""

import time
from pathlib import Path

import psutil
import pytest

from kernel.tools.browser_safety import (
    ALLOWED_DOCUMENT_CONTENT_TYPES,
    ALLOWED_STYLESHEET_CONTENT_TYPES,
    MAX_STYLESHEET_ORIGIN_LENGTH,
    MAX_URL_LENGTH,
    BrowserSafetyError,
    Origin,
    PageAuthority,
    content_type_matches,
    fresh_dns_safety_check,
    is_request_permitted,
    parse_document_url,
    parse_https_origin,
    parse_https_url,
    url_origin,
)


# --- parse_https_url ---------------------------------------------------------


def test_valid_https_url_parses_and_normalizes():
    normalized, origin = parse_https_url("https://Example.com/docs?x=1", field_name="url")
    assert normalized == "https://example.com:443/docs?x=1"
    assert origin == Origin(scheme="https", host="example.com", port=443)


def test_explicit_default_port_normalizes_the_same_as_omitted_port():
    normalized_a, origin_a = parse_https_url("https://example.com/docs", field_name="url")
    normalized_b, origin_b = parse_https_url("https://example.com:443/docs", field_name="url")
    assert normalized_a == normalized_b
    assert origin_a == origin_b


def test_explicit_non_default_port_is_preserved():
    _normalized, origin = parse_https_url("https://example.com:8443/docs", field_name="url")
    assert origin.port == 8443


def test_missing_path_defaults_to_root():
    normalized, _origin = parse_https_url("https://example.com", field_name="url")
    assert normalized == "https://example.com:443/"


def test_fragment_is_dropped():
    normalized, _origin = parse_https_url("https://example.com/docs#section", field_name="url")
    assert "#" not in normalized
    assert normalized == "https://example.com:443/docs"


def test_query_is_preserved():
    normalized, _origin = parse_https_url("https://example.com/docs?a=1&b=2", field_name="url")
    assert normalized.endswith("?a=1&b=2")


def test_http_scheme_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_url("http://example.com/docs", field_name="url")


@pytest.mark.parametrize(
    "scheme_url",
    [
        "file:///etc/passwd",
        "data:text/html,<script>1</script>",
        "javascript:alert(1)",
        "blob:https://example.com/uuid",
        "about:blank",
        "chrome://settings",
        "ftp://example.com/docs",
    ],
)
def test_disallowed_schemes_rejected(scheme_url):
    with pytest.raises(BrowserSafetyError):
        parse_https_url(scheme_url, field_name="url")


def test_userinfo_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_url("https://user:pass@example.com/docs", field_name="url")


def test_userinfo_only_username_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_url("https://user@example.com/docs", field_name="url")


@pytest.mark.parametrize(
    "host_url",
    [
        "https://localhost/docs",
        "https://LOCALHOST/docs",
        "https://127.0.0.1/docs",
        "https://127.255.255.254/docs",
        "https://10.0.0.1/docs",
        "https://172.16.0.1/docs",
        "https://192.168.1.1/docs",
        "https://169.254.169.254/docs",
        "https://169.254.1.1/docs",
        "https://[::1]/docs",
        "https://[fe80::1]/docs",
        "https://0.0.0.0/docs",
        "https://224.0.0.1/docs",
    ],
)
def test_private_and_local_hosts_rejected(host_url):
    with pytest.raises(BrowserSafetyError):
        parse_https_url(host_url, field_name="url")


def test_wildcard_host_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_url("https://*.example.com/docs", field_name="url")


def test_unicode_idn_hostname_rejected_requires_punycode():
    with pytest.raises(BrowserSafetyError):
        parse_https_url("https://münchen.example/docs", field_name="url")


def test_punycode_idn_hostname_accepted():
    normalized, _origin = parse_https_url("https://xn--mnchen-3ya.example/docs", field_name="url")
    assert "xn--mnchen-3ya.example" in normalized


def test_trailing_dot_hostname_normalizes_to_bare_form():
    normalized, origin = parse_https_url("https://example.com./docs", field_name="url")
    assert origin.host == "example.com"
    assert normalized == "https://example.com:443/docs"


def test_oversized_url_rejected():
    huge = "https://example.com/" + ("a" * MAX_URL_LENGTH)
    with pytest.raises(BrowserSafetyError):
        parse_https_url(huge, field_name="url")


def test_empty_url_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_url("", field_name="url")


def test_nul_byte_in_url_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_url("https://example.com/\x00docs", field_name="url")


def test_non_string_url_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_url(12345, field_name="url")


# --- parse_https_origin -------------------------------------------------------


def test_valid_https_origin_parses():
    origin = parse_https_origin("https://static.example.com", field_name="origin")
    assert origin == Origin(scheme="https", host="static.example.com", port=443)


def test_origin_with_trailing_slash_path_accepted():
    origin = parse_https_origin("https://static.example.com/", field_name="origin")
    assert origin.host == "static.example.com"


def test_origin_with_real_path_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_origin("https://static.example.com/some/path", field_name="origin")


def test_origin_with_query_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_origin("https://static.example.com?x=1", field_name="origin")


def test_origin_with_fragment_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_origin("https://static.example.com#frag", field_name="origin")


def test_origin_http_scheme_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_origin("http://static.example.com", field_name="origin")


def test_origin_userinfo_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_origin("https://user:pass@static.example.com", field_name="origin")


def test_origin_localhost_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_origin("https://localhost", field_name="origin")


def test_origin_private_ip_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_origin("https://192.168.0.1", field_name="origin")


def test_origin_wildcard_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_origin("https://*.example.com", field_name="origin")


def test_origin_case_normalization():
    origin = parse_https_origin("https://STATIC.Example.COM", field_name="origin")
    assert origin.host == "static.example.com"


def test_origin_explicit_default_port_equals_omitted_port():
    a = parse_https_origin("https://static.example.com", field_name="origin")
    b = parse_https_origin("https://static.example.com:443", field_name="origin")
    assert a == b


def test_origin_different_explicit_port_is_distinct():
    a = parse_https_origin("https://static.example.com", field_name="origin")
    b = parse_https_origin("https://static.example.com:8443", field_name="origin")
    assert a != b


def test_oversized_stylesheet_origin_rejected():
    huge = "https://" + ("a" * MAX_STYLESHEET_ORIGIN_LENGTH) + ".example.com"
    with pytest.raises(BrowserSafetyError):
        parse_https_origin(huge, field_name="origin")


# --- url_origin (request-time parsing, no config validation) ----------------


def test_url_origin_matches_parse_https_origin_for_the_same_https_url():
    _normalized, origin_from_url = parse_https_url("https://example.com:443/docs", field_name="url")
    origin_from_request = url_origin("https://example.com:443/docs?x=1")
    assert origin_from_url == origin_from_request


def test_url_origin_never_matches_across_schemes():
    https_origin = url_origin("https://example.com/docs")
    http_origin = url_origin("http://example.com/docs")
    assert https_origin != http_origin


def test_url_origin_default_port_for_http():
    origin = url_origin("http://example.com/docs")
    assert origin.port == 80


def test_url_origin_rejects_unparseable_url():
    with pytest.raises(BrowserSafetyError):
        url_origin("not a url at all")


def test_url_origin_never_authorizes_via_prefix_match():
    # The classic startswith() trap: "https://example.com.attacker.com"
    # naively "starts with" "https://example.com" as a raw string, but
    # must never be treated as the same origin.
    real = url_origin("https://example.com")
    attacker = url_origin("https://example.com.attacker.com")
    assert real != attacker
    assert real.host != attacker.host


def test_url_origin_userinfo_trick_resolves_to_the_real_host():
    # https://example.com@attacker.com - the ACTUAL host is attacker.com,
    # never example.com, no matter what appears before the "@".
    origin = url_origin("https://example.com@attacker.com/docs")
    assert origin.host == "attacker.com"


# --- fresh_dns_safety_check ---------------------------------------------------
#
# Milestone 44 P1 adversarial-review correction: resolution now runs in a
# real, separate, genuinely-killable OS subprocess
# (kernel/tools/dns_resolver_worker.py, launched via
# kernel/tools/process_control.py's run_capturing_stdout()) rather than a
# concurrent.futures.ThreadPoolExecutor - a Python-level monkeypatch of
# socket.getaddrinfo in THIS process has no effect on that separate
# subprocess's own interpreter, so these tests use the module's `
# worker_script`/`timeout_seconds` test-only parameters with small, fixed,
# fully offline fixture scripts (tests/kernel/tools/fixtures/) instead -
# no real DNS/network dependency, fully deterministic.

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_PUBLIC_IP_WORKER = _FIXTURES_DIR / "print_public_ip_worker.py"
_PRIVATE_IP_WORKER = _FIXTURES_DIR / "print_private_ip_worker.py"
_MIXED_IP_WORKER = _FIXTURES_DIR / "print_mixed_ip_worker.py"
_NOTHING_WORKER = _FIXTURES_DIR / "print_nothing_worker.py"
_FAIL_WORKER = _FIXTURES_DIR / "fail_worker.py"
_HANGING_WORKER = _FIXTURES_DIR / "hanging_dns_worker.py"


def test_dns_check_is_a_noop_for_an_ip_literal():
    # Must not attempt any resolution (real or subprocess) for a literal
    # IP - and a public IP literal (already validated at parse time)
    # never raises here either.
    fresh_dns_safety_check("93.184.216.34", field_name="host")


def test_dns_check_allows_a_hostname_resolving_to_only_public_addresses():
    fresh_dns_safety_check(
        "rebound.example.com", field_name="host", worker_script=_PUBLIC_IP_WORKER
    )  # must not raise


def test_dns_check_rejects_a_hostname_resolving_to_a_private_address():
    with pytest.raises(BrowserSafetyError):
        fresh_dns_safety_check(
            "rebound.example.com", field_name="host", worker_script=_PRIVATE_IP_WORKER
        )


def test_dns_check_rejects_a_mixed_public_and_private_result():
    with pytest.raises(BrowserSafetyError):
        fresh_dns_safety_check(
            "rebound.example.com", field_name="host", worker_script=_MIXED_IP_WORKER
        )


def test_dns_check_rejects_an_empty_result():
    with pytest.raises(BrowserSafetyError):
        fresh_dns_safety_check(
            "nowhere.example.com", field_name="host", worker_script=_NOTHING_WORKER
        )


def test_dns_check_rejects_a_resolver_failure_without_leaking_detail():
    with pytest.raises(BrowserSafetyError) as exc_info:
        fresh_dns_safety_check("nowhere.example.com", field_name="host", worker_script=_FAIL_WORKER)
    message = str(exc_info.value)
    assert "nowhere.example.com" not in message


def test_dns_check_genuinely_bounds_wall_clock_time_on_a_hung_resolver():
    """The load-bearing regression: the OLD ThreadPoolExecutor-based
    implementation was PROVEN (M44 P1 adversarial review) to NOT bound
    wall-clock time - a hung resolver blocked for its own full duration
    regardless of the configured timeout, because
    ThreadPoolExecutor.__exit__()'s shutdown(wait=True) blocked on the
    still-running thread. The worker subprocess CAN be killed - this test
    proves the replacement genuinely returns near the configured timeout,
    not near the hung worker's real (30s) sleep duration. Uses a small
    private timeout (not the real ~3s production value) so this stays
    fast."""

    t0 = time.monotonic()
    with pytest.raises(BrowserSafetyError):
        fresh_dns_safety_check(
            "nowhere.example.com",
            field_name="host",
            timeout_seconds=0.5,
            worker_script=_HANGING_WORKER,
        )
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0  # nowhere near the hanging worker's real 30s sleep
    assert elapsed < 2.0  # comfortably close to the configured 0.5s


def test_dns_check_leaves_no_worker_process_running_after_timeout():
    fixture_marker = str(_HANGING_WORKER)
    with pytest.raises(BrowserSafetyError):
        fresh_dns_safety_check(
            "nowhere.example.com",
            field_name="host",
            timeout_seconds=0.3,
            worker_script=_HANGING_WORKER,
        )

    time.sleep(0.3)  # brief grace period for OS-level process-table cleanup
    remaining = [
        p
        for p in psutil.process_iter(["pid", "cmdline"])
        if p.info.get("cmdline") and fixture_marker in p.info["cmdline"]
    ]
    assert remaining == []


def test_dns_check_repeated_timeouts_do_not_accumulate_worker_processes():
    fixture_marker = str(_HANGING_WORKER)
    for _ in range(3):
        with pytest.raises(BrowserSafetyError):
            fresh_dns_safety_check(
                "nowhere.example.com",
                field_name="host",
                timeout_seconds=0.3,
                worker_script=_HANGING_WORKER,
            )

    time.sleep(0.3)
    remaining = [
        p
        for p in psutil.process_iter(["pid", "cmdline"])
        if p.info.get("cmdline") and fixture_marker in p.info["cmdline"]
    ]
    assert remaining == []


# --- is_request_permitted: the pure allowlist ---------------------------------


@pytest.fixture
def authority():
    return PageAuthority(
        document_url="https://example.com:443/docs",
        document_origin=Origin(scheme="https", host="example.com", port=443),
        stylesheet_origins=(Origin(scheme="https", host="static.example.com", port=443),),
    )


def test_exact_initial_document_request_is_permitted(authority):
    assert is_request_permitted(
        authority,
        url="https://example.com:443/docs",
        method="GET",
        resource_type="document",
        is_main_frame=True,
        document_consumed=False,
    )


def test_second_document_request_is_denied_even_if_identical(authority):
    assert not is_request_permitted(
        authority,
        url="https://example.com:443/docs",
        method="GET",
        resource_type="document",
        is_main_frame=True,
        document_consumed=True,
    )


def test_subframe_document_request_is_denied_even_on_the_page_origin(authority):
    assert not is_request_permitted(
        authority,
        url="https://example.com:443/docs",
        method="GET",
        resource_type="document",
        is_main_frame=False,
        document_consumed=False,
    )


def test_document_request_to_a_different_url_on_the_same_origin_is_denied(authority):
    assert not is_request_permitted(
        authority,
        url="https://example.com:443/other-page",
        method="GET",
        resource_type="document",
        is_main_frame=True,
        document_consumed=False,
    )


def test_document_request_on_a_stylesheet_approved_origin_is_denied(authority):
    # Stylesheet authority never implies document authority, even on an
    # explicitly stylesheet-approved origin.
    assert not is_request_permitted(
        authority,
        url="https://static.example.com:443/index.html",
        method="GET",
        resource_type="document",
        is_main_frame=True,
        document_consumed=False,
    )


def test_stylesheet_from_approved_origin_is_permitted(authority):
    assert is_request_permitted(
        authority,
        url="https://static.example.com:443/style.css",
        method="GET",
        resource_type="stylesheet",
        is_main_frame=False,
        document_consumed=True,
    )


def test_stylesheet_from_unapproved_origin_is_denied(authority):
    assert not is_request_permitted(
        authority,
        url="https://unapproved.example.com:443/style.css",
        method="GET",
        resource_type="stylesheet",
        is_main_frame=False,
        document_consumed=True,
    )


def test_stylesheet_from_the_page_origin_is_denied_unless_explicitly_listed(authority):
    # The page's own origin is NOT implicitly authorized for stylesheets -
    # only origins explicitly present in allowed_stylesheet_origins are.
    assert not is_request_permitted(
        authority,
        url="https://example.com:443/style.css",
        method="GET",
        resource_type="stylesheet",
        is_main_frame=False,
        document_consumed=True,
    )


@pytest.mark.parametrize(
    "resource_type",
    [
        "script",
        "image",
        "font",
        "media",
        "xhr",
        "fetch",
        "beacon",
        "prefetch",
        "object",
        "manifest",
        "websocket",
        "other",
    ],
)
def test_every_non_document_non_stylesheet_resource_type_is_denied(authority, resource_type):
    assert not is_request_permitted(
        authority,
        url="https://example.com:443/anything",
        method="GET",
        resource_type=resource_type,
        is_main_frame=True,
        document_consumed=False,
    )
    assert not is_request_permitted(
        authority,
        url="https://static.example.com:443/anything",
        method="GET",
        resource_type=resource_type,
        is_main_frame=False,
        document_consumed=True,
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_non_get_method_is_denied_for_document(authority, method):
    assert not is_request_permitted(
        authority,
        url="https://example.com:443/docs",
        method=method,
        resource_type="document",
        is_main_frame=True,
        document_consumed=False,
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_non_get_method_is_denied_for_stylesheet(authority, method):
    assert not is_request_permitted(
        authority,
        url="https://static.example.com:443/style.css",
        method=method,
        resource_type="stylesheet",
        is_main_frame=False,
        document_consumed=True,
    )


def test_http_scheme_request_never_matches_an_https_authorized_document(authority):
    assert not is_request_permitted(
        authority,
        url="http://example.com:443/docs",
        method="GET",
        resource_type="document",
        is_main_frame=True,
        document_consumed=False,
    )


def test_prefix_lookalike_stylesheet_origin_never_matches(authority):
    # "https://static.example.com.attacker.com" naively "starts with" the
    # approved origin string - must never match via is_request_permitted.
    assert not is_request_permitted(
        authority,
        url="https://static.example.com.attacker.com:443/style.css",
        method="GET",
        resource_type="stylesheet",
        is_main_frame=False,
        document_consumed=True,
    )


# --- parse_document_url() / NormalizedDocumentURL ------------------------------
#
# Milestone 44 P1 adversarial-review correction: BLOCKER. Chromium's live
# Request.url omits a default port even when the original navigation
# target included it explicitly - a raw string comparison between
# authority.document_url (always port-inclusive) and a live Request.url
# (never port-inclusive for the default port) meant browser_read_page
# denied its OWN authorized page for any config written the ordinary way.


def test_parse_document_url_omitted_and_explicit_default_port_are_equal():
    omitted = parse_document_url("https://example.com/docs")
    explicit = parse_document_url("https://example.com:443/docs")
    assert omitted == explicit


def test_parse_document_url_non_default_port_remains_distinct():
    default_port = parse_document_url("https://example.com/docs")
    other_port = parse_document_url("https://example.com:8443/docs")
    assert default_port != other_port


def test_parse_document_url_preserves_query():
    a = parse_document_url("https://example.com/docs?x=1")
    b = parse_document_url("https://example.com/docs?x=1")
    c = parse_document_url("https://example.com/docs")
    assert a == b
    assert a != c


def test_parse_document_url_drops_fragment():
    with_fragment = parse_document_url("https://example.com/docs#section")
    without_fragment = parse_document_url("https://example.com/docs")
    assert with_fragment == without_fragment


# --- default-port regression: the real production functions together -------
#
# Reproduces the EXACT pre-fix failure through parse_https_url() +
# is_request_permitted() together - not just parse_document_url() in
# isolation - proving the fix holds through the real authority-construction
# path a config entry actually takes.


def test_default_port_regression_a_config_omits_port_live_omits_port():
    """A: configured 'https://example.com/docs', live request
    'https://example.com/docs' (Chromium's own ordinary reporting for a
    default-port page) - must permit."""

    normalized_url, origin = parse_https_url("https://example.com/docs", field_name="url")
    authority = PageAuthority(document_url=normalized_url, document_origin=origin, stylesheet_origins=())

    assert is_request_permitted(
        authority,
        url="https://example.com/docs",
        method="GET",
        resource_type="document",
        is_main_frame=True,
        document_consumed=False,
    )


def test_default_port_regression_b_config_explicit_443_live_omits_port():
    """B: configured 'https://example.com:443/docs' (an admin who wrote
    the port explicitly), live request 'https://example.com/docs' - must
    permit after canonicalization."""

    normalized_url, origin = parse_https_url("https://example.com:443/docs", field_name="url")
    authority = PageAuthority(document_url=normalized_url, document_origin=origin, stylesheet_origins=())

    assert is_request_permitted(
        authority,
        url="https://example.com/docs",
        method="GET",
        resource_type="document",
        is_main_frame=True,
        document_consumed=False,
    )


def test_default_port_regression_c_non_default_configured_port_rejects():
    """C: configured 'https://example.com:8443/docs', live request
    'https://example.com/docs' (which is actually port 443, a DIFFERENT
    origin) - must reject. Confirms the fix does not over-broaden to make
    every port equivalent."""

    normalized_url, origin = parse_https_url("https://example.com:8443/docs", field_name="url")
    authority = PageAuthority(document_url=normalized_url, document_origin=origin, stylesheet_origins=())

    assert not is_request_permitted(
        authority,
        url="https://example.com/docs",
        method="GET",
        resource_type="document",
        is_main_frame=True,
        document_consumed=False,
    )


# --- path ambiguity: dot-segments and backslashes rejected at config time ---
#
# Confirmed empirically (M44 P1 adversarial review) that Chromium resolves
# dot-segments ("/a/../b" -> "/b") and rewrites backslashes to forward
# slashes before ever reporting a live Request.url - rather than emulate
# that normalization here, a configured URL whose path could trigger
# either rewrite is rejected outright at config-validation time.


@pytest.mark.parametrize(
    "path",
    [
        "/a/../b",
        "/a/./b",
        "/../b",
        "/a/..",
        "/a/%2e%2e/b",
        "/a/%2E%2E/b",
        "/a/.%2e/b",
        "/a/%2e./b",
    ],
)
def test_dot_segment_paths_rejected(path):
    with pytest.raises(BrowserSafetyError):
        parse_https_url(f"https://example.com{path}", field_name="url")


def test_backslash_in_path_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_url("https://example.com/a\\b", field_name="url")


def test_leading_backslash_in_path_rejected():
    with pytest.raises(BrowserSafetyError):
        parse_https_url("https://example.com\\a\\b", field_name="url")


def test_ordinary_percent_encoded_path_still_allowed():
    # Only percent-encoded dots are special-cased - an ordinary
    # percent-encoded byte (here, an encoded slash) must still be allowed,
    # since Chromium was confirmed to preserve it verbatim.
    normalized, _origin = parse_https_url("https://example.com/a%2Fb", field_name="url")
    assert normalized == "https://example.com:443/a%2Fb"


def test_repeated_ordinary_slashes_still_allowed():
    # Confirmed empirically that Chromium preserves repeated slashes
    # verbatim (no collapsing) - nothing to reject here.
    normalized, _origin = parse_https_url("https://example.com/a//b", field_name="url")
    assert normalized == "https://example.com:443/a//b"


def test_empty_path_still_defaults_to_root():
    normalized, _origin = parse_https_url("https://example.com", field_name="url")
    assert normalized == "https://example.com:443/"


# --- content_type_matches ------------------------------------------------------


def test_content_type_exact_match():
    assert content_type_matches("text/html", ALLOWED_DOCUMENT_CONTENT_TYPES)


def test_content_type_with_charset_parameter_still_matches():
    assert content_type_matches("text/html; charset=utf-8", ALLOWED_DOCUMENT_CONTENT_TYPES)


def test_content_type_case_insensitive():
    assert content_type_matches("TEXT/HTML", ALLOWED_DOCUMENT_CONTENT_TYPES)


def test_content_type_mismatch_rejected():
    assert not content_type_matches("application/octet-stream", ALLOWED_DOCUMENT_CONTENT_TYPES)


def test_content_type_none_rejected():
    assert not content_type_matches(None, ALLOWED_DOCUMENT_CONTENT_TYPES)


def test_content_type_empty_string_rejected():
    assert not content_type_matches("", ALLOWED_DOCUMENT_CONTENT_TYPES)


def test_stylesheet_content_type_only_accepts_css():
    assert content_type_matches("text/css", ALLOWED_STYLESHEET_CONTENT_TYPES)
    assert not content_type_matches("text/html", ALLOWED_STYLESHEET_CONTENT_TYPES)
