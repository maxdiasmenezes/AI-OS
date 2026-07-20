# Wine

AI employee focused on wine: recommendations, tasting notes, and cellar knowledge.

`WineCapability` is constructed with three explicit dependencies — a
`ModelProvider`, a `MemoryManager`, and a `KnowledgeStore`
(`WineCapability(model_provider, memory_manager, knowledge_store)`) — all
injected by `CapabilityLoader`, which in turn receives them from the
`Orchestrator`'s own instances. `WineCapability` never constructs or
configures a provider, memory manager, or knowledge store itself, and
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
model provider, the memory manager, or the knowledge store.

## Model-backed fallback

A wine-related request that doesn't match any of the eight categories
above (e.g. wine regions, specific bottles, vintages, general buying or
serving questions) is passed to the injected model provider instead of
returning a canned "out of scope" message.

Before calling the provider, the fallback:

1. Reads an optional personal wine profile via
   `knowledge_store.get("wine_profile", "profile")` — the only knowledge
   access `WineCapability` performs, always through the public
   `KnowledgeStore` interface (see [Personal wine profile](#personal-wine-profile)
   below).
2. Recalls the last 10 entries from the existing `"conversation"` memory
   namespace (the same namespace and entries the orchestrator's fallback
   path uses), in chronological order.

The resulting prompt is assembled in a fixed order: wine-expert
instructions, then the personal wine profile (only when non-empty and
valid), then recalled conversation history under a `Conversation context:`
label (only when memory is non-empty), then the current request under a
`Current user request:` label. `WineCapability` does not persist or modify
anything itself — the orchestrator remains responsible for writing the user
prompt and the final response to memory after `handle()` returns, and
nothing in this capability ever writes to the knowledge store.

The model is scoped to wine-expert territory by `prompts/wine/fallback.md`,
which instructs it to treat the personal profile as durable, explicitly
recorded information and recalled conversation as recent and possibly
unrelated, to use only the relevant parts of either, and to never invent
preferences, cellar contents, or prior statements not present in the
supplied profile or context. Still no tools or external APIs — just general
wine knowledge from the model, informed by real recalled conversation turns
and a real personal profile when available.

## Personal wine profile

`WineCapability` reads an optional, single-record personal wine-preferences
profile from the injected `KnowledgeStore`, at namespace `"wine_profile"`,
key `"profile"` — real data would live at `storage/knowledge/wine_profile.json`,
which is never committed to Git (see `kernel/knowledge/README.md`). Access is
strictly read-only and goes only through `KnowledgeStore.get()`; `WineCapability`
never calls `list_records()`, never inspects the underlying JSON file, and
never writes, updates, or infers profile data.

Five optional fields are recognized, rendered in the fallback prompt in this
fixed order when present and non-empty:

| Field              | Type          | Prompt label          |
|--------------------|---------------|------------------------|
| `preferred_styles` | list of str   | Preferred styles       |
| `disliked_styles`  | list of str   | Disliked styles        |
| `budget_range`     | str           | Usual budget            |
| `priorities`       | list of str   | Selection priorities    |
| `notes`            | str           | Notes                   |

A missing profile, or a profile where every recognized field is missing or
empty, produces no `Personal wine profile:` section at all — the fallback
behaves exactly as it did before this field existed. Unknown fields are
ignored. A recognized field with the wrong type, or a list field containing
a non-string or empty-string element, raises `ValueError` rather than being
silently coerced or dropped, since that indicates a malformed profile rather
than an absent one.

This is durable personal context, distinct from the recent, possibly
unrelated conversation history recalled from memory. There is still no
cellar inventory, bottle-level data, purchase or ratings history, or any
way for AI-OS to write or infer profile data on its own — a human edits
`storage/knowledge/wine_profile.json` directly if and when it exists.
