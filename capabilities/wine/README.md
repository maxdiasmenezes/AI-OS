# Wine

AI employee focused on wine: recommendations, tasting notes, and cellar knowledge.

## Wine Pairing v1

Deterministic, keyword-based food-to-wine pairing. No model calls, no
external APIs, no personal cellar data yet.

Supported food categories: red meat, poultry, pork, shellfish, fish,
tomato-based pasta/pizza, spicy food, chocolate/dessert.

When a prompt matches more than one category (e.g. "spicy shrimp"),
preparation/sauce categories take priority over protein categories, in
this order: dessert, spicy, tomato_pasta, shellfish, fish, pork, poultry,
red_meat.

Wine-related requests that don't match a supported category (e.g. wine
regions, specific bottles, budget questions) get an explicit "out of
scope" response rather than a guess.
