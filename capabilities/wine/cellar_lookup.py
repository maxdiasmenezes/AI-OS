"""
Deterministic Cellar Lookup v1.

Answers a small, explicit set of factual cellar questions (total bottle
count, exact quantity, exact ownership, producer holdings, vintage listing)
directly from validated `wine_cellar` records, with no model call. This
module owns query detection, target extraction, normalization, exact
matching, aggregation, ambiguity handling, and response formatting - it is
the only place any of that logic lives. It reuses `validate_cellar_record()`
from `capabilities.wine.cellar_schema` rather than duplicating cellar schema
rules, and imports nothing from providers, memory, prompts, the
orchestrator, or kernel configuration.

Matching is exact-normalized-string-equality only: no substrings, no fuzzy
or semantic matching, no aliases, no stemming, no accent stripping. Two
spellings that differ by anything other than case, surrounding whitespace,
or trailing sentence punctuation simply do not match.
"""

import re

from capabilities.wine.cellar_schema import validate_cellar_record

_WHITESPACE_PATTERN = re.compile(r"\s+")

# --- Query parsing ---------------------------------------------------------

# Each entry is a compiled pattern anchored to the whole (whitespace-
# collapsed) prompt, with a capturing group for the target where the query
# type needs one. Patterns are deliberately literal and specific - there is
# no shared grammar or generic parser here, just a fixed, documented list of
# supported phrasings (see the milestone's conservative phrasing rules).
_TOTAL_PATTERNS = [
    re.compile(r"^how many bottles of wine do i have in total[.!?]*$", re.IGNORECASE),
    re.compile(r"^how many bottles are in my cellar[.!?]*$", re.IGNORECASE),
    re.compile(r"^how many bottles do i have in my cellar[.!?]*$", re.IGNORECASE),
]

_QUANTITY_PATTERNS = [
    re.compile(r"^how many bottles of (.+?) are in my cellar[.!?]*$", re.IGNORECASE),
    re.compile(r"^how many bottles of (.+?) do i have in my cellar[.!?]*$", re.IGNORECASE),
    re.compile(r"^how many bottles of (.+?) wine do i have[.!?]*$", re.IGNORECASE),
]

_OWNERSHIP_PATTERNS = [
    # The "any ..." forms are checked before the bare "do i have X in my
    # cellar" form, since that generic suffix would otherwise also match
    # "do i have any X in my cellar" and capture "any X" as the target.
    re.compile(r"^do i own any (.+?) wine[.!?]*$", re.IGNORECASE),
    re.compile(r"^do i have any (.+?) wine[.!?]*$", re.IGNORECASE),
    re.compile(r"^do i have any (.+?) in my cellar[.!?]*$", re.IGNORECASE),
    re.compile(r"^do i have (.+?) in my cellar[.!?]*$", re.IGNORECASE),
]

_PRODUCER_PATTERNS = [
    re.compile(r"^show me my wines from (.+?)[.!?]*$", re.IGNORECASE),
    re.compile(r"^what wines do i have from (.+?)[.!?]*$", re.IGNORECASE),
    re.compile(r"^what do i have from (.+?) in my cellar[.!?]*$", re.IGNORECASE),
]

_VINTAGE_PATTERNS = [
    re.compile(r"^what vintages of (.+?) are in my cellar[.!?]*$", re.IGNORECASE),
    re.compile(r"^what vintages of (.+?) wine do i have[.!?]*$", re.IGNORECASE),
]

# Checked in this order; "total" has no capture group so it is handled
# separately before the target-bearing categories below.
_TARGET_PATTERNS_BY_QUERY_TYPE = (
    ("quantity", _QUANTITY_PATTERNS),
    ("ownership", _OWNERSHIP_PATTERNS),
    ("producer", _PRODUCER_PATTERNS),
    ("vintage", _VINTAGE_PATTERNS),
)


def parse_cellar_query(prompt: str) -> tuple[str, str | None] | None:
    """Detect a supported deterministic cellar query and extract its target.

    Returns (query_type, target) where query_type is one of "total",
    "quantity", "ownership", "producer", or "vintage", and target is None
    for "total" or a non-empty, non-normalized string otherwise. Returns
    None for anything that does not exactly match one of the documented
    conservative phrasings - including broad fragments and pairing or
    recommendation prompts.
    """

    collapsed = _WHITESPACE_PATTERN.sub(" ", prompt.strip())
    if not collapsed:
        return None

    for pattern in _TOTAL_PATTERNS:
        if pattern.match(collapsed):
            return ("total", None)

    for query_type, patterns in _TARGET_PATTERNS_BY_QUERY_TYPE:
        for pattern in patterns:
            match = pattern.match(collapsed)
            if match:
                target = match.group(1).strip().rstrip("?.!").strip()
                if not target:
                    return None
                return (query_type, target)

    return None


# --- Normalization -----------------------------------------------------


def _normalize(text: str) -> str:
    """Case-fold, collapse internal whitespace, and drop trailing ?.!.

    Exact normalized equality is the only matching operation this module
    performs - no accent stripping, no internal punctuation changes, no
    stemming, no substring scoring.
    """

    collapsed = _WHITESPACE_PATTERN.sub(" ", text.strip())
    return collapsed.casefold().rstrip("?.!")


# --- Response formatting helpers -----------------------------------------


def _no_match_response(target: str) -> str:
    return f'I found no cellar record matching "{target}".'


def _zero_quantity_response(target: str) -> str:
    return (
        f'Cellar records matching "{target}" exist, but their current '
        "active quantity is zero."
    )


def _ambiguous_response(target: str, producers: list[str]) -> str:
    return (
        f'Multiple producers have a wine named "{target}": '
        f"{', '.join(producers)}. Include the producer name to get an exact answer."
    )


def _sorted_vintages(vintages: set[object]) -> list[str]:
    """Numeric vintages ascending, then "NV", with duplicates collapsed."""

    numeric = sorted(v for v in vintages if v != "NV")
    result = [str(v) for v in numeric]
    if "NV" in vintages:
        result.append("NV")
    return result


def _vintage_sort_key(fields: dict[str, object]) -> tuple[int, object]:
    if "vintage" not in fields:
        return (2, 0)
    if fields["vintage"] == "NV":
        return (1, 0)
    return (0, fields["vintage"])


def _holding_sort_key(record_id: str, fields: dict[str, object]) -> tuple:
    return (
        _normalize(fields["producer"]),
        _normalize(fields["wine_name"]),
        _vintage_sort_key(fields),
        record_id,
    )


def _active(matches: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    return {rid: fields for rid, fields in matches.items() if fields["quantity"] > 0}


# --- Wine identity resolution (quantity and vintage queries) --------------


def _resolve_identity_matches(
    target: str, validated: dict[str, dict[str, object]]
) -> tuple[dict[str, dict[str, object]] | None, str | None]:
    """Resolve a quantity/vintage target to its matching records.

    Eligible exact matches are wine_name, or producer + " " + wine_name. A
    producer+wine_name match is never ambiguous. A wine_name-only match that
    spans more than one distinct producer is ambiguous and returns a
    clarification response instead of matches. Returns (matches, None) on
    success, or (None, response) for a no-match or ambiguity response.
    """

    target_norm = _normalize(target)

    full_matches = {
        rid: fields
        for rid, fields in validated.items()
        if _normalize(f"{fields['producer']} {fields['wine_name']}") == target_norm
    }
    if full_matches:
        return full_matches, None

    name_matches = {
        rid: fields
        for rid, fields in validated.items()
        if _normalize(fields["wine_name"]) == target_norm
    }
    if not name_matches:
        return None, _no_match_response(target)

    producers = sorted({fields["producer"] for fields in name_matches.values()}, key=_normalize)
    if len(producers) > 1:
        return None, _ambiguous_response(target, producers)

    return name_matches, None


# --- Per-category answers --------------------------------------------------


def _answer_total(validated: dict[str, dict[str, object]]) -> str:
    active = _active(validated)
    total_quantity = sum(fields["quantity"] for fields in active.values())
    return f"The active cellar contains {total_quantity} bottles across {len(active)} holdings."


def _answer_quantity(target: str, validated: dict[str, dict[str, object]]) -> str:
    matches, response = _resolve_identity_matches(target, validated)
    if matches is None:
        return response

    active = _active(matches)
    if not active:
        return _zero_quantity_response(target)

    sample = next(iter(active.values()))
    total_quantity = sum(fields["quantity"] for fields in active.values())
    vintages = _sorted_vintages({fields["vintage"] for fields in active.values() if "vintage" in fields})

    result = f"{sample['producer']} {sample['wine_name']}: {total_quantity} bottles in the active cellar."
    if vintages:
        result += f" Vintages: {', '.join(vintages)}."
    return result


_OWNERSHIP_CATEGORY_ORDER = ("wine_name", "producer", "producer_wine_name", "region", "country")
_OWNERSHIP_CATEGORY_LABELS = {"producer": "producer", "region": "region", "country": "country"}


def _matches_ownership_category(target_norm: str, fields: dict[str, object], category: str) -> bool:
    if category == "wine_name":
        return _normalize(fields["wine_name"]) == target_norm
    if category == "producer":
        return _normalize(fields["producer"]) == target_norm
    if category == "producer_wine_name":
        return _normalize(f"{fields['producer']} {fields['wine_name']}") == target_norm
    if category == "region":
        return "region" in fields and _normalize(fields["region"]) == target_norm
    if category == "country":
        return "country" in fields and _normalize(fields["country"]) == target_norm
    raise ValueError(f"unsupported ownership match category {category!r}")


def _answer_ownership(target: str, validated: dict[str, dict[str, object]]) -> str:
    target_norm = _normalize(target)

    matched_category = None
    matches: dict[str, dict[str, object]] = {}
    for category in _OWNERSHIP_CATEGORY_ORDER:
        candidates = {
            rid: fields
            for rid, fields in validated.items()
            if _matches_ownership_category(target_norm, fields, category)
        }
        if candidates:
            matched_category = category
            matches = candidates
            break

    if not matches:
        return _no_match_response(target)

    active = _active(matches)
    if not active:
        return _zero_quantity_response(target)

    total_quantity = sum(fields["quantity"] for fields in active.values())
    holdings = len(active)

    result = f'Yes, you have {total_quantity} bottles across {holdings} holdings of "{target}".'
    category_label = _OWNERSHIP_CATEGORY_LABELS.get(matched_category)
    if category_label:
        result += f" (matched by {category_label})"
    return result


def _answer_producer(target: str, validated: dict[str, dict[str, object]]) -> str:
    target_norm = _normalize(target)

    matches = {
        rid: fields for rid, fields in validated.items() if _normalize(fields["producer"]) == target_norm
    }
    if not matches:
        return _no_match_response(target)

    active = _active(matches)
    if not active:
        return _zero_quantity_response(target)

    ordered_ids = sorted(active, key=lambda rid: _holding_sort_key(rid, active[rid]))
    lines = [f"Wines from {target}:"]
    for record_id in ordered_ids:
        fields = active[record_id]
        line = f"- {fields['producer']} {fields['wine_name']}"
        if "vintage" in fields:
            line += f" ({fields['vintage']})"
        line += f": {fields['quantity']} bottles [Cellar ID: {record_id}]"
        lines.append(line)
    return "\n".join(lines)


def _answer_vintage(target: str, validated: dict[str, dict[str, object]]) -> str:
    matches, response = _resolve_identity_matches(target, validated)
    if matches is None:
        return response

    active = _active(matches)
    if not active:
        return _zero_quantity_response(target)

    sample = next(iter(active.values()))
    vintages = _sorted_vintages({fields["vintage"] for fields in active.values() if "vintage" in fields})

    if not vintages:
        return f"{sample['producer']} {sample['wine_name']} is in the active cellar, but no vintage is recorded."
    return f"{sample['producer']} {sample['wine_name']} vintages in the active cellar: {', '.join(vintages)}."


_ANSWER_FUNCTIONS = {
    "total": lambda target, validated: _answer_total(validated),
    "quantity": _answer_quantity,
    "ownership": _answer_ownership,
    "producer": _answer_producer,
    "vintage": _answer_vintage,
}


def answer_cellar_query(
    query_type: str, target: str | None, records: dict[str, dict[str, object]]
) -> str:
    """Answer a detected deterministic cellar query from raw cellar records.

    Every record is validated via `validate_cellar_record()` - including
    unrelated and zero-quantity ones - before any matching happens. An
    invalid record raises ValueError immediately and no partial answer is
    returned.
    """

    validated = {record_id: validate_cellar_record(record_id, record) for record_id, record in records.items()}
    return _ANSWER_FUNCTIONS[query_type](target, validated)
