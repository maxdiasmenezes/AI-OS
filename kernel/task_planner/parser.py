"""
Strict, complete-response parser for kernel/task_planner/ (Milestone 41).

parse_plan_response() accepts the raw text a model provider returned plus
the exact catalog tuple that was offered to it for this request, and
returns a typed ParseOutcome - never raises for a malformed or unsafe model
response (mirrors kernel/action_protocol/parser.py:parse_decision()'s
"never raises" contract, which itself mirrors
kernel/knowledge_base/answer.py's parse_structured_answer()).
Provider-availability and timeout failures remain ordinary exceptions
raised by the model layer (kernel/models/ollama.py) - this module never
sees them and never turns a parse failure into a persisted task outcome
(persistence is out of scope for this milestone).

This module performs no I/O, imports no executor, tool registry, or task
repository, and never logs the raw model response text - only fixed,
symbolic failure categories (see kernel/task_planner/types.py's
PlannerErrorCode).

Known, accepted residual case (documented, not "fixed" here - see
docs/architecture.md's Milestone 41 section): the empirically-validated
gemma3:12b run's one miss (its own corpus label "M1") had the model select
the single registered script catalog entry ("run_registered_script" /
"whatsapp_test") for a request that generically said "run the tests" -
i.e. the model chose a real, registered, catalog-listed action, but not
necessarily the one semantically closest to what the user meant. This
parser CANNOT and MUST NOT try to catch that: judging whether a chosen
catalog_id is the *semantically correct* one for the request's free text
is exactly the model's job (constrained by prompt.py's Rules 1/2), not a
structural property this deterministic parser can check without
re-introducing exactly the kind of natural-language guessing
kernel/action_protocol/candidates.py's own docstring already rejects for
Stage A. What this parser DOES guarantee, unconditionally, in that case:
the selected catalog_id is real and registered (never invented - see
UNKNOWN_ACTION below), and requires_confirmation is derived from the
registry's own sensitivity metadata regardless of whether the model's
choice was semantically apt - see test_parser.py's dedicated regression
test for the exact fixture and what it proves.
"""

import json

from kernel.task_planner.types import (
    CannotPlan,
    CatalogEntry,
    MAX_DEPENDENCIES_PER_STEP,
    MAX_EXPECTED_RESULT_CHARS,
    MAX_JSON_NESTING_DEPTH,
    MAX_OBJECTIVE_CHARS,
    MAX_PLAN_STEPS,
    MAX_REASON_CHARS,
    MAX_RESPONSE_CHARS,
    MAX_STEP_DESCRIPTION_CHARS,
    PLAN_VERSION,
    ParsedPlan,
    ParseOutcome,
    PlannerErrorCode,
    PlannerFailure,
    PlanStep,
    StepKind,
)

_RESULT_KINDS = frozenset({"plan", "cannot_plan"})
_STEP_KINDS = frozenset({"action", "respond"})

_PLAN_FIELDS = frozenset({"plan_version", "result", "objective", "steps"})
_CANNOT_PLAN_FIELDS = frozenset({"plan_version", "result", "reason"})
_ACTION_STEP_FIELDS = frozenset(
    {"step_kind", "catalog_id", "description", "expected_result", "depends_on"}
)
_RESPOND_STEP_FIELDS = frozenset({"step_kind", "description", "expected_result", "depends_on"})


class _InvalidConstant(Exception):
    """Raised internally by the json.loads() parse_constant hook when the
    response contains NaN/Infinity/-Infinity - caught below and mapped to
    PlannerErrorCode.INVALID_CONSTANT. Never escapes this module."""


class _DuplicateKey(Exception):
    """Raised internally by the json.loads() object_pairs_hook when a
    JSON object contains the same key twice at any level - caught below
    and mapped to PlannerErrorCode.DUPLICATE_KEY. Never escapes this
    module."""


def _reject_constant(name: str):
    raise _InvalidConstant(name)


def _pairs_hook(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen[key] = value
    return seen


def _strip_exact_fence(text: str) -> str:
    """Strip a Markdown JSON fence only if the ENTIRE (whitespace-
    stripped) text is exactly that fence - matches
    kernel/action_protocol/parser.py's _strip_exact_fence() exactly. A
    fence found merely somewhere inside surrounding prose is never
    stripped - that case is left to fail JSON parsing on its own."""

    lines = text.split("\n")
    if len(lines) < 2:
        return text
    first, last = lines[0].strip(), lines[-1].strip()
    if first not in ("```", "```json") or last != "```":
        return text
    return "\n".join(lines[1:-1]).strip()


def _max_nesting_depth(text: str) -> int:
    """Max bracket-nesting depth of `text`, outside of string literals -
    computed with a single linear scan, never by recursing into
    json.loads() itself, matching
    kernel/action_protocol/parser.py's own defense against pathologically
    deep input reaching the recursive-descent JSON decoder."""

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


def _fail(error: PlannerErrorCode, detail: str) -> PlannerFailure:
    return PlannerFailure(error=error, detail=detail)


def _valid_text(value, max_len: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_len


def parse_plan_response(raw_text: str, catalog: tuple[CatalogEntry, ...]) -> ParseOutcome:
    """Strictly parse and validate one complete model response against the
    Milestone 41 planner protocol envelope. `catalog` must be the exact
    catalog tuple prompt.py's build_prompt()/build_schema() offered the
    model for this request - an "action" step's catalog_id is only ever
    resolved against this tuple, never reconstructed from the model's own
    text."""

    catalog_by_id = {entry.catalog_id: entry for entry in catalog}

    if not isinstance(raw_text, str) or not raw_text.strip():
        return _fail(PlannerErrorCode.EMPTY_RESPONSE, "empty or non-string response")

    if len(raw_text) > MAX_RESPONSE_CHARS:
        return _fail(PlannerErrorCode.RESPONSE_TOO_LARGE, "response exceeds the maximum length")

    stripped = raw_text.strip()
    candidate_text = _strip_exact_fence(stripped)

    if _max_nesting_depth(candidate_text) > MAX_JSON_NESTING_DEPTH:
        return _fail(PlannerErrorCode.EXCESSIVE_NESTING, "JSON nesting exceeds the maximum depth")

    try:
        parsed = json.loads(
            candidate_text, object_pairs_hook=_pairs_hook, parse_constant=_reject_constant
        )
    except _DuplicateKey:
        return _fail(PlannerErrorCode.DUPLICATE_KEY, "duplicate key in JSON object")
    except _InvalidConstant:
        return _fail(PlannerErrorCode.INVALID_CONSTANT, "NaN/Infinity/-Infinity is not permitted")
    except json.JSONDecodeError:
        # Covers malformed JSON, prose before/after the object (json.loads
        # requires the whole string to be one JSON value), and multiple
        # JSON objects in one response - matches
        # kernel/action_protocol/parser.py's established precedent.
        return _fail(
            PlannerErrorCode.MALFORMED_PLAN, "could not parse a single complete JSON value"
        )

    if not isinstance(parsed, dict):
        return _fail(PlannerErrorCode.MALFORMED_PLAN, "top-level value is not an object")

    if "plan_version" not in parsed:
        return _fail(PlannerErrorCode.UNSUPPORTED_PLAN_VERSION, "missing plan_version")
    plan_version = parsed.get("plan_version")
    if isinstance(plan_version, bool) or plan_version != PLAN_VERSION:
        return _fail(PlannerErrorCode.UNSUPPORTED_PLAN_VERSION, "unexpected plan_version")

    if "result" not in parsed:
        return _fail(PlannerErrorCode.MALFORMED_PLAN, "missing result")
    result = parsed.get("result")
    if not isinstance(result, str) or result not in _RESULT_KINDS:
        return _fail(PlannerErrorCode.MALFORMED_PLAN, "unrecognized result discriminator")

    if result == "cannot_plan":
        if set(parsed.keys()) != _CANNOT_PLAN_FIELDS:
            return _fail(
                PlannerErrorCode.SCHEMA_VALIDATION_FAILED,
                "unknown, missing, or cross-branch field(s) for cannot_plan",
            )
        reason = parsed.get("reason")
        if not _valid_text(reason, MAX_REASON_CHARS):
            return _fail(PlannerErrorCode.SCHEMA_VALIDATION_FAILED, "invalid reason field")
        return CannotPlan(plan_version=PLAN_VERSION, reason=reason)

    # result == "plan"
    if set(parsed.keys()) != _PLAN_FIELDS:
        return _fail(
            PlannerErrorCode.SCHEMA_VALIDATION_FAILED,
            "unknown, missing, or cross-branch field(s) for plan",
        )

    objective = parsed.get("objective")
    if not _valid_text(objective, MAX_OBJECTIVE_CHARS):
        return _fail(PlannerErrorCode.SCHEMA_VALIDATION_FAILED, "invalid objective field")

    steps_raw = parsed.get("steps")
    if not isinstance(steps_raw, list) or not (1 <= len(steps_raw) <= MAX_PLAN_STEPS):
        return _fail(PlannerErrorCode.TOO_MANY_STEPS, "steps must be a list of 1..MAX_PLAN_STEPS")

    parsed_steps: list[PlanStep] = []
    for index, raw_step in enumerate(steps_raw):
        position = index + 1
        if not isinstance(raw_step, dict):
            return _fail(PlannerErrorCode.INVALID_STEP, f"step {position} is not an object")

        step_kind = raw_step.get("step_kind")
        if not isinstance(step_kind, str) or step_kind not in _STEP_KINDS:
            return _fail(PlannerErrorCode.INVALID_STEP, f"step {position} has invalid step_kind")

        expected_fields = _ACTION_STEP_FIELDS if step_kind == "action" else _RESPOND_STEP_FIELDS
        if set(raw_step.keys()) != expected_fields:
            return _fail(
                PlannerErrorCode.INVALID_STEP, f"step {position} unknown/missing field(s)"
            )

        description = raw_step.get("description")
        if not _valid_text(description, MAX_STEP_DESCRIPTION_CHARS):
            return _fail(PlannerErrorCode.INVALID_STEP, f"step {position} invalid description")

        expected_result = raw_step.get("expected_result")
        if not _valid_text(expected_result, MAX_EXPECTED_RESULT_CHARS):
            return _fail(
                PlannerErrorCode.INVALID_STEP, f"step {position} invalid expected_result"
            )

        depends_on_raw = raw_step.get("depends_on")
        if not isinstance(depends_on_raw, list) or len(depends_on_raw) > MAX_DEPENDENCIES_PER_STEP:
            return _fail(
                PlannerErrorCode.INVALID_DEPENDENCY, f"step {position} invalid depends_on"
            )
        depends_on: list[int] = []
        for dependency in depends_on_raw:
            if isinstance(dependency, bool) or not isinstance(dependency, int):
                return _fail(
                    PlannerErrorCode.INVALID_DEPENDENCY,
                    f"step {position} has a non-integer dependency",
                )
            # Backward-only: never itself, never a later or nonexistent
            # step. This single bound (rather than a separate cycle check)
            # is what makes a dependency cycle structurally unreachable -
            # see kernel/task_planner/types.py:PlanStep's docstring.
            if not (1 <= dependency < position):
                return _fail(
                    PlannerErrorCode.INVALID_DEPENDENCY,
                    f"step {position} references a non-earlier step {dependency}",
                )
            depends_on.append(dependency)
        if len(set(depends_on)) != len(depends_on):
            return _fail(
                PlannerErrorCode.INVALID_DEPENDENCY, f"step {position} has duplicate dependencies"
            )

        catalog_entry = None
        if step_kind == "action":
            catalog_id = raw_step.get("catalog_id")
            if not isinstance(catalog_id, str) or catalog_id not in catalog_by_id:
                # Fails closed even though the dynamic schema (prompt.py)
                # should already make an unknown ID unreachable - defense
                # in depth, matching
                # kernel/action_protocol/parser.py's own posture.
                return _fail(
                    PlannerErrorCode.UNKNOWN_ACTION,
                    f"step {position} references an unknown catalog_id",
                )
            catalog_entry = catalog_by_id[catalog_id]

        parsed_steps.append(
            PlanStep(
                step_id=f"step_{position}",
                position=position,
                kind=StepKind(step_kind),
                action_name=catalog_entry.action_name if catalog_entry else None,
                resource_key=catalog_entry.resource_key if catalog_entry else None,
                catalog_id=catalog_entry.catalog_id if catalog_entry else None,
                description=description,
                expected_result=expected_result,
                depends_on=tuple(depends_on),
                requires_confirmation=catalog_entry.sensitive if catalog_entry else False,
            )
        )

    # A plan needs at least one action step - a respond-only "plan" is
    # never valid under the approved action|respond step-kind design (see
    # Rule 5 in prompt.py). Parser-side invariant, not a schema field.
    if not any(step.kind is StepKind.ACTION for step in parsed_steps):
        return _fail(PlannerErrorCode.INVALID_STEP, "plan contains no action steps")

    return ParsedPlan(plan_version=PLAN_VERSION, objective=objective, steps=tuple(parsed_steps))
