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
from dataclasses import dataclass, replace

from kernel.employee_tasks import MAX_FAILURE_CODE_CHARS, MAX_STEP_RESULT_JSON_CHARS
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


# Milestone 46 adversarial re-review (M1): kernel.tools.types.ActionResult.outcome
# is typed as a plain `str` with no length/shape constraint enforced anywhere in
# kernel/tools/ - "one of audit.py's fixed, bounded, machine-readable codes" is a
# convention every one of the 14 currently-registered handlers happens to follow
# (verified by inspection of every ActionResult(...) construction site, including
# the indirect FileResourceError.outcome path in file_metadata.py/read_text_file.py),
# never a structural guarantee the type system provides. A future or buggy handler
# violating that convention (an oversized, empty, non-string, or NUL-containing
# outcome) would otherwise reach kernel.employee_tasks.TaskRepository.fail_running_step()'s
# own failure_code validation (bounded to MAX_FAILURE_CODE_CHARS) unnormalized,
# raising TaskInputTooLargeError uncaught - reproducing the exact "claimed
# in_progress, action already executed, uncaught exception, silent orphan" defect
# class this module's build_bounded_action_observation() exists to eliminate, just
# via `outcome` instead of `message`. normalize_action_result_outcome() closes this
# at the one place both persistence paths (the embedded StepObservation field AND
# the standalone failure_code parameter kernel/task_execution/service.py passes to
# fail_running_step()) originate from, so there is no second, unnormalized path.
_ACTION_OUTCOME_INVALID_FALLBACK_CODE = "action_outcome_unavailable"


def normalize_action_result_outcome(result: ActionResult) -> ActionResult:
    """Returns `result` unchanged if result.outcome already satisfies the
    exact same contract kernel.employee_tasks.TaskRepository's own
    failure_code column validation enforces (a non-empty string, no NUL
    character, at most MAX_FAILURE_CODE_CHARS) - true for every real
    outcome any of the 14 currently-registered handlers actually produces.
    Otherwise returns a copy of `result` with `outcome` replaced by a
    fixed, short, code-owned fallback code - never a truncation of the
    real value (which could still leak partial handler-internal detail,
    and a truncated arbitrary string is not a stable machine-readable code
    either way).

    Callers that build a StepObservation/persist a failure_code from an
    ActionResult MUST call this first and use the returned ActionResult
    (not the original) - this is what makes
    build_bounded_action_observation()'s "provably serializable regardless
    of what result.outcome contains" claim structurally true rather than
    convention-dependent. success/message are never touched - this
    function normalizes machine-readable metadata only, never the action's
    real outcome or its safe, already-bounded descriptive text."""

    outcome = result.outcome
    if (
        isinstance(outcome, str)
        and 1 <= len(outcome) <= MAX_FAILURE_CODE_CHARS
        and "\x00" not in outcome
    ):
        return result
    return replace(result, outcome=_ACTION_OUTCOME_INVALID_FALLBACK_CODE)


def build_action_observation(
    step_position: int, result: ActionResult, completed_at: str
) -> StepObservation:
    """The one mapping from an ACTION step's real ActionResult to a
    durable StepObservation - reuses result.message/result.outcome
    verbatim (both already safe-to-relay/bounded), never adds anything
    beyond them. `failure_code` is None on success, and result.outcome on
    failure - outcome is already one of audit.py's fixed, bounded,
    machine-readable codes, so no separate mapping table is needed.

    PRECONDITION (Milestone 46 adversarial re-review, M1): callers whose
    ActionResult did not just come from a trusted, code-controlled literal
    must pass it through normalize_action_result_outcome() first - this
    function trusts result.outcome verbatim and performs no bounds
    checking of its own, exactly like it already trusts result.message
    verbatim (see the message-bounding note below).

    IMPORTANT: unlike RESPOND (kernel.task_execution.respond bounds its own
    synthesized text before this module ever sees it - see
    MAX_RESPOND_TEXT_CHARS), nothing bounds a handler's ActionResult.message
    - it is arbitrary, handler-owned descriptive text (e.g.
    list_files.run()'s directory listing scales with directory contents).
    A caller that has already executed the action and now needs to persist
    its outcome MUST use build_bounded_action_observation() below instead of
    this function whenever result.message is not already known to be safely
    bounded - see that function's own docstring for why."""

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


# Fixed, code-owned, deliberately short fallback summaries - never the real
# (oversized) ActionResult.message, never a truncation/prefix of it (no
# partial filenames/paths could leak through a truncation). Used only by
# build_bounded_action_observation() below when the real message cannot be
# durably persisted within MAX_STEP_RESULT_JSON_CHARS.
_OVERSIZED_ACTION_SUCCESS_SUMMARY = (
    "Action completed; detailed output omitted because it exceeded the result size limit."
)
_OVERSIZED_ACTION_FAILURE_SUMMARY = (
    "Action failed; detailed output omitted because it exceeded the result size limit."
)


def build_bounded_action_observation(
    step_position: int, result: ActionResult, completed_at: str
) -> StepObservation:
    """The same mapping as build_action_observation(), except safe_summary
    is always one of the two fixed, short constants above instead of
    result.message - never the real (potentially oversized) descriptive
    text, never a truncation or prefix of it. success/failure_code/
    action_outcome are preserved EXACTLY as build_action_observation()
    would set them: an action that actually succeeded is still recorded as
    succeeded here, and an action that actually failed is still recorded as
    failed with its real failure_code - an oversized DESCRIPTIVE message
    must never be allowed to change what the engine durably believes
    happened to the action itself.

    PRECONDITION - same as build_action_observation() above: the caller
    MUST have already passed `result` through
    normalize_action_result_outcome() (kernel/task_execution/service.py's
    _finalize_action_step() does this exactly once, at the top, before
    either builder is ever called). Given that precondition holds, every
    field this function ever sets is either a plain int/bool/enum value,
    an ISO-8601 completed_at string, result.outcome (now GUARANTEED - not
    merely conventionally expected - to be a non-empty, NUL-free string of
    at most MAX_FAILURE_CODE_CHARS, by construction of the caller's own
    contract, not by trusting handler behavior), or one of the two fixed
    summary constants above - none of which can be arbitrarily large. The
    resulting StepObservation is therefore structurally, not just
    conventionally, guaranteed serializable within MAX_STEP_RESULT_JSON_CHARS
    (MAX_FAILURE_CODE_CHARS (64) is far smaller than the JSON budget this
    tiny, otherwise-fixed observation has to spend it from) regardless of
    what the real result.message/result.outcome happened to contain."""

    safe_summary = (
        _OVERSIZED_ACTION_SUCCESS_SUMMARY if result.success else _OVERSIZED_ACTION_FAILURE_SUMMARY
    )
    return StepObservation(
        observation_version=OBSERVATION_VERSION,
        step_position=step_position,
        step_kind=StepKind.ACTION,
        success=result.success,
        safe_summary=safe_summary,
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
