"""
advance_task_execution() / approve_task_confirmation() /
deny_task_confirmation(): Milestone 42 P2's one-step-at-a-time execution
primitive.

Mirrors kernel/task_orchestration/service.py's own discipline: no internal
loop (advance_task_execution() processes exactly one plan step, or one
confirmation lifecycle event, per call - never more), every dependency
(TaskRepository, ActionRegistry, ToolsConfig, SafeTaskExecutor) is
explicitly injected by the caller, never constructed here. This module
never opens a database connection, never loads kernel/config/tools.yaml,
never constructs an ActionRegistry/ToolsConfig/SafeTaskExecutor, and never
constructs a model provider - callers build all of that and pass it in.

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

RESPOND steps are not supported yet (Milestone 42 P3): an eligible RESPOND
step fails the task closed with a fixed code
(_RESPOND_NOT_SUPPORTED_FAILURE_CODE) and calls no model. No runtime/
interface/WhatsApp wiring exists here or anywhere in this package -
nothing calls advance_task_execution()/approve_task_confirmation()/
deny_task_confirmation() from a real request as of this milestone.

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
from kernel.task_execution.eligibility import (
    evaluate_next_step,
    resolve_persisted_plan_step,
    revalidate_action,
)
from kernel.task_execution.observation import build_action_observation, serialize_observation
from kernel.task_execution.types import (
    ActionRevalidationFailure,
    AllStepsComplete,
    Blocked,
    EligibilityBlockReason,
    EligibleStep,
    ExecutionAdvanceResult,
    ExecutionAdvanceStatus,
    PlanDeserializationFailure,
    PlanIntegrityFailure,
    TASK_CONFIRMATION_TTL_SECONDS,
)
from kernel.task_planner import StepKind
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

_RESPOND_NOT_SUPPORTED_FAILURE_CODE = "respond_step_not_supported"
_RESPOND_NOT_SUPPORTED_FAILURE_SUMMARY = (
    "RESPOND steps are not yet supported by the execution engine."
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


def _finalize_action_step(
    task_id: str,
    repository: TaskRepository,
    step_position: int,
    result: ActionResult,
) -> ExecutionAdvanceResult:
    """Shared by the non-sensitive execution path and the post-approval
    execution path: build and persist the step's StepObservation, then
    propagate success/failure to the task. Always re-fetches the task
    fresh after mutating - never trusts a value computed before the
    executor call, since a concurrent writer may have moved the task to a
    terminal state while SafeTaskExecutor.execute() was in flight (see
    module docstring's cancellation-semantics section)."""

    completed_at = _now_iso()
    observation = build_action_observation(step_position, result, completed_at)
    observation_json = serialize_observation(observation)

    if result.success:
        repository.mark_step_succeeded(task_id, step_position, observation_json)
        current = repository.get_task(task_id)
        if current.state is TaskState.RUNNING:
            return ExecutionAdvanceResult(current, ExecutionAdvanceStatus.STEP_SUCCEEDED, result.outcome)
        # The task moved on (e.g. CANCELLED) while the action was
        # in flight - the step's own success is still durably recorded
        # above, but the task's actual current terminal state is what
        # gets reported here, never a fabricated STEP_SUCCEEDED.
        return _terminal_result(current)

    try:
        failed_task = repository.fail_running_step(
            task_id,
            step_position,
            failure_code=result.outcome,
            failure_summary=result.message,
            result_json=observation_json,
        )
        return ExecutionAdvanceResult(failed_task, ExecutionAdvanceStatus.TASK_FAILED, result.outcome)
    except TaskAlreadyTerminalError:
        # The task already moved to a terminal state (e.g. CANCELLED) by a
        # concurrent writer between claim and this point - the
        # already-authorized action still ran and its outcome must not be
        # lost, but the task itself must not be overwritten. Finalize just
        # the step, for audit/recovery, and report the task's actual
        # (unchanged) terminal state.
        repository.mark_step_failed(
            task_id,
            step_position,
            failure_code=result.outcome,
            failure_summary=result.message,
            result_json=observation_json,
        )
        current = repository.get_task(task_id)
        return _terminal_result(current)


def _process_running_task(
    task_id: str,
    repository: TaskRepository,
    registry: ActionRegistry,
    tools_config: ToolsConfig,
    executor: SafeTaskExecutor,
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
        # Milestone 42 P2 does not support RESPOND yet - see module
        # docstring. No model call. This temporary behavior disappears in
        # Milestone 42 P3.
        failed_task = repository.mark_failed(
            task_id,
            TaskState.RUNNING,
            _RESPOND_NOT_SUPPORTED_FAILURE_CODE,
            _RESPOND_NOT_SUPPORTED_FAILURE_SUMMARY,
        )
        return ExecutionAdvanceResult(
            failed_task, ExecutionAdvanceStatus.TASK_FAILED, _RESPOND_NOT_SUPPORTED_FAILURE_CODE
        )

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
) -> ExecutionAdvanceResult:
    """The one-step-at-a-time execution primitive. Processes exactly one
    plan step, or one confirmation-related event, per call - never loops
    internally. `task` is used only for its initial state dispatch (a fast
    rejection for a wrong starting state, mirroring
    kernel.task_orchestration.service.advance_task_planning()'s own
    doctrine) - every actual decision re-reads fresh state from
    `repository`."""

    if task.state is TaskState.READY:
        running_task = repository.transition_task(
            task.task_id,
            TaskState.READY,
            TaskState.RUNNING,
            reason_code=_TASK_STARTED_REASON_CODE,
            safe_summary=_TASK_STARTED_SAFE_SUMMARY,
        )
        return _process_running_task(running_task.task_id, repository, registry, tools_config, executor)

    if task.state is TaskState.RUNNING:
        return _process_running_task(task.task_id, repository, registry, tools_config, executor)

    if task.state is TaskState.WAITING_FOR_CONFIRMATION:
        return _process_waiting_task(task.task_id, repository)

    if task.state in TERMINAL_STATES:
        return _terminal_result(task)

    raise TaskNotReadyOrRunningError(
        f"expected task to be in 'ready', 'running', or 'waiting_for_confirmation' "
        f"but it is in {task.state.value!r}"
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
