"""Tests for kernel/employee_tasks/types.py's lifecycle-outbox pure
functions (Milestone 47 P1, adversarial-review correction MEDIUM-1/
MEDIUM-2): serialize_confirmation_required_payload()/
deserialize_confirmation_required_payload() (the closed, bounded
confirmation_required payload schema) and format_utc_timestamp() (the
canonical, lexically-sortable UTC timestamp representation
task_lifecycle_outbox relies on). Pure, no I/O, no database - mirrors
tests/kernel/task_execution/test_observation.py's own convention for a
dedicated serialize/deserialize test file."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from kernel.employee_tasks.types import (
    MAX_CONFIRMATION_ACTION_NAME_CHARS,
    MAX_CONFIRMATION_ID_CHARS,
    MAX_CONFIRMATION_RESOURCE_KEY_CHARS,
    MAX_LIFECYCLE_PAYLOAD_JSON_CHARS,
    ConfirmationRequiredPayload,
    LifecycleEventPayloadError,
    deserialize_confirmation_required_payload,
    format_utc_timestamp,
    serialize_confirmation_required_payload,
)


def _valid_payload_json(**overrides) -> str:
    data = {"confirmation_id": "abc", "action_name": "open_application", "resource_key": "notepad"}
    data.update(overrides)
    return json.dumps(data)


# --- round trip -------------------------------------------------------------


def test_valid_supported_payload_round_trips():
    payload = ConfirmationRequiredPayload(
        confirmation_id="018f-real-uuid7", action_name="open_application", resource_key="notepad"
    )
    raw = serialize_confirmation_required_payload(payload)
    result = deserialize_confirmation_required_payload(raw)
    assert result == payload


def test_valid_payload_with_null_resource_key_round_trips():
    payload = ConfirmationRequiredPayload(
        confirmation_id="018f-real-uuid7", action_name="system_status", resource_key=None
    )
    raw = serialize_confirmation_required_payload(payload)
    result = deserialize_confirmation_required_payload(raw)
    assert result == payload
    assert result.resource_key is None


# --- structural / key-shape adversarial cases --------------------------------


def test_rejects_extra_unexpected_key():
    raw = _valid_payload_json()
    data = json.loads(raw)
    data["extra"] = "unexpected"
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(json.dumps(data))


def test_rejects_missing_confirmation_id_key():
    raw = json.dumps({"action_name": "open_application", "resource_key": "notepad"})
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_missing_action_name_key():
    raw = json.dumps({"confirmation_id": "abc", "resource_key": "notepad"})
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_missing_resource_key_key():
    raw = json.dumps({"confirmation_id": "abc", "action_name": "open_application"})
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_non_object_top_level_array():
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(json.dumps([1, 2, 3]))


def test_rejects_non_object_top_level_string():
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(json.dumps("just a string"))


def test_rejects_syntactically_invalid_json():
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload("not valid json{{{")


def test_rejects_empty_string():
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload("")


def test_rejects_deeply_nested_irrelevant_extra_key():
    raw = _valid_payload_json(nested={"a": {"b": {"c": [1, 2, 3]}}})
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


# --- type adversarial cases ---------------------------------------------


@pytest.mark.parametrize("bad_value", [123, [1, 2], {"x": 1}, True, None, 1.5])
def test_rejects_wrong_type_confirmation_id(bad_value):
    raw = _valid_payload_json(confirmation_id=bad_value)
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


@pytest.mark.parametrize("bad_value", [123, [1], {"x": 1}, True, None, 1.5])
def test_rejects_wrong_type_action_name(bad_value):
    raw = _valid_payload_json(action_name=bad_value)
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


@pytest.mark.parametrize("bad_value", [123, [1], {"x": 1}, True, 1.5])
def test_rejects_wrong_type_resource_key(bad_value):
    # None is deliberately excluded here - it is the one valid non-string
    # value resource_key may take (see test_valid_payload_with_null_resource_key_round_trips).
    raw = _valid_payload_json(resource_key=bad_value)
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


# --- bound adversarial cases ----------------------------------------------


def test_rejects_over_bound_confirmation_id():
    raw = _valid_payload_json(confirmation_id="x" * (MAX_CONFIRMATION_ID_CHARS + 1))
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_over_bound_action_name():
    raw = _valid_payload_json(action_name="x" * (MAX_CONFIRMATION_ACTION_NAME_CHARS + 1))
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_over_bound_resource_key():
    raw = _valid_payload_json(resource_key="x" * (MAX_CONFIRMATION_RESOURCE_KEY_CHARS + 1))
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_empty_confirmation_id():
    raw = _valid_payload_json(confirmation_id="")
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_empty_action_name():
    raw = _valid_payload_json(action_name="")
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_empty_string_resource_key():
    # Empty string is not the same as None - PendingTaskConfirmation/
    # propose_confirmation()'s own semantics are "None means no resource,
    # never an empty-string placeholder".
    raw = _valid_payload_json(resource_key="")
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


@pytest.mark.parametrize("field", ["confirmation_id", "action_name", "resource_key"])
def test_rejects_embedded_nul_in_each_string_field(field):
    raw = _valid_payload_json(**{field: "a\x00b"})
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_raw_payload_larger_than_max_lifecycle_payload_json_chars():
    # Oversized BEFORE json.loads() is ever attempted - a huge, otherwise
    # syntactically-plausible action_name is enough to push the whole raw
    # string past the bound.
    raw = _valid_payload_json(action_name="x" * (MAX_LIFECYCLE_PAYLOAD_JSON_CHARS + 1))
    assert len(raw) > MAX_LIFECYCLE_PAYLOAD_JSON_CHARS
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(raw)


def test_rejects_non_string_raw_input():
    with pytest.raises(LifecycleEventPayloadError):
        deserialize_confirmation_required_payload(None)  # type: ignore[arg-type]


def test_exception_never_leaks_the_raw_payload_content():
    secret_looking_value = "super-secret-token-should-never-appear-in-error"
    raw = _valid_payload_json(confirmation_id=secret_looking_value * 10)  # forces over-bound
    with pytest.raises(LifecycleEventPayloadError) as excinfo:
        deserialize_confirmation_required_payload(raw)
    assert secret_looking_value not in str(excinfo.value)


# --- format_utc_timestamp() ------------------------------------------------


def test_format_utc_timestamp_always_includes_microseconds():
    zero_micros = datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=timezone.utc)
    nonzero_micros = datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc)
    assert format_utc_timestamp(zero_micros) == "2026-01-01T00:00:00.000000+00:00"
    assert format_utc_timestamp(nonzero_micros) == "2026-01-01T00:00:00.500000+00:00"
    # Both representations are the exact same fixed length - the whole
    # point of forcing timespec="microseconds".
    assert len(format_utc_timestamp(zero_micros)) == len(format_utc_timestamp(nonzero_micros))


def test_format_utc_timestamp_converts_non_utc_offset_to_utc():
    eastern_ish = datetime(2026, 1, 1, 5, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    # 05:00-05:00 is 10:00 UTC.
    assert format_utc_timestamp(eastern_ish) == "2026-01-01T10:00:00.000000+00:00"


def test_format_utc_timestamp_rejects_naive_datetime():
    with pytest.raises(ValueError):
        format_utc_timestamp(datetime(2026, 1, 1))


def test_format_utc_timestamp_lexical_order_matches_chronological_order_across_microsecond_boundary():
    """The exact invariant list_due_lifecycle_outbox_events() depends on:
    a whole-second-earlier, zero-microsecond instant must sort lexically
    BEFORE a whole-second-later instant with any microsecond value, and a
    same-second instant with fewer microseconds must sort lexically before
    one with more - never inverted, unlike plain datetime.isoformat()'s
    own variable-width output could risk in principle."""

    t1 = format_utc_timestamp(datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=timezone.utc))
    t2 = format_utc_timestamp(datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc))
    t3 = format_utc_timestamp(datetime(2026, 1, 1, 0, 0, 0, 999999, tzinfo=timezone.utc))
    t4 = format_utc_timestamp(datetime(2026, 1, 1, 0, 0, 1, 0, tzinfo=timezone.utc))
    assert t1 < t2 < t3 < t4
