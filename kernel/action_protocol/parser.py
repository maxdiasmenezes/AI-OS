"""
Strict, complete-response parser for the Milestone 39 action protocol.

parse_decision() accepts the raw text a model provider returned plus the
exact, immutable candidate tuple that was offered to it for this request,
and returns a typed ParseResult - never raises for a malformed or unsafe
model response (mirrors kernel/knowledge_base/answer.py's
parse_structured_answer() "never raises" contract). Provider-availability
and timeout failures remain ordinary exceptions raised by the model layer
(kernel/models/ollama.py) - this module never sees them and never turns a
parse failure into a persisted task; persistence begins in Milestone 40.

This module performs no I/O, imports no executor or confirmation store,
and never logs the raw model response text - only fixed, symbolic failure
categories (see ParseErrorCode).
"""

import json

from kernel.action_protocol.types import (
    MAX_CANDIDATE_ID_CHARS,
    MAX_FIELD_CHARS,
    MAX_JSON_NESTING_DEPTH,
    MAX_RESPONSE_CHARS,
    PROTOCOL_VERSION,
    ActionCandidate,
    CannotCompleteDecision,
    ParseErrorCode,
    ParseFailure,
    ParseResult,
    ParseSuccess,
    RequestClarificationDecision,
    RespondDecision,
    SelectCandidateDecision,
)

_DECISION_KINDS = frozenset(
    {"respond", "select_candidate", "request_clarification", "cannot_complete"}
)

_FIELDS_BY_DECISION = {
    "respond": frozenset({"protocol_version", "decision", "response"}),
    "select_candidate": frozenset(
        {"protocol_version", "decision", "candidate_id", "user_summary"}
    ),
    "request_clarification": frozenset({"protocol_version", "decision", "question"}),
    "cannot_complete": frozenset({"protocol_version", "decision", "reason"}),
}


class _InvalidConstant(Exception):
    """Raised internally by the json.loads() parse_constant hook when the
    response contains NaN/Infinity/-Infinity - caught by parse_decision()
    and mapped to ParseErrorCode.INVALID_CONSTANT. Never escapes this
    module."""


class _DuplicateKey(Exception):
    """Raised internally by the json.loads() object_pairs_hook when a
    JSON object contains the same key twice at any level - caught by
    parse_decision() and mapped to ParseErrorCode.DUPLICATE_KEY. Never
    escapes this module."""


def _reject_constant(name: str):
    raise _InvalidConstant(name)


def _pairs_hook(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen[key] = value
    return seen


def _strip_exact_fence(text: str) -> tuple[str, bool]:
    """Strip a Markdown JSON fence only if the ENTIRE (whitespace-
    stripped) text is exactly that fence: an opening line that is
    precisely ``` or ```json, a closing line that is precisely ```, and
    nothing else outside those two lines. A fence found merely somewhere
    inside surrounding prose is never stripped - that case is left to
    fail JSON parsing on its own, which is the correct "prose around
    JSON" rejection."""

    lines = text.split("\n")
    if len(lines) < 2:
        return text, False
    first, last = lines[0].strip(), lines[-1].strip()
    if first not in ("```", "```json") or last != "```":
        return text, False
    return "\n".join(lines[1:-1]).strip(), True


def _max_nesting_depth(text: str) -> int:
    """Max bracket-nesting depth of `text`, outside of string literals -
    computed with a single linear scan, never by recursing into
    json.loads() itself, so pathologically deep input is rejected before
    it can ever reach (and potentially crash) the recursive-descent JSON
    decoder."""

    depth = 0
    max_depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch in "}]":
            depth -= 1
    return max_depth


def _fail(error: ParseErrorCode, detail: str) -> ParseFailure:
    return ParseFailure(error=error, detail=detail)


def parse_decision(
    raw_text: str, candidates: tuple[ActionCandidate, ...]
) -> ParseResult:
    """Strictly parse and validate one complete model response against the
    Milestone 39 protocol envelope. `candidates` must be the exact,
    request-local candidate tuple Stage B offered the model for this
    request (see kernel/action_protocol/prompt.py) - a select_candidate
    decision is only ever resolved against this tuple, never
    reconstructed from the model's own text."""

    candidate_ids = [c.candidate_id for c in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        return _fail(ParseErrorCode.INVALID_CANDIDATE_SET, "duplicate candidate_id in supplied set")
    candidate_by_id = {c.candidate_id: c for c in candidates}

    if not isinstance(raw_text, str) or not raw_text.strip():
        return _fail(ParseErrorCode.EMPTY_RESPONSE, "empty or non-string response")

    if len(raw_text) > MAX_RESPONSE_CHARS:
        return _fail(ParseErrorCode.RESPONSE_TOO_LARGE, "response exceeds the maximum length")

    stripped = raw_text.strip()
    candidate_text, _fenced = _strip_exact_fence(stripped)

    if _max_nesting_depth(candidate_text) > MAX_JSON_NESTING_DEPTH:
        return _fail(ParseErrorCode.EXCESSIVE_NESTING, "JSON nesting exceeds the maximum depth")

    try:
        parsed = json.loads(
            candidate_text, object_pairs_hook=_pairs_hook, parse_constant=_reject_constant
        )
    except _DuplicateKey:
        return _fail(ParseErrorCode.DUPLICATE_KEY, "duplicate key in JSON object")
    except _InvalidConstant:
        return _fail(ParseErrorCode.INVALID_CONSTANT, "NaN/Infinity/-Infinity is not permitted")
    except json.JSONDecodeError:
        # Covers malformed JSON, prose before/after the object (json.loads
        # requires the whole string to be one JSON value), and multiple
        # JSON objects in one response - no separate check is needed for
        # any of these, matching kernel/knowledge_base/answer.py's
        # established precedent.
        return _fail(ParseErrorCode.MALFORMED_JSON, "could not parse a single complete JSON value")

    if not isinstance(parsed, dict):
        return _fail(ParseErrorCode.SCHEMA_VALIDATION_FAILED, "top-level value is not an object")

    if "protocol_version" not in parsed:
        return _fail(ParseErrorCode.UNKNOWN_PROTOCOL_VERSION, "missing protocol_version")

    protocol_version = parsed.get("protocol_version")
    if isinstance(protocol_version, bool) or protocol_version != PROTOCOL_VERSION:
        return _fail(ParseErrorCode.UNKNOWN_PROTOCOL_VERSION, "unexpected protocol_version")

    if "decision" not in parsed:
        return _fail(ParseErrorCode.UNSUPPORTED_DECISION, "missing decision")

    decision = parsed.get("decision")
    if not isinstance(decision, str) or decision not in _DECISION_KINDS:
        return _fail(ParseErrorCode.UNSUPPORTED_DECISION, "unrecognized decision kind")

    expected_fields = _FIELDS_BY_DECISION[decision]
    if set(parsed.keys()) != expected_fields:
        return _fail(
            ParseErrorCode.SCHEMA_VALIDATION_FAILED,
            "unknown, missing, or cross-branch field(s) for this decision",
        )

    def _valid_text(value) -> bool:
        return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_FIELD_CHARS

    if decision == "respond":
        if not _valid_text(parsed["response"]):
            return _fail(ParseErrorCode.SCHEMA_VALIDATION_FAILED, "invalid response field")
        return ParseSuccess(
            RespondDecision(protocol_version=PROTOCOL_VERSION, response=parsed["response"])
        )

    if decision == "request_clarification":
        if not _valid_text(parsed["question"]):
            return _fail(ParseErrorCode.SCHEMA_VALIDATION_FAILED, "invalid question field")
        return ParseSuccess(
            RequestClarificationDecision(
                protocol_version=PROTOCOL_VERSION, question=parsed["question"]
            )
        )

    if decision == "cannot_complete":
        if not _valid_text(parsed["reason"]):
            return _fail(ParseErrorCode.SCHEMA_VALIDATION_FAILED, "invalid reason field")
        return ParseSuccess(
            CannotCompleteDecision(protocol_version=PROTOCOL_VERSION, reason=parsed["reason"])
        )

    # decision == "select_candidate"
    candidate_id = parsed.get("candidate_id")
    user_summary = parsed.get("user_summary")

    if not isinstance(candidate_id, str) or not candidate_id or len(candidate_id) > MAX_CANDIDATE_ID_CHARS:
        return _fail(ParseErrorCode.SCHEMA_VALIDATION_FAILED, "invalid candidate_id field")
    if not _valid_text(user_summary):
        return _fail(ParseErrorCode.SCHEMA_VALIDATION_FAILED, "invalid user_summary field")

    candidate = candidate_by_id.get(candidate_id)
    if candidate is None:
        # Fails closed even though the dynamic schema (prompt.py) should
        # already make an unknown ID unreachable - defense in depth, not
        # the only line of defense.
        return _fail(ParseErrorCode.UNKNOWN_CANDIDATE, "candidate_id not in the offered set")

    return ParseSuccess(
        SelectCandidateDecision(
            protocol_version=PROTOCOL_VERSION,
            candidate=candidate,
            user_summary=user_summary,
        )
    )
