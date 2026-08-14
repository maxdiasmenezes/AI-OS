"""Real-browser integration tests for Milestone 44 P1 (browser_read_page):
proves the actual Playwright/Chromium request gate, not just the pure
is_request_permitted() logic already covered by
tests/kernel/tools/test_browser_safety.py.

Uses the module's PRIVATE test seam - kernel.tools.handlers.
browser_read_page._execute_read(authority, page_key) - with a hand-built
PageAuthority pointing at real, deterministic LOCAL fixture servers
(loopback only, ephemeral ports). This entirely bypasses production
validation (HTTPS-only, private-network rejection, DNS checks) by
construction: _execute_read() never inspects where an authority came from,
and this seam is reachable only by importing the module directly - never
from ToolsConfig, ActionRequest, resource_key, an environment variable, the
planner, a model, or any registry action. Production validation itself
(kernel.tools.handlers.browser_read_page.run() -> _build_authority()) has
its own, separate, dedicated tests in test_browser_read_page.py and
test_tools_config.py that prove it independently rejects localhost/private
addresses - this file never needs to (and does not) relax that boundary.

No public internet dependency anywhere - every server here is a real,
local, ephemeral-port http.server instance."""

import http.server
import select
import socketserver
import threading
import time

import pytest

from kernel.tools.browser_safety import Origin, PageAuthority
from kernel.tools.handlers import browser_read_page


# --- local fixture servers ----------------------------------------------------


class _HitLog:
    def __init__(self):
        self.hits = []

    def record(self, tag, path):
        self.hits.append(f"{tag}{path}")

    def reset(self):
        self.hits = []


def _make_handler(tag, hit_log, state):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send(self, body: bytes, status=200, content_type="text/html", extra_headers=None):
            self.send_response(status)
            if content_type is not None:
                self.send_header("Content-Type", content_type)
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            hit_log.record(tag, self.path)
            a, b, c = state["a"], state["b"], state["c"]
            p = self.path

            if p == "/plain":
                self._send(
                    b"<!doctype html><html><head><title>Plain Title</title></head>"
                    b"<body><p>Hello visible text.</p></body></html>"
                )
            elif p == "/plain-with-query?a=1&b=2":
                self._send(
                    b"<!doctype html><html><head><title>Query Title</title></head>"
                    b"<body><p>Query page text.</p></body></html>"
                )
            elif p == "/styled":
                self._send(
                    f'<!doctype html><html><head><title>Styled</title>'
                    f'<link rel="stylesheet" href="{a}/style.css"></head>'
                    f'<body><p class="hidden">Hidden by CSS.</p>'
                    f'<p class="visible">Visible text.</p></body></html>'.encode()
                )
            elif p == "/style.css":
                self._send(b".hidden{display:none;}.visible{display:block;}", content_type="text/css")

            elif p == "/redirect-main":
                self._send(b"", status=302, extra_headers={"Location": f"{a}/redirect-target"})
            elif p == "/redirect-target":
                self._send(b"<html><body>should never be reached</body></html>")
            elif p == "/redirect-to-b":
                self._send(b"", status=302, extra_headers={"Location": f"{b}/target"})
            elif tag == "B" and p == "/target":
                self._send(b"<html><body>should never be reached</body></html>")

            elif p == "/css-redirect-page":
                self._send(
                    f'<html><head><link rel="stylesheet" href="{a}/style-redirects.css"></head>'
                    f'<body>x</body></html>'.encode()
                )
            elif p == "/style-redirects.css":
                self._send(b"", status=302, extra_headers={"Location": f"{a}/real.css"})
            elif p == "/real.css":
                self._send(b"body{color:red;}", content_type="text/css")

            elif p == "/meta-refresh":
                self._send(
                    f'<html><head><meta http-equiv="refresh" content="0;url={a}/meta-target"></head>'
                    f'<body>refreshing</body></html>'.encode()
                )
            elif p == "/meta-target":
                self._send(b"<html><body>should never be reached (second document)</body></html>")

            elif p == "/css-from-c-page":
                self._send(
                    f'<html><head><link rel="stylesheet" href="{c}/from-c.css"></head>'
                    f'<body>x</body></html>'.encode()
                )
            elif tag == "C" and p == "/from-c.css":
                self._send(b"body{color:green;}", content_type="text/css")

            elif p == "/css-from-b-page":
                self._send(
                    f'<html><head><link rel="stylesheet" href="{b}/from-b.css"></head>'
                    f'<body>x</body></html>'.encode()
                )
            elif tag == "B" and p == "/from-b.css":
                self._send(b"body{color:purple;}", content_type="text/css")

            elif p == "/css-same-origin-not-listed-page":
                self._send(
                    f'<html><head><link rel="stylesheet" href="{a}/never-listed.css"></head>'
                    f'<body>x</body></html>'.encode()
                )
            elif p == "/never-listed.css":
                self._send(b"body{color:pink;}", content_type="text/css")

            elif p == "/css-import-page":
                self._send(
                    f'<html><head><style>@import url("{a}/imported.css");</style></head>'
                    f'<body>x</body></html>'.encode()
                )
            elif p == "/imported.css":
                self._send(b"body{color:orange;}", content_type="text/css")

            elif p == "/css-import-unapproved-page":
                self._send(
                    f'<html><head><style>@import url("{b}/imported-unapproved.css");</style></head>'
                    f'<body>x</body></html>'.encode()
                )
            elif tag == "B" and p == "/imported-unapproved.css":
                self._send(b"body{color:black;}", content_type="text/css")

            elif p == "/css-bg-image-page":
                self._send(
                    f'<html><head><style>body{{background:url("{a}/bg.png");}}</style></head>'
                    f'<body>x</body></html>'.encode()
                )
            elif p == "/bg.png":
                self._send(b"\x89PNG\r\n", content_type="image/png")

            elif p == "/forced-font-page":
                self._send(
                    f'<html><head><style>'
                    f'@font-face {{font-family:CustomFont; src:url("{a}/font.woff");}}'
                    f'.custom {{font-family:CustomFont;}}'
                    f'</style></head><body><p class="custom">Text using the custom font.</p></body></html>'.encode()
                )
            elif p == "/font.woff":
                self._send(b"FONTDATA", content_type="font/woff")

            elif p == "/with-img":
                self._send(f'<html><body><img src="{a}/img.png"><p>x</p></body></html>'.encode())
            elif p == "/img.png":
                self._send(b"\x89PNG\r\n", content_type="image/png")

            elif p == "/with-media":
                self._send(
                    f'<html><body><audio src="{a}/audio.mp3"></audio>'
                    f'<video src="{a}/video.mp4"></video><p>x</p></body></html>'.encode()
                )
            elif p in ("/audio.mp3", "/video.mp4"):
                self._send(b"DATA", content_type="application/octet-stream")

            elif p == "/with-prefetch":
                self._send(
                    f'<html><head><link rel="prefetch" href="{a}/prefetch.txt"></head>'
                    f'<body>x</body></html>'.encode()
                )
            elif p == "/prefetch.txt":
                self._send(b"DATA", content_type="text/plain")

            elif p == "/with-iframe":
                self._send(f'<html><body><iframe src="{a}/iframe-inner"></iframe></body></html>'.encode())
            elif p == "/iframe-inner":
                self._send(b"<html><body>should never be reached</body></html>")

            elif p == "/set-cookie-page":
                self._send(
                    f'<html><head><link rel="stylesheet" href="{a}/cookie-check.css"></head>'
                    f'<body>x</body></html>'.encode(),
                    extra_headers={"Set-Cookie": "test_cookie=abc123; Path=/"},
                )
            elif p == "/cookie-check.css":
                state["cookie_seen"] = self.headers.get("Cookie")
                self._send(b"body{}", content_type="text/css")

            elif p == "/oversized":
                big = b"x" * (browser_read_page.browser_safety.MAX_DOCUMENT_RESPONSE_BYTES + 1000)
                self._send(b"<html><body>" + big + b"</body></html>")

            elif p == "/oversized-page-for-css":
                self._send(
                    f'<html><head><link rel="stylesheet" href="{a}/oversized.css"></head>'
                    f'<body>x</body></html>'.encode()
                )
            elif p == "/oversized.css":
                big = b"x" * (browser_read_page.browser_safety.MAX_STYLESHEET_RESPONSE_BYTES + 1000)
                self._send(b"body{}" + big, content_type="text/css")

            elif p == "/wrong-content-type":
                self._send(b"binary data", content_type="application/octet-stream")

            elif p == "/plain-text-doc":
                self._send(b"Just plain text, no HTML.", content_type="text/plain")

            # --- M44 P1 adversarial-review correction fixtures ---------------

            elif p == "/many-links-page":
                n = state["many_links_count"]
                links = "".join(f'<link rel="stylesheet" href="{a}/many-link-{i}.css">' for i in range(n))
                self._send(f"<!doctype html><html><head>{links}</head><body><p>x</p></body></html>".encode())
            elif p.startswith("/many-link-") and p.endswith(".css"):
                self._send(b"body{}", content_type="text/css")

            elif p == "/import-chain-page":
                self._send(
                    f'<!doctype html><html><head><link rel="stylesheet" href="{a}/chain-0.css"></head>'
                    f'<body><p>x</p></body></html>'.encode()
                )
            elif p.startswith("/chain-") and p.endswith(".css"):
                idx = int(p[len("/chain-"):-len(".css")])
                depth = state["import_chain_depth"]
                if idx < depth - 1:
                    body = f'@import url("{a}/chain-{idx + 1}.css");'.encode()
                else:
                    body = b"body{color:red;}"
                self._send(body, content_type="text/css")

            elif p == "/cumulative-bytes-page":
                n = state["cumulative_stylesheet_count"]
                links = "".join(f'<link rel="stylesheet" href="{a}/cumulative-{i}.css">' for i in range(n))
                self._send(f"<!doctype html><html><head>{links}</head><body><p>x</p></body></html>".encode())
            elif p.startswith("/cumulative-") and p.endswith(".css"):
                size = state["cumulative_stylesheet_size"]
                self._send(b"/*" + b"x" * size + b"*/", content_type="text/css")

            elif p == "/slow-stylesheet-page":
                self._send(
                    f'<!doctype html><html><head><link rel="stylesheet" href="{a}/slow.css"></head>'
                    f'<body><p>slow stylesheet page text</p></body></html>'.encode()
                )
            elif p == "/slow.css":
                # Deterministic, wall-clock-noise-immune instrumentation
                # (M45 P3 flake fix): rather than blocking in a single
                # time.sleep() and inferring the client's fetch-timeout
                # behavior from the *test's* total elapsed time (which
                # also includes Chromium/Node process launch and teardown
                # - dominant, highly variable overhead on some machines,
                # unrelated to the fetch timeout under test), poll the raw
                # connection socket in short slices via select(). Once
                # route.fetch()'s own timeout fires client-side, the
                # underlying TCP connection is closed/reset, which makes
                # the socket select()-readable well before the full
                # server-side delay elapses. This measures exactly the
                # interval the test cares about - how long the client
                # actually waited before giving up - independent of
                # anything else in the browser lifecycle.
                start = time.monotonic()
                deadline = start + state["slow_stylesheet_delay_seconds"]
                aborted_after = None
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    readable, _, _ = select.select([self.connection], [], [], min(remaining, 0.02))
                    if readable:
                        aborted_after = time.monotonic() - start
                        break
                state["slow_stylesheet_client_abort_after_seconds"] = aborted_after
                try:
                    self._send(b"body{color:blue;}", content_type="text/css")
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                    pass

            else:
                self._send(b"", status=404)

    return Handler


def _run_server(tag, hit_log, state):
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _make_handler(tag, hit_log, state))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


@pytest.fixture(scope="module")
def servers():
    hit_log = _HitLog()
    state = {}
    a_httpd, a_port = _run_server("A", hit_log, state)
    b_httpd, b_port = _run_server("B", hit_log, state)
    c_httpd, c_port = _run_server("C", hit_log, state)
    state["a"] = f"http://127.0.0.1:{a_port}"
    state["b"] = f"http://127.0.0.1:{b_port}"
    state["c"] = f"http://127.0.0.1:{c_port}"
    yield hit_log, state
    a_httpd.shutdown()
    b_httpd.shutdown()
    c_httpd.shutdown()


@pytest.fixture
def fixtures(servers):
    hit_log, state = servers
    hit_log.reset()
    state.pop("cookie_seen", None)
    state.pop("slow_stylesheet_client_abort_after_seconds", None)
    return hit_log, state


def _origin_of(base_url: str) -> Origin:
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    return Origin(scheme=parts.scheme, host=parts.hostname, port=parts.port)


def _authority(state, path, *, stylesheet_origin_bases=()):
    doc_url = f"{state['a']}{path}"
    stylesheet_origins = tuple(_origin_of(base) for base in stylesheet_origin_bases)
    return PageAuthority(
        document_url=doc_url,
        document_origin=_origin_of(state["a"]),
        stylesheet_origins=stylesheet_origins,
    )


# --- basic success + JS disabled + visible-text extraction --------------------


def test_exact_page_loads_and_returns_bounded_text(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/plain")

    result = browser_read_page._execute_read(authority, "plain_page")

    assert result.success is True
    assert result.outcome == "executed"
    assert "Hello visible text." in result.message
    assert "Plain Title" in result.message


def test_exact_document_match_includes_the_query_string(fixtures):
    """Load-bearing precision check for is_request_permitted()'s exact
    document-URL comparison: proves Chromium's own live Request.url for
    the initial navigation matches authority.document_url byte-for-byte
    when the configured URL includes a query string, so the one
    authorized document request is neither incorrectly denied nor
    accidentally over-matched."""

    hit_log, state = fixtures
    authority = _authority(state, "/plain-with-query?a=1&b=2")

    result = browser_read_page._execute_read(authority, "with_query")

    assert result.success is True
    assert "Query page text." in result.message


def test_styled_page_visible_text_respects_display_none_when_stylesheet_allowed(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/styled", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "styled_page")

    assert result.success is True
    assert "Hidden by CSS." not in result.message
    assert "Visible text." in result.message


# --- redirects: categorically unsupported, zero hits on real targets ---------


def test_same_origin_redirect_rejected_with_zero_target_hits(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/redirect-main")

    result = browser_read_page._execute_read(authority, "redirect_main")

    assert result.success is False
    assert result.message == "That page redirected and cannot be read."
    assert not any("/redirect-target" in hit for hit in hit_log.hits)


def test_cross_origin_redirect_rejected_with_zero_b_hits(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/redirect-to-b")

    result = browser_read_page._execute_read(authority, "redirect_to_b")

    assert result.success is False
    assert result.message == "That page redirected and cannot be read."
    assert not any(hit.startswith("B") for hit in hit_log.hits)


def test_stylesheet_redirect_rejected_with_zero_target_hits(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/css-redirect-page", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "css_redirect")

    # The main document itself still loads (a failed stylesheet subresource
    # doesn't fail the whole read) - only the redirect target is proven
    # unreached.
    assert not any("/real.css" in hit for hit in hit_log.hits)


# --- no secondary main-frame navigation (meta refresh) -----------------------


def test_meta_refresh_second_document_is_blocked_even_same_origin(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/meta-refresh")

    result = browser_read_page._execute_read(authority, "meta_refresh")

    assert not any("/meta-target" in hit for hit in hit_log.hits)


# --- stylesheet-origin authority: never implicit, never document authority --


def test_same_origin_stylesheet_blocked_when_not_explicitly_listed(fixtures):
    hit_log, state = fixtures
    # Deliberately NOT including state["a"] in stylesheet_origin_bases -
    # the page's own origin must not implicitly authorize its own
    # stylesheet.
    authority = _authority(state, "/css-same-origin-not-listed-page")

    result = browser_read_page._execute_read(authority, "css_not_listed")

    assert not any("/never-listed.css" in hit for hit in hit_log.hits)


def test_explicitly_allowed_additional_stylesheet_origin_succeeds(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/css-from-c-page", stylesheet_origin_bases=(state["c"],))

    result = browser_read_page._execute_read(authority, "css_from_c")

    assert any(hit == "C/from-c.css" for hit in hit_log.hits)


def test_unapproved_stylesheet_origin_blocked(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/css-from-b-page")

    result = browser_read_page._execute_read(authority, "css_from_b")

    assert not any(hit.startswith("B") for hit in hit_log.hits)


# --- CSS @import obeys the identical stylesheet-origin policy ----------------


def test_css_import_from_approved_origin_succeeds(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/css-import-page", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "css_import_ok")

    assert any("/imported.css" in hit for hit in hit_log.hits)


def test_css_import_from_unapproved_origin_blocked(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/css-import-unapproved-page")

    result = browser_read_page._execute_read(authority, "css_import_blocked")

    assert not any(hit.startswith("B") for hit in hit_log.hits)


# --- non-document/non-stylesheet resource classes: all blocked, zero hits ---


def test_css_background_image_blocked(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/css-bg-image-page")

    result = browser_read_page._execute_read(authority, "css_bg")

    assert not any("/bg.png" in hit for hit in hit_log.hits)


def test_forced_font_request_blocked_with_zero_real_hit(fixtures):
    """Closes the validation gap from the prior M44 P1 report: this fixture
    actually applies the custom font-face to VISIBLE text, forcing
    Chromium to attempt the font fetch (a font-face never referenced by
    visible text is often never requested at all) - the gate must still
    block it before dispatch."""

    hit_log, state = fixtures
    authority = _authority(state, "/forced-font-page")

    result = browser_read_page._execute_read(authority, "forced_font")

    assert not any("/font.woff" in hit for hit in hit_log.hits)


def test_image_resource_blocked(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/with-img")

    result = browser_read_page._execute_read(authority, "with_img")

    assert not any("/img.png" in hit for hit in hit_log.hits)


def test_media_resources_blocked(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/with-media")

    result = browser_read_page._execute_read(authority, "with_media")

    assert not any("/audio.mp3" in hit for hit in hit_log.hits)
    assert not any("/video.mp4" in hit for hit in hit_log.hits)


def test_prefetch_resource_blocked(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/with-prefetch")

    result = browser_read_page._execute_read(authority, "with_prefetch")

    assert not any("/prefetch.txt" in hit for hit in hit_log.hits)


def test_iframe_blocked_even_same_origin(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/with-iframe")

    result = browser_read_page._execute_read(authority, "with_iframe")

    assert not any("/iframe-inner" in hit for hit in hit_log.hits)


# --- cookies: usable within one action, never claimed to persist -------------


def test_cookie_set_by_document_is_sent_on_permitted_stylesheet_request_same_action(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/set-cookie-page", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "set_cookie")

    assert state.get("cookie_seen") == "test_cookie=abc123"


def test_cookies_never_appear_in_action_result(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/set-cookie-page", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "set_cookie_2")

    assert "test_cookie" not in result.message
    assert "abc123" not in result.message


# --- response-size bounds: Content-Length precondition + actual body check --


def test_oversized_document_response_rejected(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/oversized")

    result = browser_read_page._execute_read(authority, "oversized_doc")

    assert result.success is False
    assert result.message == "That page is too large to read."


def test_oversized_stylesheet_response_rejected_but_document_still_loads(fixtures):
    hit_log, state = fixtures
    authority = _authority(
        state, "/oversized-page-for-css", stylesheet_origin_bases=(state["a"],)
    )

    result = browser_read_page._execute_read(authority, "oversized_css")

    # The oversized stylesheet is rejected (proven by the gate's own
    # content-length/body checks - see test_browser_read_page.py's unit
    # coverage of _fetch_and_validate directly); the main document itself
    # is small and still loads successfully. This fixture's document has
    # no <title> element - the handler substitutes a fixed placeholder
    # rather than failing the whole read (see _bounded()'s own docstring).
    assert result.success is True
    assert "(no title)" in result.message


# Note: a genuinely "lying" Content-Length header (declaring far more
# bytes than the server actually sends, then closing the connection)
# produces a real transport-level failure over an actual socket, not a
# clean early-header-rejection - it cannot be constructed as a meaningful
# integration test. The Content-Length PRECONDITION mechanism itself
# (rejecting before the authoritative body-length check, given an
# accurate but oversized header) is proven directly in
# test_browser_read_page.py's
# test_fetch_and_validate_rejects_oversized_content_length_precondition
# using a fake Route/Response object, where the distinction is actually
# observable.


# --- content-type policy ------------------------------------------------------


def test_wrong_document_content_type_rejected(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/wrong-content-type")

    result = browser_read_page._execute_read(authority, "wrong_ctype")

    assert result.success is False
    assert result.message == "That page's content could not be read."


def test_plain_text_document_content_type_accepted(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/plain-text-doc")

    result = browser_read_page._execute_read(authority, "plain_text_doc")

    assert result.success is True
    assert "Just plain text" in result.message


# --- no duplicate requests, cleanup, error privacy ----------------------------


def test_no_duplicate_requests_for_a_normal_page(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/styled", stylesheet_origin_bases=(state["a"],))

    browser_read_page._execute_read(authority, "dup_check")

    paths = [hit for hit in hit_log.hits if hit.startswith("A")]
    assert len(paths) == len(set(paths))


def test_cleanup_occurs_on_success_and_failure_no_leaked_browser_state(fixtures):
    hit_log, state = fixtures
    ok_authority = _authority(state, "/plain")
    fail_authority = _authority(state, "/redirect-main")

    result_ok = browser_read_page._execute_read(ok_authority, "cleanup_ok")
    result_fail = browser_read_page._execute_read(fail_authority, "cleanup_fail")

    assert result_ok.success is True
    assert result_fail.success is False
    # A third, independent call still works cleanly - proves the previous
    # two calls' browser/context/page were fully torn down rather than
    # leaking state that would corrupt a later action.
    result_again = browser_read_page._execute_read(_authority(state, "/plain"), "cleanup_again")
    assert result_again.success is True


def test_error_messages_never_leak_the_real_url_or_scheme(fixtures):
    hit_log, state = fixtures
    authority = _authority(state, "/redirect-to-b")

    result = browser_read_page._execute_read(authority, "no_leak")

    assert "127.0.0.1" not in result.message
    assert "http://" not in result.message
    assert state["b"] not in result.message


# --- M44 P1 adversarial-review correction: action-wide stylesheet bounds ----
#
# A real reproduction (before this correction) proved a 20-level recursive
# CSS @import chain and a 50-<link> page both passed through completely
# unrestricted - the per-response byte cap alone does not bound an action.


def test_recursive_import_chain_stops_at_the_request_count_bound(fixtures):
    hit_log, state = fixtures
    max_requests = browser_read_page.browser_safety.MAX_STYLESHEET_REQUESTS
    state["import_chain_depth"] = max_requests + 7  # substantially more than the bound
    authority = _authority(state, "/import-chain-page", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "import_chain_bound")

    chain_hits = [h for h in hit_log.hits if h.startswith("A/chain-")]
    assert len(chain_hits) <= max_requests
    # The next stylesheet in the chain must never have been requested at all.
    assert f"A/chain-{max_requests}.css" not in chain_hits
    # No CSS/@import parsing happens in Python - the gate itself is the
    # bound, and exceeding it fails the whole read closed rather than
    # silently returning partially-styled content.
    assert result.success is False
    assert result.message == "That page is too large to read."


def test_many_link_page_stops_at_the_same_request_count_bound(fixtures):
    hit_log, state = fixtures
    max_requests = browser_read_page.browser_safety.MAX_STYLESHEET_REQUESTS
    state["many_links_count"] = max_requests + 12
    authority = _authority(state, "/many-links-page", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "many_links_bound")

    link_hits = [h for h in hit_log.hits if h.startswith("A/many-link-")]
    assert len(link_hits) <= max_requests
    assert result.success is False
    assert result.message == "That page is too large to read."


def test_cumulative_stylesheet_bytes_bound_triggers_before_request_count_bound(fixtures):
    """Isolates the CUMULATIVE-byte bound specifically: fewer stylesheets
    than MAX_STYLESHEET_REQUESTS, each individually well under
    MAX_STYLESHEET_RESPONSE_BYTES, but whose combined decompressed size
    exceeds MAX_TOTAL_STYLESHEET_BYTES."""

    hit_log, state = fixtures
    max_requests = browser_read_page.browser_safety.MAX_STYLESHEET_REQUESTS
    max_total = browser_read_page.browser_safety.MAX_TOTAL_STYLESHEET_BYTES
    per_stylesheet_size = 60_000
    count = 5
    assert count < max_requests  # proves the count bound is not what fires
    assert count * per_stylesheet_size > max_total  # proves the cumulative bound must fire

    state["cumulative_stylesheet_count"] = count
    state["cumulative_stylesheet_size"] = per_stylesheet_size
    authority = _authority(state, "/cumulative-bytes-page", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "cumulative_bound")

    fetched = [h for h in hit_log.hits if h.startswith("A/cumulative-") and h.endswith(".css")]
    # Every earlier stylesheet within budget may have been fetched; the
    # count bound itself was never reached (5 < 8).
    assert len(fetched) <= count
    assert result.success is False
    assert result.message == "That page is too large to read."


def test_cumulative_stylesheet_bytes_within_budget_succeeds(fixtures):
    hit_log, state = fixtures
    max_total = browser_read_page.browser_safety.MAX_TOTAL_STYLESHEET_BYTES
    per_stylesheet_size = 1_000
    count = 3
    assert count * per_stylesheet_size < max_total

    state["cumulative_stylesheet_count"] = count
    state["cumulative_stylesheet_size"] = per_stylesheet_size
    authority = _authority(state, "/cumulative-bytes-page", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "cumulative_ok")

    assert result.success is True


# --- M44 P1 adversarial-review correction: explicit route.fetch() timeout ---


def test_stalled_stylesheet_bounded_by_fetch_timeout_not_playwright_default(fixtures, monkeypatch):
    """The load-bearing regression: route.fetch() has its own independent
    timeout (Playwright default 30s, confirmed NOT governed by
    page.set_default_timeout()). A reduced, private _FETCH_TIMEOUT_MS
    proves the explicit timeout argument actually takes effect.

    This asserts the real security property directly, via the fixture's
    own select()-based instrumentation (see the "/slow.css" handler
    above) of exactly how long the CLIENT kept the connection open before
    giving up - not the test's total wall-clock time. Total elapsed time
    for the whole _execute_read() call also includes Chromium/Node
    process launch and teardown, which is real, sometimes-multi-second,
    highly variable overhead unrelated to route.fetch()'s own timeout
    (confirmed by direct reproduction: a bare Playwright launch/close
    with no page work at all took anywhere from ~2.6s to ~12.7s on one
    real Windows machine) - asserting on it produced the M45 P3 flake
    (an observed `elapsed=3.57` against a `< 3.0` bound). The
    client-abort measurement below is immune to that noise because it is
    a server-side timestamp delta, started only once the stylesheet
    request actually arrives and stopped the instant the client's own
    fetch timeout closes the connection."""

    hit_log, state = fixtures
    fetch_timeout_ms = 800
    monkeypatch.setattr(browser_read_page, "_FETCH_TIMEOUT_MS", fetch_timeout_ms)
    state["slow_stylesheet_delay_seconds"] = 3.0  # comfortably longer than 800ms
    authority = _authority(state, "/slow-stylesheet-page", stylesheet_origin_bases=(state["a"],))

    result = browser_read_page._execute_read(authority, "slow_stylesheet")

    # The main document itself loads fine; only the slow stylesheet times
    # out and is aborted - a non-fatal per-resource failure, not a whole-
    # action failure (unlike the count/cumulative bounds above).
    assert result.success is True
    assert "raw" not in result.message.lower()

    abort_after = state["slow_stylesheet_client_abort_after_seconds"]
    fetch_timeout_s = fetch_timeout_ms / 1000
    # The client must have actually given up before the server's full 3s
    # delay elapsed - proves an explicit timeout fired at all (this is
    # None, and the assertion fails, if production regresses to an
    # unbounded wait or Playwright's own 30s default: the server would
    # then observe the full 3s delay elapse with the connection never
    # aborted early).
    assert abort_after is not None, (
        "the client never aborted the stalled stylesheet request early - "
        "route.fetch()'s explicit timeout did not fire"
    )
    # Lower bound: it waited close to the configured timeout, not some
    # unrelated near-instant rejection - proves this genuinely exercised
    # the timeout path rather than some other early-abort reason.
    assert abort_after > fetch_timeout_s * 0.5
    # Upper bound: generous margin over the configured timeout to absorb
    # ordinary OS/network scheduling jitter (observed in repeated local
    # reproduction: ~0.79s-0.82s against an 800ms configured timeout),
    # while staying nowhere near the slow server's real 3s delay and
    # orders of magnitude below Playwright's 30s default - so a
    # regression to either of those still fails this bound.
    assert abort_after < fetch_timeout_s * 3
    assert abort_after < state["slow_stylesheet_delay_seconds"] * 0.8
