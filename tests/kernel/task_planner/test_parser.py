"""Tests for kernel/task_planner/parser.py: the strict, complete-response
planner-protocol parser. Pure module - no I/O, no model call, no
execution - mirrors tests/kernel/action_protocol/test_parser.py's
adversarial coverage for the analogous Milestone 39 parser."""

import json

import pytest

from kernel.task_planner.parser import parse_plan_response
from kernel.task_planner.types import (
    CannotPlan,
    CatalogEntry,
    MAX_JSON_NESTING_DEPTH,
    MAX_RESPONSE_CHARS,
    ParsedPlan,
    PlannerErrorCode,
    PlannerFailure,
    StepKind,
)

_CATALOG = (
    CatalogEntry(
        catalog_id="action_1",
        action_name="system_status",
        resource_key=None,
        sensitive=False,
        summary="Check the current system status.",
    ),
    CatalogEntry(
        catalog_id="action_2",
        action_name="repo_health",
        resource_key="ai_os",
        sensitive=False,
        summary="Check the health of the registered 'ai_os' repository.",
    ),
    CatalogEntry(
        catalog_id="action_3",
        action_name="run_registered_script",
        resource_key="whatsapp_test",
        sensitive=True,
        summary="Run the registered 'whatsapp_test' script.",
    ),
    CatalogEntry(
        catalog_id="action_4",
        action_name="repository_backup",
        resource_key="ai_os",
        sensitive=True,
        summary="Back up the registered 'ai_os' repository.",
    ),
)


def _assert_failure(result, error: PlannerErrorCode):
    assert isinstance(result, PlannerFailure), result
    assert result.error == error


def _valid_plan_dict(**overrides):
    base = {
        "plan_version": 1,
        "result": "plan",
        "objective": "Check the system status.",
        "steps": [
            {
                "step_kind": "action",
                "catalog_id": "action_1",
                "description": "Check the current system status.",
                "expected_result": "System status is known.",
                "depends_on": [],
            }
        ],
    }
    base.update(overrides)
    return base


def _valid_cannot_plan_dict(**overrides):
    base = {"plan_version": 1, "result": "cannot_plan", "reason": "Ambiguous request."}
    base.update(overrides)
    return base


# --- happy paths -----------------------------------------------------------


def test_valid_single_action_plan():
    result = parse_plan_response(json.dumps(_valid_plan_dict()), _CATALOG)
    assert isinstance(result, ParsedPlan)
    assert result.plan_version == 1
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.kind is StepKind.ACTION
    assert step.action_name == "system_status"
    assert step.catalog_id == "action_1"
    assert step.requires_confirmation is False
    assert step.step_id == "step_1"
    assert step.position == 1


def test_valid_cannot_plan():
    result = parse_plan_response(json.dumps(_valid_cannot_plan_dict()), _CATALOG)
    assert result == CannotPlan(plan_version=1, reason="Ambiguous request.")


def test_requires_confirmation_derived_true_for_sensitive_action():
    plan = _valid_plan_dict(
        steps=[
            {
                "step_kind": "action",
                "catalog_id": "action_4",
                "description": "Back up the ai_os repository.",
                "expected_result": "Backup created.",
                "depends_on": [],
            }
        ]
    )
    result = parse_plan_response(json.dumps(plan), _CATALOG)
    assert isinstance(result, ParsedPlan)
    assert result.steps[0].requires_confirmation is True


def test_multi_step_plan_with_backward_dependency_and_respond_step():
    plan = _valid_plan_dict(
        objective="Check repository health, then back it up, then summarize.",
        steps=[
            {
                "step_kind": "action",
                "catalog_id": "action_2",
                "description": "Check repository health.",
                "expected_result": "Health known.",
                "depends_on": [],
            },
            {
                "step_kind": "action",
                "catalog_id": "action_4",
                "description": "Back up the repository.",
                "expected_result": "Backup created.",
                "depends_on": [1],
            },
            {
                "step_kind": "respond",
                "description": "Summarize the results.",
                "expected_result": "Summary presented.",
                "depends_on": [1, 2],
            },
        ],
    )
    result = parse_plan_response(json.dumps(plan), _CATALOG)
    assert isinstance(result, ParsedPlan)
    assert [s.step_id for s in result.steps] == ["step_1", "step_2", "step_3"]
    assert result.steps[1].depends_on == (1,)
    assert result.steps[2].depends_on == (1, 2)
    assert result.steps[2].kind is StepKind.RESPOND
    assert result.steps[2].action_name is None
    assert result.steps[2].requires_confirmation is False


def test_whole_response_json_fence_is_stripped():
    raw = "```json\n" + json.dumps(_valid_plan_dict()) + "\n```"
    result = parse_plan_response(raw, _CATALOG)
    assert isinstance(result, ParsedPlan)


# --- fail-closed: malformed / structural ------------------------------------


def test_empty_response_fails_closed():
    _assert_failure(parse_plan_response("", _CATALOG), PlannerErrorCode.EMPTY_RESPONSE)
    _assert_failure(parse_plan_response("   ", _CATALOG), PlannerErrorCode.EMPTY_RESPONSE)


def test_response_too_large_fails_closed():
    huge = json.dumps(_valid_plan_dict(objective="x" * MAX_RESPONSE_CHARS))
    _assert_failure(parse_plan_response(huge, _CATALOG), PlannerErrorCode.RESPONSE_TOO_LARGE)


def test_malformed_json_fails_closed():
    _assert_failure(parse_plan_response("{not json", _CATALOG), PlannerErrorCode.MALFORMED_PLAN)


def test_prose_before_json_fails_closed():
    raw = "Sure, here is the plan:\n" + json.dumps(_valid_plan_dict())
    _assert_failure(parse_plan_response(raw, _CATALOG), PlannerErrorCode.MALFORMED_PLAN)


def test_prose_after_json_fails_closed():
    raw = json.dumps(_valid_plan_dict()) + "\nLet me know if you need anything else."
    _assert_failure(parse_plan_response(raw, _CATALOG), PlannerErrorCode.MALFORMED_PLAN)


def test_fence_inside_prose_is_not_stripped_and_fails_closed():
    raw = "Here you go:\n```json\n" + json.dumps(_valid_plan_dict()) + "\n```\nThanks!"
    _assert_failure(parse_plan_response(raw, _CATALOG), PlannerErrorCode.MALFORMED_PLAN)


def test_duplicate_key_fails_closed():
    raw = '{"plan_version": 1, "plan_version": 1, "result": "cannot_plan", "reason": "x"}'
    _assert_failure(parse_plan_response(raw, _CATALOG), PlannerErrorCode.DUPLICATE_KEY)


def test_nan_and_infinity_fail_closed():
    raw = '{"plan_version": NaN, "result": "cannot_plan", "reason": "x"}'
    _assert_failure(parse_plan_response(raw, _CATALOG), PlannerErrorCode.INVALID_CONSTANT)
    raw2 = '{"plan_version": 1, "result": "cannot_plan", "reason": Infinity}'
    _assert_failure(parse_plan_response(raw2, _CATALOG), PlannerErrorCode.INVALID_CONSTANT)


def test_excessive_nesting_fails_closed():
    # Real JSON structural nesting (never bracket characters inside a
    # string literal, which _max_nesting_depth() correctly ignores).
    nested_value = "[" * (MAX_JSON_NESTING_DEPTH + 1) + "]" * (MAX_JSON_NESTING_DEPTH + 1)
    raw = f'{{"plan_version": 1, "result": "cannot_plan", "reason": {nested_value}}}'
    _assert_failure(parse_plan_response(raw, _CATALOG), PlannerErrorCode.EXCESSIVE_NESTING)


def test_top_level_array_is_not_an_object():
    _assert_failure(parse_plan_response("[1, 2, 3]", _CATALOG), PlannerErrorCode.MALFORMED_PLAN)


def test_missing_plan_version_fails_closed():
    raw = json.dumps({"result": "cannot_plan", "reason": "x"})
    _assert_failure(
        parse_plan_response(raw, _CATALOG), PlannerErrorCode.UNSUPPORTED_PLAN_VERSION
    )


def test_wrong_plan_version_fails_closed():
    raw = json.dumps(_valid_cannot_plan_dict(plan_version=2))
    _assert_failure(
        parse_plan_response(raw, _CATALOG), PlannerErrorCode.UNSUPPORTED_PLAN_VERSION
    )


def test_boolean_plan_version_fails_closed():
    raw = json.dumps(_valid_cannot_plan_dict(plan_version=True))
    _assert_failure(
        parse_plan_response(raw, _CATALOG), PlannerErrorCode.UNSUPPORTED_PLAN_VERSION
    )


def test_unrecognized_result_discriminator_fails_closed():
    raw = json.dumps({"plan_version": 1, "result": "execute_now", "reason": "x"})
    _assert_failure(parse_plan_response(raw, _CATALOG), PlannerErrorCode.MALFORMED_PLAN)


@pytest.mark.parametrize(
    "extra_field", ["confidence", "chain_of_thought", "metadata", "reasoning"]
)
def test_unknown_top_level_field_fails_closed(extra_field):
    raw = json.dumps(_valid_cannot_plan_dict(**{extra_field: "x"}))
    _assert_failure(
        parse_plan_response(raw, _CATALOG), PlannerErrorCode.SCHEMA_VALIDATION_FAILED
    )


def test_cross_branch_field_fails_closed():
    # A "reason" field (cannot_plan's field) present on a "plan" result.
    raw = json.dumps({**_valid_plan_dict(), "reason": "should not be here"})
    _assert_failure(
        parse_plan_response(raw, _CATALOG), PlannerErrorCode.SCHEMA_VALIDATION_FAILED
    )


def test_missing_objective_fails_closed():
    raw = json.dumps({"plan_version": 1, "result": "plan", "steps": []})
    _assert_failure(
        parse_plan_response(raw, _CATALOG), PlannerErrorCode.SCHEMA_VALIDATION_FAILED
    )


def test_oversized_objective_fails_closed():
    raw = json.dumps(_valid_plan_dict(objective="x" * 600))
    _assert_failure(
        parse_plan_response(raw, _CATALOG), PlannerErrorCode.SCHEMA_VALIDATION_FAILED
    )


def test_oversized_reason_fails_closed():
    raw = json.dumps(_valid_cannot_plan_dict(reason="x" * 600))
    _assert_failure(
        parse_plan_response(raw, _CATALOG), PlannerErrorCode.SCHEMA_VALIDATION_FAILED
    )


def test_empty_steps_fails_closed():
    raw = json.dumps(_valid_plan_dict(steps=[]))
    _assert_failure(parse_plan_response(raw, _CATALOG), PlannerErrorCode.TOO_MANY_STEPS)


def test_more_than_max_steps_fails_closed():
    one_step = _valid_plan_dict()["steps"][0]
    raw = json.dumps(_valid_plan_dict(steps=[one_step] * 9))
    _assert_failure(parse_plan_response(raw, _CATALOG), PlannerErrorCode.TOO_MANY_STEPS)


# --- fail-closed: step-level ------------------------------------------------


def test_unknown_catalog_id_fails_closed():
    plan = _valid_plan_dict(
        steps=[
            {
                "step_kind": "action",
                "catalog_id": "action_999",
                "description": "x",
                "expected_result": "y",
                "depends_on": [],
            }
        ]
    )
    _assert_failure(parse_plan_response(json.dumps(plan), _CATALOG), PlannerErrorCode.UNKNOWN_ACTION)


def test_catalog_id_not_in_this_call_catalog_fails_closed():
    # Structurally valid catalog_id shape, but not part of the exact
    # catalog tuple supplied to THIS parse call - defense in depth even
    # though the schema (built from the same tuple) should already make
    # this unreachable.
    plan = _valid_plan_dict()
    smaller_catalog = _CATALOG[1:]  # drops action_1
    _assert_failure(
        parse_plan_response(json.dumps(plan), smaller_catalog), PlannerErrorCode.UNKNOWN_ACTION
    )


def test_self_dependency_fails_closed():
    plan = _valid_plan_dict(
        steps=[
            {
                "step_kind": "action",
                "catalog_id": "action_1",
                "description": "x",
                "expected_result": "y",
                "depends_on": [1],
            }
        ]
    )
    _assert_failure(
        parse_plan_response(json.dumps(plan), _CATALOG), PlannerErrorCode.INVALID_DEPENDENCY
    )


def test_forward_dependency_fails_closed():
    plan = _valid_plan_dict(
        steps=[
            {
                "step_kind": "action",
                "catalog_id": "action_1",
                "description": "x",
                "expected_result": "y",
                "depends_on": [2],
            },
            {
                "step_kind": "action",
                "catalog_id": "action_2",
                "description": "x",
                "expected_result": "y",
                "depends_on": [],
            },
        ]
    )
    _assert_failure(
        parse_plan_response(json.dumps(plan), _CATALOG), PlannerErrorCode.INVALID_DEPENDENCY
    )


def test_duplicate_dependency_fails_closed():
    plan = _valid_plan_dict(
        steps=[
            {
                "step_kind": "action",
                "catalog_id": "action_1",
                "description": "x",
                "expected_result": "y",
                "depends_on": [],
            },
            {
                "step_kind": "action",
                "catalog_id": "action_2",
                "description": "x",
                "expected_result": "y",
                "depends_on": [1, 1],
            },
        ]
    )
    _assert_failure(
        parse_plan_response(json.dumps(plan), _CATALOG), PlannerErrorCode.INVALID_DEPENDENCY
    )


def test_too_many_dependencies_fails_closed():
    steps = [
        {
            "step_kind": "action",
            "catalog_id": "action_1",
            "description": "x",
            "expected_result": "y",
            "depends_on": [],
        }
    ] * 5
    steps = [dict(s, depends_on=[]) for s in steps]
    steps.append(
        {
            "step_kind": "respond",
            "description": "x",
            "expected_result": "y",
            "depends_on": [1, 2, 3, 4, 5],
        }
    )
    _assert_failure(
        parse_plan_response(json.dumps(_valid_plan_dict(steps=steps)), _CATALOG),
        PlannerErrorCode.INVALID_DEPENDENCY,
    )


def test_respond_only_plan_fails_closed():
    plan = _valid_plan_dict(
        steps=[
            {
                "step_kind": "respond",
                "description": "Just a conversational reply.",
                "expected_result": "Answer given.",
                "depends_on": [],
            }
        ]
    )
    _assert_failure(parse_plan_response(json.dumps(plan), _CATALOG), PlannerErrorCode.INVALID_STEP)


def test_oversized_step_description_fails_closed():
    plan = _valid_plan_dict(
        steps=[
            {
                "step_kind": "action",
                "catalog_id": "action_1",
                "description": "x" * 300,
                "expected_result": "y",
                "depends_on": [],
            }
        ]
    )
    _assert_failure(parse_plan_response(json.dumps(plan), _CATALOG), PlannerErrorCode.INVALID_STEP)


@pytest.mark.parametrize(
    "forbidden_field", ["requires_confirmation", "confirmed", "approved", "confirmation_granted"]
)
def test_model_cannot_set_a_confirmation_field(forbidden_field):
    plan = _valid_plan_dict(
        steps=[
            {
                "step_kind": "action",
                "catalog_id": "action_1",
                "description": "x",
                "expected_result": "y",
                "depends_on": [],
                forbidden_field: True,
            }
        ]
    )
    _assert_failure(parse_plan_response(json.dumps(plan), _CATALOG), PlannerErrorCode.INVALID_STEP)


def test_action_step_missing_catalog_id_fails_closed():
    plan = _valid_plan_dict(
        steps=[
            {
                "step_kind": "action",
                "description": "x",
                "expected_result": "y",
                "depends_on": [],
            }
        ]
    )
    _assert_failure(parse_plan_response(json.dumps(plan), _CATALOG), PlannerErrorCode.INVALID_STEP)


def test_never_raises_on_arbitrary_garbage():
    for garbage in [None, "", "null", "true", "42", "{}", "[]", "\x00\x01\x02", "🎉" * 50]:
        result = parse_plan_response(garbage, _CATALOG)
        assert isinstance(result, PlannerFailure)


# --- M1 regression: known accepted semantic limitation ----------------------


def test_m1_regression_generic_test_request_maps_to_named_script_but_stays_structurally_safe():
    """Regression fixture: the EXACT raw response gemma3:12b produced for
    corpus item M1 during the Milestone 41 model evaluation ("Check the
    AI-OS repository, run the tests, create a backup if the tests pass, and
    summarize the result.") - see docs/architecture.md's Milestone 41
    section. The model mapped generic "run the tests" to the single
    registered run_registered_script entry ("whatsapp_test"), which is not
    necessarily what the user meant.

    This is a KNOWN, ACCEPTED model-semantics limitation - not a parser
    bug, and not something this parser should try to catch (see this
    module's docstring: judging whether a chosen, real, registered
    catalog_id is the *semantically* correct one for free-text request is
    the model's job, constrained by prompt.py's Rules 1/2, not a structural
    property a deterministic parser can check without re-introducing
    natural-language guessing).

    What THIS test pins, permanently, as the actual safety guarantee: even
    in this exact known-imperfect case, (1) every selected catalog_id is
    real and registered - never invented, (2) the sensitive
    run_registered_script step's requires_confirmation is True, derived
    from the registry - so nothing about this plan could ever execute
    without an explicit human confirmation, regardless of whether the
    model's semantic choice was the one the user actually meant."""

    catalog = (
        CatalogEntry("action_7", "repo_health", "ai_os", False, "Check the health of the registered 'ai_os' repository."),
        CatalogEntry("action_6", "run_registered_script", "whatsapp_test", True, "Run the registered 'whatsapp_test' script."),
        CatalogEntry("action_8", "repository_backup", "ai_os", True, "Back up the registered 'ai_os' repository."),
    )
    raw_gemma3_response = (
        '{"plan_version": 1, "result": "plan", "objective": "Check the AI-OS repository health, '
        'run WhatsApp tests, back up the repository if tests succeed, and provide a summary.", '
        '"steps": [{"step_kind": "action", "catalog_id": "action_7", "description": "Check the '
        'health of the AI-OS repository.", "expected_result": "Repository health status is '
        'known.", "depends_on": []}, {"step_kind": "action", "catalog_id": "action_6", '
        '"description": "Run the WhatsApp tests.", "expected_result": "WhatsApp test results are '
        'available.", "depends_on": [1]}, {"step_kind": "action", "catalog_id": "action_8", '
        '"description": "Back up the AI-OS repository if tests passed.", "expected_result": '
        '"Repository backup is created (conditionally).", "depends_on": [2]}, {"step_kind": '
        '"respond", "description": "Summarize the results of the health check, test run, and '
        'conditional backup.", "expected_result": "A concise summary of all actions performed and '
        'their outcomes is presented to the user.", "depends_on": [1, 2]}]}'
    )

    result = parse_plan_response(raw_gemma3_response, catalog)

    assert isinstance(result, ParsedPlan)
    assert len(result.steps) == 4

    script_step = result.steps[1]
    assert script_step.action_name == "run_registered_script"
    assert script_step.resource_key == "whatsapp_test"
    # The known-imperfect semantic substitution, pinned: the model chose
    # this real, registered script for a generic "run the tests" request.
    assert script_step.description == "Run the WhatsApp tests."

    # The actual safety guarantee this test exists to protect, regardless
    # of the semantic mismatch above:
    assert script_step.requires_confirmation is True
    for step in result.steps:
        if step.catalog_id is not None:
            assert step.catalog_id in {"action_7", "action_6", "action_8"}
