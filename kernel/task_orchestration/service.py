"""
advance_task_planning(): Milestone 41 P2's planning-orchestration seam.

Owns the full lifecycle sequence for one task, for one invocation:

    created -> planning -> ready   (a validated plan was produced and persisted)
    created -> planning -> failed  (the request could not be planned, or the
                                     model/provider call itself failed)

This is the glue between kernel.task_planner (P1, pure - no database, no
execution) and kernel.employee_tasks (Milestone 40 - persistence, no model,
no planner). Neither of those two packages may depend on the other; this
one depends on both, and only in this direction:

    kernel.task_orchestration -> kernel.task_planner
    kernel.task_orchestration -> kernel.employee_tasks
    kernel.task_orchestration -> kernel.models (ModelProvider, injected)

Never executes a plan step, never calls a tool, never loops autonomously,
never retries. Exactly one model call per invocation (via
kernel.task_planner.plan_task()), then exactly one terminal repository
write (persist_plan_and_ready() or mark_failed()). Crash recovery for a
process that dies while a task is sitting in `planning` is explicitly out
of scope here - that is later persistence/recovery work, not this
milestone.
"""

from kernel.employee_tasks import TaskRecord, TaskRepository, TaskState
from kernel.models.base import ModelProvider
from kernel.task_planner import (
    CannotPlan,
    CatalogEntry,
    PlannerErrorCode,
    PlannerFailure,
    RequiresClarification,
    TaskPlan,
    plan_task,
)
from kernel.task_planner.serialization import PlanSerializationError, serialize_plan

_CANNOT_PLAN_FAILURE_CODE = "cannot_plan"
_CANNOT_PLAN_FAILURE_SUMMARY = "Planner could not produce a supported plan."

_CLARIFICATION_FAILURE_CODE = "clarification_required"
_CLARIFICATION_FAILURE_SUMMARY = (
    "Planner requires clarification; interactive clarification is not supported."
)

_PROVIDER_FAILURE_CODE = "planner_provider_unavailable"
_PROVIDER_FAILURE_SUMMARY = "The planning model was unavailable or the request failed."

_SERIALIZATION_FAILURE_CODE = "plan_serialization_failed"
_SERIALIZATION_FAILURE_SUMMARY = "The validated plan could not be prepared for storage."

_PLAN_READY_REASON_CODE = "plan_ready"
_PLAN_READY_SAFE_SUMMARY = "Plan validated and persisted."

# Fixed, code-authored failure summaries keyed by PlannerErrorCode. Never
# outcome.detail directly: while most PlannerFailure.detail strings are
# themselves code-authored fixed phrases, at least one
# (INVALID_DEPENDENCY's "references a non-earlier step {dependency}")
# embeds a model-supplied integer value read from the parsed response -
# so outcome.detail as a whole cannot be treated as uniformly code-authored.
# failure_summary is a trusted-display field; it must never become a
# persistence channel for anything the model produced, even a single
# bounded integer. This mapping is the one place that distinction is
# actually enforced, keeping every failure_summary this service writes
# fully code-authored regardless of which PlannerErrorCode fired.
_PLANNER_FAILURE_SUMMARIES: dict[PlannerErrorCode, str] = {
    PlannerErrorCode.EMPTY_RESPONSE: "The planner produced an empty response.",
    PlannerErrorCode.RESPONSE_TOO_LARGE: "The planner response exceeded the maximum size.",
    PlannerErrorCode.MALFORMED_PLAN: "The planner response was not valid JSON.",
    PlannerErrorCode.DUPLICATE_KEY: "The planner response contained a duplicate field.",
    PlannerErrorCode.INVALID_CONSTANT: "The planner response contained an invalid numeric value.",
    PlannerErrorCode.EXCESSIVE_NESTING: "The planner response was too deeply nested.",
    PlannerErrorCode.SCHEMA_VALIDATION_FAILED: (
        "The planner response did not match the required schema."
    ),
    PlannerErrorCode.UNSUPPORTED_PLAN_VERSION: (
        "The planner response used an unsupported plan version."
    ),
    PlannerErrorCode.TOO_MANY_STEPS: "The planner response exceeded the maximum number of steps.",
    PlannerErrorCode.INVALID_STEP: "The planner response contained an invalid step.",
    PlannerErrorCode.UNKNOWN_ACTION: "The planner response referenced an unknown action.",
    PlannerErrorCode.INVALID_DEPENDENCY: (
        "The planner response contained an invalid step dependency."
    ),
    PlannerErrorCode.UNGROUNDED_CAPABILITY: (
        "The planner selected an action not clearly requested."
    ),
}
_DEFAULT_PLANNER_FAILURE_SUMMARY = "The planner response could not be validated."


class TaskNotInCreatedStateError(Exception):
    """advance_task_planning() was called with a task not currently in
    the `created` state. Raised before any repository write or model call
    is made - this is a contract violation by the caller, not a
    concurrency conflict (see TaskRepository's own InvalidTransitionError/
    TaskAlreadyTerminalError for those)."""


def _planner_failure_summary(error: PlannerErrorCode) -> str:
    return _PLANNER_FAILURE_SUMMARIES.get(error, _DEFAULT_PLANNER_FAILURE_SUMMARY)


def advance_task_planning(
    task: TaskRecord,
    repository: TaskRepository,
    catalog: tuple[CatalogEntry, ...],
    model_provider: ModelProvider,
) -> TaskRecord:
    """Run one task through created -> planning -> {ready | failed}.

    `task` must be in TaskState.CREATED - checked here, before any
    repository write or model call, as a fast rejection for the common
    case. The `created -> planning` transition itself
    (TaskRepository.transition_task(), which independently re-checks the
    task's actual state at the database level) is the real concurrency
    safety net against a stale in-hand TaskRecord; its
    InvalidTransitionError/TaskAlreadyTerminalError propagate uncaught if
    that race is lost, exactly like every other conflict this service can
    encounter - none of them are concealed or retried.

    Exactly one call to kernel.task_planner.plan_task() is made, wrapped
    in a deliberately broad `except Exception` - see that block's own
    comment for exactly why a narrower catch is not available, and what
    is (and is not) covered by it. If that call raises, the task is
    transitioned planning -> failed with a fixed, code-authored failure
    code/summary - no retry. If a resulting TaskPlan cannot be serialized
    (should not happen given kernel.task_planner's own field bounds, but
    is not assumed away), the task also fails rather than persisting a
    broken row - that one catch (PlanSerializationError, not Exception)
    IS narrow, since serialize_plan() has an actual typed contract, unlike
    ModelProvider.send_prompt().

    Every OTHER call in this function - transition_task(), mark_failed(),
    persist_plan_and_ready() - is deliberately NOT wrapped in any
    try/except: a concurrency conflict or a genuine programming defect in
    any of them must propagate as itself, never be concealed or
    reinterpreted as an ordinary planning failure. See
    tests/kernel/task_orchestration/test_service.py's exception-scoping
    tests, which prove this by construction rather than by inspection
    alone.
    """

    if task.state is not TaskState.CREATED:
        raise TaskNotInCreatedStateError(
            f"expected task to be in 'created' but it is in {task.state.value!r}"
        )

    repository.transition_task(task.task_id, TaskState.CREATED, TaskState.PLANNING)

    try:
        outcome = plan_task(task, catalog, model_provider)
    except Exception:
        # Deliberately broad, not a shortcut: kernel.models.base.ModelProvider
        # is an ABC whose send_prompt() declares no exception contract at
        # all (see that class's own docstring) - a concrete adapter
        # (Ollama/Anthropic/OpenAI/Gemini) can raise anything, and
        # OllamaProvider itself explicitly does not narrow what it lets
        # through (its own module docstring: "this module does not catch
        # or retry that" - urllib.error.URLError, socket.timeout, a
        # malformed-response KeyError/JSONDecodeError, or any other
        # provider's own exception type). There is no common narrower type
        # to catch across providers without this service reaching into
        # provider-specific internals it must not know about.
        #
        # This also, unavoidably, catches a genuine internal defect inside
        # plan_task()'s own deterministic sub-steps (parse_plan_response()/
        # validate_capability_grounding()), should one ever exist -
        # parse_plan_response() has an explicit, tested "never raises"
        # contract (tests/kernel/task_planner/test_parser.py::
        # test_never_raises_on_arbitrary_garbage), so this is a residual,
        # theoretical risk, not a live one; validate_capability_grounding()
        # has no equivalent explicit test today. Narrowing this catch
        # would require plan_task() itself to expose a typed distinction
        # between "the provider failed" and "my own parsing logic failed",
        # which it does not do and which is out of this milestone's scope
        # to add (P1 semantics are unchanged here). What this scoping DOES
        # guarantee, and what is enforced by not wrapping anything else in
        # this function: a defect in repository.transition_task(),
        # mark_failed(), or persist_plan_and_ready() - the actual
        # persistence/concurrency layer - is never caught here and never
        # misreported as "planner_provider_unavailable".
        return repository.mark_failed(
            task.task_id,
            TaskState.PLANNING,
            failure_code=_PROVIDER_FAILURE_CODE,
            failure_summary=_PROVIDER_FAILURE_SUMMARY,
        )

    if isinstance(outcome, TaskPlan):
        try:
            plan_json = serialize_plan(outcome)
        except PlanSerializationError:
            return repository.mark_failed(
                task.task_id,
                TaskState.PLANNING,
                failure_code=_SERIALIZATION_FAILURE_CODE,
                failure_summary=_SERIALIZATION_FAILURE_SUMMARY,
            )
        return repository.persist_plan_and_ready(
            task.task_id,
            TaskState.PLANNING,
            plan_json,
            reason_code=_PLAN_READY_REASON_CODE,
            safe_summary=_PLAN_READY_SAFE_SUMMARY,
        )

    if isinstance(outcome, CannotPlan):
        return repository.mark_failed(
            task.task_id,
            TaskState.PLANNING,
            failure_code=_CANNOT_PLAN_FAILURE_CODE,
            failure_summary=_CANNOT_PLAN_FAILURE_SUMMARY,
        )

    if isinstance(outcome, RequiresClarification):
        return repository.mark_failed(
            task.task_id,
            TaskState.PLANNING,
            failure_code=_CLARIFICATION_FAILURE_CODE,
            failure_summary=_CLARIFICATION_FAILURE_SUMMARY,
        )

    if isinstance(outcome, PlannerFailure):
        return repository.mark_failed(
            task.task_id,
            TaskState.PLANNING,
            failure_code=outcome.error.value,
            failure_summary=_planner_failure_summary(outcome.error),
        )

    raise TypeError(f"plan_task() returned an unsupported outcome type: {type(outcome).__name__}")
