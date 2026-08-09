"""
Capability-grounding validation for kernel/task_planner/ - a small,
deterministic validation boundary that runs AFTER parse_plan_response()
already succeeded, never inside it.

A catalog action being real and correctly referenced (parser.py's job) is
necessary but not sufficient for plan validity. This module enforces the
remaining invariant: the planner must fail closed when a selected step
introduces an unresolved narrowing - selecting one specific catalog entry
that the request text does not itself justify. Two independent, catalog-
computed triggers feed this (see CatalogEntry.requires_capability_grounding
and catalog.py:_requires_grounding() for the full rule): an intrinsically
narrow capability (open_application, run_registered_script - even the sole
registered one is a narrowing the moment it's picked, since the action's
own semantics reveal nothing about what will happen without that specific
name), and any action with MORE THAN ONE configured resource, regardless of
type (three registered repositories means "check the repository" no longer
resolves to a single referent, the same failure class as scripts). Example:
generic "run the tests" must never silently authorize a specifically-named
run_registered_script("whatsapp_test") capability the request never
mentioned, while an explicit "run the WhatsApp test" legitimately may; the
identical reasoning applies to "check the repository" once more than one
repository is registered.

Deliberately NOT semantic/NLP reasoning: this is a purely lexical, whole-
word, case-insensitive containment check between a selected catalog
entry's resource_key and the task's own request text - no embeddings, no
fuzzy matching, no synonym tables, and no model call. This module itself
never computes either trigger - it only reads the already-computed
requires_capability_grounding flag catalog.py set once per entry, so it
generalizes to any future script, application, or newly-multi-valued
resource type automatically, not just one known case.

Performs no I/O, calls no model, executes nothing, and never mutates its
inputs.
"""

import re

from kernel.task_planner.types import (
    CatalogEntry,
    ParsedPlan,
    PlannerErrorCode,
    PlannerFailure,
    StepKind,
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> frozenset[str]:
    """Whole, case-folded alphanumeric words - punctuation/underscore/
    hyphen/whitespace are all treated as separators, so "whatsapp_test"
    and "WhatsApp test" normalize to the identical {"whatsapp", "test"}.
    Deliberately exact whole-word matching, not substring matching: a
    short token accidentally appearing inside an unrelated longer word
    (e.g. "os" inside "cost") must never count as a match - matching the
    codebase's established "favor false negatives over false positives"
    posture (see kernel/action_protocol/candidates.py's own docstring)."""

    return frozenset(_WORD_RE.findall(text.casefold()))


def validate_capability_grounding(
    parsed_plan: ParsedPlan,
    request_text: str,
    catalog: tuple[CatalogEntry, ...],
) -> PlannerFailure | None:
    """Returns a PlannerFailure (UNGROUNDED_CAPABILITY) if any action step
    selects a named-capability catalog entry whose resource_key is not
    textually grounded in `request_text` - every whole word derived from
    the resource_key must appear as a whole word somewhere in the request
    text. Returns None when every step passes (including every step whose
    catalog entry does not require grounding at all, e.g. repo_health/
    repository_backup/list_files, or a respond step).

    `catalog` must be the exact tuple `parsed_plan` was parsed against -
    the same catalog_id -> CatalogEntry resolution parser.py already
    performed, done again here rather than trusting the already-resolved
    PlanStep so this module's own contract (which fields it actually reads
    from a CatalogEntry) stays independently auditable."""

    catalog_by_id = {entry.catalog_id: entry for entry in catalog}
    request_words = _words(request_text)

    for step in parsed_plan.steps:
        if step.kind is not StepKind.ACTION or step.catalog_id is None:
            continue

        entry = catalog_by_id.get(step.catalog_id)
        if entry is None or not entry.requires_capability_grounding:
            continue
        if entry.resource_key is None:
            continue

        resource_words = _words(entry.resource_key)
        if not resource_words:
            continue

        if not resource_words.issubset(request_words):
            return PlannerFailure(
                error=PlannerErrorCode.UNGROUNDED_CAPABILITY,
                detail=(
                    f"step {step.position} selects a specifically-named capability "
                    "that the request text does not reference"
                ),
            )

    return None
