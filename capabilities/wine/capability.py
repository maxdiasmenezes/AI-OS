"""
Wine capability: deterministic Wine Pairing v1.

Matches a prompt against a small, explicit set of food categories and
returns a wine-style recommendation with a brief explanation. No model
calls, no external lookups - just keyword rules.
"""

import re

from kernel.capabilities.base import Capability

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

_OUT_OF_SCOPE_RESPONSE = (
    "I can currently suggest wine pairings for: red meat, poultry, pork, shellfish, "
    "fish, tomato-based pasta/pizza, spicy food, and chocolate/dessert. This request "
    "doesn't match one of those yet, so I can't give a reliable pairing."
)


class WineCapability(Capability):
    """AI employee for wine: deterministic food-to-wine pairing (v1)."""

    @property
    def id(self) -> str:
        return "wine"

    def handle(self, prompt: str) -> str:
        for category_id in _PRIORITY_ORDER:
            if _PATTERNS[category_id].search(prompt):
                data = _CATEGORIES[category_id]
                return (
                    f"Wine pairing for {data['label']}:\n"
                    f"- Recommended: {data['wine_style']}\n"
                    f"- Why: {data['explanation']}"
                )
        return _OUT_OF_SCOPE_RESPONSE
