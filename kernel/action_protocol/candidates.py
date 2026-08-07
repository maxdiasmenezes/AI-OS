"""
Stage A of the Milestone 39 action protocol: deterministic candidate
resolution.

resolve_action_candidates() inspects the raw user request against a small,
conservative, hand-written grammar - never a model call, never fuzzy or
semantic matching - and either:
  - recognizes a supported single-action intent whose required target is
    missing, and returns a deterministic RequestClarificationDecision
    (CandidateResolution.deterministic_clarification) without ever
    involving the model; or
  - returns zero or more immutable ActionCandidate objects
    (CandidateResolution.candidates) for Stage B (kernel/action_protocol/
    prompt.py + parser.py) to offer the model.

Favors false negatives over false positives throughout: anything not
cleanly recognized - an unregistered target, a compound request, a
destructive-looking fragment, hostile injection text - produces zero
candidates rather than a guess. This module performs no I/O, executes
nothing, and never calls a model.
"""

import re
from dataclasses import dataclass

from kernel.action_protocol.types import (
    MAX_CANDIDATES,
    PROTOCOL_VERSION,
    ActionCandidate,
    CandidateResolution,
    RequestClarificationDecision,
)
from kernel.tools.config import ToolsConfig
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest

# A conservative shape any *raw extracted target* must satisfy before it is
# even looked up against a registered key - independent of what
# kernel/config/tools.yaml itself permits. Rejects anything containing
# whitespace, slashes, colons, quotes-as-content, backticks, pipes,
# redirection, environment-variable syntax, or other shell/path-special
# characters outright, so a destructive command, a drive-letter path, or a
# quoted shell fragment can never reach a registry lookup at all. This is a
# narrow, purpose-built validator for text extracted from free-form
# natural-language requests - a different concern from
# kernel/tools/config.py's is_valid_git_branch_name()/is_valid_backup_key()
# (which validate *configured* keys at load time), so it is not a
# duplicate of either.
_SYMBOLIC_KEY_SHAPE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# Filler/article/noun words that must never be mistaken for a captured
# target - guards against the permissive (space-including) capture
# character class swallowing an article or one of the tool's own noun
# words instead of a real target.
_STOPWORDS = frozenset({
    "the", "a", "an", "it", "this", "that", "me", "for", "of", "in", "within",
    "files", "file", "directory", "folder",
    "script", "scripts",
    "application", "applications", "app", "apps",
    "repository", "repositories", "repo", "repos", "health",
    "system", "status", "backup", "back",
})

# Compound/multi-action guard (brief S11): a recognized action clause
# joined to further real content by any of these markers - on EITHER
# side, e.g. both "open notepad AND delete everything" and "delete
# everything AND open notepad" - suppresses the candidate entirely rather
# than narrowing a compound request into one apparently-safe sub-action.
_CONTINUATION_MARKER_RE = re.compile(
    r";" r"|\n" r"|\b(?:and|then|also|after\s+that|before\s+that)\b",
    re.IGNORECASE,
)

# Deliberately loose ("favor false negatives... over false positives" is
# about candidate creation, not about compound-detection - being trigger-
# happy here only ever makes the resolver *more* conservative, since it
# just means zero candidates): any alphabetic token of length >= 2 that
# isn't a filler word counts as "real content" on one side of a marker.
_TRIVIAL_WORDS = _STOPWORDS | {
    "please", "can", "you", "i", "my", "your", "to", "is", "was", "on", "at",
}


def _clean_target(raw: str) -> str:
    return raw.strip().strip("'\"").strip()


def _has_real_content(fragment: str) -> bool:
    for token in re.findall(r"[A-Za-z]{2,}", fragment):
        if token.casefold() not in _TRIVIAL_WORDS:
            return True
    return False


def _is_compound_request(text: str) -> bool:
    """True if `text` looks like two clauses joined by a continuation
    marker, with real (non-filler) content on both sides - regardless of
    which side any single recognized action clause happens to fall on."""

    for m in _CONTINUATION_MARKER_RE.finditer(text):
        before = text[: m.start()].strip(" .,")
        after = text[m.end():].strip(" .,")
        if _has_real_content(before) and _has_real_content(after):
            return True
    return False


def _is_stopword_or_noise(raw_target: str) -> bool:
    # A real symbolic key is a single token (see _SYMBOLIC_KEY_SHAPE_RE
    # below - no whitespace is ever valid), so a multi-word capture is
    # never a genuine target either - it means an earlier, looser pattern
    # in the same tool's list swallowed words that belong to a *different*
    # pattern's structure (e.g. "show [files in projects] folder" instead
    # of "show files in [projects] folder"). Treating it as noise lets
    # _try_patterns() fall through to the next, more specific pattern
    # instead of committing to a capture that can only ever fail shape
    # validation.
    if " " in raw_target.strip():
        return True
    return raw_target.casefold() in _STOPWORDS


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern
    has_target: bool  # False => a "bare" (missing-target) recognizer


def _try_patterns(text: str, patterns: list[_Pattern]):
    """Try every target-capturing pattern first (in order), then every
    bare pattern. Returns one of: None (nothing recognized), a
    (raw_target, match) tuple, or "missing_target". Compound-request
    detection is a whole-request concern (see _is_compound_request) and is
    checked once, up front, by resolve_action_candidates - not per match
    here."""

    target_patterns = [p for p in patterns if p.has_target]
    bare_patterns = [p for p in patterns if not p.has_target]

    for p in target_patterns:
        m = p.regex.search(text)
        if m is None:
            continue
        raw_target = _clean_target(m.group(1))
        if _is_stopword_or_noise(raw_target):
            continue
        return (raw_target, m)

    for p in bare_patterns:
        m = p.regex.search(text)
        if m is None:
            continue
        return "missing_target"

    return None


# --- Per-tool grammar (brief S10) ------------------------------------------

_SYSTEM_STATUS_PATTERNS = [
    _Pattern(re.compile(r"\bsystem\s+status\b", re.IGNORECASE), False),
    _Pattern(re.compile(r"\bhow\s+is\s+the\s+system\b", re.IGNORECASE), False),
]

_LIST_FILES_PATTERNS = [
    _Pattern(re.compile(
        r"\b(?:list|show)\b(?:\s+the)?\s+['\"]?([A-Za-z0-9_\- ]{1,64}?)['\"]?\s+"
        r"(?:files|directory|folder)\b", re.IGNORECASE,
    ), True),
    _Pattern(re.compile(
        r"\b(?:list|show)\b(?:\s+the)?\s+files\s+in\s+(?:the\s+)?"
        r"['\"]?([A-Za-z0-9_\- ]{1,64}?)['\"]?\s*(?:directory|folder)\b", re.IGNORECASE,
    ), True),
    _Pattern(re.compile(
        r"\b(?:list|show)\b(?:\s+the)?\s+(?:files|directory|folder)\b", re.IGNORECASE,
    ), False),
]

_OPEN_APPLICATION_PATTERNS = [
    _Pattern(re.compile(
        r"\b(?:open|launch|start)\b(?:\s+the)?\s+['\"]?([A-Za-z0-9_\- ]{1,64}?)['\"]?\s+"
        r"(?:application|app)\b", re.IGNORECASE,
    ), True),
    _Pattern(re.compile(
        r"\b(?:open|launch|start)\b(?:\s+the)?\s+(?:application|app)\s+"
        r"['\"]?([A-Za-z0-9_\-]{1,64})['\"]?", re.IGNORECASE,
    ), True),
    _Pattern(re.compile(
        r"\b(?:open|launch|start)\b(?:\s+the)?\s+(?:application|app)\b", re.IGNORECASE,
    ), False),
]

_RUN_SCRIPT_PATTERNS = [
    _Pattern(re.compile(
        r"\b(?:run|execute)\b(?:\s+the)?\s+['\"]?([A-Za-z0-9_\- ]{1,64}?)['\"]?\s+script\b",
        re.IGNORECASE,
    ), True),
    _Pattern(re.compile(
        r"\brun\b(?:\s+the)?\s+script\s+['\"]?([A-Za-z0-9_\-]{1,64})['\"]?", re.IGNORECASE,
    ), True),
    _Pattern(re.compile(r"\b(?:run|execute)\b(?:\s+the)?\s+script\b", re.IGNORECASE), False),
]

_REPO_HEALTH_PATTERNS = [
    _Pattern(re.compile(
        r"\bcheck\b(?:\s+the)?\s+health\s+of\s+(?:the\s+)?"
        r"['\"]?([A-Za-z0-9_\- ]{1,64}?)['\"]?\s*repo(?:sitory)?\b", re.IGNORECASE,
    ), True),
    _Pattern(re.compile(
        r"\bcheck\b(?:\s+the)?\s+['\"]?([A-Za-z0-9_\- ]{1,64}?)['\"]?\s+repo(?:sitory)?\b",
        re.IGNORECASE,
    ), True),
    _Pattern(re.compile(
        r"\bcheck\b(?:\s+the)?\s+repo(?:sitory)?\s+['\"]?([A-Za-z0-9_\-]{1,64})['\"]?",
        re.IGNORECASE,
    ), True),
    _Pattern(re.compile(
        r"\brepo(?:sitory)?\s+health\s+['\"]?([A-Za-z0-9_\-]{1,64})['\"]?", re.IGNORECASE,
    ), True),
    _Pattern(re.compile(r"\bcheck\b(?:\s+the)?\s+repo(?:sitory)?\b", re.IGNORECASE), False),
    _Pattern(re.compile(r"\brepo(?:sitory)?\s+health\b", re.IGNORECASE), False),
]

_REPOSITORY_BACKUP_PATTERNS = [
    _Pattern(re.compile(
        r"\bcreate\s+backup\s+of\s+(?:the\s+)?"
        r"['\"]?([A-Za-z0-9_\- ]{1,64}?)['\"]?\s*repo(?:sitory)?\b", re.IGNORECASE,
    ), True),
    _Pattern(re.compile(
        r"\b(?:back\s*up|backup)\b(?:\s+the)?\s+"
        r"['\"]?([A-Za-z0-9_\- ]{1,64}?)['\"]?\s+repo(?:sitory)?\b", re.IGNORECASE,
    ), True),
    _Pattern(re.compile(r"\bback\s*up\s+(?:it|this|that)\b", re.IGNORECASE), False),
    _Pattern(re.compile(r"\bback\s+(?:it|this|that)\s+up\b", re.IGNORECASE), False),
    _Pattern(re.compile(r"^\s*back\s*up\s*\.?\s*$", re.IGNORECASE), False),
    _Pattern(re.compile(
        r"\b(?:back\s*up|backup)\b(?:\s+the)?\s+repo(?:sitory)?\b", re.IGNORECASE,
    ), False),
]


def _resolve_target(raw_target: str, tools_config_map: dict) -> str | None:
    """Exact, case-insensitive symbolic-key resolution only (R1) - no
    fuzzy matching, no aliases, no display-name invention, no substring
    guessing, no default target. Returns the canonical (already
    casefolded) configured key, or None if the shape check fails or the
    key is not registered."""

    if not _SYMBOLIC_KEY_SHAPE_RE.match(raw_target):
        return None
    key = raw_target.casefold()
    if key in tools_config_map:
        return key
    return None


def _missing_target_question(tool_name: str) -> str:
    return {
        "list_files": "Which registered directory would you like me to list?",
        "open_application": "Which registered application would you like me to open?",
        "run_registered_script": "Which registered script would you like me to run?",
        "repo_health": "Which registered repository would you like me to check?",
        "repository_backup": "Which registered repository would you like me to back up?",
    }[tool_name]


_TOOL_GRAMMAR = {
    "system_status": (_SYSTEM_STATUS_PATTERNS, None),
    "list_files": (_LIST_FILES_PATTERNS, "approved_directories"),
    "open_application": (_OPEN_APPLICATION_PATTERNS, "approved_applications"),
    "run_registered_script": (_RUN_SCRIPT_PATTERNS, "approved_scripts"),
    "repo_health": (_REPO_HEALTH_PATTERNS, "approved_repositories"),
    "repository_backup": (_REPOSITORY_BACKUP_PATTERNS, "approved_backups"),
}

# Fixed evaluation order - "exactly one simple action only": the resolver
# stops at the first tool whose grammar recognizes something in the
# request, never accumulating candidates from more than one tool.
_TOOL_ORDER = (
    "system_status",
    "list_files",
    "open_application",
    "run_registered_script",
    "repo_health",
    "repository_backup",
)


def _summary_for(tool_name: str, resource_key: str | None) -> str:
    if tool_name == "system_status":
        return "Check the current system status."
    if tool_name == "list_files":
        return f"List files in the registered '{resource_key}' directory."
    if tool_name == "open_application":
        return f"Open the registered '{resource_key}' application."
    if tool_name == "run_registered_script":
        return f"Run the registered '{resource_key}' script."
    if tool_name == "repo_health":
        return f"Check the health of the registered '{resource_key}' repository."
    if tool_name == "repository_backup":
        return f"Back up the registered '{resource_key}' repository."
    raise ValueError(tool_name)  # unreachable - _TOOL_ORDER is closed


def resolve_action_candidates(
    request_text: str,
    *,
    registry: ActionRegistry,
    tools_config: ToolsConfig,
) -> CandidateResolution:
    """Deterministic Stage A: never calls a model, never executes an
    action. Tries each of the 6 conservative recognizers in a fixed order
    and stops at the first real signal. Zero or more raw matches are
    deduplicated (by (action, resource_key)) before candidate IDs -
    "candidate_1", "candidate_2", ... - are assigned, and the result is
    capped at MAX_CANDIDATES.

    A compound request (brief S11) is rejected up front, before any
    tool-specific pattern is even tried: a candidate must represent the
    complete request, never merely one safe fragment of it - "Delete
    everything and back up the repository" must never narrow to a backup
    candidate just because the unsupported half came first."""

    if _is_compound_request(request_text):
        return CandidateResolution(candidates=(), deterministic_clarification=None)

    raw_matches: list[tuple[str, str | None, bool]] = []  # (action, key, sensitive)

    for tool_name in _TOOL_ORDER:
        patterns, config_field = _TOOL_GRAMMAR[tool_name]
        result = _try_patterns(request_text, patterns)
        if result is None:
            continue

        if tool_name == "system_status":
            # system_status's grammar has no target-capturing patterns at
            # all (resource_key is forbidden) - any match here is already
            # a complete, valid intent, never a "missing target" case.
            # _try_patterns() only ever returns "missing_target" for a
            # bare (has_target=False) pattern, which is exactly what every
            # system_status pattern is - so a match always looks like
            # "missing_target" from _try_patterns()'s point of view, but
            # for this one tool that string actually means "matched, no
            # target needed".
            raw_matches.append(("system_status", None, registry.is_sensitive("system_status")))
            break

        if result == "missing_target":
            question = _missing_target_question(tool_name)
            return CandidateResolution(
                candidates=(),
                deterministic_clarification=RequestClarificationDecision(
                    protocol_version=PROTOCOL_VERSION, question=question
                ),
            )

        raw_target, _match = result
        config_map = getattr(tools_config, config_field)
        resolved_key = _resolve_target(raw_target, config_map)
        if resolved_key is None:
            # Unregistered or shape-invalid target: zero candidates, never
            # a guess (brief S9's "unknown target" rule).
            return CandidateResolution(candidates=(), deterministic_clarification=None)

        raw_matches.append((tool_name, resolved_key, registry.is_sensitive(tool_name)))
        break

    # Deterministic dedupe, preserving first-seen order, before IDs are
    # assigned.
    seen: set[tuple[str, str | None]] = set()
    deduped: list[tuple[str, str | None, bool]] = []
    for action, key, sensitive in raw_matches:
        dedupe_key = (action, key)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append((action, key, sensitive))

    deduped = deduped[:MAX_CANDIDATES]

    candidates = tuple(
        ActionCandidate(
            candidate_id=f"candidate_{index}",
            action_request=ActionRequest(action=action, resource_key=key),
            sensitive=sensitive,
            user_summary=_summary_for(action, key),
        )
        for index, (action, key, sensitive) in enumerate(deduped, start=1)
    )

    return CandidateResolution(candidates=candidates, deterministic_clarification=None)
