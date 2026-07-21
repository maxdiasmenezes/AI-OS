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

## Deterministic Cellar Lookup v1

After the eight pairing categories and before the model-backed fallback,
`WineCapability` checks the prompt against a second, independent
deterministic layer: `capabilities/wine/cellar_lookup.py`. This answers a
small, explicit set of factual cellar questions directly from validated
`wine_cellar` records, with no model call — total active bottle count,
exact quantity for one wine, exact ownership (by wine name, producer,
producer + wine name, region, or country), producer holdings listing, and
vintage listing. Query detection happens on the prompt text alone, before
any knowledge-store access; only a detected query touches the store, and
only via `knowledge_store.list_records("wine_cellar")` — never `get()`,
never memory recall, never the model provider. `handle()` returns a plain
`str` for a detected cellar query, exactly like a pairing match.

Supported phrasings are conservative and literal, e.g.:

- Total: "How many bottles are in my cellar?"
- Quantity: "How many bottles of Reserve Red are in my cellar?"
- Ownership: "Do I have any Burgundy wine?"
- Producer listing: "Show me my wines from Sample Estate."
- Vintage listing: "What vintages of Reserve Red are in my cellar?"

A bare question with no explicit wine or cellar cue — "Do I own Sample
Estate Reserve Red?", "Do I have any Burgundy?" — never reaches
`WineCapability` at all: `CapabilityRouter` (`kernel/orchestrator/router.py`)
only routes to `wine` on an explicit whole-word `wine`/`wines` mention or a
conservative `my cellar` phrase cue, deliberately never on generic words
like "own", "have", "bottles", "vintages", "producer", "region", or
"country" alone — it cannot safely tell "Do I own a red car?" apart from
"Do I own Sample Estate Reserve Red?" without that explicit cue. Phrase the
request as "Do I own any Sample Estate Reserve Red wine?" or "Do I have
Burgundy in my cellar?" instead.

Matching is exact and case-insensitive after normalization
(`str.casefold()`, whitespace collapsed to single spaces, trailing `? . !`
stripped) — nothing else. No accent stripping, no internal punctuation
changes, no substring matching, no fuzzy or semantic matching, no aliases.
A wine identity is normalized producer + normalized wine_name; records that
share an identity but differ in vintage, price, rating, or record ID
aggregate together for quantity and vintage answers. A wine-name-only
target that matches more than one distinct producer is ambiguous and
returns a clarification listing the distinct producers (sorted, original
spelling) instead of guessing; supplying producer + wine name together is
never ambiguous. Ownership matches on region, producer, or country may
legitimately span several wine identities — that is not ambiguity, and the
answer reports the total active quantity and holding count for that
category. Only active (`quantity > 0`) records count toward totals,
ownership, and listings; a target that matches only zero-quantity records
gets an explicit "quantity is zero" answer rather than being reported as
either owned or absent, and a target matching no record at all gets an
explicit "no match" answer. Every record in the namespace — including
unrelated and zero-quantity ones — is validated with the same
`validate_cellar_record()` used elsewhere before any of this runs; one
invalid record raises `ValueError` and aborts the whole answer, exactly
like the model-backed cellar path.

This layer adds no recommendation ranking, pairing or suitability logic,
fuzzy or semantic matching, model-assisted name resolution, or cellar
writes — it is read-only lookup only, covered by
`tests/capabilities/wine/test_cellar_lookup.py`.

## Model-backed fallback

A wine-related request that doesn't match any of the eight pairing
categories or a deterministic cellar query above (e.g. wine regions,
specific bottles, vintages, general buying or serving questions,
recommendations, or suitability judgments) is passed to the injected model
provider instead of returning a canned "out of scope" message.

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

A question like "how many bottles of Sample Estate Reserve Red do I have?"
is answered deterministically instead — see
[Deterministic Cellar Lookup v1](#deterministic-cellar-lookup-v1) above —
whenever it matches one of that layer's conservative, literal phrasings.
Only prompts that don't match any of those phrasings still reach the model
here, reasoning over this same structured cellar context.

There is still no write API on `KnowledgeStore` itself, no quantity
decrementing, no editing workflow, no embeddings, fuzzy matching, or ranking
engine — the runtime remains read-only end to end. The one write path that
exists, `scripts/import_wine_cellar.py`, is a separate, human-invoked
maintenance script (see [Personal wine cellar](#personal-wine-cellar) above
and `scripts/README.md`); it writes `storage/knowledge/wine_cellar.json`
directly, replacing the whole document, and never runs automatically. No real
personal cellar data is committed to this repository.
