"""Tests for kernel/task_execution/observation.py: StepObservation,
build_action_observation(), serialize_observation()/
deserialize_observation()."""

import pytest

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_execution.observation import (
    OBSERVATION_VERSION,
    ObservationDeserializationError,
    ObservationSerializationError,
    StepObservation,
    build_action_observation,
    build_respond_observation,
    deserialize_observation,
    serialize_observation,
)
from kernel.task_planner import StepKind
from kernel.tools.types import ActionResult


def test_build_action_observation_success_maps_fields_verbatim():
    result = ActionResult(success=True, message="System status: healthy.", outcome="executed")
    observation = build_action_observation(3, result, "2026-08-08T00:00:00+00:00")

    assert observation.observation_version == OBSERVATION_VERSION
    assert observation.step_position == 3
    assert observation.step_kind == StepKind.ACTION
    assert observation.success is True
    assert observation.safe_summary == "System status: healthy."
    assert observation.failure_code is None
    assert observation.action_outcome == "executed"
    assert observation.completed_at == "2026-08-08T00:00:00+00:00"


def test_build_action_observation_failure_sets_failure_code_to_outcome():
    result = ActionResult(success=False, message="That action could not be completed.", outcome="failed")
    observation = build_action_observation(1, result, "2026-08-08T00:00:00+00:00")

    assert observation.success is False
    assert observation.failure_code == "failed"
    assert observation.action_outcome == "failed"
    assert observation.safe_summary == "That action could not be completed."


def test_serialize_deserialize_round_trip():
    result = ActionResult(success=True, message="done", outcome="executed")
    observation = build_action_observation(2, result, "2026-08-08T00:00:00+00:00")

    raw = serialize_observation(observation)
    reloaded = deserialize_observation(raw)

    assert reloaded == observation


def test_serialize_observation_is_deterministic():
    result = ActionResult(success=True, message="done", outcome="executed")
    observation = build_action_observation(2, result, "2026-08-08T00:00:00+00:00")

    assert serialize_observation(observation) == serialize_observation(observation)


def test_serialize_observation_rejects_oversized_payload():
    result = ActionResult(success=True, message="x" * (MAX_STEP_RESULT_JSON_CHARS * 2), outcome="executed")
    observation = build_action_observation(1, result, "2026-08-08T00:00:00+00:00")

    with pytest.raises(ObservationSerializationError):
        serialize_observation(observation)


def test_deserialize_observation_rejects_malformed_json():
    with pytest.raises(ObservationDeserializationError):
        deserialize_observation("not json")


def test_deserialize_observation_rejects_non_object_json():
    with pytest.raises(ObservationDeserializationError):
        deserialize_observation("[1, 2, 3]")


def test_deserialize_observation_rejects_missing_field():
    with pytest.raises(ObservationDeserializationError):
        deserialize_observation('{"observation_version": 1}')


def test_deserialize_observation_rejects_unknown_step_kind():
    payload = (
        '{"observation_version":1,"step_position":1,"step_kind":"not_a_kind",'
        '"success":true,"safe_summary":"x","failure_code":null,'
        '"action_outcome":"executed","completed_at":"2026-08-08T00:00:00+00:00"}'
    )
    with pytest.raises(ObservationDeserializationError):
        deserialize_observation(payload)


def test_deserialize_observation_never_raises_arbitrary_exception_types():
    """Mirrors kernel.task_planner.serialization's own contract: every
    malformed-input path collapses to exactly one exception type."""

    garbage_inputs = [
        "",
        "null",
        "true",
        "42",
        '{"observation_version": "not an int", "step_position": 1}',
        '{"step_kind": [], "observation_version": 1, "step_position": 1, '
        '"success": true, "safe_summary": "x", "failure_code": null, '
        '"action_outcome": "x", "completed_at": "x"}',
    ]
    for raw in garbage_inputs:
        with pytest.raises(ObservationDeserializationError):
            deserialize_observation(raw)


def test_build_respond_observation_success_uses_fixed_symbolic_outcome():
    observation = build_respond_observation(
        4,
        success=True,
        safe_summary="Your backup completed successfully.",
        failure_code=None,
        completed_at="2026-08-08T00:00:00+00:00",
    )

    assert observation.observation_version == OBSERVATION_VERSION
    assert observation.step_position == 4
    assert observation.step_kind == StepKind.RESPOND
    assert observation.success is True
    assert observation.safe_summary == "Your backup completed successfully."
    assert observation.failure_code is None
    assert observation.action_outcome == "response_synthesized"
    assert observation.completed_at == "2026-08-08T00:00:00+00:00"


def test_build_respond_observation_failure_reuses_failure_code_for_action_outcome():
    observation = build_respond_observation(
        2,
        success=False,
        safe_summary="The response could not be generated.",
        failure_code="respond_provider_unavailable",
        completed_at="2026-08-08T00:00:00+00:00",
    )

    assert observation.success is False
    assert observation.failure_code == "respond_provider_unavailable"
    assert observation.action_outcome == "respond_provider_unavailable"


def test_respond_observation_round_trips_through_serialize_deserialize():
    observation = build_respond_observation(
        1,
        success=True,
        safe_summary="Done.",
        failure_code=None,
        completed_at="2026-08-08T00:00:00+00:00",
    )
    raw = serialize_observation(observation)
    assert deserialize_observation(raw) == observation


def test_step_observation_is_frozen():
    observation = StepObservation(
        observation_version=1,
        step_position=1,
        step_kind=StepKind.ACTION,
        success=True,
        safe_summary="x",
        failure_code=None,
        action_outcome="executed",
        completed_at="2026-08-08T00:00:00+00:00",
    )
    with pytest.raises(AttributeError):
        observation.success = False
