"""
Local machine configuration for kernel/tools/ (Milestone 33; repo_health
added in Milestone 34).

Loads kernel/config/tools.yaml - a gitignored, machine-local file holding
the actual allowlists (approved directories, applications, scripts, and
repositories) for this one machine. kernel/config/tools.example.yaml is
the committed, safe placeholder a real tools.yaml is copied from; no real
Windows path is ever tracked in git.

Fails closed:
- A *missing* file is not an error - it yields an entirely empty
  ToolsConfig, so every resource-scoped action (list_files,
  open_application, run_registered_script, repo_health) denies everything
  by having nothing allowlisted. system_status needs no configuration at
  all and is unaffected either way.
- A *present but invalid* file (malformed YAML, a duplicate key - even one
  that only collides case-insensitively, a relative path where an
  absolute one is required, or any field this schema doesn't recognize)
  always raises ToolsConfigError. Callers must treat that as "deny" and
  never silently ignore it - see capabilities/tasks/capability.py.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from kernel.tools import browser_safety

# kernel/tools/config.py -> kernel/tools -> kernel -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOOLS_YAML_PATH = _PROJECT_ROOT / "kernel" / "config" / "tools.yaml"

DEFAULT_SCRIPT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAIN_BRANCH = "main"

_TOP_LEVEL_KEYS = {
    "list_files",
    "open_application",
    "run_registered_script",
    "repo_health",
    "repository_backup",
    "approved_files",
    "create_directory",
    "copy_file",
    "approved_pages",
}
_LIST_FILES_KEYS = {"approved_directories"}
_OPEN_APPLICATION_KEYS = {"approved_applications"}
_RUN_SCRIPT_KEYS = {"approved_scripts"}
_REPO_HEALTH_KEYS = {"approved_repositories"}
_REPOSITORY_BACKUP_KEYS = {"approved_backups"}
_CREATE_DIRECTORY_KEYS = {"approved_directory_creations"}
_COPY_FILE_KEYS = {"approved_copies"}
_APPLICATION_FIELDS = {"executable", "cwd"}
_SCRIPT_REQUIRED_FIELDS = {"interpreter", "script_path", "cwd"}
_SCRIPT_ALL_FIELDS = _SCRIPT_REQUIRED_FIELDS | {"timeout_seconds"}
_REPO_REQUIRED_FIELDS = {"path"}
_REPO_ALL_FIELDS = _REPO_REQUIRED_FIELDS | {"main_branch"}
_BACKUP_REQUIRED_FIELDS = {"destination_directory"}
_BACKUP_ALL_FIELDS = _BACKUP_REQUIRED_FIELDS
_APPROVED_FILE_REQUIRED_FIELDS = {"path"}
_APPROVED_FILE_ALL_FIELDS = _APPROVED_FILE_REQUIRED_FIELDS
_APPROVED_PAGE_REQUIRED_FIELDS = {"url"}
_APPROVED_PAGE_ALL_FIELDS = _APPROVED_PAGE_REQUIRED_FIELDS | {"allowed_stylesheet_origins"}
_DIRECTORY_CREATION_REQUIRED_FIELDS = {"parent_directory", "directory_name"}
_DIRECTORY_CREATION_ALL_FIELDS = _DIRECTORY_CREATION_REQUIRED_FIELDS
_FILE_COPY_REQUIRED_FIELDS = {"source_file", "destination_directory", "destination_name"}
_FILE_COPY_ALL_FIELDS = _FILE_COPY_REQUIRED_FIELDS

# A restrictive ASCII character allowlist alone is not enough: strings
# made entirely of allowed characters can still form ref names with
# special meaning to git (a leading "-" reads as an option, ".." is a
# revision range, a ".lock" suffix collides with git's own lockfile
# convention, "@{" starts a reflog/upstream shorthand, "@" alone means
# "current branch", etc). is_valid_git_branch_name() rejects all of that
# on top of the character allowlist. It is shared (not duplicated)
# between this module's config-time validation of an admin-supplied
# main_branch and kernel/tools/handlers/repo_health.py's own re-check of
# that same value plus the live branch name `git` reports at runtime -
# the rule set below is too easy to accidentally let drift if hand-copied
# in two places.
_GIT_REF_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# A repository_backup key is used verbatim (already casefolded) inside a
# generated filename (see kernel/tools/handlers/repository_backup.py) -
# never sender-supplied text, but still conservatively restricted so a
# configured key can never itself become a path-separator, a leading-dot
# hidden-file marker, or any other filename-special sequence. Deliberately
# a plain ASCII lowercase/digit/underscore/hyphen allowlist with a first-
# character restriction (no leading "-" or "_", which some tools would
# otherwise misparse as an option or a hidden file) and a fixed length
# bound - not an attempt to allow every technically-legal filename
# character.
_BACKUP_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def is_valid_git_branch_name(value) -> bool:
    """True only for a conservative, safe-to-display git branch name.
    Modeled on `git check-ref-format`'s real rules (restricted to
    single-or-multi-component branch names - a leading/trailing slash or
    empty component is always rejected, unlike some of git's own
    hierarchical ref exceptions)."""

    if not isinstance(value, str) or not value:
        return False
    if not _GIT_REF_NAME_RE.match(value):
        return False
    # Redundant with the character allowlist today (neither "@" nor "\"
    # nor whitespace/control characters are in it) - kept explicit as a
    # safety net against the allowlist ever being loosened later.
    if value == "@" or "@{" in value or "\\" in value:
        return False
    if value.startswith(("-", ".", "/")) or value.endswith(("/", ".")):
        return False
    if ".." in value or "//" in value:
        return False
    for component in value.split("/"):
        if component.startswith(".") or component.endswith(".lock"):
            return False
    return True


def is_valid_backup_key(value) -> bool:
    """True only for a key safe to embed directly in a generated backup
    filename (see kernel/tools/handlers/repository_backup.py) - a
    conservative ASCII lowercase/digit/underscore/hyphen allowlist, at
    most 64 characters, that can never start with "-" or "_". Shared
    (not duplicated) between this module's load-time validation of every
    repository_backup.approved_backups key and repository_backup.py's own
    redundant runtime re-check - the same defense-in-depth pattern
    is_valid_git_branch_name() documents above."""

    return isinstance(value, str) and bool(_BACKUP_KEY_RE.match(value))


# Milestone 43: a conservative length bound shared by every symbolic
# identifier this milestone introduces (an approved_files key, a composite
# operation key, a referenced resource key, or a validated child name) -
# every one of these is echoed verbatim into a successful file_metadata.py/
# read_text_file.py/create_directory.py/copy_file.py ActionResult.message,
# which must always fit comfortably within
# kernel.employee_tasks.MAX_STEP_RESULT_JSON_CHARS (4,096) once wrapped in
# a StepObservation. kernel/task_execution/service.py's ACTION-step
# finalize path does NOT catch a serialization failure the way the RESPOND
# path does - and for create_directory.py/copy_file.py the consequence is
# worse still, since the real side effect (a directory actually created, a
# file actually copied) has already happened by the time serialization
# would fail. Reproduced directly: a real tools.yaml (loaded through
# load_tools_config() itself, not merely a hand-built ToolsConfig) with a
# ~4,000-character approved_files key let file_metadata/read_text_file
# succeed while producing a message that overflows the persistence bound -
# an M43 P3 pre-push review finding, closed here by extending the same
# bound _parse_create_directory()/_parse_copy_file() already enforced to
# _parse_approved_files() as well. Matches is_valid_backup_key()'s own
# established 64-character precedent (Milestone 35) - not a new
# convention.
MAX_SYMBOLIC_NAME_LENGTH = 64

# Windows reserved device names (Milestone 43 P2) - matched against the
# name's own basename (everything before its first "." if any),
# case-insensitively, since Windows treats "CON.txt" as reserved exactly
# like bare "CON".
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
)


def is_valid_child_name(value) -> bool:
    """True only for a string safe to use as ONE path component directly
    beneath an already-approved, canonicalized directory (Milestone 43 P2:
    create_directory.directory_name, copy_file.destination_name) - never a
    multi-component path, never something that could be interpreted as
    absolute, and never silently rewritten (this function only rejects; it
    never normalizes, trims, or transforms the value it is given, so a
    configured name that passes this check is used verbatim).

    Deliberately conservative: any character that could ever participate
    in path syntax is rejected outright rather than pattern-matched for
    specific "looks like a path" shapes - "/" and "\\" together already
    rule out every relative multi-component path AND every UNC path
    (\\\\server\\share), and ":" alone rules out both a drive letter
    (C:) and an NTFS alternate-data-stream suffix (name:stream). This is
    simpler and more conservatively correct than trying to enumerate every
    absolute-path shape Windows recognizes.

    Also rejects: non-string, empty, longer than MAX_SYMBOLIC_NAME_LENGTH
    (see that constant's own docstring for why - a successful
    ActionResult.message must always fit the M42 persistence bound), "."
    and ".." (current/parent directory), a NUL character, leading or
    trailing whitespace (never silently stripped - a name that needs
    stripping is rejected, not corrected), a trailing "." or trailing " "
    (both are silently discarded by the Windows filesystem APIs, so a
    configured name ending in either would not name what it appears to
    name), and the Windows reserved device basenames (CON, PRN, AUX, NUL,
    COM1-9, LPT1-9), checked case-insensitively against the name's own
    basename so "Con.txt" is rejected exactly like "CON"."""

    if not isinstance(value, str) or not value:
        return False
    if len(value) > MAX_SYMBOLIC_NAME_LENGTH:
        return False
    if value != value.strip():
        return False
    if value in (".", ".."):
        return False
    if "\x00" in value:
        return False
    if "/" in value or "\\" in value:
        return False
    if ":" in value:
        return False
    if value.endswith(".") or value.endswith(" "):
        return False
    basename = value.split(".", 1)[0]
    if basename.upper() in _WINDOWS_RESERVED_BASENAMES:
        return False
    return True


def _require_referenced_key(value, known_keys: set, field_name: str) -> str:
    """Validate that `value` (a config-authored string naming another
    resource by its symbolic key) refers to an entry that already exists
    in `known_keys` (the casefolded key set of an already-parsed section)
    - the shared referential-integrity check behind create_directory's
    parent_directory and copy_file's source_file/destination_directory
    references, mirroring _parse_repository_backup()'s own
    known_repo_keys precedent. Also bounds `value`'s own length (see
    MAX_SYMBOLIC_NAME_LENGTH's docstring) - this is checked on the
    REFERENCE string itself, independent of how long the entry it points
    to was allowed to be, so create_directory/copy_file can never inherit
    an oversized identifier from an unrelated section. Returns the
    casefolded, normalized key - never the raw path or spec the reference
    points to, which the caller looks up separately from the
    already-parsed section itself."""

    if not isinstance(value, str) or not value.strip():
        raise ToolsConfigError(f"{field_name} must be a non-empty string")
    if len(value) > MAX_SYMBOLIC_NAME_LENGTH:
        raise ToolsConfigError(
            f"{field_name} exceeds the maximum length ({MAX_SYMBOLIC_NAME_LENGTH}): {value!r}"
        )
    normalized = value.casefold()
    if normalized not in known_keys:
        raise ToolsConfigError(f"{field_name} has no matching entry: {value!r}")
    return normalized


class ToolsConfigError(ValueError):
    """Raised for any missing-but-required, malformed, duplicated, or
    otherwise invalid tools.yaml content. Never raised merely because the
    file itself is absent - see load_tools_config()."""


@dataclass(frozen=True)
class ApplicationSpec:
    executable: str
    cwd: str


@dataclass(frozen=True)
class ScriptSpec:
    interpreter: str
    script_path: str
    cwd: str
    timeout_seconds: float


@dataclass(frozen=True)
class RepoSpec:
    path: str
    main_branch: str


@dataclass(frozen=True)
class RepoBackupSpec:
    """destination_directory only - the repository path itself is never
    duplicated here. repository_backup.py looks it up via the same key in
    ToolsConfig.approved_repositories (RepoSpec.path), which
    _parse_repository_backup() below has already confirmed exists."""

    destination_directory: str


@dataclass(frozen=True)
class FileSpec:
    """One exact, individually-approved file (Milestone 43 P1) - the
    resource kernel/tools/handlers/file_metadata.py and read_text_file.py
    resolve a resource_key against, via kernel/tools/file_safety.py.
    Deliberately NOT a directory allowlist: approving a directory
    (list_files.approved_directories) does not imply permission to read or
    inspect every file inside it - each approved_files key names exactly
    one file the human/configuration has explicitly approved. `path` is
    the exact configured absolute file path - never a directory, never a
    pattern, never combined with any caller-supplied filename."""

    path: str


@dataclass(frozen=True)
class ApprovedPageSpec:
    """One exact, individually-approved browser page (Milestone 44 P1) -
    the resource kernel/tools/handlers/browser_read_page.py resolves a
    resource_key against. `url` is the exact, config-authored, already
    HTTPS-validated and normalized canonical page URL (path/query
    preserved, fragment dropped - see kernel/tools/browser_safety.py's
    parse_https_url()) - never a directory, never a pattern, never
    combined with any caller-supplied path/query. `allowed_stylesheet_origins`
    is a small, explicit tuple of already-validated, normalized HTTPS
    origin strings (kernel/tools/browser_safety.py's parse_https_origin())
    authorizing GET stylesheet requests ONLY - it never authorizes a
    document request, on that origin or any other, even the page's own
    origin (see browser_safety.py's AUTHORITY MODEL docstring section: an
    admin who wants the page's own origin to also serve stylesheets must
    list it explicitly - it is never implied)."""

    url: str
    allowed_stylesheet_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class DirectoryCreationSpec:
    """One exact, pre-authorized directory-creation operation (Milestone
    43 P2) - the resource kernel/tools/handlers/create_directory.py
    resolves a resource_key against. `parent_directory_key` references an
    existing ToolsConfig.approved_directories entry (never a duplicated
    raw path); `directory_name` is a single, config-owned, validated path
    component (see is_valid_child_name() above) - never a caller/model-
    supplied name, never more than one path component, never combined
    with any runtime-supplied text."""

    parent_directory_key: str
    directory_name: str


@dataclass(frozen=True)
class FileCopySpec:
    """One exact, pre-authorized file-copy operation (Milestone 43 P2) -
    the resource kernel/tools/handlers/copy_file.py resolves a
    resource_key against. `source_file_key` references an existing
    ToolsConfig.approved_files entry and `destination_directory_key`
    references an existing ToolsConfig.approved_directories entry (never
    duplicated raw paths); `destination_name` is a single, config-owned,
    validated path component (see is_valid_child_name() above) - never a
    caller/model-supplied name."""

    source_file_key: str
    destination_directory_key: str
    destination_name: str


@dataclass(frozen=True)
class ToolsConfig:
    approved_directories: dict
    approved_applications: dict
    approved_scripts: dict
    approved_repositories: dict = field(default_factory=dict)
    approved_backups: dict = field(default_factory=dict)
    approved_files: dict = field(default_factory=dict)
    approved_directory_creations: dict = field(default_factory=dict)
    approved_copies: dict = field(default_factory=dict)
    approved_pages: dict = field(default_factory=dict)


EMPTY_TOOLS_CONFIG = ToolsConfig(
    approved_directories={},
    approved_applications={},
    approved_scripts={},
    approved_repositories={},
    approved_backups={},
    approved_files={},
    approved_directory_creations={},
    approved_copies={},
    approved_pages={},
)


class _DuplicateKeyLoader(yaml.SafeLoader):
    """A SafeLoader that raises on a duplicate mapping key at any level,
    instead of PyYAML's default of silently keeping the last one."""


def _construct_mapping_no_duplicates(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ToolsConfigError(f"duplicate key in tools configuration: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates
)


def _require_absolute_path(value, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolsConfigError(f"{field_name} must be a non-empty string")
    if not Path(value).is_absolute():
        raise ToolsConfigError(f"{field_name} must be an absolute path, got: {value!r}")
    return value


def _require_no_nul(value: str, field_name: str) -> str:
    if "\x00" in value:
        raise ToolsConfigError(f"{field_name} must not contain a NUL character")
    return value


def _require_git_ref_name(value, field_name: str) -> str:
    if not is_valid_git_branch_name(value):
        raise ToolsConfigError(f"{field_name} is not a valid git branch name: {value!r}")
    return value


def _reject_unknown_fields(present: set, allowed: set, context: str) -> None:
    unknown = present - allowed
    if unknown:
        raise ToolsConfigError(f"unsupported field(s) in {context}: {sorted(unknown)}")


def _casefolded_unique_key(
    raw_key, seen: dict, context: str, *, max_length: int | None = None
) -> str:
    """`max_length` is opt-in (default None, unbounded) - every existing
    call site is unaffected. Milestone 43 P2's two composite-operation
    parsers pass `max_length=MAX_SYMBOLIC_NAME_LENGTH`, since a
    composite operation's own key is echoed verbatim into a successful
    create_directory.py/copy_file.py ActionResult.message (see that
    constant's own docstring for why this must be bounded)."""

    if not isinstance(raw_key, str) or not raw_key.strip():
        raise ToolsConfigError(f"{context} keys must be non-empty strings")
    if max_length is not None and len(raw_key) > max_length:
        raise ToolsConfigError(
            f"{context} key exceeds the maximum length ({max_length}): {raw_key!r}"
        )
    normalized = raw_key.casefold()
    if normalized in seen:
        raise ToolsConfigError(
            f"duplicate key (case-insensitive) in {context}: {raw_key!r}"
        )
    seen[normalized] = raw_key
    return normalized


def _parse_list_files(section) -> dict:
    if not isinstance(section, dict):
        raise ToolsConfigError("list_files must be a mapping")
    _reject_unknown_fields(set(section), _LIST_FILES_KEYS, "list_files")

    directories = section.get("approved_directories", {})
    if not isinstance(directories, dict):
        raise ToolsConfigError("list_files.approved_directories must be a mapping")

    result = {}
    seen: dict = {}
    for raw_key, path in directories.items():
        key = _casefolded_unique_key(raw_key, seen, "list_files.approved_directories")
        result[key] = _require_absolute_path(
            path, f"list_files.approved_directories[{raw_key!r}]"
        )
    return result


def _parse_open_application(section) -> dict:
    if not isinstance(section, dict):
        raise ToolsConfigError("open_application must be a mapping")
    _reject_unknown_fields(set(section), _OPEN_APPLICATION_KEYS, "open_application")

    apps = section.get("approved_applications", {})
    if not isinstance(apps, dict):
        raise ToolsConfigError("open_application.approved_applications must be a mapping")

    result = {}
    seen: dict = {}
    for raw_key, spec in apps.items():
        key = _casefolded_unique_key(
            raw_key, seen, "open_application.approved_applications"
        )
        if not isinstance(spec, dict):
            raise ToolsConfigError(
                f"open_application.approved_applications[{raw_key!r}] must be a mapping"
            )
        context = f"open_application.approved_applications[{raw_key!r}]"
        _reject_unknown_fields(set(spec), _APPLICATION_FIELDS, context)
        missing = _APPLICATION_FIELDS - set(spec)
        if missing:
            raise ToolsConfigError(f"{context} missing required field(s): {sorted(missing)}")

        result[key] = ApplicationSpec(
            executable=_require_absolute_path(spec["executable"], f"{context}.executable"),
            cwd=_require_absolute_path(spec["cwd"], f"{context}.cwd"),
        )
    return result


def _parse_run_registered_script(section) -> dict:
    if not isinstance(section, dict):
        raise ToolsConfigError("run_registered_script must be a mapping")
    _reject_unknown_fields(set(section), _RUN_SCRIPT_KEYS, "run_registered_script")

    scripts = section.get("approved_scripts", {})
    if not isinstance(scripts, dict):
        raise ToolsConfigError("run_registered_script.approved_scripts must be a mapping")

    result = {}
    seen: dict = {}
    for raw_key, spec in scripts.items():
        key = _casefolded_unique_key(raw_key, seen, "run_registered_script.approved_scripts")
        if not isinstance(spec, dict):
            raise ToolsConfigError(
                f"run_registered_script.approved_scripts[{raw_key!r}] must be a mapping"
            )
        context = f"run_registered_script.approved_scripts[{raw_key!r}]"
        _reject_unknown_fields(set(spec), _SCRIPT_ALL_FIELDS, context)
        missing = _SCRIPT_REQUIRED_FIELDS - set(spec)
        if missing:
            raise ToolsConfigError(f"{context} missing required field(s): {sorted(missing)}")

        timeout_seconds = spec.get("timeout_seconds", DEFAULT_SCRIPT_TIMEOUT_SECONDS)
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ToolsConfigError(f"{context}.timeout_seconds must be a positive number")

        result[key] = ScriptSpec(
            interpreter=_require_absolute_path(spec["interpreter"], f"{context}.interpreter"),
            script_path=_require_absolute_path(spec["script_path"], f"{context}.script_path"),
            cwd=_require_absolute_path(spec["cwd"], f"{context}.cwd"),
            timeout_seconds=float(timeout_seconds),
        )
    return result


def _parse_repo_health(section) -> dict:
    if not isinstance(section, dict):
        raise ToolsConfigError("repo_health must be a mapping")
    _reject_unknown_fields(set(section), _REPO_HEALTH_KEYS, "repo_health")

    repos = section.get("approved_repositories", {})
    if not isinstance(repos, dict):
        raise ToolsConfigError("repo_health.approved_repositories must be a mapping")

    result = {}
    seen: dict = {}
    for raw_key, spec in repos.items():
        key = _casefolded_unique_key(raw_key, seen, "repo_health.approved_repositories")
        if not isinstance(spec, dict):
            raise ToolsConfigError(
                f"repo_health.approved_repositories[{raw_key!r}] must be a mapping"
            )
        context = f"repo_health.approved_repositories[{raw_key!r}]"
        _reject_unknown_fields(set(spec), _REPO_ALL_FIELDS, context)
        missing = _REPO_REQUIRED_FIELDS - set(spec)
        if missing:
            raise ToolsConfigError(f"{context} missing required field(s): {sorted(missing)}")

        main_branch = spec.get("main_branch", DEFAULT_MAIN_BRANCH)
        result[key] = RepoSpec(
            path=_require_absolute_path(spec["path"], f"{context}.path"),
            main_branch=_require_git_ref_name(main_branch, f"{context}.main_branch"),
        )
    return result


def _parse_repository_backup(section, known_repo_keys: set) -> dict:
    if not isinstance(section, dict):
        raise ToolsConfigError("repository_backup must be a mapping")
    _reject_unknown_fields(set(section), _REPOSITORY_BACKUP_KEYS, "repository_backup")

    backups = section.get("approved_backups", {})
    if not isinstance(backups, dict):
        raise ToolsConfigError("repository_backup.approved_backups must be a mapping")

    result = {}
    seen: dict = {}
    for raw_key, spec in backups.items():
        key = _casefolded_unique_key(raw_key, seen, "repository_backup.approved_backups")
        if not is_valid_backup_key(key):
            raise ToolsConfigError(
                f"repository_backup.approved_backups key is not filename-safe: {raw_key!r}"
            )
        if key not in known_repo_keys:
            raise ToolsConfigError(
                f"repository_backup.approved_backups[{raw_key!r}] has no matching "
                "repo_health.approved_repositories entry"
            )
        if not isinstance(spec, dict):
            raise ToolsConfigError(
                f"repository_backup.approved_backups[{raw_key!r}] must be a mapping"
            )
        context = f"repository_backup.approved_backups[{raw_key!r}]"
        _reject_unknown_fields(set(spec), _BACKUP_ALL_FIELDS, context)
        missing = _BACKUP_REQUIRED_FIELDS - set(spec)
        if missing:
            raise ToolsConfigError(f"{context} missing required field(s): {sorted(missing)}")

        result[key] = RepoBackupSpec(
            destination_directory=_require_absolute_path(
                spec["destination_directory"], f"{context}.destination_directory"
            ),
        )
    return result


def _parse_approved_files(section) -> dict:
    """approved_files (Milestone 43 P1): a flat, top-level mapping of
    symbolic key -> FileSpec, shared by two actions (file_metadata,
    read_text_file) rather than owned by a single action-named section
    like list_files/open_application/etc. - so, unlike those, this is not
    further nested under its own "approved_X" sub-key; the top-level
    "approved_files" key IS the resource mapping. Each entry names exactly
    one file - never a directory, never a glob/pattern."""

    if not isinstance(section, dict):
        raise ToolsConfigError("approved_files must be a mapping")

    result = {}
    seen: dict = {}
    for raw_key, spec in section.items():
        key = _casefolded_unique_key(
            raw_key, seen, "approved_files", max_length=MAX_SYMBOLIC_NAME_LENGTH
        )
        if not isinstance(spec, dict):
            raise ToolsConfigError(f"approved_files[{raw_key!r}] must be a mapping")
        context = f"approved_files[{raw_key!r}]"
        _reject_unknown_fields(set(spec), _APPROVED_FILE_ALL_FIELDS, context)
        missing = _APPROVED_FILE_REQUIRED_FIELDS - set(spec)
        if missing:
            raise ToolsConfigError(f"{context} missing required field(s): {sorted(missing)}")

        raw_path = _require_absolute_path(spec["path"], f"{context}.path")
        _require_no_nul(raw_path, f"{context}.path")
        result[key] = FileSpec(path=raw_path)
    return result


def _parse_approved_pages(section) -> dict:
    """approved_pages (Milestone 44 P1): a flat, top-level mapping of
    symbolic key -> ApprovedPageSpec, matching approved_files' own
    precedent - the top-level "approved_pages" key IS the resource
    mapping, not further nested under its own "approved_X" sub-key. Each
    entry names exactly one canonical HTTPS page URL plus an optional,
    small, explicit list of additional HTTPS origins authorized for
    stylesheet GET requests only - see kernel/tools/browser_safety.py's
    module docstring (AUTHORITY MODEL) for why stylesheet authority never
    implies document authority, even on the page's own origin."""

    if not isinstance(section, dict):
        raise ToolsConfigError("approved_pages must be a mapping")

    result = {}
    seen: dict = {}
    for raw_key, spec in section.items():
        key = _casefolded_unique_key(
            raw_key, seen, "approved_pages", max_length=MAX_SYMBOLIC_NAME_LENGTH
        )
        if not isinstance(spec, dict):
            raise ToolsConfigError(f"approved_pages[{raw_key!r}] must be a mapping")
        context = f"approved_pages[{raw_key!r}]"
        _reject_unknown_fields(set(spec), _APPROVED_PAGE_ALL_FIELDS, context)
        missing = _APPROVED_PAGE_REQUIRED_FIELDS - set(spec)
        if missing:
            raise ToolsConfigError(f"{context} missing required field(s): {sorted(missing)}")

        try:
            normalized_url, _ = browser_safety.parse_https_url(
                spec["url"], field_name=f"{context}.url"
            )
        except browser_safety.BrowserSafetyError as exc:
            raise ToolsConfigError(str(exc)) from exc

        raw_origins = spec.get("allowed_stylesheet_origins", [])
        if not isinstance(raw_origins, list):
            raise ToolsConfigError(f"{context}.allowed_stylesheet_origins must be a list")
        if len(raw_origins) > browser_safety.MAX_STYLESHEET_ORIGINS:
            raise ToolsConfigError(
                f"{context}.allowed_stylesheet_origins exceeds the maximum count "
                f"({browser_safety.MAX_STYLESHEET_ORIGINS})"
            )

        normalized_origins = []
        seen_origins: set = set()
        for index, raw_origin in enumerate(raw_origins):
            try:
                origin = browser_safety.parse_https_origin(
                    raw_origin,
                    field_name=f"{context}.allowed_stylesheet_origins[{index}]",
                )
            except browser_safety.BrowserSafetyError as exc:
                raise ToolsConfigError(str(exc)) from exc
            origin_str = str(origin)
            if origin_str in seen_origins:
                raise ToolsConfigError(
                    f"{context}.allowed_stylesheet_origins contains a duplicate "
                    f"origin (after normalization): {raw_origin!r}"
                )
            seen_origins.add(origin_str)
            normalized_origins.append(origin_str)

        result[key] = ApprovedPageSpec(
            url=normalized_url,
            allowed_stylesheet_origins=tuple(normalized_origins),
        )
    return result


def _parse_create_directory(section, known_directory_keys: set) -> dict:
    """create_directory (Milestone 43 P2): each entry is one complete,
    pre-authorized (parent directory key, child directory name) pair -
    parent_directory must already exist as a list_files.approved_directories
    key (referential integrity enforced here, at config-load time, the
    same structural precedent _parse_repository_backup() already
    established for repository_backup.approved_backups)."""

    if not isinstance(section, dict):
        raise ToolsConfigError("create_directory must be a mapping")
    _reject_unknown_fields(set(section), _CREATE_DIRECTORY_KEYS, "create_directory")

    creations = section.get("approved_directory_creations", {})
    if not isinstance(creations, dict):
        raise ToolsConfigError("create_directory.approved_directory_creations must be a mapping")

    result = {}
    seen: dict = {}
    for raw_key, spec in creations.items():
        key = _casefolded_unique_key(
            raw_key,
            seen,
            "create_directory.approved_directory_creations",
            max_length=MAX_SYMBOLIC_NAME_LENGTH,
        )
        if not isinstance(spec, dict):
            raise ToolsConfigError(
                f"create_directory.approved_directory_creations[{raw_key!r}] must be a mapping"
            )
        context = f"create_directory.approved_directory_creations[{raw_key!r}]"
        _reject_unknown_fields(set(spec), _DIRECTORY_CREATION_ALL_FIELDS, context)
        missing = _DIRECTORY_CREATION_REQUIRED_FIELDS - set(spec)
        if missing:
            raise ToolsConfigError(f"{context} missing required field(s): {sorted(missing)}")

        parent_directory_key = _require_referenced_key(
            spec["parent_directory"], known_directory_keys, f"{context}.parent_directory"
        )
        directory_name = spec["directory_name"]
        if not is_valid_child_name(directory_name):
            raise ToolsConfigError(f"{context}.directory_name is not a safe name: {directory_name!r}")

        result[key] = DirectoryCreationSpec(
            parent_directory_key=parent_directory_key,
            directory_name=directory_name,
        )
    return result


def _parse_copy_file(section, known_file_keys: set, known_directory_keys: set) -> dict:
    """copy_file (Milestone 43 P2): each entry is one complete,
    pre-authorized (source file key, destination directory key,
    destination name) triple - source_file must already exist as an
    approved_files key and destination_directory must already exist as a
    list_files.approved_directories key (referential integrity enforced
    here, at config-load time, mirroring _parse_create_directory() above
    and _parse_repository_backup()'s original precedent)."""

    if not isinstance(section, dict):
        raise ToolsConfigError("copy_file must be a mapping")
    _reject_unknown_fields(set(section), _COPY_FILE_KEYS, "copy_file")

    copies = section.get("approved_copies", {})
    if not isinstance(copies, dict):
        raise ToolsConfigError("copy_file.approved_copies must be a mapping")

    result = {}
    seen: dict = {}
    for raw_key, spec in copies.items():
        key = _casefolded_unique_key(
            raw_key, seen, "copy_file.approved_copies", max_length=MAX_SYMBOLIC_NAME_LENGTH
        )
        if not isinstance(spec, dict):
            raise ToolsConfigError(f"copy_file.approved_copies[{raw_key!r}] must be a mapping")
        context = f"copy_file.approved_copies[{raw_key!r}]"
        _reject_unknown_fields(set(spec), _FILE_COPY_ALL_FIELDS, context)
        missing = _FILE_COPY_REQUIRED_FIELDS - set(spec)
        if missing:
            raise ToolsConfigError(f"{context} missing required field(s): {sorted(missing)}")

        source_file_key = _require_referenced_key(
            spec["source_file"], known_file_keys, f"{context}.source_file"
        )
        destination_directory_key = _require_referenced_key(
            spec["destination_directory"], known_directory_keys, f"{context}.destination_directory"
        )
        destination_name = spec["destination_name"]
        if not is_valid_child_name(destination_name):
            raise ToolsConfigError(
                f"{context}.destination_name is not a safe name: {destination_name!r}"
            )

        result[key] = FileCopySpec(
            source_file_key=source_file_key,
            destination_directory_key=destination_directory_key,
            destination_name=destination_name,
        )
    return result


def load_tools_config(path: Path | None = None) -> ToolsConfig:
    """Load and validate local machine tool configuration. See module
    docstring for the fail-closed rules."""

    resolved_path = path or DEFAULT_TOOLS_YAML_PATH
    if not resolved_path.exists():
        return EMPTY_TOOLS_CONFIG

    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolsConfigError(f"could not read tools configuration: {resolved_path}") from exc

    try:
        data = yaml.load(raw_text, Loader=_DuplicateKeyLoader)
    except yaml.YAMLError as exc:
        raise ToolsConfigError("tools configuration is not valid YAML") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ToolsConfigError("tools configuration must be a mapping at the top level")

    _reject_unknown_fields(set(data), _TOP_LEVEL_KEYS, "tools configuration")

    # Extracted into locals (rather than inlined in the ToolsConfig(...)
    # call below, as every field before Milestone 43 P2 was) because
    # create_directory/copy_file need to reference the ALREADY-PARSED
    # approved_directories/approved_files dicts for their own referential-
    # integrity checks - the same forward-dependency approved_repositories
    # already had on repository_backup, now generalized to every field.
    approved_directories = (
        _parse_list_files(data["list_files"]) if "list_files" in data else {}
    )
    approved_applications = (
        _parse_open_application(data["open_application"]) if "open_application" in data else {}
    )
    approved_scripts = (
        _parse_run_registered_script(data["run_registered_script"])
        if "run_registered_script" in data
        else {}
    )
    approved_repositories = (
        _parse_repo_health(data["repo_health"]) if "repo_health" in data else {}
    )
    approved_backups = (
        _parse_repository_backup(data["repository_backup"], set(approved_repositories))
        if "repository_backup" in data
        else {}
    )
    approved_files = (
        _parse_approved_files(data["approved_files"]) if "approved_files" in data else {}
    )
    approved_directory_creations = (
        _parse_create_directory(data["create_directory"], set(approved_directories))
        if "create_directory" in data
        else {}
    )
    approved_copies = (
        _parse_copy_file(data["copy_file"], set(approved_files), set(approved_directories))
        if "copy_file" in data
        else {}
    )
    approved_pages = (
        _parse_approved_pages(data["approved_pages"]) if "approved_pages" in data else {}
    )

    return ToolsConfig(
        approved_directories=approved_directories,
        approved_applications=approved_applications,
        approved_scripts=approved_scripts,
        approved_repositories=approved_repositories,
        approved_backups=approved_backups,
        approved_files=approved_files,
        approved_directory_creations=approved_directory_creations,
        approved_copies=approved_copies,
        approved_pages=approved_pages,
    )
