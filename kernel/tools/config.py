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
}
_LIST_FILES_KEYS = {"approved_directories"}
_OPEN_APPLICATION_KEYS = {"approved_applications"}
_RUN_SCRIPT_KEYS = {"approved_scripts"}
_REPO_HEALTH_KEYS = {"approved_repositories"}
_APPLICATION_FIELDS = {"executable", "cwd"}
_SCRIPT_REQUIRED_FIELDS = {"interpreter", "script_path", "cwd"}
_SCRIPT_ALL_FIELDS = _SCRIPT_REQUIRED_FIELDS | {"timeout_seconds"}
_REPO_REQUIRED_FIELDS = {"path"}
_REPO_ALL_FIELDS = _REPO_REQUIRED_FIELDS | {"main_branch"}

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
class ToolsConfig:
    approved_directories: dict
    approved_applications: dict
    approved_scripts: dict
    approved_repositories: dict = field(default_factory=dict)


EMPTY_TOOLS_CONFIG = ToolsConfig(
    approved_directories={},
    approved_applications={},
    approved_scripts={},
    approved_repositories={},
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


def _require_git_ref_name(value, field_name: str) -> str:
    if not is_valid_git_branch_name(value):
        raise ToolsConfigError(f"{field_name} is not a valid git branch name: {value!r}")
    return value


def _reject_unknown_fields(present: set, allowed: set, context: str) -> None:
    unknown = present - allowed
    if unknown:
        raise ToolsConfigError(f"unsupported field(s) in {context}: {sorted(unknown)}")


def _casefolded_unique_key(raw_key, seen: dict, context: str) -> str:
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise ToolsConfigError(f"{context} keys must be non-empty strings")
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

    return ToolsConfig(
        approved_directories=(
            _parse_list_files(data["list_files"]) if "list_files" in data else {}
        ),
        approved_applications=(
            _parse_open_application(data["open_application"])
            if "open_application" in data
            else {}
        ),
        approved_scripts=(
            _parse_run_registered_script(data["run_registered_script"])
            if "run_registered_script" in data
            else {}
        ),
        approved_repositories=(
            _parse_repo_health(data["repo_health"]) if "repo_health" in data else {}
        ),
    )
