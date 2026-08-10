"""
StepObservation: the durable, bounded record of one plan step's execution
result - an ACTION step's (Milestone 42 P2) or a RESPOND step's
(Milestone 42 P3) - and its deterministic serialize/deserialize pair -
matching kernel/task_planner/serialization.py's exact discipline for
TaskPlan.

kernel.tools.executor.SafeTaskExecutor.execute() already returns
ActionResult(success, message, outcome) - message is already the
safe-to-relay result summary (never raw stdout/stderr/exception text/
handler internals/secrets - see kernel/tools/executor.py and
kernel/tools/types.py), and outcome is already one of kernel/tools/audit.py's
fixed, bounded, machine-readable outcome codes ("executed", "failed",
"rejected", "timed_out", ...). build_action_observation() reuses both
fields exactly as given - it never inspects, reformats, or adds to them,
and never accepts or stores anything beyond what SafeTaskExecutor already
decided was safe to hand back. build_respond_observation() (Milestone 42
P3) is the equivalent mapping for a RESPOND step: its `safe_summary` is
either the synthesized response text (kernel.task_execution.respond
already validated and bounded it) or a fixed, code-authored failure
summary - never raw model output, a provider exception, or unvalidated
text.

Deliberately two distinct failure concerns, kept apart on purpose (mirrors
kernel/task_planner/serialization.py's own PlanSerializationError/
PlanDeserializationError split):

  - ObservationSerializationError - serialize_observation() could not
    produce a string within MAX_STEP_RESULT_JSON_CHARS (imported from
    kernel.employee_tasks, the single source of truth for that bound -
    the same import direction kernel/task_planner/serialization.py already
    established). Should be effectively unreachable given every field's
    own small bound, but not assumed away.

  - ObservationDeserializationError - deserialize_observation() was given
    text that is not valid, well-formed serialized-StepObservation JSON -
    an execution/storage integrity concern, never a planning concern.
    Never represented as, or converted into, a
    kernel.task_planner.PlannerErrorCode/PlannerFailure - those describe a
    problem with a model's response, not a corrupted persisted
    observation, which was never a model response to begin with.

Both functions perform no I/O and call no model.
"""

import json
from dataclasses import dataclass

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_planner import StepKind
from kernel.tools.types import ActionResult

OBSERVATION_VERSION = 1


@dataclass(frozen=True)
class StepObservation:
    """One ACTION step's durable, bounded execution result - matches the
    plain, frozen-dataclass convention every other domain type in this
    codebase already uses (TaskStepProgress, PlanStep, ...). Never carries
    raw tool output, an exception object, a stack trace, or a secret -
    only what ActionResult already decided was safe (`safe_summary` <-
    ActionResult.message, `action_outcome` <- ActionResult.outcome), plus
    a small amount of execution-domain metadata (`step_position`,
    `step_kind`, `completed_at`) this module itself adds."""

    observation_version: int
    step_position: int
    step_kind: StepKind
    success: bool
    safe_summary: str
    failure_code: str | None
    action_outcome: str
    completed_at: str


class ObservationSerializationError(Exception):
    """serialize_observation() could not produce a string within
    MAX_STEP_RESULT_JSON_CHARS. Should be effectively unreachable given
    every field's own small bound, but is still real defense in depth."""


class ObservationDeserializationError(Exception):
    """deserialize_observation() was given text that is not valid,
    well-formed serialized-StepObservation JSON - a storage/corruption
    concern, never a planning concern. Never convert this into a
    kernel.task_planner.PlannerFailure."""


def build_action_observation(
    step_position: int, result: ActionResult, completed_at: str
) -> StepObservation:
    """The one mapping from an ACTION step's real ActionResult to a
    durable StepObservation - reuses result.message/result.outcome
    verbatim (both already safe-to-relay/bounded), never adds anything
    beyond them. `failure_code` is None on success, and result.outcome on
    failure - outcome is already one of audit.py's fixed, bounded,
    machine-readable codes, so no separate mapping table is needed."""

    return StepObservation(
        observation_version=OBSERVATION_VERSION,
        step_position=step_position,
        step_kind=StepKind.ACTION,
        success=result.success,
        safe_summary=result.message,
        failure_code=None if result.success else result.outcome,
        action_outcome=result.outcome,
        completed_at=completed_at,
    )


_RESPOND_SUCCESS_OUTCOME = "response_synthesized"


def build_respond_observation(
    step_position: int,
    *,
    success: bool,
    safe_summary: str,
    failure_code: str | None,
    completed_at: str,
) -> StepObservation:
    """The one mapping from a RESPOND step's synthesis outcome (Milestone
    42 P3 - see respond.py) to a durable StepObservation - mirrors
    build_action_observation()'s exact shape and discipline for the
    RESPOND step kind. Unlike an ACTION step, a RESPOND step has no
    ActionResult/audit outcome code of its own to reuse for
    `action_outcome` (a field this dataclass still requires regardless of
    step kind - see StepObservation's own docstring) -
    `_RESPOND_SUCCESS_OUTCOME` is this module's own fixed, code-authored
    symbolic value for a successful synthesis; a failure reuses
    `failure_code` for `action_outcome`, exactly like
    build_action_observation() already reuses ActionResult.outcome for
    both fields on its own failure path. `safe_summary` is the caller's
    already-validated/bounded text (the synthesized response on success,
    a fixed code-authored failure summary on failure) - this function
    never validates or bounds it itself, exactly like
    build_action_observation() never re-validates ActionResult.message."""

    return StepObservation(
        observation_version=OBSERVATION_VERSION,
        step_position=step_position,
        step_kind=StepKind.RESPOND,
        success=success,
        safe_summary=safe_summary,
        failure_code=None if success else failure_code,
        action_outcome=_RESPOND_SUCCESS_OUTCOME if success else failure_code,
        completed_at=completed_at,
    )


def serialize_observation(observation: StepObservation) -> str:
    """Deterministic, canonical JSON serialization: the exact same
    StepObservation always produces the exact same string (sorted object
    keys, compact separators). Raises ObservationSerializationError if the
    result would exceed MAX_STEP_RESULT_JSON_CHARS; never truncates."""

    payload = {
        "observation_version": observation.observation_version,
        "step_position": observation.step_position,
        "step_kind": observation.step_kind.value,
        "success": observation.success,
        "safe_summary": observation.safe_summary,
        "failure_code": observation.failure_code,
        "action_outcome": observation.action_outcome,
        "completed_at": observation.completed_at,
    }

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    if len(serialized) > MAX_STEP_RESULT_JSON_CHARS:
        raise ObservationSerializationError(
            f"serialized observation exceeds MAX_STEP_RESULT_JSON_CHARS "
            f"({MAX_STEP_RESULT_JSON_CHARS})"
        )

    return serialized


def deserialize_observation(raw: str) -> StepObservation:
    """The inverse of serialize_observation(). Never raises anything
    other than ObservationDeserializationError - every malformed-input
    path (invalid JSON, wrong top-level shape, a missing/mistyped field,
    an unrecognized StepKind value) is caught and re-raised as that one
    type."""

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ObservationDeserializationError("observation is not valid JSON") from exc

    if not isinstance(data, dict):
        raise ObservationDeserializationError("observation is not a JSON object")

    try:
        return StepObservation(
            observation_version=data["observation_version"],
            step_position=data["step_position"],
            step_kind=StepKind(data["step_kind"]),
            success=data["success"],
            safe_summary=data["safe_summary"],
            failure_code=data["failure_code"],
            action_outcome=data["action_outcome"],
            completed_at=data["completed_at"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ObservationDeserializationError(
            "observation does not match the expected StepObservation shape"
        ) from exc
