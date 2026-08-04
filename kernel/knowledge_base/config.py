"""
Local machine configuration for kernel/knowledge_base/ (Milestone 36).

Loads kernel/config/knowledge_base.yaml - a gitignored, machine-local file
holding the approved knowledge sources (symbolic key -> absolute directory
or file path) for this one machine. kernel/config/knowledge_base.example.yaml
is the committed, safe placeholder a real knowledge_base.yaml is copied
from; no real path is ever tracked in git.

Fails closed, mirroring kernel/tools/config.py's established rules:

- A *missing* file is not an error - it yields an entirely empty
  KnowledgeBaseConfig, so every source-scoped operation (ingest, search
  filtering) denies everything by having nothing approved.
- A *present but invalid* file (malformed YAML, a duplicate key - even one
  that only collides case-insensitively, a relative path, a non-boolean
  `recursive`, a missing required field, or any field this schema doesn't
  recognize) always raises KnowledgeConfigError. Callers must treat that
  as "deny" and never silently ignore it.

This module intentionally does not import kernel/tools/config.py's
duplicate-key YAML loader - per Milestone 36 scope, a small private
loader is implemented here instead, so this module has no dependency on
kernel/tools/.

Configuration parsing here performs no filesystem traversal or ingestion;
it only validates shape. Whether a configured path actually exists, is
the right type, and isn't a symlink/junction/reparse point is checked
later, in traversal.py, when a source is actually opened.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from kernel.knowledge_base.types import KnowledgeConfigError

# kernel/knowledge_base/config.py -> kernel/knowledge_base -> kernel -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KNOWLEDGE_BASE_YAML_PATH = (
    _PROJECT_ROOT / "kernel" / "config" / "knowledge_base.yaml"
)

_TOP_LEVEL_KEYS = {"approved_sources"}
_SOURCE_FIELDS = {"path", "recursive"}
_REQUIRED_SOURCE_FIELDS = {"path", "recursive"}

# Conservative ASCII lowercase/digit/underscore/hyphen allowlist, at most
# 64 characters, first character restricted to letter-or-digit - the same
# shape kernel/tools/config.py uses for its own symbolic keys. Duplicated
# rather than imported: this module deliberately has no dependency on
# kernel/tools/ (see module docstring).
_SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def is_valid_source_key(value) -> bool:
    """True only for a conservative, safe symbolic source key."""

    return isinstance(value, str) and bool(_SOURCE_KEY_RE.match(value))


@dataclass(frozen=True)
class SourceSpec:
    path: str
    recursive: bool


@dataclass(frozen=True)
class KnowledgeBaseConfig:
    approved_sources: dict[str, SourceSpec]


EMPTY_KNOWLEDGE_BASE_CONFIG = KnowledgeBaseConfig(approved_sources={})


class _DuplicateKeyLoader(yaml.SafeLoader):
    """A SafeLoader that raises on a duplicate mapping key at any level,
    instead of PyYAML's default of silently keeping the last one. A
    small, private copy of the same technique used by
    kernel/tools/config.py's own loader - not shared, by design, in this
    milestone."""


def _construct_mapping_no_duplicates(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise KnowledgeConfigError(
                f"duplicate key in knowledge base configuration: {key!r}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates
)


def _reject_unknown_fields(present: set, allowed: set, context: str) -> None:
    unknown = present - allowed
    if unknown:
        raise KnowledgeConfigError(f"unsupported field(s) in {context}: {sorted(unknown)}")


def _casefolded_unique_key(raw_key, seen: dict, context: str) -> str:
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise KnowledgeConfigError(f"{context} keys must be non-empty strings")
    normalized = raw_key.casefold()
    if normalized in seen:
        raise KnowledgeConfigError(
            f"duplicate key (case-insensitive) in {context}: {raw_key!r}"
        )
    seen[normalized] = raw_key
    return normalized


def _parse_source_spec(raw_key, spec, context: str) -> SourceSpec:
    if not isinstance(spec, dict):
        raise KnowledgeConfigError(f"{context} must be a mapping")

    _reject_unknown_fields(set(spec), _SOURCE_FIELDS, context)
    missing = _REQUIRED_SOURCE_FIELDS - set(spec)
    if missing:
        raise KnowledgeConfigError(f"{context} missing required field(s): {sorted(missing)}")

    path = spec["path"]
    if not isinstance(path, str) or not path.strip():
        raise KnowledgeConfigError(f"{context}.path must be a non-empty string")
    if not Path(path).is_absolute():
        raise KnowledgeConfigError(f"{context}.path must be an absolute path, got: {path!r}")

    recursive = spec["recursive"]
    if not isinstance(recursive, bool):
        raise KnowledgeConfigError(f"{context}.recursive must be a boolean")

    return SourceSpec(path=path, recursive=recursive)


def _parse_approved_sources(section) -> dict[str, SourceSpec]:
    if not isinstance(section, dict):
        raise KnowledgeConfigError("approved_sources must be a mapping")

    result: dict[str, SourceSpec] = {}
    seen: dict = {}
    for raw_key, spec in section.items():
        key = _casefolded_unique_key(raw_key, seen, "approved_sources")
        if not is_valid_source_key(key):
            raise KnowledgeConfigError(
                f"approved_sources key is not a valid symbolic key: {raw_key!r}"
            )
        context = f"approved_sources[{raw_key!r}]"
        result[key] = _parse_source_spec(raw_key, spec, context)
    return result


def load_knowledge_base_config(path: Path | None = None) -> KnowledgeBaseConfig:
    """Load and validate local machine knowledge-base configuration. See
    module docstring for the fail-closed rules."""

    resolved_path = path or DEFAULT_KNOWLEDGE_BASE_YAML_PATH
    if not resolved_path.exists():
        return EMPTY_KNOWLEDGE_BASE_CONFIG

    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KnowledgeConfigError(
            f"could not read knowledge base configuration: {resolved_path}"
        ) from exc

    try:
        data = yaml.load(raw_text, Loader=_DuplicateKeyLoader)
    except yaml.YAMLError as exc:
        raise KnowledgeConfigError("knowledge base configuration is not valid YAML") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise KnowledgeConfigError(
            "knowledge base configuration must be a mapping at the top level"
        )

    _reject_unknown_fields(set(data), _TOP_LEVEL_KEYS, "knowledge base configuration")

    approved_sources = (
        _parse_approved_sources(data["approved_sources"])
        if "approved_sources" in data
        else {}
    )

    return KnowledgeBaseConfig(approved_sources=approved_sources)
