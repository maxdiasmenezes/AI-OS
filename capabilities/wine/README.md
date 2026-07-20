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

## Model-backed fallback

A wine-related request that doesn't match any of the eight categories
above (e.g. wine regions, specific bottles, vintages, general buying or
serving questions) is passed to the injected model provider instead of
returning a canned "out of scope" message. The model is scoped to
wine-expert territory by `prompts/wine/fallback.md` and given the original
request verbatim. Still no cellar data, personal preferences, memory
injection, tools, or external APIs — just general wine knowledge from the
model.
