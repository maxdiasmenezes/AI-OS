"""
Typed data for kernel/task_planner/ (Milestone 41 - Bounded Task Planner):
the model-facing action catalog, the closed step/plan representation, and
the typed outcomes plan_task() and parse_plan_response() return instead of
raising.

Nothing here performs I/O, calls a model, executes an action, or mutates
kernel/employee_tasks/ - these are plain, frozen data shared between
kernel/task_planner/catalog.py, prompt.py, parser.py, and planner.py,
matching kernel/action_protocol/types.py's own convention. plan_version is
fixed at 1 for the whole of Milestone 41; there is no mechanism here for
negotiating a different one.

PlanOutcome is deliberately a four-way closed union so a caller can always
distinguish:
  - TaskPlan            - a valid, fully-catalog-covered, executable plan.
  - CannotPlan           - the model's own explicit decision that the
                            request cannot be safely represented (this is
                            the ONLY branch the empirically-validated M41
                            wire protocol - see prompt.py/parser.py -
                            currently produces for both a genuinely
                            unsupported request and an ambiguous one; Rule
                            2 in prompt.py requires the model to use this
                            branch, with a reason, whenever it cannot tell
                            exactly which operation/target was meant).
  - RequiresClarification - reserved for a request that is understood well
                            enough to know a clarifying question would
                            resolve it, but not well enough to plan.
                            **Not yet producible by parse_plan_response()
                            in this milestone**: the schema validated
                            against real Ollama models (see
                            docs/architecture.md's Milestone 41 section)
                            has only two model-facing branches, "plan" and
                            "cannot_plan" - adding a third would invalidate
                            that empirical validation. This type exists so
                            the domain model does not need a breaking
                            change if a future milestone's schema adds a
                            third branch; today, an ambiguous request is
                            represented as CannotPlan.
  - PlannerFailure        - the raw model response failed closed during
                            parsing/validation (malformed JSON, an unknown
                            catalog_id, an invalid dependency, ...) - never
                            a safe response the model actually intended.

Provider-availability and timeout failures are NOT part of this union -
they remain ordinary exceptions raised by the model layer
(kernel/models/ollama.py) and propagate out of plan_task() uncaught,
exactly like kernel/action_protocol/parser.py's own contract; this module
never turns a provider failure into a persisted task outcome (persistence
is out of scope for this milestone - see kernel/task_planner/__init__.py).
"""

from dataclasses import dataclass
from enum import Enum

PLAN_VERSION = 1

# Fixed, code-level limits shared by prompt.py (declared in the dynamic
# JSON schema's maxLength/maxItems constraints) and parser.py (re-checked
# at parse time - the schema is a strong hint to the model, never the only
# line of defense, matching kernel/action_protocol/parser.py's own
# doctrine). Not user- or config-configurable. No bound is declared here for
# RequiresClarification.question - it has no enforcement site anywhere
# (parser.py cannot produce that type - see the module docstring above), and
# a bound with nothing to enforce it is not declared until a future
# milestone's schema actually adds that branch.
MAX_PLAN_STEPS = 8
MAX_OBJECTIVE_CHARS = 512
MAX_STEP_DESCRIPTION_CHARS = 256
MAX_EXPECTED_RESULT_CHARS = 256
MAX_REASON_CHARS = 256
MAX_DEPENDENCIES_PER_STEP = 4
MAX_RESPONSE_CHARS = 8_000
MAX_JSON_NESTING_DEPTH = 10

# The empirically validated decoding setting for this protocol (see
# docs/architecture.md's Milestone 41 section: gemma3:12b, 97.0% strict
# semantic-plan accuracy against a 33-request corpus, at this exact
# temperature). Not caller-configurable - a different value was never
# validated and must not be silently substituted.
PLANNER_TEMPERATURE_OVERRIDE = 0.0


class StepKind(str, Enum):
    """Inherits str so a member compares equal to, and can be stored/read
    as, its plain string value - matches
    kernel/employee_tasks/types.py:TaskState's own convention."""

    ACTION = "action"
    RESPOND = "respond"


@dataclass(frozen=True)
class CatalogEntry:
    """One immutable, code-owned catalog entry the model may reference by
    its opaque catalog_id (see kernel/task_planner/catalog.py). Built from
    the real ActionRegistry x ToolsConfig, independent of any one request -
    unlike kernel/action_protocol/candidates.py's per-request
    ActionCandidate, this catalog is the same for every planning request
    given the same registry+config state. `summary` is generated by code
    from ActionRegistry metadata only - it must never contain a path,
    command, executable, script content, or secret.

    `requires_capability_grounding` is a fixed, catalog-owned contract
    property (computed by catalog.py:_requires_grounding(), never by a
    model or request text) - the OR of two independent triggers:

      1. Intrinsic (action-type based): True unconditionally for an action
         whose resource_key names one specific, narrowly-purposed
         capability (currently open_application, run_registered_script) -
         a script or application name is a SPECIES (one narrow thing among
         possibly-many); picking even the SOLE configured one is still a
         narrowing, because the action's own semantics ("run a script")
         reveal nothing about what will actually happen without that name.

      2. Structural (cardinality based): True for ANY action - including
         list_files/repo_health/repository_backup - when the catalog has
         MORE THAN ONE configured resource for it. A phrase like "the
         repository" is a GENUS: with exactly one repository registered it
         denotes that one with no narrowing at all (grounding not
         required - already correctly handled by prompt.py's Rule 2
         ambiguity guidance), but with two or more registered, selecting
         one specific repository without the request text resolving which
         is the same silent-narrowing failure class trigger 1 exists to
         catch for scripts.

    False only when neither trigger applies (a genus-type action with
    exactly one configured resource). See kernel/task_planner/grounding.py,
    the only reader of this field - parser.py never inspects it, and it is
    never sent to the model."""

    catalog_id: str
    action_name: str
    resource_key: str | None
    sensitive: bool
    summary: str
    requires_capability_grounding: bool = False


@dataclass(frozen=True)
class PlanStep:
    """One step of a validated TaskPlan. `step_id`/`position` are always
    code-generated from the step's array position - never model text (no
    "ordinal" field exists in the wire protocol at all, so there is
    nothing for the model to get wrong or for a caller to trust
    incorrectly). `depends_on` may only reference strictly earlier
    positions in the same plan (enforced by parser.py), which makes a
    dependency cycle structurally unreachable rather than merely
    checked-for. `action_name`/`resource_key`/`requires_confirmation` are
    None/False for a RESPOND step and always resolved from the exact
    CatalogEntry the model selected (never reconstructed from model text)
    for an ACTION step. `requires_confirmation` is derived deterministically
    from ActionRegistry.is_sensitive() by the parser (K1) - it is not, and
    can never be, a field the model supplies (see prompt.py/parser.py -
    the schema has no such field for either step kind)."""

    step_id: str
    position: int
    kind: StepKind
    action_name: str | None
    resource_key: str | None
    catalog_id: str | None
    description: str
    expected_result: str
    depends_on: tuple[int, ...]
    requires_confirmation: bool


@dataclass(frozen=True)
class ParsedPlan:
    """Parser-level result for a successful "plan" response - no task
    identity yet (the parser never sees a TaskRecord). planner.py's
    plan_task() wraps this into the final, task-stamped TaskPlan."""

    plan_version: int
    objective: str
    steps: tuple[PlanStep, ...]


@dataclass(frozen=True)
class TaskPlan:
    """The final, immutable, task-stamped plan planner.py:plan_task()
    returns on success. `task_id` and `created_at` are always
    code-generated (from the TaskRecord passed to plan_task(), and the
    system clock, respectively) - never model text and never present as
    fields the model could set in the wire protocol."""

    plan_version: int
    task_id: str
    objective: str
    steps: tuple[PlanStep, ...]
    created_at: str


@dataclass(frozen=True)
class CannotPlan:
    """The model's own, well-formed decision that the request cannot be
    safely represented using only the supplied catalog - see Rule 1/Rule 2
    in prompt.py. `reason` is the model's bounded free-text explanation;
    it is informational only and this layer never parses or acts on its
    content."""

    plan_version: int
    reason: str


@dataclass(frozen=True)
class RequiresClarification:
    """Reserved domain type - see the module docstring above. Not
    producible by the current parse_plan_response()."""

    plan_version: int
    question: str


class PlannerErrorCode(Enum):
    """Typed protocol-parsing failure categories - mirrors
    kernel/action_protocol/types.py:ParseErrorCode's convention. Every one
    of these is a problem with the model's *output*, never a
    provider-availability or timeout problem (those stay ordinary
    exceptions - see the module docstring above). CYCLIC_PLAN is
    deliberately absent: a dependency cycle is structurally unreachable
    given depends_on may only reference strictly earlier array positions
    (see PlanStep above and parser.py), so the parser can never actually
    raise it.

    UNGROUNDED_CAPABILITY is not raised by parser.py at all - it is
    produced only by kernel/task_planner/grounding.py's post-parse
    validate_capability_grounding(), called from planner.py after a
    structurally valid ParsedPlan is already in hand. It means: every
    catalog_id was real and correctly referenced (parsing succeeded), but
    a selected named-capability action (see CatalogEntry.
    requires_capability_grounding above) is narrower or materially
    different from what the request text actually asked for - a real
    catalog action being correctly referenced is necessary but not
    sufficient for plan validity."""

    EMPTY_RESPONSE = "empty_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    MALFORMED_PLAN = "malformed_plan"
    DUPLICATE_KEY = "duplicate_key"
    INVALID_CONSTANT = "invalid_constant"
    EXCESSIVE_NESTING = "excessive_nesting"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    UNSUPPORTED_PLAN_VERSION = "unsupported_plan_version"
    TOO_MANY_STEPS = "too_many_steps"
    INVALID_STEP = "invalid_step"
    UNKNOWN_ACTION = "unknown_action"
    INVALID_DEPENDENCY = "invalid_dependency"
    UNGROUNDED_CAPABILITY = "ungrounded_capability"


@dataclass(frozen=True)
class PlannerFailure:
    """A failed parse. `detail` is a short, fixed, human-readable phrase
    describing the failure category - it never contains, quotes, or
    echoes any part of the raw model response (matches
    kernel/action_protocol/parser.py's "do not log raw model text"
    requirement)."""

    error: PlannerErrorCode
    detail: str


# What parser.py:parse_plan_response() returns - no task identity yet.
ParseOutcome = ParsedPlan | CannotPlan | RequiresClarification | PlannerFailure

# What planner.py:plan_task() returns - ParsedPlan promoted to a
# task-stamped TaskPlan on success, every other outcome passed through
# unchanged.
PlanOutcome = TaskPlan | CannotPlan | RequiresClarification | PlannerFailure
