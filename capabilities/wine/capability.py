"""
Wine capability: deterministic Wine Pairing v1, with a model-backed fallback.

Matches a prompt against a small, explicit set of food categories and
returns a wine-style recommendation with a brief explanation - no model
calls, just keyword rules. A wine-related prompt that matches none of the
categories falls back to the injected ModelProvider, scoped to wine expertise
via prompts/wine/fallback.md, with recent conversation history recalled from
the injected MemoryManager for context.
"""

import re
from pathlib import Path

from kernel.capabilities.base import Capability
from kernel.memory import MemoryManager
from kernel.models.base import ModelProvider, ModelResponse

_MEMORY_NAMESPACE = "conversation"
_MEMORY_LIMIT = 10

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


class WineCapability(Capability):
    """AI employee for wine: deterministic food-to-wine pairing (v1)."""

    def __init__(self, model_provider: ModelProvider, memory_manager: MemoryManager) -> None:
        self._model_provider = model_provider
        self._memory_manager = memory_manager

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
        entries = self._memory_manager.recall(_MEMORY_NAMESPACE, limit=_MEMORY_LIMIT)

        parts = [_FALLBACK_INSTRUCTIONS]
        if entries:
            transcript = "\n".join(f"{e.metadata['role']}: {e.content}" for e in entries)
            parts.append(f"Conversation context:\n{transcript}")
        parts.append(f"Current user request:\n{prompt}")

        fallback_prompt = "\n\n".join(parts)
        return self._model_provider.send_prompt(fallback_prompt)
