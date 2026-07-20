"""
Wine capability: deterministic Wine Pairing v1, with a model-backed fallback.

Matches a prompt against a small, explicit set of food categories and
returns a wine-style recommendation with a brief explanation - no model
calls, just keyword rules. A wine-related prompt that matches none of the
categories falls back to the injected ModelProvider, scoped to wine expertise
via prompts/wine/fallback.md, with recent conversation history recalled from
the injected MemoryManager and an optional personal wine profile and
read-only cellar inventory read from the injected KnowledgeStore for context.
"""

import re
from pathlib import Path

from capabilities.wine.cellar_schema import validate_cellar_record
from kernel.capabilities.base import Capability
from kernel.knowledge import KnowledgeStore
from kernel.memory import MemoryManager
from kernel.models.base import ModelProvider, ModelResponse

_MEMORY_NAMESPACE = "conversation"
_MEMORY_LIMIT = 10

_PROFILE_NAMESPACE = "wine_profile"
_PROFILE_KEY = "profile"

# Recognized profile fields, in the fixed order they are rendered in the
# fallback prompt, together with their prompt label and expected shape.
_PROFILE_LIST_FIELDS = ("preferred_styles", "disliked_styles", "priorities")
_PROFILE_STRING_FIELDS = ("budget_range", "notes")
_PROFILE_FIELD_LABELS = (
    ("preferred_styles", "Preferred styles"),
    ("disliked_styles", "Disliked styles"),
    ("budget_range", "Usual budget"),
    ("priorities", "Selection priorities"),
    ("notes", "Notes"),
)

_CELLAR_NAMESPACE = "wine_cellar"

# v1 cap on the number of active cellar records included in a single
# fallback prompt - see _prepare_cellar_section().
_CELLAR_MAX_RECORDS = 100

# capabilities/wine/capability.py -> capabilities/wine -> capabilities -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FALLBACK_PROMPT_PATH = _PROJECT_ROOT / "prompts" / "wine" / "fallback.md"
_FALLBACK_INSTRUCTIONS = _FALLBACK_PROMPT_PATH.read_text(encoding="utf-8").strip()

# Each entry: (category id, human label, keywords, wine style, explanation).
# Keywords are matched case-insensitively as whole words/phrases.
_CATEGORIES: dict[str, dict] = {
    "dessert": {
        "label": "chocolate/dessert",
        "keywords": ["chocolate", "dessert", "cake", "tiramisu"],
        "wine_style": "a Port or other sweet fortified wine",
        "explanation": "the wine's sweetness needs to match or exceed the dish's, or it will taste thin and sour.",
    },
    "spicy": {
        "label": "spicy food",
        "keywords": ["spicy", "curry", "chili", "chilli"],
        "wine_style": "an off-dry white like Riesling or Gewurztraminer",
        "explanation": "a touch of sweetness and low tannin cool the heat instead of amplifying it.",
    },
    "tomato_pasta": {
        "label": "tomato-based pasta/pizza",
        "keywords": ["marinara", "bolognese", "tomato sauce", "pizza"],
        "wine_style": "a medium-bodied Italian red like Chianti or Sangiovese",
        "explanation": "its bright acidity matches the tomato sauce's acidity instead of being overwhelmed by it.",
    },
    "shellfish": {
        "label": "shellfish",
        "keywords": [
            "shrimp", "prawn", "crab", "lobster", "scallop", "scallops",
            "oyster", "oysters", "clam", "clams", "mussel", "mussels",
        ],
        "wine_style": "a crisp white or sparkling wine like Chablis or Champagne",
        "explanation": "high acidity and minerality complement delicate shellfish without overpowering it.",
    },
    "fish": {
        "label": "fish",
        "keywords": ["salmon", "tuna", "cod", "trout", "halibut", "sushi", "sashimi", "fish"],
        "wine_style": "a light white like Sauvignon Blanc or Pinot Grigio",
        "explanation": "a light, clean white won't overpower the fish's delicate flavor.",
    },
    "pork": {
        "label": "pork",
        "keywords": ["pork", "ham", "bacon", "prosciutto"],
        "wine_style": "a Pinot Noir or off-dry Riesling",
        "explanation": "pork's mild sweetness pairs with a light red or a wine with its own touch of sweetness.",
    },
    "poultry": {
        "label": "poultry",
        "keywords": ["chicken", "turkey"],
        "wine_style": "a Chardonnay or Pinot Noir",
        "explanation": "poultry is versatile enough for a medium-bodied red or white, depending on preparation.",
    },
    "red_meat": {
        "label": "red meat",
        "keywords": ["steak", "beef", "burger", "venison"],
        "wine_style": "a full-bodied red like Cabernet Sauvignon or Malbec",
        "explanation": "firm tannins and bold fruit stand up to the richness and fat of red meat.",
    },
}

# Preparation/sauce categories take priority over protein categories when a
# prompt matches more than one (e.g. "spicy shrimp" -> spicy, not shellfish).
_PRIORITY_ORDER = [
    "dessert",
    "spicy",
    "tomato_pasta",
    "shellfish",
    "fish",
    "pork",
    "poultry",
    "red_meat",
]

_PATTERNS = {
    category_id: re.compile(
        r"\b(?:" + "|".join(re.escape(kw) for kw in data["keywords"]) + r")\b",
        re.IGNORECASE,
    )
    for category_id, data in _CATEGORIES.items()
}


def _extract_profile_fields(profile: dict[str, object]) -> dict[str, list[str] | str]:
    """Validate recognized profile fields and return only the non-empty ones.

    Unknown fields are ignored. A missing field, an empty string, or an empty
    list is simply omitted from the result. A recognized field with the
    wrong type - or a list field containing a non-string or empty-string
    element - raises ValueError naming the offending field, since that
    indicates a malformed profile rather than an absent one.
    """

    fields: dict[str, list[str] | str] = {}

    for field in _PROFILE_LIST_FIELDS:
        if field not in profile:
            continue
        value = profile[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"wine profile field {field!r} must be a list of strings")
        if any(item == "" for item in value):
            raise ValueError(f"wine profile field {field!r} must not contain empty strings")
        if value:
            fields[field] = value

    for field in _PROFILE_STRING_FIELDS:
        if field not in profile:
            continue
        value = profile[field]
        if not isinstance(value, str):
            raise ValueError(f"wine profile field {field!r} must be a string")
        if value:
            fields[field] = value

    return fields


def _format_profile(profile: dict[str, object] | None) -> str | None:
    """Render the recognized, non-empty fields of a profile record, or None."""

    if profile is None:
        return None

    fields = _extract_profile_fields(profile)
    if not fields:
        return None

    lines = []
    for field, label in _PROFILE_FIELD_LABELS:
        if field not in fields:
            continue
        value = fields[field]
        rendered = ", ".join(value) if isinstance(value, list) else value
        lines.append(f"- {label}: {rendered}")

    return "Personal wine profile:\n" + "\n".join(lines)


def _format_cellar_record(record_id: str, fields: dict[str, object]) -> str:
    """Render one active cellar record in the documented fixed field order."""

    lines = [f"- Cellar ID: {record_id}"]
    lines.append(f"  Producer: {fields['producer']}")
    lines.append(f"  Wine: {fields['wine_name']}")
    if "vintage" in fields:
        lines.append(f"  Vintage: {fields['vintage']}")
    lines.append(f"  Color: {fields['color']}")
    if "style" in fields:
        lines.append(f"  Style: {fields['style']}")
    if "country" in fields:
        lines.append(f"  Country: {fields['country']}")
    if "region" in fields:
        lines.append(f"  Region: {fields['region']}")
    if "grapes" in fields:
        lines.append(f"  Grapes: {', '.join(fields['grapes'])}")
    lines.append(f"  Quantity: {fields['quantity']}")
    if "estimated_price" in fields:
        lines.append(f"  Estimated price: {fields['estimated_price']} {fields['price_currency']}")
    if "vivino_rating" in fields:
        lines.append(f"  Vivino rating: {fields['vivino_rating']}")
    if "drinking_window" in fields:
        lines.append(f"  Drinking window: {fields['drinking_window']}")
    if fields.get("special_occasion") is True:
        lines.append("  Special occasion: yes")
    if "notes" in fields:
        lines.append(f"  Notes: {fields['notes']}")

    return "\n".join(lines)


def _prepare_cellar_section(knowledge_store: KnowledgeStore) -> str | None:
    """Validate the cellar inventory and render it into a prompt section.

    Every record is validated, including zero-quantity ones. Zero-quantity
    records are then excluded, and the remaining active records are sorted
    by their record key - no merging, deduplication, or ranking. When the
    active cellar exceeds _CELLAR_MAX_RECORDS, an honest size-limit section
    is returned instead of a partial inventory; the caller still calls the
    provider exactly once either way.
    """

    records = knowledge_store.list_records(_CELLAR_NAMESPACE)
    if not records:
        return None

    validated = {
        record_id: validate_cellar_record(record_id, record)
        for record_id, record in records.items()
    }

    active = {
        record_id: fields for record_id, fields in validated.items() if fields["quantity"] > 0
    }
    if not active:
        return None

    if len(active) > _CELLAR_MAX_RECORDS:
        return (
            "Personal wine cellar:\n"
            f"The cellar has {len(active)} active holdings, which exceeds the v1 "
            f"limit of {_CELLAR_MAX_RECORDS} records per request. No partial "
            "inventory is supplied here - ask the user to narrow their request "
            "rather than claiming to identify the best cellar bottle."
        )

    record_blocks = [
        _format_cellar_record(record_id, active[record_id]) for record_id in sorted(active)
    ]
    return "Personal wine cellar:\n" + "\n\n".join(record_blocks)


class WineCapability(Capability):
    """AI employee for wine: deterministic food-to-wine pairing (v1)."""

    def __init__(
        self,
        model_provider: ModelProvider,
        memory_manager: MemoryManager,
        knowledge_store: KnowledgeStore,
    ) -> None:
        self._model_provider = model_provider
        self._memory_manager = memory_manager
        self._knowledge_store = knowledge_store

    @property
    def id(self) -> str:
        return "wine"

    def handle(self, prompt: str) -> str | ModelResponse:
        for category_id in _PRIORITY_ORDER:
            if _PATTERNS[category_id].search(prompt):
                data = _CATEGORIES[category_id]
                return (
                    f"Wine pairing for {data['label']}:\n"
                    f"- Recommended: {data['wine_style']}\n"
                    f"- Why: {data['explanation']}"
                )
        return self._fallback(prompt)

    def _fallback(self, prompt: str) -> ModelResponse:
        profile = self._knowledge_store.get(_PROFILE_NAMESPACE, _PROFILE_KEY)
        profile_section = _format_profile(profile)

        cellar_section = _prepare_cellar_section(self._knowledge_store)

        entries = self._memory_manager.recall(_MEMORY_NAMESPACE, limit=_MEMORY_LIMIT)

        parts = [_FALLBACK_INSTRUCTIONS]
        if profile_section:
            parts.append(profile_section)
        if cellar_section:
            parts.append(cellar_section)
        if entries:
            transcript = "\n".join(f"{e.metadata['role']}: {e.content}" for e in entries)
            parts.append(f"Conversation context:\n{transcript}")
        parts.append(f"Current user request:\n{prompt}")

        fallback_prompt = "\n\n".join(parts)
        return self._model_provider.send_prompt(fallback_prompt)
