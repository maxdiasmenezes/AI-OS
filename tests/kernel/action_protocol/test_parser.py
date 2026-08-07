"""Tests for kernel/action_protocol/parser.py: the strict, complete-response
protocol parser. Pure module - no I/O, no model call, no execution - every
candidate here is constructed directly.
"""

import json

import pytest

from kernel.action_protocol.parser import parse_decision
from kernel.action_protocol.types import (
    ActionCandidate,
    CannotCompleteDecision,
    ParseErrorCode,
    ParseFailure,
    ParseSuccess,
    RequestClarificationDecision,
    RespondDecision,
    SelectCandidateDecision,
)
from kernel.tools.types import ActionRequest


def _candidate(candidate_id="candidate_1", action="open_application", resource_key="notepad"):
    return ActionCandidate(
        candidate_id=candidate_id,
        action_request=ActionRequest(action=action, resource_key=resource_key),
        sensitive=True,
        user_summary="Open the registered 'notepad' application.",
    )


def _assert_failure(result, error: ParseErrorCode):
    assert isinstance(result, ParseFailure)
    assert result.error == error


# --- every valid branch --------------------------------------------------


def test_respond_valid():
    raw = json.dumps({"protocol_version": 1, "decision": "respond", "response": "Paris."})
    result = parse_decision(raw, ())
    assert isinstance(result, ParseSuccess)
    assert result.decision == RespondDecision(protocol_version=1, response="Paris.")


def test_select_candidate_valid():
    candidate = _candidate()
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1", "user_summary": "Opening notepad.",
    })
    result = parse_decision(raw, (candidate,))
    assert isinstance(result, ParseSuccess)
    assert isinstance(result.decision, SelectCandidateDecision)
    assert result.decision.candidate is candidate
    assert result.decision.user_summary == "Opening notepad."


def test_request_clarification_valid():
    raw = json.dumps({
        "protocol_version": 1, "decision": "request_clarification", "question": "Which one?",
    })
    result = parse_decision(raw, ())
    assert result.decision == RequestClarificationDecision(protocol_version=1, question="Which one?")


def test_cannot_complete_valid():
    raw = json.dumps({
        "protocol_version": 1, "decision": "cannot_complete", "reason": "Unsupported.",
    })
    result = parse_decision(raw, ())
    assert result.decision == CannotCompleteDecision(protocol_version=1, reason="Unsupported.")


# --- whitespace / fences ---------------------------------------------------


def test_surrounding_whitespace_is_stripped():
    raw = "  \n" + json.dumps({"protocol_version": 1, "decision": "respond", "response": "x"}) + "\n  "
    assert isinstance(parse_decision(raw, ()), ParseSuccess)


def test_exact_whole_response_json_fence_is_stripped():
    body = json.dumps({"protocol_version": 1, "decision": "respond", "response": "x"})
    raw = f"```json\n{body}\n```"
    assert isinstance(parse_decision(raw, ()), ParseSuccess)


def test_exact_whole_response_bare_fence_is_stripped():
    body = json.dumps({"protocol_version": 1, "decision": "respond", "response": "x"})
    raw = f"```\n{body}\n```"
    assert isinstance(parse_decision(raw, ()), ParseSuccess)


def test_prose_before_json_is_rejected():
    body = json.dumps({"protocol_version": 1, "decision": "respond", "response": "x"})
    raw = f"Sure, here is the answer:\n{body}"
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.MALFORMED_JSON)


def test_prose_after_json_is_rejected():
    body = json.dumps({"protocol_version": 1, "decision": "respond", "response": "x"})
    raw = f"{body}\nHope that helps!"
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.MALFORMED_JSON)


def test_fence_with_prose_outside_it_is_rejected():
    body = json.dumps({"protocol_version": 1, "decision": "respond", "response": "x"})
    raw = f"Here you go:\n```json\n{body}\n```"
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.MALFORMED_JSON)


def test_multiple_json_objects_rejected():
    body = json.dumps({"protocol_version": 1, "decision": "respond", "response": "x"})
    raw = body + body
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.MALFORMED_JSON)


def test_malformed_json_rejected():
    _assert_failure(parse_decision("{not valid json", ()), ParseErrorCode.MALFORMED_JSON)


# --- duplicate keys ----------------------------------------------------------


def test_duplicate_top_level_key_rejected():
    raw = '{"protocol_version": 1, "decision": "respond", "response": "a", "response": "b"}'
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.DUPLICATE_KEY)


def test_duplicate_key_in_a_deeper_position_rejected():
    # Even though our schema never nests, the parser's object_pairs_hook
    # must catch a duplicate at any level, not just the top.
    raw = '{"protocol_version": 1, "decision": "respond", "response": "x", "nested": {"a": 1, "a": 2}}'
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.DUPLICATE_KEY)


# --- NaN / Infinity ------------------------------------------------------


def test_nan_rejected():
    raw = '{"protocol_version": NaN, "decision": "respond", "response": "x"}'
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.INVALID_CONSTANT)


def test_infinity_rejected():
    raw = '{"protocol_version": Infinity, "decision": "respond", "response": "x"}'
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.INVALID_CONSTANT)


def test_negative_infinity_rejected():
    raw = '{"protocol_version": -Infinity, "decision": "respond", "response": "x"}'
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.INVALID_CONSTANT)


# --- top-level shape -------------------------------------------------------


@pytest.mark.parametrize("raw", ['"just a string"', "42", "true", "null", "[1, 2, 3]"])
def test_non_object_top_level_rejected(raw):
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


# --- protocol_version / decision presence and validity ----------------------


def test_missing_protocol_version_rejected():
    raw = '{"decision": "respond", "response": "x"}'
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.UNKNOWN_PROTOCOL_VERSION)


def test_wrong_protocol_version_rejected():
    raw = json.dumps({"protocol_version": 2, "decision": "respond", "response": "x"})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.UNKNOWN_PROTOCOL_VERSION)


def test_boolean_protocol_version_rejected():
    raw = json.dumps({"protocol_version": True, "decision": "respond", "response": "x"})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.UNKNOWN_PROTOCOL_VERSION)


def test_missing_decision_rejected():
    raw = '{"protocol_version": 1, "response": "x"}'
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.UNSUPPORTED_DECISION)


def test_unknown_decision_rejected():
    raw = json.dumps({"protocol_version": 1, "decision": "propose_tool", "response": "x"})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.UNSUPPORTED_DECISION)


def test_non_string_decision_rejected():
    raw = json.dumps({"protocol_version": 1, "decision": 1, "response": "x"})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.UNSUPPORTED_DECISION)


# --- field-set exactness ------------------------------------------------------


def test_missing_field_rejected():
    raw = '{"protocol_version": 1, "decision": "respond"}'
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_unknown_extra_field_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "respond", "response": "x", "confidence": 0.9,
    })
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_cross_branch_field_rejected():
    # "reason" belongs to cannot_complete, not respond.
    raw = json.dumps({
        "protocol_version": 1, "decision": "respond", "response": "x", "reason": "y",
    })
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_select_candidate_with_tool_name_field_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate", "candidate_id": "candidate_1",
        "user_summary": "x", "tool_name": "open_application",
    })
    _assert_failure(parse_decision(raw, (_candidate(),)), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_select_candidate_with_resource_key_field_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate", "candidate_id": "candidate_1",
        "user_summary": "x", "resource_key": "notepad",
    })
    _assert_failure(parse_decision(raw, (_candidate(),)), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_select_candidate_with_arguments_field_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate", "candidate_id": "candidate_1",
        "user_summary": "x", "arguments": {"resource_key": "notepad"},
    })
    _assert_failure(parse_decision(raw, (_candidate(),)), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_plan_or_steps_field_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "respond", "response": "x", "plan": ["a", "b"],
    })
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


# --- empty / blank / oversized text -------------------------------------------


def test_null_required_text_rejected():
    raw = json.dumps({"protocol_version": 1, "decision": "respond", "response": None})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_empty_string_required_text_rejected():
    raw = json.dumps({"protocol_version": 1, "decision": "respond", "response": ""})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_blank_whitespace_only_text_rejected():
    raw = json.dumps({"protocol_version": 1, "decision": "respond", "response": "   "})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_oversized_response_string_rejected():
    raw = json.dumps({"protocol_version": 1, "decision": "respond", "response": "x" * 4000})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.RESPONSE_TOO_LARGE)


def test_oversized_text_field_within_bounded_response_rejected():
    # Below the whole-response 4000-char cap, but above the 800-char
    # per-field cap.
    raw = json.dumps({"protocol_version": 1, "decision": "respond", "response": "x" * 900})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_boolean_where_string_expected_rejected():
    raw = json.dumps({"protocol_version": 1, "decision": "respond", "response": True})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_array_where_string_expected_rejected():
    raw = json.dumps({"protocol_version": 1, "decision": "respond", "response": ["x"]})
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


# --- excessive nesting ---------------------------------------------------


def test_excessive_nesting_rejected():
    nested = '{"a": ' * 20 + "1" + "}" * 20
    raw = f'{{"protocol_version": 1, "decision": "respond", "response": {nested}}}'
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.EXCESSIVE_NESTING)


def test_nesting_at_exactly_the_limit_is_not_excessive_nesting():
    # The enclosing top-level object is itself depth 1, so 7 levels of
    # array nesting inside it reaches exactly depth 8 (the limit) without
    # exceeding it. The JSON will still fail schema validation (wrong
    # shape), but must not be misreported as EXCESSIVE_NESTING.
    nested = "[" * 7 + "1" + "]" * 7
    raw = f'{{"protocol_version": 1, "decision": "respond", "response": {nested}}}'
    result = parse_decision(raw, ())
    assert isinstance(result, ParseFailure)
    assert result.error != ParseErrorCode.EXCESSIVE_NESTING


# --- empty / non-string / oversized raw response -----------------------------


def test_empty_response_rejected():
    _assert_failure(parse_decision("", ()), ParseErrorCode.EMPTY_RESPONSE)


def test_whitespace_only_response_rejected():
    _assert_failure(parse_decision("   \n  ", ()), ParseErrorCode.EMPTY_RESPONSE)


def test_non_string_response_rejected():
    _assert_failure(parse_decision(None, ()), ParseErrorCode.EMPTY_RESPONSE)  # type: ignore[arg-type]


def test_oversized_raw_response_rejected():
    body = json.dumps({"protocol_version": 1, "decision": "respond", "response": "x"})
    raw = body + " " * 5000
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.RESPONSE_TOO_LARGE)


# --- select_candidate resolution ----------------------------------------------


def test_select_candidate_with_unknown_id_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_99", "user_summary": "x",
    })
    _assert_failure(parse_decision(raw, (_candidate(),)), ParseErrorCode.UNKNOWN_CANDIDATE)


def test_select_candidate_with_empty_candidate_tuple_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1", "user_summary": "x",
    })
    _assert_failure(parse_decision(raw, ()), ParseErrorCode.UNKNOWN_CANDIDATE)


def test_candidate_id_prefix_trick_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1_extra", "user_summary": "x",
    })
    _assert_failure(parse_decision(raw, (_candidate(),)), ParseErrorCode.UNKNOWN_CANDIDATE)


def test_candidate_id_case_change_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "CANDIDATE_1", "user_summary": "x",
    })
    _assert_failure(parse_decision(raw, (_candidate(),)), ParseErrorCode.UNKNOWN_CANDIDATE)


def test_candidate_id_with_whitespace_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": " candidate_1 ", "user_summary": "x",
    })
    _assert_failure(parse_decision(raw, (_candidate(),)), ParseErrorCode.UNKNOWN_CANDIDATE)


def test_oversized_candidate_id_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1" + "x" * 100, "user_summary": "x",
    })
    _assert_failure(parse_decision(raw, (_candidate(),)), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


def test_candidate_id_copied_into_user_text_does_not_matter():
    # The parser only ever resolves against the candidates actually
    # supplied to it for this call - text the user typed has no separate
    # channel of influence here (that's Stage A/B's concern, not the
    # parser's), so this is really just re-confirming resolution is by
    # supplied-candidate-set membership only.
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1", "user_summary": "x",
    })
    result = parse_decision(raw, (_candidate(candidate_id="candidate_1"),))
    assert isinstance(result, ParseSuccess)
    result_unknown = parse_decision(raw, (_candidate(candidate_id="candidate_2"),))
    _assert_failure(result_unknown, ParseErrorCode.UNKNOWN_CANDIDATE)


def test_returned_candidate_is_the_exact_supplied_immutable_object():
    candidate = _candidate()
    other_equal_candidate = _candidate()  # equal by value, different object
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1", "user_summary": "x",
    })
    result = parse_decision(raw, (candidate,))
    assert result.decision.candidate is candidate
    assert result.decision.candidate is not other_equal_candidate


def test_duplicate_candidate_id_in_supplied_set_rejected():
    duplicated = (_candidate(candidate_id="candidate_1"), _candidate(candidate_id="candidate_1"))
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1", "user_summary": "x",
    })
    _assert_failure(parse_decision(raw, duplicated), ParseErrorCode.INVALID_CANDIDATE_SET)


def test_select_candidate_missing_or_blank_user_summary_rejected():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1", "user_summary": "",
    })
    _assert_failure(parse_decision(raw, (_candidate(),)), ParseErrorCode.SCHEMA_VALIDATION_FAILED)


# --- no execution / no confirmation access ------------------------------------


def test_parser_module_imports_no_executor_or_confirmation_store():
    import inspect

    import kernel.action_protocol.parser as module

    source = inspect.getsource(module)
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for forbidden in ("kernel.tools.executor", "kernel.tools.confirmation", "SafeTaskExecutor"):
        assert not any(forbidden in line for line in import_lines)


def test_parser_never_raises_for_any_malformed_input():
    bad_inputs = [
        "", None, "not json", "{}", "[]", "42",
        '{"protocol_version": 1}',
        '{"protocol_version": 1, "decision": "select_candidate"}',
        "```json\nnot json\n```",
    ]
    for raw in bad_inputs:
        result = parse_decision(raw, ())  # type: ignore[arg-type]
        assert isinstance(result, ParseFailure)


def test_failure_detail_never_echoes_raw_model_text():
    secret_marker = "a-very-specific-secret-token-xyz"
    raw = f"{secret_marker} this is not json"
    result = parse_decision(raw, ())
    assert isinstance(result, ParseFailure)
    assert secret_marker not in result.detail


# --- Milestone 39 revision: selection stays proposal-only -------------------
#
# Regression coverage for the prompt-policy revision: a successful
# select_candidate parse must never be, or be confused with, a record that
# an action ran or that confirmation was granted - those concepts have no
# field anywhere in this closed type, no matter what the model outputs.


def test_select_candidate_decision_has_no_execution_or_confirmation_field():
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1", "user_summary": "x",
    })
    result = parse_decision(raw, (_candidate(),))
    assert isinstance(result, ParseSuccess)
    field_names = set(vars(result.decision))
    assert field_names == {"protocol_version", "candidate", "user_summary"}


def test_selecting_a_sensitive_candidate_does_not_alter_or_grant_confirmation():
    sensitive_candidate = _candidate(candidate_id="candidate_1")
    assert sensitive_candidate.sensitive is True  # test fixture default
    raw = json.dumps({
        "protocol_version": 1, "decision": "select_candidate",
        "candidate_id": "candidate_1", "user_summary": "x",
    })
    result = parse_decision(raw, (sensitive_candidate,))
    assert isinstance(result, ParseSuccess)
    # The exact, unmodified candidate object is returned - sensitivity is
    # unchanged and nothing about "confirmed" exists on it to have been set.
    assert result.decision.candidate is sensitive_candidate
    assert result.decision.candidate.sensitive is True
    assert not hasattr(result.decision.candidate, "confirmed")
    assert not hasattr(result.decision, "confirmed")
