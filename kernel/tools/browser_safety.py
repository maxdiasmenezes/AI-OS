"""
Shared URL/origin parsing, normalization, private-network rejection, and
DNS defense-in-depth for kernel/tools/config.py (config-load-time
validation of approved_pages) and kernel/tools/handlers/browser_read_page.py
(the runtime request-allowlist gate) - mirrors kernel/tools/file_safety.py's
and kernel/tools/git_safety.py's own "shared, not duplicated" precedent for
logic more than one caller needs identically.

Nothing here launches a browser or performs a browser action - this module
is pure URL/origin validation plus one narrow, bounded DNS-resolution
helper. It has no dependency on kernel/tools/config.py (config.py depends
on this module, never the reverse) and no dependency on playwright.

AUTHORITY MODEL (Milestone 44 P1 - see docs/architecture.md): a browser
page's document URL and its stylesheet-origin allowlist are two distinct,
non-overlapping kinds of authority. `Origin` (scheme + normalized host +
normalized port only - never path, query, fragment, or userinfo) is the
comparison unit for stylesheet authority; the full normalized URL (with
path and query, fragment always dropped) is the comparison unit for the
one permitted main-document request. `is_request_permitted()` is the
single, pure allowlist function shared by the real runtime gate and by
network-policy unit tests that never launch a browser - a stylesheet-
approved origin NEVER authorizes a document request on that origin, and a
document is permitted exactly once per action (see PageAuthority /
is_request_permitted's own docstrings).

PRIVATE-NETWORK POLICY: `parse_https_url()`/`parse_https_origin()` reject
https-only, userinfo, and any host that is the literal name "localhost" or
a literal IP address in a loopback/private/link-local/reserved/multicast/
unspecified range at parse time. A plain hostname (not an IP literal)
cannot be checked this way, since its resolved address isn't known until
DNS resolution happens - `fresh_dns_safety_check()` is the separate,
execution-time defense-in-depth check for that case (see its own
docstring for the honestly-documented residual DNS-rebinding limitation:
this is not full DNS pinning, and Chromium performs its own independent
resolution afterward).
"""

import ipaddress
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from kernel.tools import process_control

# kernel/tools/browser_safety.py -> kernel/tools -> kernel -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DNS_WORKER_SCRIPT = Path(__file__).resolve().parent / "dns_resolver_worker.py"
_DNS_WORKER_MAX_OUTPUT_BYTES = 4096

# Conservative fixed bounds on config-authored strings - mirrors
# kernel/tools/config.py's own MAX_SYMBOLIC_NAME_LENGTH precedent: every
# one of these values may eventually be echoed (in normalized form) into
# something bounded by kernel.employee_tasks.MAX_STEP_RESULT_JSON_CHARS,
# so none of them may be arbitrarily large even though they are always
# config-authored, never model- or request-supplied.
MAX_URL_LENGTH = 2048
MAX_STYLESHEET_ORIGIN_LENGTH = 256
MAX_STYLESHEET_ORIGINS = 5

_ALLOWED_SCHEME = "https"
_DEFAULT_PORTS = {"https": 443, "http": 80}
_LOCALHOST_NAMES = frozenset({"localhost"})

# A conservative ASCII-only hostname character allowlist (letters, digits,
# dot, hyphen) - rejects a wildcard ("*.example.com"), any other glob/regex
# metacharacter, and any raw Unicode-typed IDN hostname outright at parse
# time. This is a deliberate policy choice, not an oversight: a config
# author who wants an internationalized domain must write its ASCII/
# punycode ("xn--...") form explicitly - never a Unicode string this
# module would otherwise need to IDNA-encode (and could encode
# ambiguously/differently than the browser itself does) before comparison.
_HOSTNAME_CHAR_RE = re.compile(r"^[A-Za-z0-9.\-]+$")


def _validate_hostname_syntax(host: str, *, field_name: str) -> None:
    if not _HOSTNAME_CHAR_RE.match(host):
        raise BrowserSafetyError(f"{field_name} contains an invalid hostname character")


# Milestone 44 P1 correction: Chromium rewrites a configured URL's path
# before ever reporting it back as a live Request.url - confirmed
# empirically for two specific rewrites:
#   - dot-segment resolution ("/a/../b" -> "/b", "/a/./b" -> "/a/b"),
#     including case-insensitive percent-encoded "%2e"/"%2E" forms of ".";
#   - backslash-as-slash ("/a\b" -> "/a/b", and a leading backslash
#     immediately after the host behaves the same way).
# Rather than emulate Chromium's own RFC 3986 section 5.2.4 path
# canonicalization in this module (a general canonicalizer this codebase
# would then have to keep in lockstep with whatever Chromium actually
# does), a configured document URL whose path could trigger either
# rewrite is rejected outright at config-validation time - the admin must
# write the already-resolved, backslash-free path directly. Ordinary
# percent-encoded path bytes (anything other than an encoded dot) and
# repeated ordinary slashes are left untouched: both were confirmed
# empirically to be preserved verbatim by Chromium, so there is nothing
# to reject or normalize about them.
_PERCENT_DOT_RE = re.compile(r"%2e", re.IGNORECASE)


def _reject_ambiguous_path(path: str, *, field_name: str) -> None:
    if "\\" in path:
        raise BrowserSafetyError(f"{field_name} must not contain a backslash")

    for segment in path.split("/"):
        decoded = _PERCENT_DOT_RE.sub(".", segment)
        if decoded in (".", ".."):
            raise BrowserSafetyError(
                f"{field_name} must not contain a '.' or '..' path segment"
            )

# Bounded, code-owned timeout for the defense-in-depth DNS check below -
# never unbounded, since a slow/unresponsive resolver must not stall an
# action indefinitely.
DNS_RESOLUTION_TIMEOUT_SECONDS = 3.0

# Section 25/26 of the M44 P1 design: route.fetch(max_redirects=0)
# provides no hard pre-buffering response-byte ceiling (empirically
# confirmed during the M44 P1 validation passes - a multi-megabyte
# response was fetched and fulfilled with no friction). These are the
# P1 code-owned ceilings enforced AFTER route.fetch() returns and BEFORE
# a response is ever fulfilled into Chromium - see
# kernel/tools/handlers/browser_read_page.py's own docstring for the
# two-stage (Content-Length precondition, then actual body length) check
# this bounds.
#
# CONTENT-LENGTH IS ADVISORY ONLY, NEVER AUTHORITATIVE (M44 P1 adversarial
# review correction): empirically confirmed that Playwright's
# APIResponse.body() returns DECOMPRESSED bytes, while the Content-Length
# header reflects the WIRE (possibly compressed) size - a 519-byte gzip
# response was shown to decompress to 500,000 bytes. A Content-Length
# precondition therefore NEVER catches a compressed response before the
# expensive fetch+decompress has already happened inside Playwright's Node
# driver process - it is a cheap, best-effort EARLY REJECTION for the
# common uncompressed/accurately-labeled case, nothing more. The
# authoritative bound is always len(APIResponse.body()) - the actual
# decompressed byte count - checked AFTER fetch, BEFORE fulfillment; that
# check is what guarantees Chromium is never fulfilled with an oversized
# body, regardless of what any Content-Length header claimed.
#
# RESIDUAL RISK, HONESTLY STATED: neither check prevents the underlying
# fetch()'s own network/memory/decompression cost inside Playwright's Node
# driver process, which has already been paid by the time either bound is
# checked - a small, highly-compressible response can force a large
# decompression before Python-side rejection ever occurs. Accepted for a
# personal, single-action-at-a-time system whose whole browser process is
# destroyed at the end of every action; MAX_STYLESHEET_REQUESTS and
# MAX_TOTAL_STYLESHEET_BYTES below bound how many times this cost can be
# paid, and how much it can accumulate to, within one action - they do not
# eliminate the per-response amplification risk itself.
#
# MAX_DOCUMENT_RESPONSE_BYTES (256 KiB) is sized generously enough to hold
# a real HTML documentation-style page (raw markup is far more verbose per
# visible character than the extracted text browser_read_page.py
# ultimately returns) while still being a small, fixed ceiling - not an
# attempt to support an arbitrarily large document.
# MAX_STYLESHEET_RESPONSE_BYTES (64 KiB) is smaller because a legitimate
# stylesheet is typically much smaller than a full page and is never
# returned in output at all - it only affects which text is `display:none`.
MAX_DOCUMENT_RESPONSE_BYTES = 262_144
MAX_STYLESHEET_RESPONSE_BYTES = 65_536

# M44 P1 adversarial-review correction: the per-response bound above does
# NOT bound an action as a whole. A real-browser reproduction proved a
# 20-level recursive CSS @import chain and a page with 50 distinct <link
# rel=stylesheet> tags both passed through completely unrestricted -
# nothing previously counted how many stylesheet requests, or how many
# cumulative bytes, one action had already consumed. These two bounds
# close that gap:
#
# MAX_STYLESHEET_REQUESTS (8): P1 is a bounded static-page reader, not a
# general browser - eight external/imported stylesheet requests is enough
# for ordinary static-page rendering (a page's own stylesheet plus a
# handful of explicitly-approved CDN/font/shared stylesheets) while making
# recursive @import and <link> fan-out explicitly, deterministically
# bounded rather than left to incidental Chromium scheduling. Checked and
# enforced BEFORE route.fetch() is ever called for the (N+1)th stylesheet
# request - an excess request is aborted before any network dispatch, not
# fetched and then discarded (see kernel/tools/handlers/browser_read_page.py's
# gate closure).
#
# MAX_TOTAL_STYLESHEET_BYTES (256 KiB): tracks the authoritative
# DECOMPRESSED body length of every stylesheet actually fulfilled so far
# in the current action - never Content-Length (see above). Even with
# MAX_STYLESHEET_REQUESTS capping the count, 8 stylesheets at the full
# per-response 64 KiB ceiling would total 512 KiB; this cumulative bound
# is set below that product specifically so the two bounds are not purely
# redundant - a page cannot spend its full per-request allowance on every
# one of its 8 permitted stylesheets. Enforced AFTER each stylesheet body
# is fetched (the actual byte count is not knowable before fetching), so
# it cannot prevent that one response's own fetch/decompression cost (see
# the residual-risk note above) - only cumulative fulfillment past it.
MAX_STYLESHEET_REQUESTS = 8
MAX_TOTAL_STYLESHEET_BYTES = 256 * 1024

# Content-Type policy (design section 27) - an exact match against the
# MIME type only (the part before any ";charset=..."/parameter suffix) -
# never a substring/prefix check on the full raw header value.
ALLOWED_DOCUMENT_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
ALLOWED_STYLESHEET_CONTENT_TYPES = frozenset({"text/css"})


class BrowserSafetyError(ValueError):
    """Raised for any URL/origin/host that fails validation. Config-load-time
    callers (kernel/tools/config.py) wrap this into ToolsConfigError context;
    runtime callers (kernel/tools/handlers/browser_read_page.py) treat it as
    a rejection - never relayed verbatim into an ActionResult.message (see
    that handler's own error-privacy discipline)."""


@dataclass(frozen=True)
class Origin:
    """scheme + normalized host + normalized port - deliberately never
    path, query, or fragment (see module docstring's AUTHORITY MODEL
    section: origin authority is strictly narrower than URL authority)."""

    scheme: str
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


def _normalize_host(host: str) -> str:
    """Lowercase, and strip exactly one trailing dot - a fully-qualified
    DNS name's trailing dot carries no semantic difference from the same
    name without it, so both must compare equal."""

    normalized = host.lower()
    if normalized != "." and normalized.endswith("."):
        normalized = normalized[:-1]
    return normalized


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _reject_disallowed_address(addr, *, field_name: str) -> None:
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        raise BrowserSafetyError(
            f"{field_name} must not target a private, loopback, or link-local address"
        )


def _reject_disallowed_host(host: str, *, field_name: str) -> None:
    """Config/parse-time rejection for a normalized host: the literal name
    'localhost', or a literal IP address in a disallowed range. A plain
    hostname that isn't an IP literal passes here (nothing is knowable
    about what it resolves to yet) - see fresh_dns_safety_check() for the
    separate, execution-time check of that case."""

    if host in _LOCALHOST_NAMES:
        raise BrowserSafetyError(f"{field_name} must not target localhost")

    if _is_ip_literal(host):
        addr = ipaddress.ip_address(host.strip("[]"))
        _reject_disallowed_address(addr, field_name=field_name)


def parse_https_url(raw, *, field_name: str) -> tuple[str, Origin]:
    """Parse and validate `raw` as an absolute, well-formed HTTPS URL with
    no userinfo and no disallowed literal host. Returns
    (normalized_full_url, Origin) - the normalized URL preserves path and
    query (needed for exact main-document authority - see
    kernel/tools/handlers/browser_read_page.py's PageAuthority.document_url)
    but always drops any fragment (fragments are client-side only and are
    never sent to a server, so they carry no network authority). Raises
    BrowserSafetyError on any problem; never returns a partial result."""

    if not isinstance(raw, str) or not raw.strip():
        raise BrowserSafetyError(f"{field_name} must be a non-empty string")
    if len(raw) > MAX_URL_LENGTH:
        raise BrowserSafetyError(f"{field_name} exceeds the maximum length ({MAX_URL_LENGTH})")
    if "\x00" in raw:
        raise BrowserSafetyError(f"{field_name} must not contain a NUL character")

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise BrowserSafetyError(f"{field_name} is not a valid URL") from exc

    if parts.scheme.lower() != _ALLOWED_SCHEME:
        raise BrowserSafetyError(f"{field_name} must use https")
    if parts.username is not None or parts.password is not None:
        raise BrowserSafetyError(f"{field_name} must not contain userinfo")
    if not parts.hostname:
        raise BrowserSafetyError(f"{field_name} must include a hostname")
    _validate_hostname_syntax(parts.hostname, field_name=field_name)

    host = _normalize_host(parts.hostname)
    port = parts.port or _DEFAULT_PORTS["https"]
    _reject_disallowed_host(host, field_name=field_name)
    _reject_ambiguous_path(parts.path or "/", field_name=field_name)

    normalized = f"https://{host}:{port}{parts.path or '/'}"
    if parts.query:
        normalized = f"{normalized}?{parts.query}"

    return normalized, Origin(scheme="https", host=host, port=port)


def parse_https_origin(raw, *, field_name: str) -> Origin:
    """Parse and validate `raw` as an https ORIGIN (no path beyond an
    optional empty/'/' path, no query, no fragment, no userinfo, no
    disallowed literal host) - used for
    approved_pages[*].allowed_stylesheet_origins entries, which authorize
    an origin, never a specific path (see module docstring's AUTHORITY
    MODEL section). Raises BrowserSafetyError on any problem."""

    if not isinstance(raw, str) or not raw.strip():
        raise BrowserSafetyError(f"{field_name} must be a non-empty string")
    if len(raw) > MAX_STYLESHEET_ORIGIN_LENGTH:
        raise BrowserSafetyError(
            f"{field_name} exceeds the maximum length ({MAX_STYLESHEET_ORIGIN_LENGTH})"
        )
    if "\x00" in raw:
        raise BrowserSafetyError(f"{field_name} must not contain a NUL character")

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise BrowserSafetyError(f"{field_name} is not a valid URL") from exc

    if parts.scheme.lower() != _ALLOWED_SCHEME:
        raise BrowserSafetyError(f"{field_name} must use https")
    if parts.username is not None or parts.password is not None:
        raise BrowserSafetyError(f"{field_name} must not contain userinfo")
    if not parts.hostname:
        raise BrowserSafetyError(f"{field_name} must include a hostname")
    if parts.path not in ("", "/"):
        raise BrowserSafetyError(f"{field_name} must be an origin only, with no path")
    if parts.query or parts.fragment:
        raise BrowserSafetyError(f"{field_name} must be an origin only, with no query or fragment")
    _validate_hostname_syntax(parts.hostname, field_name=field_name)

    host = _normalize_host(parts.hostname)
    port = parts.port or _DEFAULT_PORTS["https"]
    _reject_disallowed_host(host, field_name=field_name)

    return Origin(scheme="https", host=host, port=port)


def url_origin(url: str) -> Origin:
    """Parse ANY url (not necessarily config-authored - typically a live
    Request.url seen by the runtime gate) into an Origin for request-time
    comparison against an already-validated allowlist. Deliberately
    permissive about path/query (ignored, never validated) since this
    classifies a REQUEST's origin, not a config value - parsed-URL
    semantics only, never a startswith()/substring/prefix check (see
    module docstring). Raises BrowserSafetyError if the URL has no
    parseable scheme+host+known-default-port; a non-https URL parses fine
    but will simply never equal any (https-only) configured Origin."""

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise BrowserSafetyError("URL is not parseable") from exc
    if not parts.scheme or not parts.hostname:
        raise BrowserSafetyError("URL has no scheme/host")

    host = _normalize_host(parts.hostname)
    scheme = parts.scheme.lower()
    port = parts.port or _DEFAULT_PORTS.get(scheme)
    if port is None:
        raise BrowserSafetyError("URL scheme has no known default port")

    return Origin(scheme=scheme, host=host, port=port)


@dataclass(frozen=True)
class NormalizedDocumentURL:
    """The comparison unit for exact main-document authority (Milestone 44
    P1 correction). scheme + normalized host + normalized (default-filled)
    port + path (defaulting to '/') + query - fragment always excluded
    (see module docstring's AUTHORITY MODEL section). BOTH a configured
    page URL and a live Playwright Request.url are parsed into this SAME
    representation via parse_document_url() before ever being compared -
    never raw string equality. This closes a confirmed, load-bearing
    mismatch: Chromium's own reported Request.url OMITS a default port
    (443 for https) even when the original navigation target included it
    explicitly, while a plain string built by always appending the port
    (as this module's own normalized URLs do, for the string actually
    passed to page.goto()) would never equal that reported form. Parsing
    both sides into port-defaulted, structured fields the same way
    url_origin()/Origin already do for stylesheet-origin comparison
    (proven correct there from the start) makes an omitted default port
    and an explicit default port compare equal, while a genuinely
    different, non-default explicit port remains distinct."""

    scheme: str
    host: str
    port: int
    path: str
    query: str


def parse_document_url(url: str) -> NormalizedDocumentURL:
    """Parse ANY url (a config-authored, already-validated document URL,
    or a live Request.url reported by the runtime gate) into a
    NormalizedDocumentURL for exact document-identity comparison. Path and
    query are preserved as literal strings - Chromium does not normalize
    percent-encoding case or collapse repeated slashes (confirmed
    empirically) - except dot-segment/backslash rewriting, which Chromium
    DOES perform; a configured URL containing either is rejected outright
    at config-validation time by parse_https_url() (via
    _reject_ambiguous_path()) rather than emulated here, so a live
    Request.url for an approved page is never expected to differ from the
    configured form in that respect. Raises BrowserSafetyError if the URL
    has no parseable scheme+host+known-or-defaultable port."""

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise BrowserSafetyError("URL is not parseable") from exc
    if not parts.scheme or not parts.hostname:
        raise BrowserSafetyError("URL has no scheme/host")

    scheme = parts.scheme.lower()
    host = _normalize_host(parts.hostname)
    port = parts.port or _DEFAULT_PORTS.get(scheme)
    if port is None:
        raise BrowserSafetyError("URL scheme has no known default port")

    return NormalizedDocumentURL(
        scheme=scheme, host=host, port=port, path=parts.path or "/", query=parts.query
    )


def fresh_dns_safety_check(
    host: str,
    *,
    field_name: str,
    timeout_seconds: float = DNS_RESOLUTION_TIMEOUT_SECONDS,
    worker_script: Path = _DNS_WORKER_SCRIPT,
) -> None:
    """Freshly resolve `host` and reject if ANY resolved address is
    loopback/private/link-local/reserved/multicast/unspecified. A no-op
    for a literal IP host (already validated at parse time by
    _reject_disallowed_host()).

    GENUINELY BOUNDED, NOT MERELY CLAIMED: resolution runs in a real,
    separate OS subprocess (kernel/tools/dns_resolver_worker.py), launched
    and bounded via kernel/tools/process_control.py's
    run_capturing_stdout() - the same shell=False, list-form-argv,
    full-process-tree-kill-and-reap primitive already used by
    open_application/run_registered_script/repo_health/repository_backup.
    This module previously bounded resolution with a
    concurrent.futures.ThreadPoolExecutor + future.result(timeout=...),
    which was PROVEN, empirically, not to bound wall-clock time at all: a
    hung resolver call left the ThreadPoolExecutor's context-manager exit
    (shutdown(wait=True)) blocking for the resolver's own full duration
    regardless of the configured timeout, since a Python thread blocked
    inside a C-level socket call cannot be cancelled from another thread.
    A real OS process CAN be killed - see process_control._terminate_and_reap()
    for the full termination/reap guarantee this now rests on: this
    function does not return until the worker process (and any
    descendant) is confirmed no longer running, whether it finished
    normally or was killed for exceeding `timeout_seconds`.

    `worker_script`/`timeout_seconds` are overridable ONLY for tests (see
    tests/kernel/tools/test_browser_safety.py) - production code never
    supplies either, always using the real worker script and the real,
    fixed DNS_RESOLUTION_TIMEOUT_SECONDS.

    DEFENSE IN DEPTH, NOT DNS PINNING: this does not pin Chromium's own,
    later, independent resolution of the same hostname to the address
    checked here - a sufficiently well-timed DNS-rebinding attack against
    a plain hostname is not fully closed by this check alone. HTTPS-only
    navigation (TLS hostname verification) is an additional, independent
    boundary, but this module makes no claim that the combination
    eliminates DNS rebinding entirely - see docs/architecture.md's M44 P1
    section for the honestly-documented residual risk.

    Raises BrowserSafetyError if resolution fails, times out, resolves to
    no addresses, or resolves to any disallowed address. Never includes
    the raw resolver exception, the hostname, or any resolved address in
    the raised message."""

    if _is_ip_literal(host):
        return

    result = process_control.run_capturing_stdout(
        [sys.executable, str(worker_script), host],
        cwd=str(_PROJECT_ROOT),
        timeout_seconds=timeout_seconds,
        max_output_bytes=_DNS_WORKER_MAX_OUTPUT_BYTES,
    )

    if result.timed_out or not result.success or not result.stdout:
        raise BrowserSafetyError(f"{field_name} could not be safely resolved")

    try:
        decoded = result.stdout.decode("ascii")
    except UnicodeDecodeError:
        raise BrowserSafetyError(f"{field_name} could not be safely resolved")

    addresses = [line.strip() for line in decoded.splitlines() if line.strip()]
    if not addresses:
        raise BrowserSafetyError(f"{field_name} could not be safely resolved")

    for ip_str in addresses:
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        _reject_disallowed_address(addr, field_name=field_name)


@dataclass(frozen=True)
class PageAuthority:
    """Everything ONE browser_read_page action is authorized to do at the
    network level - the runtime input to the context-wide request gate in
    kernel/tools/handlers/browser_read_page.py. Built fresh for every
    action (see that handler's own docstring for the fresh-validation,
    fresh-isolated-context-per-action session model) - never cached or
    reused across actions.

    `document_url` is the one exact, normalized URL STRING (path/query
    included, fragment excluded) actually passed to page.goto() - kept as
    a string only because that is what Playwright's navigation API needs.
    Authority comparison never uses this string directly (see
    is_request_permitted() and NormalizedDocumentURL's own docstring for
    why raw string equality is wrong here) - it is parsed into a
    NormalizedDocumentURL via parse_document_url() at comparison time, the
    exact same way a live Request.url is. `stylesheet_origins` authorizes
    GET stylesheet requests on those origins ONLY - it never authorizes a
    document request, on that origin or any other, even though
    document_origin's own origin is not automatically added to it (see
    design section 13: same-origin stylesheets are not implicitly
    authorized merely because the page itself is approved)."""

    document_url: str
    document_origin: Origin
    stylesheet_origins: tuple[Origin, ...]


def is_request_permitted(
    authority: PageAuthority,
    *,
    url: str,
    method: str,
    resource_type: str,
    is_main_frame: bool,
    document_consumed: bool,
) -> bool:
    """The ENTIRE request allowlist for one browser_read_page action -
    shared by the real runtime gate (kernel/tools/handlers/
    browser_read_page.py) and by network-policy unit tests that never
    launch a browser. Performs no network I/O and never calls
    route.fetch() itself - it only decides whether the caller may attempt
    that fetch at all.

    Returns True only for:
      - the single exact configured main-frame document URL, compared via
        NormalizedDocumentURL (parse_document_url() applied identically to
        both sides - never raw string equality; see that type's own
        docstring for the confirmed default-port mismatch this closes),
        never a prefix or origin-only match, on the main frame only, and
        only once per action (`document_consumed` distinguishes the one
        permitted initial document request from any later document
        request - meta refresh, an unexpected second navigation, or
        anything else - which must always be denied: P1 has no HTTP
        redirect following and no secondary main-frame navigation of any
        kind, so the configured URL is the entire page-navigation
        authority);
      - a GET stylesheet request whose normalized origin (see url_origin())
        is present in authority.stylesheet_origins - a stylesheet-approved
        origin NEVER authorizes a document request on that same origin.

    Every other combination - wrong method, wrong resource_type, a
    subframe document, an unmatched origin, a second document request -
    returns False."""

    if method != "GET":
        return False

    if resource_type == "document":
        if not is_main_frame:
            return False
        if document_consumed:
            return False
        try:
            candidate = parse_document_url(url)
            configured = parse_document_url(authority.document_url)
        except BrowserSafetyError:
            return False
        return candidate == configured

    if resource_type == "stylesheet":
        try:
            origin = url_origin(url)
        except BrowserSafetyError:
            return False
        return origin in authority.stylesheet_origins

    return False


def content_type_matches(header_value, allowed: frozenset) -> bool:
    """True if `header_value` (a raw Content-Type header string, or None)
    names a MIME type present in `allowed` - compared on the MIME type
    only (the part before any ';charset=...'/parameter suffix), never a
    substring check on the full header."""

    if not header_value or not isinstance(header_value, str):
        return False
    mime = header_value.split(";", 1)[0].strip().lower()
    return mime in allowed
