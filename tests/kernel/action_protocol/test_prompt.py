"""Tests for kernel/action_protocol/prompt.py: dynamic per-request prompt
and JSON Schema construction for Stage B. Pure module - no I/O, no model
call - every candidate here is constructed directly, independent of
Stage A's resolver.
"""

import json

import pytest

from kernel.action_protocol.prompt import build_prompt, build_schema
from kernel.action_protocol.types import MAX_CANDIDATES, MAX_FIELD_CHARS, ActionCandidate
from kernel.tools.types import ActionRequest


def _candidate(candidate_id="candidate_1", action="open_application", resource_key="notepad",
               sensitive=True, user_summary="Open the registered 'notepad' application."):
    return ActionCandidate(
        candidate_id=candidate_id,
        action_request=ActionRequest(action=action, resource_key=resource_key),
        sensitive=sensitive,
        user_summary=user_summary,
    )


# --- determinism ---------------------------------------------------------


def test_build_prompt_is_deterministic_for_the_same_input():
    candidates = (_candidate(),)
    first = build_prompt("Open notepad.", candidates)
    second = build_prompt("Open notepad.", candidates)
    assert first == second


def test_build_schema_is_deterministic_for_the_same_input():
    candidates = (_candidate(),)
    assert build_schema(candidates) == build_schema(candidates)


def test_prompt_contains_the_raw_user_request_verbatim():
    prompt = build_prompt("Open the 'notepad' application.", ())
    assert "Open the 'notepad' application." in prompt


# --- candidate rendering / ordering ---------------------------------------


def test_prompt_lists_candidates_in_supplied_order():
    candidates = (
        _candidate(candidate_id="candidate_1", user_summary="First summary."),
        _candidate(candidate_id="candidate_2", user_summary="Second summary."),
    )
    prompt = build_prompt("do something", candidates)
    assert prompt.index("candidate_1") < prompt.index("candidate_2")
    assert prompt.index("First summary.") < prompt.index("Second summary.")


def test_prompt_shows_candidate_id_summary_and_sensitivity_only():
    candidate = _candidate(sensitive=True)
    prompt = build_prompt("open notepad", (candidate,))
    assert 'candidate_id="candidate_1"' in prompt
    assert candidate.user_summary in prompt
    assert "sensitive=yes" in prompt


def test_prompt_shows_sensitive_no_for_non_sensitive_candidate():
    candidate = _candidate(sensitive=False, user_summary="Check system status.")
    prompt = build_prompt("check status", (candidate,))
    assert "sensitive=no" in prompt


def test_prompt_states_no_candidates_available_when_empty():
    prompt = build_prompt("What is the capital of France?", ())
    assert "select_candidate" in prompt  # rule text still explains the decision kind
    assert "No candidates are available" in prompt


# --- schema shape: zero / one / multiple candidates ------------------------


def test_schema_has_three_branches_with_zero_candidates():
    schema = build_schema(())
    decisions = {branch["properties"]["decision"]["const"] for branch in schema["oneOf"]}
    assert decisions == {"respond", "request_clarification", "cannot_complete"}
    assert len(schema["oneOf"]) == 3


def test_select_candidate_branch_omitted_with_zero_candidates():
    schema = build_schema(())
    for branch in schema["oneOf"]:
        assert branch["properties"]["decision"]["const"] != "select_candidate"


def test_schema_has_four_branches_with_one_candidate():
    schema = build_schema((_candidate(),))
    decisions = {branch["properties"]["decision"]["const"] for branch in schema["oneOf"]}
    assert decisions == {"respond", "select_candidate", "request_clarification", "cannot_complete"}
    assert len(schema["oneOf"]) == 4


def test_schema_has_four_branches_with_multiple_candidates():
    candidates = (
        _candidate(candidate_id="candidate_1"),
        _candidate(candidate_id="candidate_2", action="run_registered_script",
                   resource_key="daily_report"),
        _candidate(candidate_id="candidate_3", action="repo_health", resource_key="ai-os",
                   sensitive=False),
    )
    schema = build_schema(candidates)
    assert len(schema["oneOf"]) == 4


def test_candidate_id_enum_is_exact_with_one_candidate():
    schema = build_schema((_candidate(candidate_id="candidate_1"),))
    select_branch = next(
        b for b in schema["oneOf"] if b["properties"]["decision"]["const"] == "select_candidate"
    )
    assert select_branch["properties"]["candidate_id"]["enum"] == ["candidate_1"]


def test_candidate_id_enum_is_exact_with_multiple_candidates():
    candidates = (
        _candidate(candidate_id="candidate_1"),
        _candidate(candidate_id="candidate_2", action="repo_health", resource_key="ai-os"),
    )
    schema = build_schema(candidates)
    select_branch = next(
        b for b in schema["oneOf"] if b["properties"]["decision"]["const"] == "select_candidate"
    )
    assert select_branch["properties"]["candidate_id"]["enum"] == ["candidate_1", "candidate_2"]


def test_build_schema_rejects_more_than_max_candidates():
    too_many = tuple(
        _candidate(candidate_id=f"candidate_{i}") for i in range(MAX_CANDIDATES + 1)
    )
    with pytest.raises(ValueError):
        build_schema(too_many)


# --- schema strictness -----------------------------------------------------


def test_every_branch_sets_additional_properties_false():
    schema = build_schema((_candidate(),))
    for branch in schema["oneOf"]:
        assert branch["additionalProperties"] is False


def test_protocol_version_is_const_one_in_every_branch():
    schema = build_schema((_candidate(),))
    for branch in schema["oneOf"]:
        assert branch["properties"]["protocol_version"] == {"const": 1}


def test_each_branch_required_fields_are_exact():
    schema = build_schema((_candidate(),))
    required_by_decision = {
        branch["properties"]["decision"]["const"]: set(branch["required"])
        for branch in schema["oneOf"]
    }
    assert required_by_decision["respond"] == {"protocol_version", "decision", "response"}
    assert required_by_decision["select_candidate"] == {
        "protocol_version", "decision", "candidate_id", "user_summary"
    }
    assert required_by_decision["request_clarification"] == {
        "protocol_version", "decision", "question"
    }
    assert required_by_decision["cannot_complete"] == {"protocol_version", "decision", "reason"}


def test_response_field_has_maxlength_enforced():
    schema = build_schema(())
    respond_branch = next(
        b for b in schema["oneOf"] if b["properties"]["decision"]["const"] == "respond"
    )
    assert respond_branch["properties"]["response"]["maxLength"] == MAX_FIELD_CHARS


def test_select_candidate_user_summary_has_maxlength_enforced():
    schema = build_schema((_candidate(),))
    select_branch = next(
        b for b in schema["oneOf"] if b["properties"]["decision"]["const"] == "select_candidate"
    )
    assert select_branch["properties"]["user_summary"]["maxLength"] == MAX_FIELD_CHARS


# --- forbidden fields --------------------------------------------------------


def test_schema_never_contains_a_tool_name_field():
    schema = json.dumps(build_schema((_candidate(),)))
    assert "tool_name" not in schema


def test_schema_never_contains_a_resource_key_field():
    schema = json.dumps(build_schema((_candidate(),)))
    assert "resource_key" not in schema


def test_schema_never_contains_an_arguments_field():
    schema = json.dumps(build_schema((_candidate(),)))
    assert '"arguments"' not in schema


def test_schema_never_contains_a_confirmation_or_approval_field():
    schema = json.dumps(build_schema((_candidate(),)))
    for forbidden in ("confirm", "approval", "approved"):
        assert forbidden not in schema.casefold()


def test_candidate_list_rendering_never_shows_a_raw_tool_name_or_resource_key_field():
    # The base rule text legitimately says "never tool_name, never
    # resource_key" (telling the model what NOT to output) - what must
    # never happen is the *candidate list* itself exposing either as a
    # raw field label (brief S14), so this checks that section in
    # isolation via the module's own rendering function.
    from kernel.action_protocol.prompt import _render_candidate_list

    rendered = _render_candidate_list((_candidate(),))
    assert "tool_name" not in rendered
    assert "resource_key" not in rendered


def test_prompt_never_leaks_a_filesystem_path_or_secret():
    candidate = _candidate(user_summary="Open the registered 'notepad' application.")
    prompt = build_prompt("open notepad", (candidate,))
    assert "C:\\" not in prompt
    assert "/etc" not in prompt
    assert "password" not in prompt.casefold()
    assert "secret" not in prompt.casefold()
    assert "token" not in prompt.casefold()


def test_prompt_instructs_no_chain_of_thought_or_plan():
    prompt = build_prompt("anything", ())
    lowered = prompt.casefold()
    assert "chain-of-thought" in lowered or "chain of thought" in lowered
    assert "no plan" in lowered or "never a plan" in lowered


def test_prompt_forbids_markdown_and_extra_text():
    prompt = build_prompt("anything", ())
    lowered = prompt.casefold()
    assert "no markdown" in lowered
    assert "nothing else" in lowered or "exactly one json object" in lowered


# --- Milestone 39 revision: explicit decision precedence -------------------
#
# Regression coverage for the semantic-routing failure the empirical pass
# found: the model using "respond" to fabricate the outcome of an action or
# current-state check it never performed. These tests only verify the
# shipped prompt *text* states the policy - they cannot verify a live
# model's behavior (that is the empirical harness's job, not pytest's).


def test_prompt_states_select_candidate_is_first_priority_and_proposal_only():
    prompt = build_prompt("anything", (_candidate(),)).casefold()
    assert "select_candidate" in prompt
    assert "proposal" in prompt
    assert "nothing has run yet" in prompt


def test_prompt_forbids_respond_from_claiming_a_candidate_action_completed():
    prompt = build_prompt("anything", (_candidate(),)).casefold()
    assert "never describe it" in prompt
    assert "already done, checked, opened, run, or" in prompt


def test_prompt_forbids_respond_from_simulating_an_unsupported_action():
    prompt = build_prompt("anything", ()).casefold()
    assert "must never simulate, invent, or claim the" in prompt
    assert "never use \"respond\" to claim" in prompt
    assert "an unsupported" in prompt


def test_prompt_directs_cannot_complete_when_no_candidate_can_perform_the_action():
    prompt = build_prompt("anything", ()).casefold()
    assert "cannot_complete" in prompt
    assert "no candidate below can do" in prompt


def test_prompt_distinguishes_missing_detail_from_unsupported_action_kind():
    # Regression for the specific residual failure the second empirical
    # pass found: the model asking a clarifying question about an action
    # (delete, format, email, shutdown) that no candidate could ever
    # represent, instead of declining outright.
    prompt = build_prompt("anything", ()).casefold()
    assert "not one a candidate could ever represent" in prompt
    assert "do not ask a clarifying question about the details" in prompt


def test_prompt_directs_clarification_for_missing_or_ambiguous_target():
    prompt = build_prompt("anything", ()).casefold()
    assert "request_clarification" in prompt
    assert "a candidate could exist once you know that detail" in prompt


def test_prompt_includes_worked_examples_for_all_four_decisions():
    prompt = build_prompt("anything", (_candidate(),))
    for marker in (
        "correct: select_candidate",
        "correct: cannot_complete",
        "correct: request_clarification",
        "correct: respond",
    ):
        assert marker in prompt


def test_decision_precedence_order_is_select_then_clarify_then_cannot_complete_then_respond():
    prompt = build_prompt("anything", (_candidate(),))
    assert (
        prompt.index('1. "select_candidate"')
        < prompt.index('2. "request_clarification"')
        < prompt.index('3. "cannot_complete"')
        < prompt.index('4. "respond"')
    )
