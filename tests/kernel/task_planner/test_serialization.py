"""Tests for kernel/task_planner/serialization.py: serialize_plan()/
deserialize_plan(). Pure module - no I/O, no model call, no execution."""

import json

import pytest

from kernel.task_planner.serialization import (
    PlanDeserializationError,
    PlanSerializationError,
    deserialize_plan,
    serialize_plan,
)
from kernel.employee_tasks import MAX_PLAN_JSON_CHARS
from kernel.task_planner.types import PlanStep, StepKind, TaskPlan


def _sample_plan() -> TaskPlan:
    return TaskPlan(
        plan_version=1,
        task_id="0198c1e0-0000-7000-8000-000000000000",
        objective="Check the system status.",
        steps=(
            PlanStep(
                step_id="step_1",
                position=1,
                kind=StepKind.ACTION,
                action_name="system_status",
                resource_key=None,
                catalog_id="action_1",
                description="Check system status.",
                expected_result="Status known.",
                depends_on=(),
                requires_confirmation=False,
            ),
            PlanStep(
                step_id="step_2",
                position=2,
                kind=StepKind.RESPOND,
                action_name=None,
                resource_key=None,
                catalog_id=None,
                description="Summarize the result.",
                expected_result="Summary presented.",
                depends_on=(1,),
                requires_confirmation=False,
            ),
        ),
        created_at="2026-08-08T00:00:00+00:00",
    )


def test_round_trip_preserves_every_field():
    plan = _sample_plan()
    round_tripped = deserialize_plan(serialize_plan(plan))
    assert round_tripped == plan


def test_round_trip_preserves_task_id():
    # Named explicitly, separate from the whole-object equality check
    # above: task_id is the field a future execution consumer (Milestone
    # 42) must cross-check against the TaskRecord it loaded the plan from
    # before executing anything - see this module's own "TASK IDENTITY"
    # docstring section.
    plan = _sample_plan()
    round_tripped = deserialize_plan(serialize_plan(plan))
    assert round_tripped.task_id == plan.task_id


def test_serialized_json_embeds_task_id_verbatim():
    plan = _sample_plan()
    parsed = json.loads(serialize_plan(plan))
    assert parsed["task_id"] == plan.task_id


def test_serialized_output_is_deterministic():
    plan = _sample_plan()
    first = serialize_plan(plan)
    second = serialize_plan(plan)
    assert first == second


def test_serialized_output_is_valid_json():
    plan = _sample_plan()
    parsed = json.loads(serialize_plan(plan))
    assert parsed["plan_version"] == 1
    assert parsed["task_id"] == plan.task_id
    assert len(parsed["steps"]) == 2


def test_serialized_output_has_sorted_keys():
    plan = _sample_plan()
    raw = serialize_plan(plan)
    parsed = json.loads(raw)
    assert list(parsed.keys()) == sorted(parsed.keys())


def test_oversized_plan_raises_serialization_error():
    huge_objective = "x" * (MAX_PLAN_JSON_CHARS + 1)
    plan = TaskPlan(
        plan_version=1,
        task_id="t",
        objective=huge_objective,
        steps=(
            PlanStep(
                step_id="step_1",
                position=1,
                kind=StepKind.ACTION,
                action_name="system_status",
                resource_key=None,
                catalog_id="action_1",
                description="x",
                expected_result="y",
                depends_on=(),
                requires_confirmation=False,
            ),
        ),
        created_at="2026-08-08T00:00:00+00:00",
    )
    with pytest.raises(PlanSerializationError):
        serialize_plan(plan)


def test_deserialize_malformed_json_fails_closed_with_domain_error():
    with pytest.raises(PlanDeserializationError):
        deserialize_plan("not json at all")


def test_deserialize_non_object_json_fails_closed():
    with pytest.raises(PlanDeserializationError):
        deserialize_plan("[1, 2, 3]")


def test_deserialize_missing_field_fails_closed():
    with pytest.raises(PlanDeserializationError):
        deserialize_plan(json.dumps({"plan_version": 1, "task_id": "t"}))


def test_deserialize_missing_step_field_fails_closed():
    payload = {
        "plan_version": 1,
        "task_id": "t",
        "objective": "x",
        "created_at": "2026-08-08T00:00:00+00:00",
        "steps": [{"step_id": "step_1"}],  # missing every other field
    }
    with pytest.raises(PlanDeserializationError):
        deserialize_plan(json.dumps(payload))


def test_deserialize_invalid_step_kind_fails_closed():
    payload = {
        "plan_version": 1,
        "task_id": "t",
        "objective": "x",
        "created_at": "2026-08-08T00:00:00+00:00",
        "steps": [
            {
                "step_id": "step_1",
                "position": 1,
                "kind": "not_a_real_kind",
                "action_name": None,
                "resource_key": None,
                "catalog_id": None,
                "description": "x",
                "expected_result": "y",
                "depends_on": [],
                "requires_confirmation": False,
            }
        ],
    }
    with pytest.raises(PlanDeserializationError):
        deserialize_plan(json.dumps(payload))


def test_deserialize_wrong_type_for_steps_fails_closed():
    payload = {
        "plan_version": 1,
        "task_id": "t",
        "objective": "x",
        "created_at": "2026-08-08T00:00:00+00:00",
        "steps": "not a list",
    }
    with pytest.raises(PlanDeserializationError):
        deserialize_plan(json.dumps(payload))


def test_deserialization_failure_is_never_a_planner_failure():
    # Deliberately import PlannerFailure locally to prove
    # PlanDeserializationError shares no type relationship with it -
    # storage corruption must never be representable as, or mistaken for,
    # a model-planning failure (see this module's own docstring).
    from kernel.task_planner.types import PlannerFailure

    with pytest.raises(PlanDeserializationError) as exc_info:
        deserialize_plan("garbage")

    assert not isinstance(exc_info.value, PlannerFailure)
    assert not issubclass(PlanDeserializationError, type(PlannerFailure))


def test_deserialize_never_raises_a_bare_json_or_key_error():
    # Every malformed-input path must surface as PlanDeserializationError
    # specifically - never a raw json.JSONDecodeError/KeyError/TypeError/
    # ValueError escaping to the caller.
    for garbage in ["", "null", "true", "42", "{}", "[]", "\x00\x01", "🎉" * 10]:
        with pytest.raises(PlanDeserializationError):
            deserialize_plan(garbage)
