"""
repo_health action handler: a read-only git status/sync report for one
registered, symbolic repository key (Milestone 34).

The caller never supplies a path - only a key already validated against
kernel/config/tools.yaml's repo_health.approved_repositories. Every git
call is shell=False with a fixed argv list and the canonicalized,
validated repository root as cwd; nothing here is ever built from a
message sender's text beyond the symbolic key used to look up the
RepoSpec. No fetch, pull, push, checkout, reset, merge, or commit is ever
issued - only read-only inspection commands (rev-parse, status --porcelain,
log, ls-remote).

Every git subprocess - local or remote - runs with a fixed safety prefix
(`--no-optional-locks`, `--no-pager`, `--no-replace-objects`,
`-c core.fsmonitor=false`) and an explicitly sanitized environment (see
_sanitized_git_env()), both supplied on the command line / in the
process environment so a value in the target repository's own
.git/config - or an inherited environment variable - can never remove or
override them: the flags above are global git options, not config keys,
and a command-line `-c` always wins over repo config in git's own
resolution order. This stops `git status` from writing the optional
refresh/untracked-cache index data it would otherwise opportunistically
write, stops a repository-configured `core.fsmonitor` hook command from
ever executing, and stops a repository-configured replace ref
(`refs/replace/...`) from ever substituting a different object for the
one actually reported. _sanitized_git_env() also sets
`GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=os.devnull` for every
call - no system- or machine-global git config is ever consulted,
local calls still read the approved repository's own local config where
needed (e.g. to read remote.origin.url) - and strips every inherited
variable that could redirect repository/ref/index/object discovery
(`GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`,
`GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`,
`GIT_NAMESPACE`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`,
`GIT_CEILING_DIRECTORIES`, `GIT_REPLACE_REF_BASE`), disable transport
security, or leak transport detail (`GIT_SSL_NO_VERIFY`,
`GIT_CURL_VERBOSE`, every `GIT_TRACE*` variable, `GIT_REDIRECT_STDIN`,
`GIT_REDIRECT_STDOUT`, `GIT_REDIRECT_STDERR`) - matched
case-insensitively, since environment variable names are case-insensitive
on Windows.

The one network call (`git ls-remote`) never targets the symbolic remote
name "origin" - HTTPS-only protocol restrictions alone are insufficient,
since repository or inherited git configuration could still point
"origin" at an arbitrary HTTPS host, embed credentials in the URL,
rewrite URLs (`url.*.insteadOf`), inject extra HTTP headers, or redirect
through a configured proxy. Instead: the repository's local
`remote.origin.url` is read (`git config --local --no-includes --get-all
-z remote.origin.url` - NUL-delimited so every configured value is
positively enumerated rather than relying on `--get`'s inconsistent
behavior on a multi-valued key, and --no-includes so an include/includeIf
directive cannot smuggle in an extra value; a local, read-only config
lookup - no network access), required to be exactly one non-empty,
properly NUL-terminated value, and strictly parsed and validated
(_parse_github_origin() - https scheme only, hostname `github.com` only,
no username/password/port/query/fragment, no control characters or
malformed percent-encoding, path shaped exactly like `/<owner>/<repo>` or
`/<owner>/<repo>.git`), and normalized internally to
`https://github.com/<owner>/<repository>.git`. Any origin that fails this
- absent, malformed, multi-valued, or simply not a bare github.com HTTPS
repository URL - reports "GitHub unreachable" without ever touching the
network, and no raw or normalized value is ever put in the reply or the
audit log. Only this normalized, fixed argv element is ever passed to
`ls-remote` - never the sender, never "origin", never anything read
as-is from git config.

That call additionally runs from a neutral, pre-existing, verified-non-
worktree directory (never the approved repository, never a newly created
directory) with its own controlled `GIT_CEILING_DIRECTORIES` pinned to
that same neutral directory (on top of the system/global isolation every
call already gets) - so it is fully isolated from repository, global,
*and* system git configuration, and nothing (a rewritten URL, an extra
header, a proxy, a credential helper) configured anywhere on this machine
can reach it. It also pins `-c protocol.allow=never -c
protocol.https.allow=always`, `-c credential.helper= -c core.askPass= -c
http.extraHeader= -c http.proxy=` (empty values, which git treats as
"use none of this"), `-c http.sslVerify=true` (so nothing inherited can
disable TLS certificate verification for this call), strips any
inherited `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` (any case) from its
environment, and sets `GIT_TERMINAL_PROMPT=0` / `GCM_INTERACTIVE=Never`
- so nothing about this call can select a credential helper, prompt
interactively, add an authorization header, route through a proxy, skip
certificate verification, or hang waiting for any of those.

Every value that reaches the reply is either a small fixed string or has
been validated/sanitized first: branch names and commit hashes must pass
strict validation or the whole report is refused; the commit subject is
reduced to one sanitized, length-capped line; `git status` output is only
ever used to decide clean vs. dirty, never echoed. No path, remote URL,
credential, prompt, or raw stderr/stdout is ever returned or logged - see
kernel/tools/audit.py.
"""

import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from kernel.tools.config import is_valid_git_branch_name
from kernel.tools.git_safety import GIT_SAFE_PREFIX as _GIT_SAFE_PREFIX
from kernel.tools.git_safety import sanitized_git_env as _sanitized_git_env
from kernel.tools.process_control import run_capturing_stdout
from kernel.tools.types import ActionRequest, ActionResult

LOCAL_GIT_TIMEOUT_SECONDS = 5.0
REMOTE_GIT_TIMEOUT_SECONDS = 8.0
MAX_CAPTURED_BYTES = 4096
MAX_COMMIT_SUBJECT_LENGTH = 120

_NOT_REGISTERED = ActionResult(False, "That repository is not registered.", "rejected")
_NOT_AVAILABLE = ActionResult(False, "That repository is not available.", "failed")

_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")

_DETACHED_HEAD_MARKER = "HEAD"

# _GIT_SAFE_PREFIX is imported from kernel/tools/git_safety.py (extracted
# in Milestone 35, reused by repository_backup.py) - see that module's
# docstring for the rationale. Bound to this private name above for
# backward compatibility with this module's own tests.

# Only the network call (ls-remote) needs a transport at all, so only it
# gets these: an HTTPS-only protocol allowlist (deny every protocol by
# default, then explicitly re-allow only https - no file, ssh, git://,
# ext, or custom remote helper is ever permitted, in production or in
# tests), disabling any credential helper / askpass program the
# repository, global, or system git config might otherwise select,
# clearing any configured extra HTTP header or proxy, and forcing TLS
# certificate verification on regardless of inherited environment or
# config.
_GIT_REMOTE_SAFETY_ARGS = [
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "credential.helper=",
    "-c", "core.askPass=",
    "-c", "http.extraHeader=",
    "-c", "http.proxy=",
    "-c", "http.sslVerify=true",
]

_PROXY_ENV_VARS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}

# Conservative allowlists for a GitHub owner/repo path component - not an
# attempt to replicate GitHub's exact naming rules, just a tight bound on
# what's accepted before the value is ever placed in an argv element.
_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
_MALFORMED_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_CONTROL_OR_WHITESPACE_RE = re.compile(r"[\x00-\x20\x7f]")

# _sanitized_git_env is imported from kernel/tools/git_safety.py
# (extracted in Milestone 35) - see that module's docstring for the exact
# blocklist and rationale. Bound to this private name above for backward
# compatibility with this module's own tests.


def _run_local_bytes(args: list[str], cwd: Path) -> bytes | None:
    """Run a fast, local, read-only git command (args is the subcommand
    and its own flags - the safety prefix is added here, always).
    Returns raw stdout bytes on a clean zero-exit, or None on any
    failure, timeout, or nonzero exit - callers must treat None as "this
    repository is not usable"."""

    argv = [*_GIT_SAFE_PREFIX, *args]
    result = run_capturing_stdout(
        argv, str(cwd), LOCAL_GIT_TIMEOUT_SECONDS, env=_sanitized_git_env(), max_output_bytes=MAX_CAPTURED_BYTES
    )
    if result.timed_out or not result.success:
        return None
    return result.stdout or b""


def _run_local(args: list[str], cwd: Path) -> str | None:
    """Like _run_local_bytes, but decodes stdout as text - for every
    local command except the byte-exact, NUL-delimited one
    (_read_origin_url)."""

    raw = _run_local_bytes(args, cwd)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def _read_origin_url(repo_root: Path) -> str | None:
    """Reads remote.origin.url via `git config --local --no-includes
    --get-all -z`: NUL-delimited so every configured value can be
    positively enumerated (unlike `--get`, whose behavior on a
    multi-valued key is not something to rely on), and --no-includes so
    an `include`/`includeIf` directive elsewhere in local config cannot
    smuggle in an additional value. Returns the sole value only if
    exactly one non-empty value is present and the output is properly
    NUL-terminated; None for zero values, two or more values, or any
    malformed/truncated output. Never logs or returns the raw value(s)."""

    raw = _run_local_bytes(
        ["config", "--local", "--no-includes", "--get-all", "-z", "remote.origin.url"], repo_root
    )
    if not raw:
        return None
    if not raw.endswith(b"\x00"):
        # -z NUL-terminates every value, including the last - anything
        # else is truncated or malformed output, never trusted.
        return None

    parts = raw.split(b"\x00")
    if parts[-1] != b"":
        return None
    values = parts[:-1]
    non_empty_values = [v for v in values if v != b""]
    if len(non_empty_values) != 1:
        return None

    try:
        return non_empty_values[0].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _parse_github_origin(raw: str) -> str | None:
    """Strictly validate a raw `remote.origin.url` value and, only if it
    is exactly a bare, credential-free github.com HTTPS repository URL,
    return the normalized `https://github.com/<owner>/<repo>.git` form.
    Returns None for anything else - including a non-https scheme, a
    non-github.com host, an embedded username/password, an explicit port,
    a query string or fragment, control characters or whitespace,
    malformed percent-encoding, or a path that isn't exactly
    `/<owner>/<repo>` (optionally suffixed `.git`). The raw value is never
    logged or returned by this function or any of its callers."""

    if not isinstance(raw, str) or not raw:
        return None
    if _CONTROL_OR_WHITESPACE_RE.search(raw):
        return None
    if _MALFORMED_PERCENT_RE.search(raw):
        return None

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None

    if parsed.scheme.lower() != "https":
        return None

    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        return None

    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment:
        return None

    if parsed.hostname != "github.com":
        return None

    path = parsed.path
    if not path.startswith("/"):
        return None
    segments = path[1:].split("/")
    if len(segments) != 2:
        return None

    owner, repo = segments
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]

    if not _GITHUB_OWNER_RE.match(owner):
        return None
    if not repo or repo.startswith(".") or repo in (".", ".."):
        return None
    if not _GITHUB_REPO_RE.match(repo):
        return None

    return f"https://github.com/{owner}/{repo}.git"


def _remove_proxy_env_vars(env: dict) -> None:
    for key in list(env):
        if key.upper() in _PROXY_ENV_VARS:
            del env[key]


def _neutral_cwd(repo_root: Path) -> Path | None:
    """A pre-existing, never-created-here directory to run the network
    git call from: never the approved repository itself, and verified -
    not merely assumed - to not be inside any git worktree at all, so the
    call can be fully isolated from repository config via
    GIT_CEILING_DIRECTORIES. Returns None if neither condition can be
    established, which callers must treat as "cannot safely check the
    remote right now"."""

    candidate = Path(tempfile.gettempdir()).resolve()
    try:
        candidate.relative_to(repo_root)
        return None
    except ValueError:
        pass

    # _run_local returning anything at all here means the probe command
    # succeeded - i.e. candidate genuinely is inside some git worktree -
    # which is the unsafe case.
    probe = _run_local(["rev-parse", "--is-inside-work-tree"], candidate)
    if probe is not None:
        return None
    return candidate


def _remote_git_env(neutral_dir: Path) -> dict:
    """_sanitized_git_env() already applies GIT_CONFIG_NOSYSTEM=1 and
    GIT_CONFIG_GLOBAL=os.devnull to every call; this layers on the
    remote-only controls: the call's own GIT_CEILING_DIRECTORIES
    (stripped from the inherited environment by _sanitized_git_env(), so
    only this controlled value is ever in effect), proxy variable
    removal, and disabling any interactive credential prompt."""

    env = _sanitized_git_env()
    env["GIT_CEILING_DIRECTORIES"] = str(neutral_dir)
    _remove_proxy_env_vars(env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    return env


def _sanitize_commit_subject(raw: str) -> str:
    first_line = raw.split("\n", 1)[0].split("\r", 1)[0]
    printable = "".join(ch if ch.isprintable() else " " for ch in first_line)
    normalized = " ".join(printable.split())
    if not normalized:
        return "(no subject)"
    return normalized[:MAX_COMMIT_SUBJECT_LENGTH]


def _check_remote(repo_root: Path, main_branch: str, local_main_sha: str) -> str:
    """Read-only reachability + sync check against the repository's
    GitHub origin. Never fetches, never mutates local state - `git
    ls-remote` only queries the remote's advertised refs, and it is run
    against a validated, normalized URL from a neutral, isolated
    directory - never against the symbolic remote name "origin" inside
    the repository itself. Any failure - an origin that isn't a bare
    github.com HTTPS URL, no safe neutral directory available, a network
    timeout, or a nonzero exit - reports as "GitHub unreachable" and never
    affects the already-gathered local report or the overall executed
    outcome."""

    origin_url = _read_origin_url(repo_root)
    if origin_url is None:
        return "GitHub unreachable"

    normalized_url = _parse_github_origin(origin_url)
    if normalized_url is None:
        return "GitHub unreachable"

    neutral_dir = _neutral_cwd(repo_root)
    if neutral_dir is None:
        return "GitHub unreachable"

    argv = [
        *_GIT_SAFE_PREFIX,
        *_GIT_REMOTE_SAFETY_ARGS,
        "ls-remote",
        normalized_url,
        f"refs/heads/{main_branch}",
    ]
    result = run_capturing_stdout(
        argv,
        str(neutral_dir),
        REMOTE_GIT_TIMEOUT_SECONDS,
        env=_remote_git_env(neutral_dir),
        max_output_bytes=MAX_CAPTURED_BYTES,
    )
    if result.timed_out or not result.success:
        return "GitHub unreachable"

    stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    if not stdout:
        return "remote branch unavailable"

    first_line = stdout.splitlines()[0]
    remote_sha = first_line.split()[0].strip() if first_line.split() else ""
    if not _SHA_RE.match(remote_sha):
        return "remote branch unavailable"

    return "up to date" if remote_sha == local_main_sha else "differs"


def run(request: ActionRequest, tools_config) -> ActionResult:
    key = request.resource_key
    spec = tools_config.approved_repositories.get(key)
    if spec is None:
        return _NOT_REGISTERED

    # Re-validated here, redundantly with kernel/tools/config.py's own
    # load-time check - defense in depth against a ToolsConfig ever built
    # without going through load_tools_config(). Rejected before any git
    # process runs, since spec.main_branch is substituted into argv below.
    if not is_valid_git_branch_name(spec.main_branch):
        return _NOT_AVAILABLE

    try:
        canonical_root = Path(spec.path).resolve(strict=True)
    except OSError:
        return _NOT_AVAILABLE
    if not canonical_root.is_dir():
        return _NOT_AVAILABLE

    is_worktree = _run_local(["rev-parse", "--is-inside-work-tree"], canonical_root)
    if is_worktree is None or is_worktree.strip() != "true":
        return _NOT_AVAILABLE

    # The configured path must be the repository root itself, not merely a
    # subdirectory of a larger worktree - reject if they differ. Neither
    # path is ever included in the reply or the audit trail.
    toplevel_raw = _run_local(["rev-parse", "--show-toplevel"], canonical_root)
    if toplevel_raw is None or not toplevel_raw.strip():
        return _NOT_AVAILABLE
    try:
        toplevel_canonical = Path(toplevel_raw.strip()).resolve(strict=True)
    except OSError:
        return _NOT_AVAILABLE
    if toplevel_canonical != canonical_root:
        return _NOT_AVAILABLE

    raw_branch = _run_local(["rev-parse", "--abbrev-ref", "HEAD"], canonical_root)
    if raw_branch is None or not raw_branch.strip():
        return _NOT_AVAILABLE
    raw_branch = raw_branch.strip()
    branch_display = "detached" if raw_branch == _DETACHED_HEAD_MARKER else raw_branch
    if branch_display != "detached" and not is_valid_git_branch_name(branch_display):
        return _NOT_AVAILABLE

    status_raw = _run_local(["status", "--porcelain"], canonical_root)
    if status_raw is None:
        return _NOT_AVAILABLE
    clean = status_raw.strip() == ""

    log_raw = _run_local(["log", "-1", "--format=%h%x09%s"], canonical_root)
    if log_raw is None or not log_raw.strip():
        commit_line = "Latest commit: unavailable"
    else:
        first_line = log_raw.splitlines()[0]
        parts = first_line.split("\t", 1)
        short_hash = parts[0].strip()
        subject_raw = parts[1] if len(parts) > 1 else ""
        if not _SHA_RE.match(short_hash):
            return _NOT_AVAILABLE
        commit_line = f"Latest commit: {short_hash} {_sanitize_commit_subject(subject_raw)}"

    head_sha_raw = _run_local(["rev-parse", "HEAD"], canonical_root)
    if head_sha_raw is None:
        return _NOT_AVAILABLE
    head_sha = head_sha_raw.strip()
    if not _SHA_RE.match(head_sha):
        return _NOT_AVAILABLE

    main_sha_raw = _run_local(["rev-parse", f"refs/heads/{spec.main_branch}"], canonical_root)
    main_sha = None
    if main_sha_raw is not None and _SHA_RE.match(main_sha_raw.strip()):
        main_sha = main_sha_raw.strip()
        head_matches_main = "yes" if head_sha == main_sha else "no"
    else:
        head_matches_main = "local branch not found"

    lines = [
        f"Repository: {key}",
        f"Branch: {branch_display}",
        f"Working tree: {'clean' if clean else 'dirty'}",
        commit_line,
        f"HEAD matches local {spec.main_branch}: {head_matches_main}",
    ]

    if main_sha is None:
        lines.append("Local main vs GitHub main: local branch not found")
    else:
        remote_status = _check_remote(canonical_root, spec.main_branch, main_sha)
        lines.append(f"Local main vs GitHub main: {remote_status}")

    return ActionResult(True, "\n".join(lines), "executed")
