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
memory recall, no external APIs, no knowledge-store access of any kind
(including the personal cellar — see [Model-backed fallback](#model-backed-fallback)
below).

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
   `knowledge_store.get("wine_profile", "profile")` (see
   [Personal wine profile](#personal-wine-profile) below).
2. Reads the personal cellar inventory via
   `knowledge_store.list_records("wine_cellar")` (see
   [Personal wine cellar](#personal-wine-cellar) below) — always through the
   public `KnowledgeStore` interface, the same two calls it exposes for
   every capability.
3. Recalls the last 10 entries from the existing `"conversation"` memory
   namespace (the same namespace and entries the orchestrator's fallback
   path uses), in chronological order.

The resulting prompt is assembled in a fixed order: wine-expert
instructions, then the personal wine profile (only when non-empty and
valid), then the personal wine cellar (only when non-empty and valid — see
[Personal wine cellar](#personal-wine-cellar) below), then recalled
conversation history under a `Conversation context:` label (only when
memory is non-empty), then the current request under a `Current user
request:` label. `WineCapability` does not persist or modify anything
itself — the orchestrator remains responsible for writing the user prompt
and the final response to memory after `handle()` returns, and nothing in
this capability ever writes to the knowledge store.

The model is scoped to wine-expert territory by `prompts/wine/fallback.md`,
which instructs it to treat the personal profile as durable, explicitly
recorded information, the cellar as real but read-only inventory data, and
recalled conversation as recent and possibly unrelated, to use only the
relevant parts of any of them, and to never invent preferences, cellar
contents, prices, ratings, or prior statements not present in the supplied
profile, cellar, or context. The prompt also carries explicit
everyday-versus-special-occasion guidance: never assume an occasion is
special unless the current request clearly says so, prefer lower-priced or
lower-rated bottles for ordinary requests, reserve `special_occasion: true`
bottles for clearly stated special occasions, and never claim a bottle was
consumed or its quantity decremented. Still no tools or external APIs — just
general wine knowledge from the model, informed by real recalled
conversation turns, a real personal profile, and a real cellar inventory
when available.

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
unrelated conversation history recalled from memory, and distinct from the
read-only cellar inventory below. There is still no purchase or ratings
history, or any way for AI-OS to write or infer profile data on its own — a
human edits `storage/knowledge/wine_profile.json` directly if and when it
exists.

## Personal wine cellar

`WineCapability` reads an optional, read-only personal cellar inventory from
the injected `KnowledgeStore`, at namespace `"wine_cellar"`, via
`knowledge_store.list_records("wine_cellar")` only — real data would live at
`storage/knowledge/wine_cellar.json`, which is gitignored and never
committed (see `kernel/knowledge/README.md`). This access only happens on
the model-backed fallback path, never for the eight deterministic pairing
categories.

Cellar record validation itself lives in `capabilities/wine/cellar_schema.py`,
not in `capability.py`. `WineCapability` imports and calls
`validate_cellar_record()` from that module; it does not implement its own
copy of the validation rules. `capabilities/wine/cellar_schema.py` is the
shared, domain-level schema — the single source of truth for what a valid
cellar record is — and it is deliberately narrow: only field validation lives
there. Prompt formatting, cellar size limiting, cellar sorting, and fallback
behavior are prompt-consumption concerns, not schema concerns, and stay
private to `WineCapability`.

A separate, human-controlled maintenance script,
`scripts/import_wine_cellar.py` (see `scripts/README.md`), reuses the exact
same `validate_cellar_record()` to import a CSV into
`storage/knowledge/wine_cellar.json`. Because both the runtime read path and
the import write path validate through the same function, the importer never
accepts a record `WineCapability` would reject, and never rejects one it
would accept. The runtime itself remains entirely read-only — `WineCapability`
and `KnowledgeStore` never write anything — importing is a separate, manual,
human-invoked step, run outside the kernel, that never happens automatically
and never happens as a side effect of handling a prompt. One CSV import fully
replaces the cellar document; it does not merge with what was there before.

One JSON record represents one wine **holding**, not one physical bottle —
the top-level JSON key is the cellar record's own ID; there is no separate
`id` field inside the record. Every record, including a zero-quantity one,
must contain four required fields:

| Field       | Type | Constraint                          |
|-------------|------|--------------------------------------|
| `producer`  | str  | non-empty                            |
| `wine_name` | str  | non-empty                            |
| `color`     | str  | non-empty                            |
| `quantity`  | int  | `>= 0`, `bool` is not accepted as int |

Ten further fields are optional, rendered in the fallback prompt in this
fixed order when supplied:

| Field             | Type              | Constraint                                    |
|-------------------|-------------------|------------------------------------------------|
| `vintage`         | int or `"NV"`     | int 1800–2100, or the exact string `"NV"`      |
| `country`         | str               | non-empty                                      |
| `region`          | str               | non-empty                                      |
| `style`           | str               | non-empty                                      |
| `grapes`          | list of str       | every item non-empty; authored order preserved |
| `estimated_price` | int or float      | `>= 0`; requires `price_currency` and vice versa |
| `price_currency`  | str               | non-empty; requires `estimated_price` and vice versa |
| `vivino_rating`   | int or float      | `0`–`5`                                        |
| `drinking_window` | str               | non-empty                                      |
| `notes`           | str               | non-empty                                      |
| `special_occasion`| bool              | rendered only when `true`                      |

A missing or invalid required field, or an invalid recognized optional
field, raises `ValueError` naming the record and the field — the provider is
never called after a validation failure. Unknown fields are ignored, and no
value is ever coerced into another type. Validation runs on every record in
the namespace, including zero-quantity ones, before any filtering happens.

After validation, zero-quantity records are excluded and the remaining
active records are sorted by their own record key — never merged,
deduplicated, ranked, or reordered by price, rating, producer, vintage, or
model inference. Two records for a similar or identical wine stay separate
because their record keys differ. A missing `wine_cellar` namespace, an
empty one, or one containing only zero-quantity records produces no
`Personal wine cellar:` section at all.

A private constant, `_CELLAR_MAX_RECORDS = 100`, caps how many active
records go into a single prompt. Up to and including 100 active records are
all included, with no silent truncation. Above that limit, no partial
inventory is sent — instead, an honest section states how many active
holdings exist, that it exceeds the v1 limit, that no partial inventory was
supplied, and that the model should ask the user to narrow the request
rather than claim to have evaluated the whole cellar. Either way the
provider is still called exactly once.

Deterministic bottle-count lookup and wine-name matching are **not**
implemented in this milestone — a question like "how many bottles of
Sample Estate Reserve Red do I have?" is still answered by the model,
reasoning over the structured cellar context in the prompt, not by exact
code-level lookup. This remains planned, not implemented.

There is still no write API on `KnowledgeStore` itself, no quantity
decrementing, no editing workflow, no embeddings, fuzzy matching, or ranking
engine — the runtime remains read-only end to end. The one write path that
exists, `scripts/import_wine_cellar.py`, is a separate, human-invoked
maintenance script (see [Personal wine cellar](#personal-wine-cellar) above
and `scripts/README.md`); it writes `storage/knowledge/wine_cellar.json`
directly, replacing the whole document, and never runs automatically. No real
personal cellar data is committed to this repository.
