"""
advance_task_execution() / approve_task_confirmation() /
deny_task_confirmation(): Milestone 42 P2's one-step-at-a-time execution
primitive. run_task_until_blocked() (Milestone 42 P3): the bounded
autonomous driver over that same primitive.

Mirrors kernel/task_orchestration/service.py's own discipline: no internal
loop in advance_task_execution() itself (it processes exactly one plan
step, or one confirmation lifecycle event, per call - never more), every
dependency (TaskRepository, ActionRegistry, ToolsConfig, SafeTaskExecutor,
and - Milestone 42 P3 - a conversational ModelProvider) is explicitly
injected by the caller, never constructed here. This module never opens a
database connection, never loads kernel/config/tools.yaml, never
constructs an ActionRegistry/ToolsConfig/SafeTaskExecutor, and never
constructs a model provider - callers build all of that and pass it in.
The injected ModelProvider is never kernel.task_planner's gemma3:12b
structured-planner provider and is never called to plan or re-plan
anything - see kernel/task_execution/respond.py's own docstring for the
full model-role-separation contract; this module only ever passes that
provider through to synthesize_response(), never calls
ModelProvider.send_prompt() itself.

kernel.task_execution.eligibility.evaluate_next_step() (Milestone 42 P1)
remains the sole source of "what step is next and is it currently valid" -
this module never reimplements that logic, only acts on its typed
outcomes. kernel.tools.executor.SafeTaskExecutor is the ONLY action
execution boundary this module ever calls - never a handler, never
kernel.tools.process_control, directly. catalog_id and the persisted
PlanStep.requires_confirmation are never treated as execution authority
here either, exactly like eligibility.py: sensitivity is always whatever
EligibleStep.currently_sensitive said (itself re-derived from the CURRENT
ActionRegistry by evaluate_next_step()), and the ONLY action/resource ever
sent to SafeTaskExecutor is action_name/resource_key exactly as they
appear on the persisted PlanStep - for the non-sensitive path, EligibleStep's
own PlanStep; for the approval path, the persisted PlanStep re-resolved
fresh via kernel.task_execution.eligibility.resolve_persisted_plan_step()
and verified to match the pending confirmation row EXACTLY (Milestone 42
P2 correction - see approve_task_confirmation()'s own docstring).
task_pending_confirmation is a durable record of a confirmation PROPOSAL,
never execution authority by itself: its action_name/resource_key/
step_position are correlation/check values only, never the source an
ActionRequest is built from.

RESPOND steps (Milestone 42 P3): an eligible RESPOND step is claimed
through the exact same claim_step() boundary as a non-sensitive ACTION
step, then synthesized via kernel.task_execution.respond.synthesize_response()
using the injected ModelProvider - see that module's own docstring for the
full input/validation contract. A RESPOND step never requires confirmation
(eligibility.py always reports currently_sensitive=False for one - see
that module's own docstring) and is never itself sensitive; its own
success/failure is persisted through the identical StepObservation/
task_step_progress mechanism ACTION steps already use (see
observation.py's build_respond_observation()). No runtime/interface/
WhatsApp wiring exists here or anywhere in this package - nothing calls
advance_task_execution()/run_task_until_blocked()/approve_task_confirmation()/
deny_task_confirmation() from a real request as of this milestone.

run_task_until_blocked() (Milestone 42 P3) performs ALL NORMAL execution
progression exclusively through advance_task_execution() - it never
selects a step, never claims a step, never calls SafeTaskExecutor or
ModelProvider directly, never manages a confirmation, never alters or
replans the persisted TaskPlan, and never completes a task itself. Its ONE
permitted direct repository mutation is the code-owned safety failure
issued when MAX_EXECUTION_ADVANCES is exhausted (RUNNING -> FAILED,
execution_advance_limit_exceeded) - a bounded-loop safety measure, never
an execution or planning decision - see that function's own docstring for
its exact stopping conditions and this one exception to "only
advance_task_execution()".

CANCELLATION SEMANTICS (see module docstring in
kernel/employee_tasks/repository.py's claim_step()/propose_confirmation()
for the durable side of this): a successful claim (whether via claim_step()
for a non-sensitive step, or via consume_confirmation_and_claim_step() for
an approved sensitive one) is the execution authorization boundary for
that ONE step. Cancellation BEFORE a claim commits prevents the claim
(claim_step()/propose_confirmation()/consume_confirmation_and_claim_step()
all require the task to still be RUNNING/WAITING_FOR_CONFIRMATION, checked
atomically inside the same transaction as the claim) - the executor is
never called in that case. Cancellation AFTER a claim commits does NOT,
and structurally cannot, retroactively revoke an already-authorized
external action that may already be running - SafeTaskExecutor.execute()
is a synchronous, already-in-flight call by that point. What THIS module
guarantees instead: the step's own result is still durably recorded (for
audit/recovery - see _finalize_action_step()'s TaskAlreadyTerminalError
fallback below), but the task itself is never overwritten out of whatever
terminal state a concurrent writer already committed it to, and no further
step is ever started once a task is no longer RUNNING. There is no new
EXECUTING task state - a claimed, in-flight step is represented purely by
its task_step_progress row being `in_progress` while the task itself may
independently be RUNNING or (if raced) already terminal.
"""

from datetime import datetime, timezone

from kernel.employee_tasks import (
    TERMINAL_STATES,
    ConfirmationExpiredError,
    ConfirmationMismatchError,
    NoPendingConfirmationError,
    TaskAlreadyTerminalError,
    TaskRecord,
    TaskRepository,
    TaskState,
    parse_task_timestamp,
)
from kernel.models.base import ModelProvider
from kernel.task_execution.eligibility import (
    evaluate_next_step,
    resolve_persisted_plan_step,
    revalidate_action,
)
from kernel.task_execution.observation import (
    ObservationSerializationError,
    build_action_observation,
    build_respond_observation,
    serialize_observation,
)
from kernel.task_execution.respond import (
    RespondContextTooLargeFailure,
    RespondDependencyFailure,
    RespondInvalidOutputFailure,
    RespondProviderFailure,
    RespondSuccess,
    synthesize_response,
)
from kernel.task_execution.types import (
    ActionRevalidationFailure,
    AllStepsComplete,
    Blocked,
    EligibilityBlockReason,
    EligibleStep,
    ExecutionAdvanceResult,
    ExecutionAdvanceStatus,
    MAX_EXECUTION_ADVANCES,
    PlanDeserializationFailure,
    PlanIntegrityFailure,
    TASK_CONFIRMATION_TTL_SECONDS,
)
from kernel.task_planner import PlanStep, StepKind
from kernel.tools.config import ToolsConfig
from kernel.tools.executor import SafeTaskExecutor
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest, ActionResult

# -- caller-contract violations (mirrors task_orchestration.service's --
# -- TaskNotInCreatedStateError: raised before any repository write, --
# -- never a concurrency conflict) --------------------------------------


class TaskNotReadyOrRunningError(Exception):
    """advance_task_execution() was called with a task not currently in
    READY, RUNNING, WAITING_FOR_CONFIRMATION, or a terminal state (e.g.
    still CREATED or PLANNING). Raised before any repository write - a
    contract violation by the caller, not a concurrency conflict."""


class TaskNotWaitingForConfirmationError(Exception):
    """approve_task_confirmation()/deny_task_confirmation() was called
    with a task not currently WAITING_FOR_CONFIRMATION. Raised before any
    repository write - a contract violation by the caller."""


# -- fixed, code-authored reason/failure text ----------------------------
# Every one of these is code-authored and fixed - never raw plan_json
# content, model output, or request text. kernel/employee_tasks/ itself
# stays policy-free (see its own docstring) - this module is where that
# policy actually lives, exactly like kernel/task_orchestration/service.py
# owns its own fixed failure summaries for kernel.task_planner outcomes.

_TASK_STARTED_REASON_CODE = "task_started"
_TASK_STARTED_SAFE_SUMMARY = "Task execution started."

_TASK_COMPLETED_REASON_CODE = "all_steps_complete"
_TASK_COMPLETED_SAFE_SUMMARY = "All plan steps completed successfully."

_STEP_FAILED_FAILURE_CODE = "step_failed"
_STEP_FAILED_FAILURE_SUMMARY = "A previous step failed; the task cannot continue."

_STEP_UNCERTAIN_FAILURE_CODE = "step_execution_uncertain"
_STEP_UNCERTAIN_FAILURE_SUMMARY = (
    "A previous step's execution outcome is uncertain; the task cannot safely continue."
)

_DEPENDENCY_VIOLATION_FAILURE_CODE = "plan_dependency_violation"
_DEPENDENCY_VIOLATION_FAILURE_SUMMARY = (
    "The persisted plan's step dependencies could not be satisfied."
)

_BLOCKED_FAILURE_TEXT = {
    EligibilityBlockReason.STEP_FAILED: (_STEP_FAILED_FAILURE_CODE, _STEP_FAILED_FAILURE_SUMMARY),
    EligibilityBlockReason.STEP_IN_PROGRESS: (
        _STEP_UNCERTAIN_FAILURE_CODE,
        _STEP_UNCERTAIN_FAILURE_SUMMARY,
    ),
    EligibilityBlockReason.DEPENDENCIES_NOT_SATISFIED: (
        _DEPENDENCY_VIOLATION_FAILURE_CODE,
        _DEPENDENCY_VIOLATION_FAILURE_SUMMARY,
    ),
}

_PLAN_INTEGRITY_FAILURE_CODE = "plan_identity_violation"
_PLAN_INTEGRITY_FAILURE_SUMMARY = "The task's persisted plan identity could not be trusted."

_PLAN_DESERIALIZATION_FAILURE_CODE = "plan_storage_corrupt"
_PLAN_DESERIALIZATION_FAILURE_SUMMARY = "The task's persisted plan could not be read."

_ACTION_REVALIDATION_FAILURE_CODE = "action_no_longer_valid"
_ACTION_REVALIDATION_FAILURE_SUMMARY = (
    "The next step's action/resource is no longer valid in the current configuration."
)

_RESPOND_DEPENDENCY_FAILURE_CODE = "respond_dependency_integrity_violation"
_RESPOND_DEPENDENCY_FAILURE_SUMMARY = (
    "A dependency this response relies on could not be durably verified."
)

_RESPOND_PROVIDER_UNAVAILABLE_FAILURE_CODE = "respond_provider_unavailable"
_RESPOND_PROVIDER_UNAVAILABLE_FAILURE_SUMMARY = (
    "The response could not be generated because the model provider was unavailable."
)

_RESPOND_INVALID_OUTPUT_FAILURE_CODE = "respond_invalid_output"
_RESPOND_INVALID_OUTPUT_FAILURE_SUMMARY = (
    "The generated response did not meet the required format or size."
)

_RESPOND_CONTEXT_TOO_LARGE_FAILURE_CODE = "respond_context_too_large"
_RESPOND_CONTEXT_TOO_LARGE_FAILURE_SUMMARY = (
    "The combined task/step/dependency context was too large to synthesize a response from."
)

_EXECUTION_ADVANCE_LIMIT_EXCEEDED_FAILURE_CODE = "execution_advance_limit_exceeded"
_EXECUTION_ADVANCE_LIMIT_EXCEEDED_FAILURE_SUMMARY = (
    "Execution stopped after reaching the maximum number of automatic advances."
)

_CONFIRMATION_PROPOSED_REASON_CODE = "confirmation_proposed"
_CONFIRMATION_PROPOSED_SAFE_SUMMARY = "Sensitive action requires confirmation."

_CONFIRMATION_APPROVED_REASON_CODE = "confirmation_approved"
_CONFIRMATION_APPROVED_SAFE_SUMMARY = "Confirmation approved; step claimed."

_CONFIRMATION_DENIED_REASON_CODE = "confirmation_denied"
_CONFIRMATION_DENIED_SAFE_SUMMARY = "Confirmation denied."

_CONFIRMATION_EXPIRED_FAILURE_CODE = "confirmation_expired"
_CONFIRMATION_EXPIRED_FAILURE_SUMMARY = "The confirmation window expired before it was approved."

# Shares _ACTION_REVALIDATION_FAILURE_CODE (defined above) rather than a
# second, independently-declared constant with the same string value - the
# same "one code, reused across both the non-sensitive and approval paths"
# discipline _PLAN_INTEGRITY_FAILURE_CODE/_PLAN_DESERIALIZATION_FAILURE_CODE
# already use below. The SUMMARY stays distinct and approval-specific
# ("the approved action/resource", not "the next step's action/resource")
# since that wording is genuinely more accurate at approval time - only the
# machine-readable code is shared.
_ACTION_NO_LONGER_VALID_AT_APPROVAL_FAILURE_SUMMARY = (
    "The approved action/resource is no longer valid in the current configuration."
)

# Milestone 42 P2 correction: task_pending_confirmation is a durable record
# of a confirmation PROPOSAL, never execution authority by itself - the
# persisted TaskPlan remains the authority for what a task was actually
# allowed to execute. If the pending row's step_position/action_name/
# resource_key do not match EXACTLY what the persisted plan says at that
# position, approval fails closed under this one dedicated code rather
# than silently trusting the pending row. Malformed plan_json, a
# plan.task_id mismatch, a missing step position, or the position now
# mapping to a RESPOND step instead of ACTION are a different, narrower
# concern (the persisted plan itself is not trustworthy/well-shaped) and
# reuse the SAME _PLAN_INTEGRITY_FAILURE_CODE/_PLAN_DESERIALIZATION_FAILURE_CODE
# _process_running_task() already uses for the equivalent non-sensitive-path
# outcomes, rather than inventing parallel codes for the same concern.
_CONFIRMATION_PLAN_MISMATCH_FAILURE_CODE = "confirmation_plan_mismatch"
_CONFIRMATION_PLAN_MISMATCH_FAILURE_SUMMARY = (
    "The pending confirmation no longer matches the task's persisted plan."
)

_TERMINAL_STATUS_BY_STATE = {
    TaskState.COMPLETED: ExecutionAdvanceStatus.TASK_COMPLETED,
    TaskState.FAILED: ExecutionAdvanceStatus.TASK_FAILED,
    TaskState.CANCELLED: ExecutionAdvanceStatus.TASK_CANCELLED,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terminal_result(task: TaskRecord) -> ExecutionAdvanceResult:
    return ExecutionAdvanceResult(task, _TERMINAL_STATUS_BY_STATE[task.state])


def _finalize_step_success(
    task_id: str,
    repository: TaskRepository,
    step_position: int,
    observation_json: str,
    detail: str | None,
) -> ExecutionAdvanceResult:
    """Shared by every already-claimed step's success path, regardless of
    step kind: persist the step as durably succeeded, then re-fetch the
    task fresh (never trust a value computed before the step's own work
    finished, since a concurrent writer may have moved the task to a
    terminal state while that work was in flight - see module docstring's
    cancellation-semantics section)."""

    repository.mark_step_succeeded(task_id, step_position, observation_json)
    current = repository.get_task(task_id)
    if current.state is TaskState.RUNNING:
        return ExecutionAdvanceResult(current, ExecutionAdvanceStatus.STEP_SUCCEEDED, detail)
    # The task moved on (e.g. CANCELLED) while the step's own work was in
    # flight - the step's own success is still durably recorded above, but
    # the task's actual current terminal state is what gets reported here,
    # never a fabricated STEP_SUCCEEDED.
    return _terminal_result(current)


def _finalize_step_failure(
    task_id: str,
    repository: TaskRepository,
    step_position: int,
    failure_code: str,
    failure_summary: str,
    observation_json: str,
) -> ExecutionAdvanceResult:
    """Shared by every already-claimed step's failure path, regardless of
    step kind: atomically fail the step AND the RUNNING task together
    (fail_running_step()) - unless a concurrent writer already moved the
    task to a terminal state (e.g. CANCELLED) since the step was claimed,
    in which case the step's own outcome must still not be lost, but the
    task itself must never be overwritten (see module docstring's
    cancellation-semantics section)."""

    try:
        failed_task = repository.fail_running_step(
            task_id,
            step_position,
            failure_code=failure_code,
            failure_summary=failure_summary,
            result_json=observation_json,
        )
        return ExecutionAdvanceResult(failed_task, ExecutionAdvanceStatus.TASK_FAILED, failure_code)
    except TaskAlreadyTerminalError:
        repository.mark_step_failed(
            task_id,
            step_position,
            failure_code=failure_code,
            failure_summary=failure_summary,
            result_json=observation_json,
        )
        current = repository.get_task(task_id)
        return _terminal_result(current)


def _finalize_action_step(
    task_id: str,
    repository: TaskRepository,
    step_position: int,
    result: ActionResult,
) -> ExecutionAdvanceResult:
    """Shared by the non-sensitive execution path and the post-approval
    execution path: build and persist the step's StepObservation, then
    propagate success/failure to the task via the shared
    _finalize_step_success()/_finalize_step_failure() helpers (Milestone
    42 P3: these were factored out of what used to be this function's own
    body so kernel/task_execution/service.py's RESPOND path - see
    _process_running_task() below - can share the exact same claim/
    cancellation-safe finalization mechanics, unchanged, rather than
    duplicating them)."""

    completed_at = _now_iso()
    observation = build_action_observation(step_position, result, completed_at)
    observation_json = serialize_observation(observation)

    if result.success:
        return _finalize_step_success(task_id, repository, step_position, observation_json, result.outcome)
    return _finalize_step_failure(
        task_id, repository, step_position, result.outcome, result.message, observation_json
    )


def _finalize_respond_failure(
    task_id: str,
    repository: TaskRepository,
    step_position: int,
    failure_code: str,
    failure_summary: str,
) -> ExecutionAdvanceResult:
    """Build a RESPOND failure StepObservation from a fixed, code-authored
    failure_code/failure_summary (never model output or exception text -
    see respond.py's own docstring on why every RespondOutcome failure
    variant is mapped to exactly one of a small, fixed set of these), then
    finalize it through the same shared path every other step failure
    uses."""

    completed_at = _now_iso()
    observation = build_respond_observation(
        step_position,
        success=False,
        safe_summary=failure_summary,
        failure_code=failure_code,
        completed_at=completed_at,
    )
    observation_json = serialize_observation(observation)
    return _finalize_step_failure(task_id, repository, step_position, failure_code, failure_summary, observation_json)


def _process_running_task(
    task_id: str,
    repository: TaskRepository,
    registry: ActionRegistry,
    tools_config: ToolsConfig,
    executor: SafeTaskExecutor,
    model_provider: ModelProvider,
) -> ExecutionAdvanceResult:
    """Process exactly one unit of work for a task already RUNNING - never
    more than one plan step (or one confirmation proposal) per call.
    Always re-reads the task and its step progress fresh from
    `repository`, never trusting a caller-held TaskRecord for this
    decision - matches evaluate_next_step()'s own "always fresh" contract."""

    task = repository.get_task(task_id)
    step_progress = repository.list_step_progress(task_id)
    outcome = evaluate_next_step(task, step_progress, registry, tools_config)

    if isinstance(outcome, AllStepsComplete):
        completed_task = repository.transition_task(
            task_id,
            TaskState.RUNNING,
            TaskState.COMPLETED,
            reason_code=_TASK_COMPLETED_REASON_CODE,
            safe_summary=_TASK_COMPLETED_SAFE_SUMMARY,
        )
        return ExecutionAdvanceResult(completed_task, ExecutionAdvanceStatus.TASK_COMPLETED)

    if isinstance(outcome, Blocked):
        failure_code, failure_summary = _BLOCKED_FAILURE_TEXT[outcome.reason]
        failed_task = repository.mark_failed(task_id, TaskState.RUNNING, failure_code, failure_summary)
        return ExecutionAdvanceResult(failed_task, ExecutionAdvanceStatus.TASK_FAILED, failure_code)

    if isinstance(outcome, PlanIntegrityFailure):
        failed_task = repository.mark_failed(
            task_id, TaskState.RUNNING, _PLAN_INTEGRITY_FAILURE_CODE, _PLAN_INTEGRITY_FAILURE_SUMMARY
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _PLAN_INTEGRITY_FAILURE_CODE
        )

    if isinstance(outcome, PlanDeserializationFailure):
        failed_task = repository.mark_failed(
            task_id,
            TaskState.RUNNING,
            _PLAN_DESERIALIZATION_FAILURE_CODE,
            _PLAN_DESERIALIZATION_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _PLAN_DESERIALIZATION_FAILURE_CODE
        )

    if isinstance(outcome, ActionRevalidationFailure):
        failed_task = repository.mark_failed(
            task_id,
            TaskState.RUNNING,
            _ACTION_REVALIDATION_FAILURE_CODE,
            _ACTION_REVALIDATION_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _ACTION_REVALIDATION_FAILURE_CODE
        )

    # outcome is EligibleStep from here on.
    step: EligibleStep = outcome
    plan_step = step.step

    if plan_step.kind is StepKind.RESPOND:
        # Milestone 42 P3: claim first, exactly like the non-sensitive
        # ACTION path below - claim_step() itself requires the task to
        # still be RUNNING (P1); if a concurrent writer already moved it
        # on, this raises and propagates uncaught, and synthesize_response()
        # is never called (mirrors the ACTION path's own claim-first
        # discipline - see module docstring's cancellation-semantics
        # section).
        repository.claim_step(task_id, plan_step.position)

        # Resolve every declared dependency's PERSISTED PlanStep (never
        # the durable observation alone) so synthesize_response() can
        # verify each dependency observation's step_kind against what the
        # plan actually says - task.plan_json is immutable/write-once
        # (see kernel.employee_tasks.TaskRepository.persist_plan_and_ready()'s
        # own docstring), so the `task` already read at the top of this
        # function is still exactly correct here despite the claim above.
        dependency_steps: dict[int, PlanStep] = {}
        for dependency_position in plan_step.depends_on:
            resolved = resolve_persisted_plan_step(task, dependency_position)
            if isinstance(resolved, PlanIntegrityFailure):
                return _finalize_respond_failure(
                    task_id,
                    repository,
                    plan_step.position,
                    _PLAN_INTEGRITY_FAILURE_CODE,
                    _PLAN_INTEGRITY_FAILURE_SUMMARY,
                )
            if isinstance(resolved, PlanDeserializationFailure):
                return _finalize_respond_failure(
                    task_id,
                    repository,
                    plan_step.position,
                    _PLAN_DESERIALIZATION_FAILURE_CODE,
                    _PLAN_DESERIALIZATION_FAILURE_SUMMARY,
                )
            dependency_steps[dependency_position] = resolved

        outcome = synthesize_response(task, plan_step, dependency_steps, step_progress, model_provider)

        if isinstance(outcome, RespondSuccess):
            completed_at = _now_iso()
            observation = build_respond_observation(
                plan_step.position,
                success=True,
                safe_summary=outcome.text,
                failure_code=None,
                completed_at=completed_at,
            )
            # A response that passes respond.py's own raw-text bound
            # (MAX_RESPOND_TEXT_CHARS) does NOT by itself guarantee the
            # resulting StepObservation fits inside MAX_STEP_RESULT_JSON_CHARS
            # once serialized - json.dumps() escaping (quotes, backslashes,
            # control characters) can expand the serialized length well
            # past the raw one. serialize_observation() is the
            # AUTHORITATIVE check for that; a response that fails it here
            # has no external side effect to undo (the model call already
            # completed with no side effect of its own - see respond.py's
            # own docstring), so it is safe to classify as an ordinary,
            # known RESPOND failure (respond_invalid_output) rather than an
            # unknown/uncaught error - never left in_progress, never
            # silently truncated to force a fit.
            try:
                observation_json = serialize_observation(observation)
            except ObservationSerializationError:
                return _finalize_respond_failure(
                    task_id,
                    repository,
                    plan_step.position,
                    _RESPOND_INVALID_OUTPUT_FAILURE_CODE,
                    _RESPOND_INVALID_OUTPUT_FAILURE_SUMMARY,
                )
            return _finalize_step_success(
                task_id, repository, plan_step.position, observation_json, observation.action_outcome
            )

        if isinstance(outcome, RespondDependencyFailure):
            return _finalize_respond_failure(
                task_id,
                repository,
                plan_step.position,
                _RESPOND_DEPENDENCY_FAILURE_CODE,
                _RESPOND_DEPENDENCY_FAILURE_SUMMARY,
            )

        if isinstance(outcome, RespondContextTooLargeFailure):
            return _finalize_respond_failure(
                task_id,
                repository,
                plan_step.position,
                _RESPOND_CONTEXT_TOO_LARGE_FAILURE_CODE,
                _RESPOND_CONTEXT_TOO_LARGE_FAILURE_SUMMARY,
            )

        if isinstance(outcome, RespondProviderFailure):
            return _finalize_respond_failure(
                task_id,
                repository,
                plan_step.position,
                _RESPOND_PROVIDER_UNAVAILABLE_FAILURE_CODE,
                _RESPOND_PROVIDER_UNAVAILABLE_FAILURE_SUMMARY,
            )

        # outcome is RespondInvalidOutputFailure - the only remaining
        # member of the closed RespondOutcome union.
        if isinstance(outcome, RespondInvalidOutputFailure):
            return _finalize_respond_failure(
                task_id,
                repository,
                plan_step.position,
                _RESPOND_INVALID_OUTPUT_FAILURE_CODE,
                _RESPOND_INVALID_OUTPUT_FAILURE_SUMMARY,
            )
        raise TypeError(f"synthesize_response() returned an unrecognized outcome: {outcome!r}")

    if step.currently_sensitive:
        waiting_task = repository.propose_confirmation(
            task_id,
            plan_step.position,
            plan_step.action_name,
            plan_step.resource_key,
            TASK_CONFIRMATION_TTL_SECONDS,
            reason_code=_CONFIRMATION_PROPOSED_REASON_CODE,
            safe_summary=_CONFIRMATION_PROPOSED_SAFE_SUMMARY,
        )
        return ExecutionAdvanceResult(waiting_task, ExecutionAdvanceStatus.CONFIRMATION_REQUIRED)

    # Non-sensitive ACTION step: claim, execute exactly once through
    # SafeTaskExecutor, then finalize. claim_step() itself requires the
    # task to still be RUNNING (P1) - if a concurrent writer already
    # cancelled/failed/completed it, this raises and propagates uncaught,
    # exactly like every other concurrency conflict in this module (see
    # module docstring's cancellation-semantics section): the claim fails
    # and SafeTaskExecutor is never called.
    repository.claim_step(task_id, plan_step.position)
    action_request = ActionRequest(action=plan_step.action_name, resource_key=plan_step.resource_key)
    result = executor.execute(action_request)
    return _finalize_action_step(task_id, repository, plan_step.position, result)


def _process_waiting_task(task_id: str, repository: TaskRepository) -> ExecutionAdvanceResult:
    """A task already WAITING_FOR_CONFIRMATION: never creates a
    replacement confirmation automatically. If the pending confirmation is
    unexpired, returns it unchanged, without executing anything. If it has
    expired, atomically expires/fails the task."""

    task = repository.get_task(task_id)
    pending = repository.get_pending_confirmation(task_id)
    if pending is None:
        # Should be structurally unreachable given the atomic invariants
        # every propose/consume/deny/fail_pending_confirmation call
        # maintains - never assumed away. No mutation; nothing this layer
        # can safely do without inventing new policy.
        return ExecutionAdvanceResult(
            task,
            ExecutionAdvanceStatus.WAITING_FOR_CONFIRMATION,
            "no_pending_confirmation_found",
        )

    # A real chronological comparison of parsed, timezone-aware datetimes -
    # never a lexical string comparison (see
    # kernel.employee_tasks.parse_task_timestamp()'s own docstring for
    # why). A malformed persisted expires_at fails closed as
    # TaskStorageCorruptError here, propagated uncaught - never silently
    # treated as "not yet expired".
    if datetime.now(timezone.utc) > parse_task_timestamp(pending.expires_at, "expires_at"):
        failed_task = repository.fail_pending_confirmation(
            task_id,
            pending.confirmation_id,
            _CONFIRMATION_EXPIRED_FAILURE_CODE,
            _CONFIRMATION_EXPIRED_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _CONFIRMATION_EXPIRED_FAILURE_CODE
        )

    return ExecutionAdvanceResult(task, ExecutionAdvanceStatus.WAITING_FOR_CONFIRMATION)


def advance_task_execution(
    task: TaskRecord,
    repository: TaskRepository,
    registry: ActionRegistry,
    tools_config: ToolsConfig,
    executor: SafeTaskExecutor,
    model_provider: ModelProvider,
) -> ExecutionAdvanceResult:
    """The one-step-at-a-time execution primitive. Processes exactly one
    plan step, or one confirmation-related event, per call - never loops
    internally. `task` is used only for its initial state dispatch (a fast
    rejection for a wrong starting state, mirroring
    kernel.task_orchestration.service.advance_task_planning()'s own
    doctrine) - every actual decision re-reads fresh state from
    `repository`.

    `model_provider` (Milestone 42 P3) is injected by the caller, exactly
    like every other dependency here - this function never constructs one
    and never calls it directly; it is only ever threaded through to
    _process_running_task(), which passes it to
    kernel.task_execution.respond.synthesize_response() if and only if the
    one step this call processes turns out to be an eligible RESPOND step.
    An ACTION-only call path never touches it at all."""

    if task.state is TaskState.READY:
        running_task = repository.transition_task(
            task.task_id,
            TaskState.READY,
            TaskState.RUNNING,
            reason_code=_TASK_STARTED_REASON_CODE,
            safe_summary=_TASK_STARTED_SAFE_SUMMARY,
        )
        return _process_running_task(
            running_task.task_id, repository, registry, tools_config, executor, model_provider
        )

    if task.state is TaskState.RUNNING:
        return _process_running_task(
            task.task_id, repository, registry, tools_config, executor, model_provider
        )

    if task.state is TaskState.WAITING_FOR_CONFIRMATION:
        return _process_waiting_task(task.task_id, repository)

    if task.state in TERMINAL_STATES:
        return _terminal_result(task)

    raise TaskNotReadyOrRunningError(
        f"expected task to be in 'ready', 'running', or 'waiting_for_confirmation' "
        f"but it is in {task.state.value!r}"
    )


# Milestone 42 P3: stopping statuses for run_task_until_blocked() below -
# every ExecutionAdvanceStatus EXCEPT STEP_SUCCEEDED, which is the only one
# the bounded runner ever continues past automatically.
_RUNNER_STOPPING_STATUSES = frozenset(
    {
        ExecutionAdvanceStatus.CONFIRMATION_REQUIRED,
        ExecutionAdvanceStatus.WAITING_FOR_CONFIRMATION,
        ExecutionAdvanceStatus.TASK_COMPLETED,
        ExecutionAdvanceStatus.TASK_FAILED,
        ExecutionAdvanceStatus.TASK_CANCELLED,
    }
)


def run_task_until_blocked(
    task: TaskRecord,
    repository: TaskRepository,
    registry: ActionRegistry,
    tools_config: ToolsConfig,
    executor: SafeTaskExecutor,
    model_provider: ModelProvider,
) -> ExecutionAdvanceResult:
    """Milestone 42 P3's bounded autonomous execution runner.

    Performs ALL NORMAL execution progression exclusively through
    advance_task_execution(): repeatedly calls it until it reports a
    stopping status (CONFIRMATION_REQUIRED, WAITING_FOR_CONFIRMATION,
    TASK_COMPLETED, TASK_FAILED, or TASK_CANCELLED), continuing
    automatically only past STEP_SUCCEEDED. Returns the
    ExecutionAdvanceResult of whichever call stopped it. Its ONE permitted
    direct repository mutation - the only line of this function that
    touches `repository` itself rather than going through
    advance_task_execution() - is the code-owned safety failure below when
    MAX_EXECUTION_ADVANCES is exhausted; see that section's own comment.

    This function never selects a step, never claims a step, never calls
    SafeTaskExecutor or ModelProvider directly, never manages a
    confirmation, never constructs or alters a TaskPlan, never replans,
    and never completes a task itself - every actual execution decision
    still belongs entirely to advance_task_execution()
    (-> _process_running_task() -> kernel.task_execution.eligibility.
    evaluate_next_step()). This is a bounded DRIVER over that one-step
    primitive, nothing more - it does not even inspect *why* a call
    succeeded or failed, only whether to call again.

    A sensitive ACTION step's proposed confirmation
    (CONFIRMATION_REQUIRED/WAITING_FOR_CONFIRMATION) always stops this
    loop immediately: it never auto-approves, polls, sleeps, or creates
    another confirmation. A future caller that later calls
    approve_task_confirmation()/deny_task_confirmation() may invoke this
    function again afterward to continue - this function itself never
    calls either of those.

    MAX_EXECUTION_ADVANCES (kernel/task_execution/types.py) is a
    deterministic, code-owned hard ceiling on how many times this loop may
    call advance_task_execution() in one invocation - see that constant's
    own docstring for why it is sized the way it is. Exceeding it fails
    the task closed (execution_advance_limit_exceeded) rather than ever
    returning a still-RUNNING task as though execution had finished; this
    mark_failed() call is the one mutation this function performs
    directly, and it is a bounded-loop safety measure, never an execution
    or planning decision. Every successful iteration that continues the
    loop ends with the task RUNNING (see _finalize_step_success() - a
    STEP_SUCCEEDED result is only ever returned while the task is RUNNING),
    so this call is always well-formed if the limit is ever reached."""

    result = advance_task_execution(task, repository, registry, tools_config, executor, model_provider)

    for _ in range(MAX_EXECUTION_ADVANCES - 1):
        if result.status in _RUNNER_STOPPING_STATUSES:
            return result
        result = advance_task_execution(
            result.task, repository, registry, tools_config, executor, model_provider
        )

    if result.status in _RUNNER_STOPPING_STATUSES:
        return result

    failed_task = repository.mark_failed(
        result.task.task_id,
        TaskState.RUNNING,
        _EXECUTION_ADVANCE_LIMIT_EXCEEDED_FAILURE_CODE,
        _EXECUTION_ADVANCE_LIMIT_EXCEEDED_FAILURE_SUMMARY,
    )
    return ExecutionAdvanceResult(
        failed_task, ExecutionAdvanceStatus.TASK_FAILED, _EXECUTION_ADVANCE_LIMIT_EXCEEDED_FAILURE_CODE
    )


def approve_task_confirmation(
    task: TaskRecord,
    repository: TaskRepository,
    registry: ActionRegistry,
    tools_config: ToolsConfig,
    executor: SafeTaskExecutor,
    confirmation_id: str,
) -> ExecutionAdvanceResult:
    """Approve exactly the pending confirmation identified by
    `confirmation_id`.

    task_pending_confirmation is a durable record of a confirmation
    PROPOSAL, never execution authority by itself - the immutable
    persisted TaskPlan remains the authority for what the task was
    actually allowed to execute. Before consuming anything, this function
    re-binds the pending row to the CURRENT TaskRecord's persisted plan
    (resolve_persisted_plan_step()) and verifies the located PlanStep is
    an ACTION step whose position/action_name/resource_key match the
    pending row EXACTLY - no fuzzy resolution, no action substitution, no
    catalog_id authority. Only once that binding succeeds does it
    revalidate the action/resource against the CURRENT ActionRegistry/
    ToolsConfig - if either check fails, the task fails closed WITHOUT
    executing. Only after the atomic consume+claim transaction commits
    does SafeTaskExecutor ever get called - never inside that transaction,
    never before it - and the ActionRequest it receives is built from the
    VERIFIED PERSISTED PlanStep, never from task_pending_confirmation's own
    fields (those are correlation/check values only, not authority). No
    request-text interpretation, no model, no replanning."""

    if task.state is not TaskState.WAITING_FOR_CONFIRMATION:
        raise TaskNotWaitingForConfirmationError(
            f"expected task to be in 'waiting_for_confirmation' but it is in {task.state.value!r}"
        )

    # Always re-read fresh - never trust a caller-held TaskRecord for the
    # plan-binding decision below (mirrors _process_running_task()'s own
    # "always fresh" doctrine).
    current_task = repository.get_task(task.task_id)

    pending = repository.get_pending_confirmation(current_task.task_id)
    if pending is None:
        raise NoPendingConfirmationError(current_task.task_id)
    if pending.confirmation_id != confirmation_id:
        raise ConfirmationMismatchError(current_task.task_id)

    resolved = resolve_persisted_plan_step(current_task, pending.step_position)

    if isinstance(resolved, PlanIntegrityFailure):
        failed_task = repository.fail_pending_confirmation(
            current_task.task_id,
            confirmation_id,
            _PLAN_INTEGRITY_FAILURE_CODE,
            _PLAN_INTEGRITY_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _PLAN_INTEGRITY_FAILURE_CODE
        )

    if isinstance(resolved, PlanDeserializationFailure):
        failed_task = repository.fail_pending_confirmation(
            current_task.task_id,
            confirmation_id,
            _PLAN_DESERIALIZATION_FAILURE_CODE,
            _PLAN_DESERIALIZATION_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _PLAN_DESERIALIZATION_FAILURE_CODE
        )

    # resolved is a real, persisted PlanStep from here on.
    plan_step = resolved

    if plan_step.kind is not StepKind.ACTION:
        # The pending confirmation's position now maps to a RESPOND step
        # (or anything else that isn't ACTION) - the persisted plan and
        # the pending row can no longer agree on what was proposed.
        failed_task = repository.fail_pending_confirmation(
            current_task.task_id,
            confirmation_id,
            _PLAN_INTEGRITY_FAILURE_CODE,
            _PLAN_INTEGRITY_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _PLAN_INTEGRITY_FAILURE_CODE
        )

    if (
        plan_step.position != pending.step_position
        or plan_step.action_name != pending.action_name
        or plan_step.resource_key != pending.resource_key
    ):
        # The pending row no longer matches the persisted plan exactly -
        # e.g. tampered directly, or a structural inconsistency this
        # layer never assumes away. Fail closed through the durable
        # confirmation-aware failure path so the pending row is consumed
        # consistently rather than left stuck in WAITING_FOR_CONFIRMATION.
        failed_task = repository.fail_pending_confirmation(
            current_task.task_id,
            confirmation_id,
            _CONFIRMATION_PLAN_MISMATCH_FAILURE_CODE,
            _CONFIRMATION_PLAN_MISMATCH_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _CONFIRMATION_PLAN_MISMATCH_FAILURE_CODE
        )

    # Only now, with the pending confirmation re-bound to and verified
    # against the persisted plan, revalidate against the CURRENT registry/
    # config. Sensitivity itself is re-derived implicitly by this same
    # check having already succeeded once at propose time and being
    # re-checked here for validity, not for whether the action is STILL
    # sensitive - an approval already explicitly obtained from a human is
    # honored even if the action is no longer sensitive by the time it is
    # approved (see this module's own docstring on that policy - it is not
    # a bypass, it is proceeding with an approval already given).
    if not revalidate_action(plan_step.action_name, plan_step.resource_key, registry, tools_config):
        failed_task = repository.fail_pending_confirmation(
            current_task.task_id,
            confirmation_id,
            _ACTION_REVALIDATION_FAILURE_CODE,
            _ACTION_NO_LONGER_VALID_AT_APPROVAL_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task,
            ExecutionAdvanceStatus.TASK_FAILED,
            _ACTION_REVALIDATION_FAILURE_CODE,
        )

    try:
        repository.consume_confirmation_and_claim_step(
            current_task.task_id,
            confirmation_id,
            plan_step.position,
            plan_step.action_name,
            plan_step.resource_key,
            reason_code=_CONFIRMATION_APPROVED_REASON_CODE,
            safe_summary=_CONFIRMATION_APPROVED_SAFE_SUMMARY,
        )
    except ConfirmationExpiredError:
        failed_task = repository.fail_pending_confirmation(
            current_task.task_id,
            confirmation_id,
            _CONFIRMATION_EXPIRED_FAILURE_CODE,
            _CONFIRMATION_EXPIRED_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _CONFIRMATION_EXPIRED_FAILURE_CODE
        )

    # ONLY AFTER COMMIT may external execution occur. Built from the
    # VERIFIED PERSISTED PlanStep - never from task_pending_confirmation's
    # own fields, which were correlation/check values only.
    action_request = ActionRequest(action=plan_step.action_name, resource_key=plan_step.resource_key)
    result = executor.execute(action_request)
    return _finalize_action_step(current_task.task_id, repository, plan_step.position, result)


def deny_task_confirmation(
    task: TaskRecord,
    repository: TaskRepository,
    confirmation_id: str,
) -> ExecutionAdvanceResult:
    """Deny exactly the pending confirmation identified by
    `confirmation_id`: WAITING_FOR_CONFIRMATION -> CANCELLED. No
    replanning - this is a plain cancellation, never a request for a
    different action."""

    if task.state is not TaskState.WAITING_FOR_CONFIRMATION:
        raise TaskNotWaitingForConfirmationError(
            f"expected task to be in 'waiting_for_confirmation' but it is in {task.state.value!r}"
        )

    cancelled_task = repository.deny_confirmation(
        task.task_id,
        confirmation_id,
        reason_code=_CONFIRMATION_DENIED_REASON_CODE,
        safe_summary=_CONFIRMATION_DENIED_SAFE_SUMMARY,
    )
    return ExecutionAdvanceResult(cancelled_task, ExecutionAdvanceStatus.TASK_CANCELLED)
