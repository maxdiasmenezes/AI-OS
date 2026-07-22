"""
Wine Data Readiness and Acceptance Check v1: a human-invoked, read-only
utility that verifies a local wine profile and cellar are structurally
usable by the existing system, without writing anything.

A local maintenance script, not part of the runtime kernel. By default it
never calls a model provider and never touches persistent memory - it uses
small private test doubles (a fail-fast provider/memory for deterministic
checks, a recording fake provider and a no-op memory for prompt-assembly
checks) so it can exercise the real WineCapability, cellar_schema, and
cellar_lookup code paths without any side effects. It reads the local
profile and cellar JSON files directly and never writes to them, to any
other file, or to storage/memory or storage/logs.

Deterministic acceptance cases are derived from the real cellar data itself
(sorted by record id for determinism), never invented or hard-coded to a
particular producer or wine. A condition-dependent case (e.g. a region, a
zero-quantity identity, an ambiguous wine name) that has no matching fixture
in the real data is reported SKIPPED with a clear reason rather than failing
or being silently omitted.

The real, configured model provider is only constructed - lazily, inside the
opt-in path - when --call-model is passed explicitly. Those responses are
printed for human review and are never asserted against exact wording;
running without --call-model works even when no local model server is
running.

Usage:
    uv run python -m scripts.wine_acceptance_check
    uv run python -m scripts.wine_acceptance_check --call-model
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from capabilities.wine.capability import WineCapability
from capabilities.wine.cellar_schema import validate_cellar_record
from kernel.knowledge import JSONKnowledgeStore
from kernel.models.base import ModelProvider, ModelResponse

# scripts/wine_acceptance_check.py -> scripts -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PROFILE_PATH = _PROJECT_ROOT / "storage" / "knowledge" / "wine_profile.json"
_DEFAULT_CELLAR_PATH = _PROJECT_ROOT / "storage" / "knowledge" / "wine_cellar.json"

_FALLBACK_PROMPT_PATH = _PROJECT_ROOT / "prompts" / "wine" / "fallback.md"
_FALLBACK_INSTRUCTIONS = _FALLBACK_PROMPT_PATH.read_text(encoding="utf-8").strip()

# Recognized wine-profile fields. Kept in sync with, but intentionally not
# imported from, capabilities/wine/capability.py's private field lists -
# this script performs only minimal onboarding safety checks and explicitly
# does not duplicate the complete profile rendering/prompt-assembly logic,
# which is instead exercised for real via run_prompt_assembly_check().
_PROFILE_LIST_FIELDS = ("preferred_styles", "disliked_styles", "priorities")
_PROFILE_STRING_FIELDS = ("budget_range", "notes")


class WineAcceptanceError(ValueError):
    """Raised for profile, cellar, or acceptance setup problems."""


# --- Private test doubles ---------------------------------------------------


class _NoOpMemory:
    """Empty/no-op memory: no recalled history, records nothing, writes nothing."""

    def recall(self, namespace: str, limit: int | None = None) -> list:
        return []

    def remember(self, namespace: str, content: str, metadata: dict | None = None) -> None:
        return None


class _FailFastMemory:
    """Raises immediately if touched, proving a deterministic path avoids memory."""

    def recall(self, namespace: str, limit: int | None = None):
        raise AssertionError("deterministic acceptance check must not access memory")

    def remember(self, namespace: str, content: str, metadata: dict | None = None):
        raise AssertionError("deterministic acceptance check must not access memory")


class _FailFastProvider(ModelProvider):
    """Raises immediately if called, proving a deterministic path avoids the provider."""

    def __init__(self):
        pass

    def send_prompt(self, prompt: str) -> ModelResponse:
        raise AssertionError("deterministic acceptance check must not call the model provider")


class _RecordingFakeProvider(ModelProvider):
    """Captures the fallback prompt it receives and returns a fixed, non-empty response."""

    def __init__(self):
        self.received_prompts: list[str] = []
        self.response = ModelResponse(
            text="Acceptance check placeholder pairing suggestion.",
            model="acceptance-check-fake-model",
            input_tokens=1,
            output_tokens=1,
            latency_seconds=0.0,
        )

    def send_prompt(self, prompt: str) -> ModelResponse:
        self.received_prompts.append(prompt)
        return self.response


# --- Result types ------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS", "FAIL", "SKIP", or "MANUAL REVIEW"
    detail: str


@dataclass
class CellarStats:
    total_holdings: int
    active_holdings: int
    zero_holdings: int
    active_bottle_total: int
    producer_count: int
    country_count: int
    region_count: int


@dataclass
class CaseSelection:
    """Representative deterministic query targets, derived from real cellar data."""

    primary_record_id: str
    primary_producer: str
    primary_wine_name: str
    primary_active_quantity: int
    primary_producer_active_quantity: int
    primary_vintages: list[object]
    zero_quantity_record: tuple[str, dict[str, object]] | None
    ambiguous_wine_name: str | None
    ambiguous_producers: list[str] | None
    region: str | None
    country: str | None
    multi_vintage_producer: str | None
    multi_vintage_wine_name: str | None
    multi_vintage_vintages: list[object] | None


@dataclass
class AcceptanceResult:
    profile_path: Path
    cellar_path: Path
    profile_check: CheckResult
    cellar_check: CheckResult
    cellar_not_empty_check: CheckResult | None
    cellar_stats: CellarStats | None
    deterministic_checks: list[CheckResult]
    prompt_assembly_check: CheckResult
    manual_review_checks: list[CheckResult]
    call_model: bool
    provider_usable: bool | None
    required_passed: int
    required_total: int
    optional_skipped: int
    success: bool


# --- Profile loading ---------------------------------------------------------


def _validate_profile_fields(profile: dict[str, object]) -> None:
    """Minimal onboarding safety checks for recognized profile fields.

    Not a duplicate of WineCapability's full profile rendering rules (e.g. it
    does not reject empty-string list items) - just enough to catch a
    structurally broken profile before it reaches the real fallback path.
    """

    for field in _PROFILE_LIST_FIELDS:
        if field not in profile:
            continue
        value = profile[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise WineAcceptanceError(f"profile field {field!r} must be a list of strings")

    for field in _PROFILE_STRING_FIELDS:
        if field not in profile:
            continue
        if not isinstance(profile[field], str):
            raise WineAcceptanceError(f"profile field {field!r} must be a string")


def _has_usable_profile_value(profile: dict[str, object]) -> bool:
    for field in _PROFILE_LIST_FIELDS:
        value = profile.get(field)
        if isinstance(value, list) and any(isinstance(item, str) and item for item in value):
            return True
    for field in _PROFILE_STRING_FIELDS:
        value = profile.get(field)
        if isinstance(value, str) and value:
            return True
    return False


def load_profile(profile_path: str | Path) -> dict[str, object]:
    """Load and minimally validate the wine profile document.

    Returns the 'profile' record unchanged (never mutated). Raises
    WineAcceptanceError for a missing or unreadable file, malformed JSON, a
    non-object top level, a missing or non-object 'profile' record, an
    invalid recognized field, or a profile with no usable recognized value.
    Unknown fields are permitted and ignored.
    """

    path = Path(profile_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise WineAcceptanceError(f"profile file not found: {path}") from e
    except OSError as e:
        raise WineAcceptanceError(f"profile file is unreadable: {path}: {e}") from e

    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise WineAcceptanceError(f"{path}: malformed JSON ({e})") from e

    if not isinstance(document, dict):
        raise WineAcceptanceError(f"{path}: top-level JSON value must be an object")

    if "profile" not in document:
        raise WineAcceptanceError(f"{path}: missing top-level 'profile' record")

    profile = document["profile"]
    if not isinstance(profile, dict):
        raise WineAcceptanceError(f"{path}: 'profile' value must be an object")

    _validate_profile_fields(profile)

    if not _has_usable_profile_value(profile):
        recognized = ", ".join(_PROFILE_LIST_FIELDS + _PROFILE_STRING_FIELDS)
        raise WineAcceptanceError(
            f"{path}: profile contains no usable value in any recognized field ({recognized})"
        )

    return profile


# --- Cellar loading and statistics -------------------------------------------


def load_cellar(cellar_path: str | Path) -> dict[str, dict[str, object]]:
    """Load and fully schema-validate every cellar record.

    Raises WineAcceptanceError for a missing or unreadable file, malformed
    JSON, a non-object top level, a non-object record, or any record failing
    validate_cellar_record() - including unrelated and zero-quantity
    records. One invalid record fails the entire load.
    """

    path = Path(cellar_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise WineAcceptanceError(f"cellar file not found: {path}") from e
    except OSError as e:
        raise WineAcceptanceError(f"cellar file is unreadable: {path}: {e}") from e

    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise WineAcceptanceError(f"{path}: malformed JSON ({e})") from e

    if not isinstance(document, dict):
        raise WineAcceptanceError(f"{path}: top-level JSON value must be an object")

    validated: dict[str, dict[str, object]] = {}
    for record_id, record in document.items():
        if not isinstance(record, dict):
            raise WineAcceptanceError(f"{path}: cellar record {record_id!r} must be an object")
        try:
            validated[record_id] = validate_cellar_record(record_id, record)
        except ValueError as e:
            raise WineAcceptanceError(str(e)) from e

    return validated


def compute_cellar_stats(validated: dict[str, dict[str, object]]) -> CellarStats:
    """Compute factual cellar statistics from already-validated records.

    Producer, country, and region counts are computed over active
    (quantity > 0) holdings only, consistent with how WineCapability and
    cellar_lookup.py treat "the cellar" everywhere else.
    """

    active = {rid: fields for rid, fields in validated.items() if fields["quantity"] > 0}
    return CellarStats(
        total_holdings=len(validated),
        active_holdings=len(active),
        zero_holdings=len(validated) - len(active),
        active_bottle_total=sum(fields["quantity"] for fields in active.values()),
        producer_count=len({fields["producer"] for fields in active.values()}),
        country_count=len({fields["country"] for fields in active.values() if "country" in fields}),
        region_count=len({fields["region"] for fields in active.values() if "region" in fields}),
    )


# --- Deterministic case selection --------------------------------------------


def _normalize(text: str) -> str:
    """Whitespace-collapsed, case-folded text - just enough for identity grouping."""

    return " ".join(text.split()).casefold()


def _vintage_sort_key(vintage: object) -> tuple[int, object]:
    return (1, 0) if vintage == "NV" else (0, vintage)


def select_cases(validated: dict[str, dict[str, object]]) -> CaseSelection | None:
    """Deterministically select representative query targets from real cellar data.

    Selection is driven entirely by sorted record ids and real field values -
    nothing is invented. Returns None when there is no active (quantity > 0)
    holding to build a primary case from.
    """

    active = {rid: fields for rid, fields in validated.items() if fields["quantity"] > 0}
    if not active:
        return None

    primary_id = sorted(active)[0]
    primary = active[primary_id]
    primary_producer = primary["producer"]
    primary_wine_name = primary["wine_name"]
    primary_key = (_normalize(primary_producer), _normalize(primary_wine_name))

    identity_active = {
        rid: fields
        for rid, fields in active.items()
        if (_normalize(fields["producer"]), _normalize(fields["wine_name"])) == primary_key
    }
    primary_active_quantity = sum(fields["quantity"] for fields in identity_active.values())
    primary_vintages = sorted(
        {fields["vintage"] for fields in identity_active.values() if "vintage" in fields},
        key=_vintage_sort_key,
    )

    producer_active = {
        rid: fields
        for rid, fields in active.items()
        if _normalize(fields["producer"]) == _normalize(primary_producer)
    }
    primary_producer_active_quantity = sum(fields["quantity"] for fields in producer_active.values())

    # A zero-quantity record only proves the zero-quantity response path when
    # every record sharing its identity is also zero-quantity - otherwise the
    # identity would legitimately report a non-zero active total instead.
    zero_quantity_record: tuple[str, dict[str, object]] | None = None
    for rid in sorted(validated):
        fields = validated[rid]
        if fields["quantity"] != 0:
            continue
        identity_key = (_normalize(fields["producer"]), _normalize(fields["wine_name"]))
        identity_total = sum(
            other["quantity"]
            for other in validated.values()
            if (_normalize(other["producer"]), _normalize(other["wine_name"])) == identity_key
        )
        if identity_total == 0:
            zero_quantity_record = (rid, fields)
            break

    ambiguous_wine_name: str | None = None
    ambiguous_producers: list[str] | None = None
    name_to_producers: dict[str, set[str]] = {}
    for fields in validated.values():
        name_to_producers.setdefault(_normalize(fields["wine_name"]), set()).add(fields["producer"])
    for rid in sorted(validated):
        fields = validated[rid]
        producers = name_to_producers[_normalize(fields["wine_name"])]
        if len(producers) > 1:
            ambiguous_wine_name = fields["wine_name"]
            ambiguous_producers = sorted(producers, key=_normalize)
            break

    region = next((active[rid]["region"] for rid in sorted(active) if "region" in active[rid]), None)
    country = next((active[rid]["country"] for rid in sorted(active) if "country" in active[rid]), None)

    multi_vintage_producer: str | None = None
    multi_vintage_wine_name: str | None = None
    multi_vintage_vintages: list[object] | None = None
    identity_vintage_sets: dict[tuple[str, str], set[object]] = {}
    identity_display: dict[tuple[str, str], tuple[str, str]] = {}
    for rid in sorted(active):
        fields = active[rid]
        if "vintage" not in fields:
            continue
        key = (_normalize(fields["producer"]), _normalize(fields["wine_name"]))
        identity_vintage_sets.setdefault(key, set()).add(fields["vintage"])
        identity_display.setdefault(key, (fields["producer"], fields["wine_name"]))
    for key in sorted(identity_vintage_sets):
        vintages = identity_vintage_sets[key]
        if len(vintages) > 1:
            multi_vintage_producer, multi_vintage_wine_name = identity_display[key]
            multi_vintage_vintages = sorted(vintages, key=_vintage_sort_key)
            break

    return CaseSelection(
        primary_record_id=primary_id,
        primary_producer=primary_producer,
        primary_wine_name=primary_wine_name,
        primary_active_quantity=primary_active_quantity,
        primary_producer_active_quantity=primary_producer_active_quantity,
        primary_vintages=primary_vintages,
        zero_quantity_record=zero_quantity_record,
        ambiguous_wine_name=ambiguous_wine_name,
        ambiguous_producers=ambiguous_producers,
        region=region,
        country=country,
        multi_vintage_producer=multi_vintage_producer,
        multi_vintage_wine_name=multi_vintage_wine_name,
        multi_vintage_vintages=multi_vintage_vintages,
    )


def _build_knowledge_store(profile_path: Path, cellar_path: Path) -> JSONKnowledgeStore:
    """Build the single JSONKnowledgeStore WineCapability needs for both namespaces.

    WineCapability reads both the "wine_profile" and "wine_cellar" namespaces
    from one injected KnowledgeStore, so the profile and cellar files must
    live in the same directory and keep the exact filenames
    JSONKnowledgeStore's namespace convention expects.
    """

    if profile_path.parent != cellar_path.parent:
        raise WineAcceptanceError(
            "profile and cellar files must be in the same directory to be read "
            f"through KnowledgeStore (got {profile_path.parent} and {cellar_path.parent})"
        )
    if profile_path.name != "wine_profile.json":
        raise WineAcceptanceError(
            f"profile file must be named 'wine_profile.json' to match KnowledgeStore's "
            f"namespace convention, got {profile_path.name!r}"
        )
    if cellar_path.name != "wine_cellar.json":
        raise WineAcceptanceError(
            f"cellar file must be named 'wine_cellar.json' to match KnowledgeStore's "
            f"namespace convention, got {cellar_path.name!r}"
        )

    return JSONKnowledgeStore(profile_path.parent)


# --- Deterministic acceptance cases (section 8) ------------------------------


def run_deterministic_checks(
    knowledge_store: JSONKnowledgeStore,
    validated_cellar: dict[str, dict[str, object]],
    stats: CellarStats,
) -> list[CheckResult]:
    """Run cases A-K through the real WineCapability.handle() integration.

    Uses a fail-fast provider and a fail-fast memory object throughout, so
    any provider or memory access - which a truly deterministic cellar query
    should never trigger - surfaces as an immediate check failure.
    """

    wine = WineCapability(_FailFastProvider(), _FailFastMemory(), knowledge_store)

    def _run(name: str, query: str, verify: Callable[[str], tuple[bool, str]]) -> CheckResult:
        try:
            response = wine.handle(query)
        except AssertionError as e:
            return CheckResult(name, "FAIL", f"unexpected provider or memory access: {e}")
        except ValueError as e:
            return CheckResult(name, "FAIL", f"cellar validation failed during query: {e}")

        if not isinstance(response, str):
            return CheckResult(name, "FAIL", "expected a deterministic string response, got a model response")

        ok, detail = verify(response)
        return CheckResult(name, "PASS" if ok else "FAIL", detail)

    checks: list[CheckResult] = []

    # A. Total bottle count
    checks.append(_run(
        "total_bottle_count",
        "How many bottles of wine do I have in total?",
        lambda r: (
            str(stats.active_bottle_total) in r,
            f"expected {stats.active_bottle_total} active bottles reflected; got: {r}",
        ),
    ))

    selection = select_cases(validated_cellar)
    if selection is None:
        reason = "cellar has no active (quantity > 0) holdings"
        for name in (
            "exact_quantity",
            "producer_ownership",
            "producer_holdings_listing",
            "vintage_listing",
            "region_ownership",
            "country_ownership",
            "zero_quantity_behavior",
            "ambiguous_wine_name",
            "multiple_vintages",
        ):
            checks.append(CheckResult(name, "SKIP", reason))
    else:
        # B. Exact quantity across vintages for one producer+wine identity
        checks.append(_run(
            "exact_quantity",
            f"How many bottles of {selection.primary_producer} {selection.primary_wine_name} are in my cellar?",
            lambda r: (
                str(selection.primary_active_quantity) in r,
                f"expected {selection.primary_active_quantity} bottles; got: {r}",
            ),
        ))

        # C. Producer ownership
        checks.append(_run(
            "producer_ownership",
            f"Do I have any {selection.primary_producer} wine?",
            lambda r: (
                "Yes, you have" in r and str(selection.primary_producer_active_quantity) in r,
                f"expected ownership confirmation with {selection.primary_producer_active_quantity} bottles; got: {r}",
            ),
        ))

        # D. Producer holdings listing
        checks.append(_run(
            "producer_holdings_listing",
            f"Show me my wines from {selection.primary_producer}.",
            lambda r: (
                bool(r) and selection.primary_wine_name in r,
                f"expected a non-empty listing containing {selection.primary_wine_name!r}; got: {r}",
            ),
        ))

        # E. Vintage listing
        if selection.primary_vintages:
            expected_vintage = selection.primary_vintages[0]
            checks.append(_run(
                "vintage_listing",
                f"What vintages of {selection.primary_producer} {selection.primary_wine_name} are in my cellar?",
                lambda r: (
                    str(expected_vintage) in r,
                    f"expected vintage {expected_vintage} present; got: {r}",
                ),
            ))
        else:
            checks.append(CheckResult(
                "vintage_listing", "SKIP",
                "no active record for the selected producer+wine identity has a vintage",
            ))

        # F. Region ownership
        if selection.region is not None:
            checks.append(_run(
                "region_ownership",
                f"Do I have any {selection.region} wine?",
                lambda r: (
                    "Yes, you have" in r and f'"{selection.region}"' in r,
                    f"expected region ownership confirmation for {selection.region!r}; got: {r}",
                ),
            ))
        else:
            checks.append(CheckResult("region_ownership", "SKIP", "no active cellar record has a region"))

        # G. Country ownership
        if selection.country is not None:
            checks.append(_run(
                "country_ownership",
                f"Do I have any {selection.country} wine?",
                lambda r: (
                    "Yes, you have" in r and f'"{selection.country}"' in r,
                    f"expected country ownership confirmation for {selection.country!r}; got: {r}",
                ),
            ))
        else:
            checks.append(CheckResult("country_ownership", "SKIP", "no active cellar record has a country"))

        # H. Zero-quantity behavior
        if selection.zero_quantity_record is not None:
            _, fields = selection.zero_quantity_record
            checks.append(_run(
                "zero_quantity_behavior",
                f"How many bottles of {fields['producer']} {fields['wine_name']} are in my cellar?",
                lambda r: (
                    "active quantity is zero" in r,
                    f"expected a zero-active-quantity response; got: {r}",
                ),
            ))
        else:
            checks.append(CheckResult(
                "zero_quantity_behavior", "SKIP",
                "no cellar identity exists where every matching record is zero-quantity",
            ))

        # I. Ambiguous wine name
        if selection.ambiguous_wine_name is not None:
            checks.append(_run(
                "ambiguous_wine_name",
                f"How many bottles of {selection.ambiguous_wine_name} are in my cellar?",
                lambda r: (
                    "Multiple producers" in r and "Include the producer name" in r,
                    f"expected a producer-clarification request; got: {r}",
                ),
            ))
        else:
            checks.append(CheckResult(
                "ambiguous_wine_name", "SKIP", "no wine_name is shared by more than one producer",
            ))

        # J. Multiple vintages
        if selection.multi_vintage_producer is not None:
            expected_vintages = selection.multi_vintage_vintages
            checks.append(_run(
                "multiple_vintages",
                f"What vintages of {selection.multi_vintage_producer} {selection.multi_vintage_wine_name} are in my cellar?",
                lambda r: (
                    all(str(v) in r for v in expected_vintages),
                    f"expected vintages {expected_vintages} present; got: {r}",
                ),
            ))
        else:
            checks.append(CheckResult(
                "multiple_vintages", "SKIP", "no producer+wine identity has more than one active vintage",
            ))

    # K. Unknown wine - an obviously synthetic target that cannot exist in real data
    checks.append(_run(
        "unknown_wine",
        "How many bottles of Nonexistent Acceptance Winery XYZ987 are in my cellar?",
        lambda r: (
            "no cellar record matching" in r,
            f"expected a no-match response; got: {r}",
        ),
    ))

    return checks


# --- Prompt-assembly acceptance (section 9) ----------------------------------


def _profile_values(profile: dict[str, object]) -> list[str]:
    values: list[str] = []
    for field in _PROFILE_LIST_FIELDS:
        value = profile.get(field)
        if isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str) and item)
    for field in _PROFILE_STRING_FIELDS:
        value = profile.get(field)
        if isinstance(value, str) and value:
            values.append(value)
    return values


def run_prompt_assembly_check(
    knowledge_store: JSONKnowledgeStore,
    profile: dict[str, object] | None,
    validated_cellar: dict[str, dict[str, object]],
) -> CheckResult:
    """Exercise the real model-backed fallback prompt assembly, structurally only.

    Uses the empty/no-op memory and a recording fake provider. Never prints
    or returns the complete captured prompt, since it contains personal data
    - only a pass/fail summary naming what was checked.
    """

    provider = _RecordingFakeProvider()
    wine = WineCapability(provider, _NoOpMemory(), knowledge_store)

    request = "What would you recommend for dinner tonight?"
    try:
        response = wine.handle(request)
    except ValueError as e:
        return CheckResult("prompt_assembly", "FAIL", f"fallback prompt assembly raised: {e}")

    if len(provider.received_prompts) != 1:
        return CheckResult(
            "prompt_assembly", "FAIL",
            f"expected the provider to be called exactly once, got {len(provider.received_prompts)}",
        )

    prompt = provider.received_prompts[0]
    problems: list[str] = []

    if not prompt:
        problems.append("captured prompt is empty")
    if _FALLBACK_INSTRUCTIONS not in prompt:
        problems.append("missing wine-expert instructions")

    if "Personal wine profile:" not in prompt:
        problems.append("missing personal-profile section")
    elif not any(value in prompt for value in _profile_values(profile or {})):
        problems.append("no configured profile value found in the prompt")

    if "Personal wine cellar:" not in prompt:
        problems.append("missing personal-cellar section")
    else:
        active_names = [
            name
            for fields in validated_cellar.values()
            if fields["quantity"] > 0
            for name in (fields["producer"], fields["wine_name"])
        ]
        if not any(name in prompt for name in active_names):
            problems.append("no active producer or wine name found in the prompt")

    if not isinstance(response, ModelResponse) or not response.text:
        problems.append("provider returned an empty or invalid response")

    if problems:
        return CheckResult("prompt_assembly", "FAIL", "; ".join(problems))

    return CheckResult(
        "prompt_assembly", "PASS",
        "provider called exactly once; prompt contains instructions, profile, and cellar sections",
    )


# --- Optional real-model checks (section 10) ---------------------------------


def _default_provider_factory() -> ModelProvider:
    """Lazily construct the real, configured provider - only reached via --call-model."""

    from kernel.config.config import load_config
    from kernel.models import get_provider

    return get_provider(load_config())


def _build_real_model_requests(profile: dict[str, object] | None) -> list[tuple[str, str]]:
    preferred_style = None
    if profile:
        styles = profile.get("preferred_styles")
        if isinstance(styles, list) and styles and isinstance(styles[0], str):
            preferred_style = styles[0]
    style_phrase = preferred_style or "a style I usually enjoy"

    return [
        ("ordinary_weekday_meal", "What wine should I have with dinner tonight on a normal weeknight?"),
        ("special_dinner", "I'm hosting a special anniversary dinner tonight - what should I open?"),
        ("preferred_style_or_region", f"Can you recommend something in the style of {style_phrase}?"),
        ("cellar_constrained", "Recommend a wine to drink this week, using only bottles from my own cellar."),
        ("no_obvious_cellar_match", "What wine would you suggest for a dish you have no information about?"),
    ]


def run_real_model_checks(
    profile_path: Path,
    cellar_path: Path,
    profile: dict[str, object] | None,
    provider_factory: Callable[[], ModelProvider] | None,
) -> tuple[list[CheckResult], bool]:
    """Construct the real provider and run a concise set of model-backed prompts.

    Returns (results, provider_usable). Responses are labeled MANUAL REVIEW
    and printed for a human, never asserted against exact wording. Provider
    construction/connectivity failures produce a single FAIL result and
    provider_usable=False; a per-request exception or empty response produces
    a FAIL result for that request only.
    """

    factory = provider_factory if provider_factory is not None else _default_provider_factory

    try:
        provider = factory()
    except Exception as e:
        return [CheckResult(
            "real_provider_construction", "FAIL",
            f"could not construct or reach the configured provider: {e}",
        )], False

    try:
        knowledge_store = _build_knowledge_store(profile_path, cellar_path)
    except WineAcceptanceError as e:
        return [CheckResult("real_provider_construction", "FAIL", str(e))], False

    wine = WineCapability(provider, _NoOpMemory(), knowledge_store)

    results: list[CheckResult] = []
    for name, request in _build_real_model_requests(profile):
        try:
            response = wine.handle(request)
        except Exception as e:
            results.append(CheckResult(name, "FAIL", f"provider call failed: {e}"))
            continue

        if isinstance(response, str):
            text, model_id = response, "capability:wine (deterministic)"
        else:
            text, model_id = response.text, response.model

        if not text:
            results.append(CheckResult(name, "FAIL", "provider returned an empty response"))
            continue

        results.append(CheckResult(
            name, "MANUAL REVIEW",
            f"request: {request}\nmodel: {model_id}\nresponse:\n{text}",
        ))

    return results, True


# --- Orchestration ------------------------------------------------------------


def run_acceptance(
    profile_path: str | Path,
    cellar_path: str | Path,
    *,
    call_model: bool = False,
    provider_factory: Callable[[], ModelProvider] | None = None,
) -> AcceptanceResult:
    """Run the complete model-free acceptance check, and optionally real-model checks.

    Never writes a file and never constructs the configured model provider
    unless call_model=True. Expected profile, cellar, and validation problems
    are reported as FAIL check results, not raised.
    """

    profile_path = Path(profile_path)
    cellar_path = Path(cellar_path)

    try:
        profile = load_profile(profile_path)
        profile_check = CheckResult("profile_readable_and_valid", "PASS", f"loaded from {profile_path}")
    except WineAcceptanceError as e:
        profile = None
        profile_check = CheckResult("profile_readable_and_valid", "FAIL", str(e))

    try:
        validated_cellar = load_cellar(cellar_path)
        cellar_check = CheckResult(
            "cellar_readable_and_valid", "PASS",
            f"loaded from {cellar_path}, {len(validated_cellar)} record(s) validated",
        )
    except WineAcceptanceError as e:
        validated_cellar = None
        cellar_check = CheckResult("cellar_readable_and_valid", "FAIL", str(e))

    cellar_stats = compute_cellar_stats(validated_cellar) if validated_cellar is not None else None
    cellar_not_empty_check = None
    if cellar_stats is not None:
        cellar_not_empty_check = CheckResult(
            "cellar_not_empty",
            "PASS" if cellar_stats.active_holdings > 0 else "FAIL",
            f"{cellar_stats.active_holdings} active holding(s) of {cellar_stats.total_holdings} total",
        )

    store_error: WineAcceptanceError | None = None
    knowledge_store = None
    try:
        knowledge_store = _build_knowledge_store(profile_path, cellar_path)
    except WineAcceptanceError as e:
        store_error = e

    if validated_cellar is None:
        deterministic_checks = [CheckResult(
            "deterministic_cellar_queries", "FAIL",
            "cellar failed to load; deterministic checks were not run",
        )]
    elif store_error is not None:
        deterministic_checks = [CheckResult("deterministic_cellar_queries", "FAIL", str(store_error))]
    else:
        deterministic_checks = run_deterministic_checks(knowledge_store, validated_cellar, cellar_stats)

    if store_error is not None:
        prompt_assembly_check = CheckResult("prompt_assembly", "FAIL", str(store_error))
    elif validated_cellar is None:
        prompt_assembly_check = CheckResult(
            "prompt_assembly", "FAIL", "cellar failed to load; prompt-assembly check was not run",
        )
    else:
        prompt_assembly_check = run_prompt_assembly_check(knowledge_store, profile, validated_cellar)

    required = [profile_check, cellar_check]
    if cellar_not_empty_check is not None:
        required.append(cellar_not_empty_check)
    required.extend(deterministic_checks)
    required.append(prompt_assembly_check)

    required_non_skip = [c for c in required if c.status != "SKIP"]
    required_total = len(required_non_skip)
    required_passed = sum(1 for c in required_non_skip if c.status == "PASS")
    optional_skipped = sum(1 for c in deterministic_checks if c.status == "SKIP")

    model_free_success = required_passed == required_total

    manual_review_checks: list[CheckResult] = []
    provider_usable: bool | None = None
    if call_model:
        manual_review_checks, provider_usable = run_real_model_checks(
            profile_path, cellar_path, profile, provider_factory,
        )

    if call_model:
        real_model_success = bool(provider_usable) and bool(manual_review_checks) and all(
            c.status == "MANUAL REVIEW" for c in manual_review_checks
        )
        success = model_free_success and real_model_success
    else:
        success = model_free_success

    return AcceptanceResult(
        profile_path=profile_path,
        cellar_path=cellar_path,
        profile_check=profile_check,
        cellar_check=cellar_check,
        cellar_not_empty_check=cellar_not_empty_check,
        cellar_stats=cellar_stats,
        deterministic_checks=deterministic_checks,
        prompt_assembly_check=prompt_assembly_check,
        manual_review_checks=manual_review_checks,
        call_model=call_model,
        provider_usable=provider_usable,
        required_passed=required_passed,
        required_total=required_total,
        optional_skipped=optional_skipped,
        success=success,
    )


# --- Reporting ----------------------------------------------------------------


def _print_report(result: AcceptanceResult) -> None:
    print(f"Profile path: {result.profile_path}")
    print(f"Cellar path: {result.cellar_path}")
    print()

    print(f"[{result.profile_check.status}] {result.profile_check.name}: {result.profile_check.detail}")
    print(f"[{result.cellar_check.status}] {result.cellar_check.name}: {result.cellar_check.detail}")
    if result.cellar_not_empty_check is not None:
        c = result.cellar_not_empty_check
        print(f"[{c.status}] {c.name}: {c.detail}")
    print()

    if result.cellar_stats is not None:
        s = result.cellar_stats
        print("Cellar statistics:")
        print(f"  Total holdings: {s.total_holdings}")
        print(f"  Active holdings: {s.active_holdings}")
        print(f"  Zero-quantity holdings: {s.zero_holdings}")
        print(f"  Active bottle total: {s.active_bottle_total}")
        print(f"  Unique producers: {s.producer_count}")
        print(f"  Represented countries: {s.country_count}")
        print(f"  Represented regions: {s.region_count}")
        print()

    print("Deterministic acceptance checks:")
    for check in result.deterministic_checks:
        print(f"[{check.status}] {check.name}: {check.detail}")
    print()

    pac = result.prompt_assembly_check
    print(f"[{pac.status}] {pac.name}: {pac.detail}")
    print()

    if result.call_model:
        print("Real-model checks (--call-model). MANUAL REVIEW REQUIRED for every response below:")
        for check in result.manual_review_checks:
            print(f"[{check.status}] {check.name}")
            print(check.detail)
            print()
        print("A real, configured model provider was called for the checks above.")
    else:
        print("No real model provider was called (model-free run).")

    print("No files were written by this acceptance check.")
    print(f"Required checks passed: {result.required_passed}/{result.required_total}")
    print(f"Optional checks skipped: {result.optional_skipped}")
    print()
    print(("PASS" if result.success else "FAIL") + " - overall acceptance result.")


# --- CLI ------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.wine_acceptance_check",
        description="Verify that a local wine profile and cellar are structurally usable by WineCapability.",
    )
    parser.add_argument(
        "--call-model",
        action="store_true",
        help=(
            "Additionally run a concise set of real-provider prompts for manual review. "
            "Off by default; without it, no model provider is constructed or contacted."
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    profile_path: str | Path | None = None,
    cellar_path: str | Path | None = None,
    provider_factory: Callable[[], ModelProvider] | None = None,
) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    resolved_profile = Path(profile_path) if profile_path is not None else _DEFAULT_PROFILE_PATH
    resolved_cellar = Path(cellar_path) if cellar_path is not None else _DEFAULT_CELLAR_PATH

    try:
        result = run_acceptance(
            resolved_profile,
            resolved_cellar,
            call_model=args.call_model,
            provider_factory=provider_factory,
        )
    except (ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    _print_report(result)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
