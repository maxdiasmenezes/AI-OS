"""
browser_read_page action handler (Milestone 44 P1 - Browser Foundation +
Read-Only Page Inspection): a bounded, read-only render of exactly one
individually registered page (ToolsConfig.approved_pages) - never an
arbitrary URL, selector, form value, or piece of JavaScript. resource_key
is the ONLY input this handler ever accepts; the exact page it renders is
decided entirely by configuration (kernel/config/tools.yaml), never by
request text, description, or expected_result - the same discipline
kernel/tools/handlers/read_text_file.py already established for
approved_files.

FINAL P1 AUTHORITY MODEL (see docs/architecture.md and
kernel/tools/browser_safety.py's own module docstring for the full design
history/validation):

  resource_key
  -> CURRENT ToolsConfig.approved_pages
  -> exact configured canonical HTTPS URL
  -> fresh URL/DNS/private-network validation (_build_authority())
  -> fresh, isolated, JavaScript-disabled browser context
  -> context-wide request gate (_execute_read()'s `gate` closure)
  -> exactly ONE authorized main-frame GET document URL,
     OR an explicitly-authorized stylesheet-origin GET
  -> route.fetch(max_redirects=0)
  -> reject every HTTP redirect and every non-2xx response
  -> fulfill only a validated, bounded, correctly-content-typed response
  -> bounded main-document visible-text observation

Nothing in page content can ever expand this authority: JavaScript is
disabled (java_script_enabled=False - structural, not configurable), so no
page script can ever run, meaning no fetch()/XHR/WebSocket/service-worker
registration/JS navigation/window.open() can ever be attempted in the
first place (see browser_safety.py's module docstring for the empirical
validation this rests on). The context-wide gate is a small, fixed
ALLOWLIST (main-frame document once, or an approved-origin stylesheet) -
not a denylist - so a resource type this module's authors never thought to
name explicitly is denied by default, never allowed by omission.

HTTP REDIRECTS ARE CATEGORICALLY UNSUPPORTED (a deliberate P1 design
choice, not a temporary limitation - Playwright's context.route() does not
reliably re-invoke for a redirected request's target in this environment,
a confirmed, currently-open upstream limitation; see
kernel/tools/browser_safety.py's module docstring). Every permitted
request is fetched via route.fetch(max_redirects=0) and inspected before
ever being fulfilled into Chromium - any redirect (or any non-2xx status)
fails the whole read closed. The administrator must configure the final
canonical HTTPS URL directly. This also means meta refresh, HTML-triggered
navigation, and any other second main-frame document request are always
denied (see PageAuthority/is_request_permitted's own docstrings) - the one
configured URL is the entire page-navigation authority, consumed exactly
once per action.

STYLESHEET REQUEST/BYTE BOUNDS (M44 P1 adversarial-review correction): a
real reproduction proved a 20-level recursive CSS @import chain and a
50-<link> page both passed through completely unrestricted before this
correction - the per-response MAX_STYLESHEET_RESPONSE_BYTES bound alone
does not bound an ACTION. `_execute_read()`'s gate closure now tracks, per
action, how many stylesheet requests have been fulfilled
(browser_safety.MAX_STYLESHEET_REQUESTS, checked and consumed BEFORE
route.fetch() is ever called for the next one - an excess request is
aborted before any network dispatch) and how many cumulative decompressed
bytes have been fulfilled (browser_safety.MAX_TOTAL_STYLESHEET_BYTES,
checked after each fetch, before that response is ever fulfilled).
Exceeding either bound fails the WHOLE action closed once page.goto()
returns - never a silent partial-styling success. No CSS/@import parsing
happens in this module or anywhere in Python; the request gate itself is
the bound, exactly as intended.

NETWORK-WAIT UPPER BOUND: every route.fetch() call (document or
stylesheet) now receives an explicit `timeout=_FETCH_TIMEOUT_MS` (M44 P1
adversarial-review correction - route.fetch() has its own independent
timeout, defaulting to Playwright's unrelated 30s, confirmed empirically
NOT governed by page.set_default_timeout()/set_default_navigation_timeout()
at all). Combined with the one-document-request-only rule and
MAX_STYLESHEET_REQUESTS, this gives a calculable theoretical upper bound
on total network-wait time for one action:

    (1 document fetch + MAX_STYLESHEET_REQUESTS stylesheet fetches)
    * _FETCH_TIMEOUT_MS
    = 9 * 5,000ms = 45,000ms worst case

This is a THEORETICAL ceiling assuming every permitted fetch independently
stalls for the full timeout with no parallelism; the actual page/
navigation lifecycle (bounded separately by _NAVIGATION_TIMEOUT_MS on
page.goto() itself) may, and in ordinary operation does, terminate sooner.
No general action-wide cancellation/deadline infrastructure was added -
this bound is the deliberately smallest architecture that still gives a
defensible, calculable ceiling (one explicit per-fetch timeout + one
explicit per-action request-count cap), per design instruction.

SESSION MODEL: one fresh, isolated Playwright browser process + browser
context + page is launched, used, and fully closed within this single
handler call - no persistence, no reuse across actions, no user Chrome/Edge
profile, headless (fixed, code-owned, not configurable). A server may set
cookies that are automatically reused by later PERMITTED requests within
this SAME action's own request sequence (ordinary browser behavior) - but
the entire context, and any such cookie state, is destroyed at the end of
this one call and never persists to another action; browser_read_page has
no authenticated-session feature of any kind.

PRIVATE TEST SEAM: the public run() entry point is the ONLY path that ever
builds a PageAuthority from untrusted, config-authored strings - it always
calls _build_authority(), which always performs full HTTPS/private-network/
DNS validation, with no bypass reachable from ToolsConfig, ActionRequest,
resource_key, an environment variable, the planner, a model, or any other
registry action. _execute_read() is the private, low-level executor that
performs the actual browser work given an ALREADY-BUILT PageAuthority -
tests import it directly and construct their own PageAuthority pointing at
real local loopback fixture servers, entirely bypassing
production validation (see tests/kernel/tools/handlers/
test_browser_read_page.py and the M44 P1 integration test module for the
real production validation's own, separate, dedicated tests).

RESPONSE-SIZE RESIDUAL RISK (see kernel/tools/browser_safety.py's own
MAX_DOCUMENT_RESPONSE_BYTES/MAX_STYLESHEET_RESPONSE_BYTES/
MAX_STYLESHEET_REQUESTS/MAX_TOTAL_STYLESHEET_BYTES docstrings): Playwright
1.62.0's route.fetch() exposes no hard pre-buffering response-byte
ceiling. A Content-Length precondition is a cheap, best-effort EARLY
REJECTION only - NEVER authoritative, confirmed empirically: compression
means Content-Length reflects the WIRE size, while APIResponse.body()
returns DECOMPRESSED bytes (a 519-byte gzip response was shown to
decompress to 500,000 bytes) - so a compressed response always bypasses
that precondition regardless of its true decompressed size. The
authoritative bound is always the actual len(APIResponse.body()), checked
after fetch and before fulfillment - that check, together with
MAX_STYLESHEET_REQUESTS and MAX_TOTAL_STYLESHEET_BYTES, guarantees
Chromium is never FULFILLED with more than a bounded amount of data in one
action. None of these checks prevent the underlying fetch() call's own
network/memory/decompression cost inside Playwright's Node driver
process for any ONE response, which has already been paid by the time any
check runs - a small, highly-compressible response can still force a
large decompression before Python-side rejection occurs. This is an
accepted, honestly-documented residual risk for a personal, single-
action-at-a-time system (see docs/architecture.md's M44 P1 section) -
mitigated, not eliminated, by the fixed per-fetch timeout
(_FETCH_TIMEOUT_MS), the per-action request-count/cumulative-byte bounds,
and the fact that the whole browser process is destroyed at the end of
every single action, never a sustained or accumulating resource leak
across actions. No decompression subsystem exists or is planned in P1 -
this risk is bounded in scope and duration, not eliminated.
"""

import re
from urllib.parse import urlsplit

from kernel.tools import browser_safety
from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH
from kernel.tools.types import ActionRequest, ActionResult

# --- fixed, code-owned timeouts (never caller/model-supplied - see design
# section "No timeout argument") -------------------------------------------
_NAVIGATION_TIMEOUT_MS = 15_000
_DEFAULT_TIMEOUT_MS = 8_000

# M44 P1 adversarial-review correction: route.fetch() has its OWN
# independent timeout, defaulting (per Playwright's own documentation) to
# 30,000ms - confirmed empirically NOT governed by
# page.set_default_timeout()/set_default_navigation_timeout() at all (a
# short page-level default left a route.fetch() call still pending well
# past it). Every production route.fetch() call must therefore receive
# this explicit timeout directly - never Playwright's own default, and
# never inferred from the page-level settings above. 5 seconds is
# conservative for the P1 static-reader use case (one small document, a
# handful of small stylesheets, all on already-DNS/private-network-checked
# hosts) while still being short enough that MAX_STYLESHEET_REQUESTS
# stalled/slow permitted stylesheets cannot silently multiply into an
# unbounded total wait (see this module's own docstring's NETWORK-WAIT
# UPPER BOUND section for the resulting calculable worst case).
_FETCH_TIMEOUT_MS = 5_000

# --- output field bounds (Milestone 44 P1 design sections 30-33) ----------
# MAX_SYMBOLIC_NAME_LENGTH (64) bounds the page key - reused, not
# reinvented, matching read_text_file.py/file_metadata.py's own precedent.
# The remaining three are chosen with the same discipline
# read_text_file.py's own MAX_TEXT_FILE_BYTES docstring documents: small
# enough, combined, to leave real margin inside
# kernel.employee_tasks.MAX_STEP_RESULT_JSON_CHARS (4,096) even under
# worst-case JSON-escaping (every character a literal double quote, the
# single most expensive ordinary character to escape) - see
# tests/kernel/tools/handlers/test_browser_read_page.py's dedicated
# worst-case-serialization proof for the authoritative measurement against
# the REAL build_action_observation()/serialize_observation() pipeline,
# not an estimate.
MAX_TITLE_CHARS = 200
MAX_LOCATION_CHARS = 200
MAX_VISIBLE_TEXT_CHARS = 1200

# Every C0 control byte/codepoint except tab/newline/carriage-return, plus
# DEL - identical policy to, and for the identical reason as,
# read_text_file.py's own _DISALLOWED_CONTROL_BYTES_RE (JSON-escaping cost,
# not merely "looks binary"): rejecting these outright caps every
# character's own JSON-escaping expansion at 2x (a quote, backslash, tab,
# newline, or carriage return) rather than up to 6x.
_DISALLOWED_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# --- fixed, code-authored safe messages - never raw exception/URL/DNS/
# certificate/filesystem/browser-path text (design section 35) -----------
_NOT_REGISTERED_MESSAGE = "That page is not registered."
_NOT_PERMITTED_MESSAGE = "That page is not permitted."
_UNAVAILABLE_MESSAGE = "That page is unavailable."
_REDIRECTED_MESSAGE = "That page redirected and cannot be read."
_TOO_LARGE_MESSAGE = "That page is too large to read."
_CONTENT_UNREADABLE_MESSAGE = "That page's content could not be read."
_BROWSER_UNAVAILABLE_MESSAGE = "Browser support is unavailable."

_NOT_REGISTERED = ActionResult(False, _NOT_REGISTERED_MESSAGE, "rejected")

# A missing <title> or an empty extracted body are both ordinary,
# legitimate page states (not every real page has a <title> element, and
# an image-only or markup-only page can have no text nodes at all) - see
# _bounded()'s own docstring for why these get a placeholder rather than
# failing the whole read.
_NO_TITLE_PLACEHOLDER = "(no title)"
_NO_TEXT_PLACEHOLDER = "(no visible text)"

_REASON_MESSAGE = {
    "too_large": _TOO_LARGE_MESSAGE,
    "unreadable": _CONTENT_UNREADABLE_MESSAGE,
}

_MESSAGE_BY_DOCUMENT_OUTCOME = {
    "denied": _NOT_PERMITTED_MESSAGE,
    "redirect": _REDIRECTED_MESSAGE,
    "http_error": _UNAVAILABLE_MESSAGE,
    "bad_content_type": _CONTENT_UNREADABLE_MESSAGE,
    "too_large": _TOO_LARGE_MESSAGE,
    "fetch_error": _UNAVAILABLE_MESSAGE,
}
_OUTCOME_CODE_BY_DOCUMENT_OUTCOME = {
    "denied": "rejected",
    "redirect": "rejected",
    "http_error": "failed",
    "bad_content_type": "rejected",
    "too_large": "rejected",
    "fetch_error": "failed",
}

# M44 P1 adversarial-review correction: exceeding either the per-action
# stylesheet request-count or cumulative-byte bound fails the WHOLE read
# closed (design instruction: "fail the browser_read_page action closed
# with a fixed safe message rather than silently succeeding with
# partially-styled content") - reuses the existing generic
# _TOO_LARGE_MESSAGE vocabulary rather than adding a near-duplicate
# message, matching this module's established minimal-message-set
# discipline.
_STYLESHEET_LIMIT_EXCEEDED_MESSAGE = _TOO_LARGE_MESSAGE


def _build_authority(spec) -> browser_safety.PageAuthority:
    """Fresh, execution-time re-validation of one configured
    ApprovedPageSpec - config-load-time validation
    (kernel/tools/config.py) is never treated as a guarantee at execution
    time (mirrors kernel/tools/file_safety.py's own FRESH CHECKS ONLY
    doctrine). This is also the ONLY path in this module that ever builds
    a PageAuthority from untrusted, config-authored strings - a
    hand-built ToolsConfig that bypasses load_tools_config()'s own checks
    is still caught here, before any browser is ever launched. Raises
    browser_safety.BrowserSafetyError on any problem; never returns a
    partially-validated PageAuthority."""

    normalized_url, document_origin = browser_safety.parse_https_url(
        spec.url, field_name="approved_pages.url"
    )
    browser_safety.fresh_dns_safety_check(
        document_origin.host, field_name="approved_pages.url"
    )

    stylesheet_origins = []
    for raw_origin in spec.allowed_stylesheet_origins:
        origin = browser_safety.parse_https_origin(
            raw_origin, field_name="approved_pages.allowed_stylesheet_origins"
        )
        browser_safety.fresh_dns_safety_check(
            origin.host, field_name="approved_pages.allowed_stylesheet_origins"
        )
        stylesheet_origins.append(origin)

    return browser_safety.PageAuthority(
        document_url=normalized_url,
        document_origin=document_origin,
        stylesheet_origins=tuple(stylesheet_origins),
    )


def _fetch_and_validate(route, *, max_bytes: int, allowed_content_types: frozenset):
    """Shared post-permission validation for both the document and
    stylesheet branches of the gate: fetch WITHOUT following redirects and
    with an explicit, code-owned _FETCH_TIMEOUT_MS (M44 P1 adversarial-
    review correction - route.fetch() has its own independent timeout,
    defaulting to Playwright's unrelated 30s, never governed by
    page.set_default_timeout()), then fail closed on any redirect, any
    non-2xx status, a mismatched Content-Type, or an oversized response (a
    cheap, ADVISORY-ONLY Content-Length precondition first, then the
    authoritative actual body-length check - see
    kernel/tools/browser_safety.py's own MAX_DOCUMENT_RESPONSE_BYTES
    docstring for why Content-Length can never be authoritative:
    APIResponse.body() returns DECOMPRESSED bytes, which a compressed
    response's Content-Length header does not reflect). Returns
    (APIResponse, body_length, "ok") on success, or (None, None,
    reason_code) on any failure - never raises to the caller; a fetch
    exception is itself just one more reason code."""

    try:
        response = route.fetch(max_redirects=0, timeout=_FETCH_TIMEOUT_MS)
    except Exception:
        return None, None, "fetch_error"

    if 300 <= response.status < 400:
        return None, None, "redirect"
    if not response.ok:
        return None, None, "http_error"
    if not browser_safety.content_type_matches(
        response.headers.get("content-type"), allowed_content_types
    ):
        return None, None, "bad_content_type"

    # Advisory-only early rejection - see module docstring; never
    # authoritative on its own, so a malformed or absent header falls
    # through to the authoritative check below rather than being treated
    # as a failure by itself.
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                return None, None, "too_large"
        except ValueError:
            pass

    body = response.body()
    if len(body) > max_bytes:
        return None, None, "too_large"

    return response, len(body), "ok"


def _safe_location(url: str) -> str:
    """origin + path only - never query, fragment, or userinfo (design
    section 30: even though the exact configured query is part of what
    was authorized for network use, it is never persisted into output,
    since it may carry tokens never meant to be relayed)."""

    parts = urlsplit(url)
    port = parts.port or 443
    path = parts.path or "/"
    return f"{parts.scheme}://{parts.hostname}:{port}{path}"


def _bounded(
    value: str, max_chars: int, *, placeholder: str | None = None
) -> tuple[str | None, str | None]:
    """Returns (value, None) on success, or (None, reason_code) on
    failure - the caller must fail closed in every failure case, never
    silently truncate (design section 32/33: 'do not silently truncate and
    claim successful page reading'). `reason_code` is "too_large" (the
    length bound was exceeded, or - with no placeholder - the value was
    empty) or "unreadable" (a disallowed control character is present) -
    kept distinct so the caller maps each to its own correct fixed
    message, never conflating "oversized" with "contains unreadable
    content".

    If `placeholder` is given, an EMPTY value is treated as legitimate
    (e.g. a page with no <title> element, or no extractable visible text -
    both real, ordinary page states, not failures) and returns
    (placeholder, None) rather than failing. `location` never passes a
    placeholder: it is always derived from the real final URL
    (kernel/tools/browser_safety.py's URL parsing always yields at least a
    "/" path), so an empty value there would indicate an actual defect,
    never a legitimate page state."""

    if not value:
        if placeholder is not None:
            return placeholder, None
        return None, "too_large"
    if _DISALLOWED_CONTROL_CHARS_RE.search(value):
        return None, "unreadable"
    if len(value) > max_chars:
        return None, "too_large"
    return value, None


def _execute_read(authority: browser_safety.PageAuthority, page_key: str) -> ActionResult:
    """The private, low-level browser executor - receives an ALREADY-BUILT
    PageAuthority (never a resource_key or ToolsConfig) and performs
    exactly one bounded read. See this module's own docstring's PRIVATE
    TEST SEAM section: production code only ever reaches this through
    run() -> _build_authority(), but tests may call this directly with a
    hand-built PageAuthority pointing at real local fixture servers."""

    from playwright.sync_api import sync_playwright

    document_consumed = [False]
    document_outcome = {"code": None}
    # M44 P1 adversarial-review correction: per-action stylesheet
    # bookkeeping - a real reproduction proved a 20-level recursive CSS
    # @import chain and a 50-<link> page both passed through completely
    # unrestricted without this. "count" is checked and incremented BEFORE
    # route.fetch() is ever called for a stylesheet, so the (N+1)th
    # request is aborted before any network dispatch, never fetched and
    # then discarded. "total_bytes" accumulates each fulfilled
    # stylesheet's authoritative, DECOMPRESSED body length (never
    # Content-Length - see _fetch_and_validate()'s own docstring).
    # "outcome", once set, fails the WHOLE action closed after page.goto()
    # returns, even if the main document itself loaded successfully -
    # never a silent partial-styling success (design instruction).
    #
    # CONCURRENCY HONESTY: Chromium may dispatch several stylesheet
    # requests for the same page near-simultaneously. This bookkeeping
    # bounds how many are ever FULFILLED and how many cumulative bytes are
    # ever fulfilled - it cannot retroactively un-dispatch a request
    # Chromium already sent concurrently with the one that pushed a bound
    # over its limit. No stronger cancellation is claimed than Playwright
    # actually provides.
    stylesheet_state = {"count": 0, "total_bytes": 0, "outcome": None}

    def gate(route, request):
        resource_type = request.resource_type
        method = request.method
        url = request.url
        is_main_frame = request.frame == page.main_frame

        permitted = browser_safety.is_request_permitted(
            authority,
            url=url,
            method=method,
            resource_type=resource_type,
            is_main_frame=is_main_frame,
            document_consumed=document_consumed[0],
        )

        if resource_type == "document" and is_main_frame:
            if not permitted:
                if document_outcome["code"] is None:
                    document_outcome["code"] = "denied"
                route.abort()
                return
            document_consumed[0] = True
            response, _body_len, reason = _fetch_and_validate(
                route,
                max_bytes=browser_safety.MAX_DOCUMENT_RESPONSE_BYTES,
                allowed_content_types=browser_safety.ALLOWED_DOCUMENT_CONTENT_TYPES,
            )
            document_outcome["code"] = reason
            if response is None:
                route.abort()
                return
            route.fulfill(response=response)
            return

        if not permitted:
            route.abort()
            return

        if resource_type == "stylesheet":
            if stylesheet_state["count"] >= browser_safety.MAX_STYLESHEET_REQUESTS:
                if stylesheet_state["outcome"] is None:
                    stylesheet_state["outcome"] = "count_exceeded"
                route.abort()
                return
            stylesheet_state["count"] += 1

            response, body_len, _reason = _fetch_and_validate(
                route,
                max_bytes=browser_safety.MAX_STYLESHEET_RESPONSE_BYTES,
                allowed_content_types=browser_safety.ALLOWED_STYLESHEET_CONTENT_TYPES,
            )
            if response is None:
                route.abort()
                return

            if (
                stylesheet_state["total_bytes"] + body_len
                > browser_safety.MAX_TOTAL_STYLESHEET_BYTES
            ):
                if stylesheet_state["outcome"] is None:
                    stylesheet_state["outcome"] = "cumulative_exceeded"
                route.abort()
                return

            stylesheet_state["total_bytes"] += body_len
            route.fulfill(response=response)
            return

        route.abort()

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception:
                return ActionResult(False, _BROWSER_UNAVAILABLE_MESSAGE, "failed")

            try:
                context = browser.new_context(
                    java_script_enabled=False, service_workers="block"
                )
                try:
                    page = context.new_page()
                    page.set_default_navigation_timeout(_NAVIGATION_TIMEOUT_MS)
                    page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
                    context.route("**/*", gate)

                    try:
                        page.goto(authority.document_url, wait_until="load")
                    except Exception:
                        code = document_outcome["code"] or "fetch_error"
                        return ActionResult(
                            False,
                            _MESSAGE_BY_DOCUMENT_OUTCOME.get(code, _UNAVAILABLE_MESSAGE),
                            _OUTCOME_CODE_BY_DOCUMENT_OUTCOME.get(code, "failed"),
                        )

                    if document_outcome["code"] != "ok":
                        code = document_outcome["code"] or "fetch_error"
                        return ActionResult(
                            False,
                            _MESSAGE_BY_DOCUMENT_OUTCOME.get(code, _UNAVAILABLE_MESSAGE),
                            _OUTCOME_CODE_BY_DOCUMENT_OUTCOME.get(code, "failed"),
                        )

                    if stylesheet_state["outcome"] is not None:
                        # The main document itself loaded fine, but the
                        # page required more stylesheet requests, or more
                        # cumulative stylesheet bytes, than P1 authorizes -
                        # fail the whole read closed rather than silently
                        # succeed with partially-styled (and therefore
                        # potentially wrongly-visible/hidden) text.
                        return ActionResult(False, _STYLESHEET_LIMIT_EXCEEDED_MESSAGE, "rejected")

                    try:
                        raw_title = page.title()
                        raw_text = page.locator("body").inner_text()
                        final_url = page.url
                    except Exception:
                        return ActionResult(False, _CONTENT_UNREADABLE_MESSAGE, "failed")
                finally:
                    context.close()
            finally:
                browser.close()
    except Exception:
        return ActionResult(False, _BROWSER_UNAVAILABLE_MESSAGE, "failed")

    location, reason = _bounded(_safe_location(final_url), MAX_LOCATION_CHARS)
    if location is None:
        return ActionResult(False, _REASON_MESSAGE[reason], "rejected")

    title, reason = _bounded(raw_title, MAX_TITLE_CHARS, placeholder=_NO_TITLE_PLACEHOLDER)
    if title is None:
        return ActionResult(False, _REASON_MESSAGE[reason], "rejected")

    text, reason = _bounded(raw_text, MAX_VISIBLE_TEXT_CHARS, placeholder=_NO_TEXT_PLACEHOLDER)
    if text is None:
        return ActionResult(False, _REASON_MESSAGE[reason], "rejected")

    message = f"Page: '{page_key}'\nLocation: {location}\nTitle: {title}\nContent:\n{text}"
    return ActionResult(True, message, "executed")


def run(request: ActionRequest, tools_config) -> ActionResult:
    resource_key = request.resource_key
    if resource_key is not None and len(resource_key) > MAX_SYMBOLIC_NAME_LENGTH:
        return _NOT_REGISTERED
    if resource_key is None:
        return _NOT_REGISTERED

    spec = tools_config.approved_pages.get(resource_key)
    if spec is None:
        return _NOT_REGISTERED

    try:
        authority = _build_authority(spec)
    except browser_safety.BrowserSafetyError:
        return ActionResult(False, _NOT_PERMITTED_MESSAGE, "rejected")

    return _execute_read(authority, resource_key)
