"""
Strict command grammar for the /knowledge interface (Milestone 37). Every
supported form is listed explicitly below - there is no natural-language
matching, no fuzzy matching, no partial-token matching, and no argparse
(argparse is reserved for the offline `scripts/knowledge.py` CLI
grammar, which is a distinct surface). Anything that doesn't exactly
match one of these forms is a parse error, not a guess.

Supported forms ("/knowledge", the verb, and every option name are
matched case-insensitively; a source key is casefolded so it matches
kernel/config/knowledge_base.yaml's approved_sources keys):

    /knowledge
    /knowledge help
    /knowledge status
    /knowledge status --source <source-key>
    /knowledge search -- <query text>
    /knowledge search --source <source-key> -- <query text>
    /knowledge search --limit <1-10> -- <query text>
    /knowledge search --source <source-key> --limit <1-10> -- <query text>
    /knowledge search --limit <1-10> --source <source-key> -- <query text>
    /knowledge ask -- <question>
    /knowledge ask --source <source-key> -- <question>
    /knowledge ask --limit <1-5> -- <question>
    /knowledge ask --source <source-key> --limit <1-5> -- <question>
    /knowledge ask --limit <1-5> --source <source-key> -- <question>
    /knowledge ingest <source-key>
    /knowledge confirm
    /knowledge cancel

`search` and `ask` each require exactly one literal bare `--` token
separating options from query/question text; everything after the first
bare `--` is query/question text, not command syntax - it is never
re-parsed for further options, even if it itself contains tokens that look
like `--source` or another `--`. `--source`/`--limit` may each appear at
most once and in either order. A source key must match
kernel/knowledge_base/config.py's own _SOURCE_KEY_RE shape
(^[a-z0-9][a-z0-9_-]{0,63}$) - this is a shape check only; whether it is
currently *approved* is decided later, downstream, by the existing
Milestone 36 allowlist (config.approved_sources), never by this module.
`search`'s `--limit` value must be an unsigned integer from 1 through 10
(MAX_INTERFACE_RESULT_LIMIT) - this is the interface-level cap, tighter
than kernel/knowledge_base/search.py's own service-level cap of 50. `ask`'s
`--limit` value (an evidence-chunk count, a distinct concept from search's
result count, deliberately not sharing a range) must be an unsigned
integer from 1 through 5 (MAX_ASK_EVIDENCE_LIMIT); its question text must
be from 1 through 200 characters (MAX_QUESTION_CHARACTERS) after the same
whitespace-run normalization every command's tokens already get - this
mirrors kernel/knowledge_base/query.py's MAX_QUERY_CHARACTERS as a
deliberately duplicated literal (not an import), matching this module's
existing policy of having no dependency on kernel/knowledge_base/ for pure
grammar validation (see the source-key shape regex below for the same
policy already in effect).

Query/question text preserves every user-supplied Unicode letter and
punctuation character exactly; only whitespace *runs* between tokens are
normalized to a single ASCII space (an artifact of token-based splitting,
not content mutation).
"""

import re
from dataclasses import dataclass

KNOWLEDGE_PREFIX = "/knowledge"

MIN_INTERFACE_RESULT_LIMIT = 1
MAX_INTERFACE_RESULT_LIMIT = 10

MIN_ASK_EVIDENCE_LIMIT = 1
MAX_ASK_EVIDENCE_LIMIT = 5

# Deliberately duplicated from kernel/knowledge_base/query.py's
# MAX_QUERY_CHARACTERS rather than imported - see module docstring.
MAX_QUESTION_CHARACTERS = 200

# Same conservative shape kernel/knowledge_base/config.py's own
# _SOURCE_KEY_RE uses - duplicated rather than imported so this module has
# no dependency on kernel/knowledge_base/ for pure grammar validation;
# whether a shape-valid key is actually *approved* is checked later by the
# real config.
_SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LIMIT_RE = re.compile(r"^[0-9]+$")

_KNOWN_VERBS = frozenset({"help", "status", "search", "ask", "ingest", "confirm", "cancel"})


@dataclass(frozen=True)
class ParsedKnowledgeCommand:
    verb: str  # "help" | "status" | "search" | "ask" | "ingest" | "confirm" | "cancel"
    source_key: str | None = None
    limit: int | None = None
    query: str | None = None


@dataclass(frozen=True)
class KnowledgeParseError:
    reason: str  # stable, symbolic reason code - never free text, never the raw prompt


def _is_valid_source_key_shape(value: str) -> bool:
    return bool(_SOURCE_KEY_RE.match(value))


def _scan_options(tokens: list[str], allowed: frozenset):
    """Consume a leading run of `--option value` pairs (any order, each
    option at most once) from `allowed`. Stops at the first token that is
    a bare `--` delimiter or that doesn't look like a `--option` token at
    all, returning it (and everything after it) as `remaining` for the
    caller to interpret. Returns (options, remaining, error)."""

    options: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        folded = token.casefold()
        if folded == "--" or not folded.startswith("--"):
            break
        if folded not in allowed:
            return None, None, KnowledgeParseError("unknown_option")
        if folded in options:
            return None, None, KnowledgeParseError("duplicate_option")
        if i + 1 >= len(tokens) or tokens[i + 1].startswith("--"):
            return None, None, KnowledgeParseError("missing_option_value")
        options[folded] = tokens[i + 1]
        i += 2
    return options, tokens[i:], None


def _parse_status(args: list[str]):
    if not args:
        return ParsedKnowledgeCommand("status")

    options, remaining, error = _scan_options(args, frozenset({"--source"}))
    if error is not None:
        return error
    if remaining:
        return KnowledgeParseError("unexpected_argument")
    if "--source" not in options:
        return KnowledgeParseError("unexpected_argument")

    source_key = options["--source"].casefold()
    if not _is_valid_source_key_shape(source_key):
        return KnowledgeParseError("invalid_source_key")
    return ParsedKnowledgeCommand("status", source_key=source_key)


def _parse_ingest(args: list[str]):
    if len(args) != 1:
        return KnowledgeParseError("wrong_argument_count")

    source_key = args[0].casefold()
    if not _is_valid_source_key_shape(source_key):
        return KnowledgeParseError("invalid_source_key")
    return ParsedKnowledgeCommand("ingest", source_key=source_key)


def _parse_search(args: list[str]):
    if not args:
        return KnowledgeParseError("missing_delimiter")

    options, remaining, error = _scan_options(args, frozenset({"--source", "--limit"}))
    if error is not None:
        return error

    if not remaining or remaining[0] != "--":
        return KnowledgeParseError("missing_delimiter")

    query_tokens = remaining[1:]
    if not query_tokens:
        return KnowledgeParseError("blank_query")
    query = " ".join(query_tokens)

    source_key = None
    if "--source" in options:
        source_key = options["--source"].casefold()
        if not _is_valid_source_key_shape(source_key):
            return KnowledgeParseError("invalid_source_key")

    limit = None
    if "--limit" in options:
        raw_limit = options["--limit"]
        if not _LIMIT_RE.match(raw_limit):
            return KnowledgeParseError("invalid_limit")
        parsed_limit = int(raw_limit)
        if parsed_limit < MIN_INTERFACE_RESULT_LIMIT or parsed_limit > MAX_INTERFACE_RESULT_LIMIT:
            return KnowledgeParseError("invalid_limit")
        limit = parsed_limit

    return ParsedKnowledgeCommand("search", source_key=source_key, limit=limit, query=query)


def _parse_ask(args: list[str]):
    if not args:
        return KnowledgeParseError("missing_delimiter")

    options, remaining, error = _scan_options(args, frozenset({"--source", "--limit"}))
    if error is not None:
        return error

    if not remaining or remaining[0] != "--":
        return KnowledgeParseError("missing_delimiter")

    question_tokens = remaining[1:]
    if not question_tokens:
        return KnowledgeParseError("blank_question")
    question = " ".join(question_tokens)
    if len(question) > MAX_QUESTION_CHARACTERS:
        return KnowledgeParseError("oversized_question")

    source_key = None
    if "--source" in options:
        source_key = options["--source"].casefold()
        if not _is_valid_source_key_shape(source_key):
            return KnowledgeParseError("invalid_source_key")

    limit = None
    if "--limit" in options:
        raw_limit = options["--limit"]
        if not _LIMIT_RE.match(raw_limit):
            return KnowledgeParseError("invalid_limit")
        parsed_limit = int(raw_limit)
        if parsed_limit < MIN_ASK_EVIDENCE_LIMIT or parsed_limit > MAX_ASK_EVIDENCE_LIMIT:
            return KnowledgeParseError("invalid_limit")
        limit = parsed_limit

    return ParsedKnowledgeCommand("ask", source_key=source_key, limit=limit, query=question)


def parse_knowledge_command(prompt: str):
    """Parse one /knowledge command. Returns ParsedKnowledgeCommand on
    success or KnowledgeParseError on any malformed input - never raises,
    never guesses, never partially honors an unrecognized form."""

    tokens = prompt.strip().split()
    if not tokens or tokens[0].casefold() != KNOWLEDGE_PREFIX:
        return KnowledgeParseError("not_a_knowledge_command")

    rest = tokens[1:]
    if not rest:
        return ParsedKnowledgeCommand("help")

    verb = rest[0].casefold()
    args = rest[1:]

    if verb not in _KNOWN_VERBS:
        return KnowledgeParseError("unknown_verb")

    if verb in ("help", "confirm", "cancel"):
        if args:
            return KnowledgeParseError("wrong_argument_count")
        return ParsedKnowledgeCommand(verb)

    if verb == "status":
        return _parse_status(args)
    if verb == "ingest":
        return _parse_ingest(args)
    if verb == "ask":
        return _parse_ask(args)
    return _parse_search(args)
