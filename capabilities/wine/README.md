# Wine

AI employee focused on wine: recommendations, tasting notes, and cellar knowledge.

`WineCapability` is constructed with two explicit dependencies —
a `ModelProvider` and a `MemoryManager` (`WineCapability(model_provider,
memory_manager)`) — both injected by `CapabilityLoader`, which in turn
receives them from the `Orchestrator`'s own instances. `WineCapability`
never constructs or configures a provider or memory manager itself, and
imports only kernel abstractions.

## Wine Pairing v1

Deterministic, keyword-based food-to-wine pairing. No model calls, no
memory recall, no external APIs, no personal cellar data yet.

Supported food categories: red meat, poultry, pork, shellfish, fish,
tomato-based pasta/pizza, spicy food, chocolate/dessert.

When a prompt matches more than one category (e.g. "spicy shrimp"),
preparation/sauce categories take priority over protein categories, in
this order: dessert, spicy, tomato_pasta, shellfish, fish, pork, poultry,
red_meat. A deterministic match returns immediately — it never touches the
model provider or the memory manager.

## Model-backed fallback

A wine-related request that doesn't match any of the eight categories
above (e.g. wine regions, specific bottles, vintages, general buying or
serving questions) is passed to the injected model provider instead of
returning a canned "out of scope" message.

Before calling the provider, the fallback recalls the last 10 entries from
the existing `"conversation"` memory namespace (the same namespace and
entries the orchestrator's fallback path uses), in chronological order.
When entries exist, they are included in the prompt as plain conversational
history — role and content per turn — under a `Conversation context:`
label, followed by the current request under a `Current user request:`
label. When memory is empty, no conversation-context section is included.
This is recalled history only, not structured preferences or cellar
knowledge, and `WineCapability` does not persist anything itself — the
orchestrator remains responsible for writing the user prompt and the final
response to memory after `handle()` returns.

The model is scoped to wine-expert territory by `prompts/wine/fallback.md`,
which instructs it to use only relevant recalled context, ignore unrelated
history, and never invent preferences, cellar contents, or prior statements
not present in the supplied context. Still no personal cellar database,
knowledge retrieval, tools, or external APIs — just general wine knowledge
from the model, informed by real recalled conversation turns when available.
