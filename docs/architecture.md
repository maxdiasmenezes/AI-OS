# Architecture

## Overview

AI-OS is organized as a **kernel** that provides shared infrastructure, a set of
**capabilities** that act as independent AI employees, and a set of
**interfaces** through which those employees will eventually be reached.
Everything is tied together through shared **prompts** and **storage**.

Today, the system is reachable through a single CLI entry point
(`kernel/main.py`); the `interfaces/` layer is scaffolded but not yet wired
to the kernel.

```
                    +------------------+
                    |    interfaces    |   planned: README stubs only
                    | claude / whatsapp|   (claude, whatsapp, web, voice),
                    |   web / voice    |   not wired to the kernel yet
                    +--------+---------+
                             |
                    +--------v---------+
                    |   kernel.main    |   implemented: current entry point
                    |      (CLI)       |
                    +--------+---------+
                             |
                    +--------v---------+
                    |   orchestrator   |   implemented
                    +--------+---------+
                             |
        +--------------------+--------------------+
        |                    |                     |
  +-----v-----+       +------v------+       +------v-------+
  |  memory   |       |   models    |       | capabilities  |
  | (JSONL)   |       | (provider   |       | registry +    |
  |           |       |  adapters)  |       | loader +      |
  +-----------+       +-------------+       | router        |
                                             +------+--------+
                                                    |
                                             +------v--------+
                                             | WineCapability |
                                             |    (wine)      |
                                             +----------------+

  kernel/knowledge: read-only KnowledgeStore contract + JSON implementation,
  wired into WineCapability's model-backed fallback for an optional personal
  wine profile and a read-only personal wine cellar inventory, and into its
  Deterministic Cellar Lookup v1 for the same read-only cellar inventory.
  kernel/tools: directory exists; planned, no code yet.
```

## Layers

### Interfaces

`interfaces/` holds the intended entry points through which a human will
interact with AI-OS: Claude, WhatsApp, a web app, and voice. Each currently
exists only as a directory with a short README describing its intent — none
contain code, and none are wired to the orchestrator yet. The system's actual
entry point today is a CLI: `python -m kernel.main "<prompt>"`
(`kernel/main.py`), which is a composition root rather than part of
`interfaces/`. Once a real interface is built, it will translate a
channel-specific message into a call into the orchestrator, and translate the
result back into that channel's format — interfaces are meant to contain no
business logic of their own.

### Kernel

`kernel/` is the shared core that every capability depends on:

- **orchestrator** — receives a prompt, decides whether a capability should
  handle it, and returns the result either way. Implemented: it wires up a
  model provider, a memory manager, a read-only knowledge store (a
  `JSONKnowledgeStore` constructed from `config.knowledge_storage_dir`), and
  a capability router once per run, then owns the per-request routing/fallback
  decision described in [Request flow](#request-flow). On the routed branch it
  passes its own provider, memory manager, and knowledge store instances to
  the capability loader (`capability_loader(capability_id, self._provider,
  self._memory, self._knowledge)`), so a capability can reuse the same
  instances the orchestrator already built, rather than constructing or
  configuring its own.
- **memory** — conversation history persisted across requests. Implemented:
  `MemoryManager` (`kernel/memory/manager.py`) backed by a JSONL file per
  namespace (`kernel/memory/jsonl.py`), stored under the directory configured
  in `kernel/config/config.yaml` (`storage/memory/` by default).
- **knowledge** — the shared knowledge base infrastructure (storage and
  retrieval) capabilities use to look up domain knowledge. A minimal,
  read-only contract exists: `KnowledgeStore` (`kernel/knowledge/base.py`)
  defines `get(namespace, key)` and `list_records(namespace)`, with one
  implementation, `JSONKnowledgeStore` (`kernel/knowledge/json_store.py`),
  that reads one keyed JSON document per namespace
  (`<storage_dir>/<namespace>.json`) from a storage directory explicitly
  injected by the caller — the orchestrator constructs the shared instance
  from `config.knowledge_storage_dir` (`kernel/config/config.yaml`'s
  `knowledge.storage_dir`, `storage/knowledge` by default). A missing
  namespace is treated as empty; malformed knowledge data raises an error
  rather than being treated as an empty store. `WineCapability` is the first
  consumer, reading an optional personal wine-preferences profile via
  `get()` and an optional, read-only personal cellar inventory via
  `list_records()` (see Capabilities below); there is still no write API,
  search, embeddings, vector retrieval, or web access.
- **models** — the abstraction layer over language models, so capabilities
  and the orchestrator do not depend on a specific model provider directly.
  The `ModelProvider` contract and a `get_provider()` factory are implemented
  (`kernel/models/base.py`, `kernel/models/factory.py`), and adapter modules
  exist for Ollama, Anthropic, OpenAI, and Gemini. Of these, only Ollama is
  selected as the active provider in `kernel/config/config.yaml` and exercised
  end-to-end today; the other adapters are present in the codebase but not
  verified as the active path.
- **tools** — reusable tools (actions, integrations, lookups) that
  capabilities could invoke. Not yet implemented — `kernel/tools/` contains
  only a README describing intent.
- **config** — settings that govern how the kernel and its components
  behave. Implemented: non-secret settings load from `kernel/config/config.yaml`
  (active provider, provider settings, memory, knowledge, and log locations),
  secrets load from `.env` (`kernel/config/config.py`). `Config.knowledge_storage_dir`
  is resolved to an absolute `Path` the same way `log_path` is — relative to
  the repository root — so `JSONKnowledgeStore` can be constructed directly
  from it without any further path handling.

The kernel is domain-agnostic. It knows how to run a capability; it does not
know what wine, travel, or strategy mean.

### Capabilities

`capabilities/` holds the AI employees. One is implemented today —
**wine** — reached through a small, explicit pipeline:

- **Capability contract** (`kernel/capabilities/base.py`) — an ABC every
  capability implements: an `id` property and a
  `handle(prompt) -> str | ModelResponse` method. Returning a plain `str`
  means a deterministic response with no model call; returning a
  `ModelResponse` (the kernel's provider-agnostic model-result type,
  `kernel/models/base.py`) means the capability called a model itself and
  the result carries that model's real metadata. The orchestrator branches
  on which type it gets back (see Request flow) — it never inspects a
  capability's internals to decide.
- **Registry** (`kernel/capabilities/registry.py`) — discovers capability
  directories under `capabilities/` by name, without importing anything
  inside them.
- **Loader** (`capabilities/loader.py`) — the one place allowed to know about
  concrete capability classes; maps a known id to its class and instantiates
  it (currently `{"wine": WineCapability}`). `CapabilityLoader.load(capability_id,
  model_provider, memory_manager, knowledge_store)` takes the model
  provider, memory manager, and knowledge store explicitly and passes all
  three to the capability's constructor — the loader does not construct or
  configure any of them itself.
- **Router** (`kernel/orchestrator/router.py`) — deterministic prompt-to-id
  matching; routes to `"wine"` on the literal, case-insensitive whole word
  `\bwines?\b` (singular or plural), on a conservative `\bmy\s+cellar\b`
  phrase cue, or on a small, explicit set of natural wine-selection and
  food-pairing phrases (e.g. "which bottle should I open", or a
  pairing/selection verb combined with a small set of router-level food
  cues such as "pair this with chicken"), otherwise returns `None`. These
  phrase rules are plain compiled regexes with no model calls, fuzzy
  matching, scoring, or configuration involved, and are intentionally
  conservative: generic words like "drink", "bottle", "pair", "open",
  "suitable", "food", "own", "have", "bottles", "vintages", "producer",
  "region", or "country" never route on their own, only specific words or
  phrase combinations do — so a bare question like "Do I own Sample Estate
  Reserve Red?" does not route, since the router cannot safely distinguish
  it from "Do I own a red car?" without an explicit wine or cellar cue; the
  same question phrased as "Do I own any Sample Estate Reserve Red wine?"
  does route. The router does not depend on or import from
  `capabilities/wine/capability.py`, and carries no cellar-record knowledge
  or wine-name lists of its own.
- **WineCapability** (`capabilities/wine/capability.py`) — Wine Pairing v1,
  plus Deterministic Cellar Lookup v1, plus a memory- and knowledge-aware,
  model-backed fallback, tried in that fixed order on every `handle()`
  call. Constructed with three explicit dependencies,
  `WineCapability(model_provider, memory_manager, knowledge_store)`, all
  injected rather than self-constructed. Wine Pairing v1 is deterministic,
  keyword-based food-to-wine pairing across eight food categories with a
  defined priority order for overlapping matches (e.g. "spicy shrimp"
  resolves to spicy, not shellfish) — no model calls, no memory recall, no
  knowledge-store access, and `handle()` returns a plain `str` for these,
  immediately on match.

  A prompt that matches none of the eight pairing categories is checked
  next against Deterministic Cellar Lookup v1
  (`capabilities/wine/cellar_lookup.py`): a small, explicit set of factual
  cellar questions — total active bottle count, exact quantity for one
  wine, exact ownership (by wine name, producer, producer + wine name,
  region, or country), producer holdings listing, and vintage listing —
  answered directly from validated `wine_cellar` records, with no model
  call. Query detection (`parse_cellar_query()`) runs on the prompt text
  alone, before any knowledge-store access, against a small set of literal,
  conservative phrasings (e.g. "How many bottles of Reserve Red are in my
  cellar?", "Do I have any Burgundy wine?", "Show me my wines from Sample
  Estate.") — broad fragments like "How many?" or "What do I have?" and any
  pairing/recommendation prompt are not detected. Once a query is detected,
  `WineCapability` calls `knowledge_store.list_records("wine_cellar")` —
  the only knowledge access this path performs, never `get()` — and passes
  the raw records to `answer_cellar_query()`, which validates every record
  with the same `validate_cellar_record()` used by the fallback path
  before doing anything else, so one invalid record (including an unrelated
  or zero-quantity one) raises `ValueError` and aborts the whole answer.
  Matching is exact, case-insensitive equality after normalization
  (`str.casefold()`, collapsed whitespace, trailing `? . !` stripped) —
  no accent stripping, no substring or fuzzy matching, no aliases. A wine
  identity is normalized producer + normalized wine_name; only active
  (`quantity > 0`) records count toward totals, ownership, and listings.
  A wine-name-only target spanning more than one distinct producer is
  ambiguous and returns a clarification instead of guessing; supplying
  producer + wine name is never ambiguous, and an ownership match on
  region, producer, or country legitimately spanning several wine
  identities is not treated as ambiguity either. A target matching only
  zero-quantity records, or no record at all, gets an explicit factual
  answer saying so rather than silence or a guess. This layer returns a
  plain `str` immediately on a detected query, exactly like a pairing
  match, and adds no recommendation ranking, pairing/suitability logic,
  fuzzy or semantic matching, model-assisted name resolution, or cellar
  writes. Covered by `tests/capabilities/wine/test_cellar_lookup.py`.

  A prompt that matches neither the eight pairing categories nor a
  deterministic cellar query falls back to the injected `ModelProvider`
  (`kernel/models/base.py`): it first reads
  an optional personal wine-preferences profile via
  `knowledge_store.get("wine_profile", "profile")`, then reads the personal
  cellar inventory via `knowledge_store.list_records("wine_cellar")` (the
  only two knowledge accesses it performs), then recalls the last 10
  entries from the existing `"conversation"` memory namespace via the
  injected `MemoryManager`, in chronological order. The fallback prompt is
  assembled in a fixed order: wine-expert instructions, the personal
  profile (only when it contains at least one recognized, non-empty
  field), the personal cellar (only when at least one active record
  exists, or when the active cellar exceeds the v1 size limit), recalled
  conversation history (role and content per turn, only when entries
  exist), then the current request. The recognized profile fields —
  `preferred_styles`, `disliked_styles`, `budget_range`, `priorities`,
  `notes` — are validated by small private logic inside
  `capabilities/wine/capability.py`; an unrecognized field is ignored, and
  a recognized field with an invalid type or list value raises
  `ValueError` rather than being silently coerced.

  Each `wine_cellar` record represents one wine holding (not one physical
  bottle), keyed by its own record ID with no duplicate `id` field inside
  it. Every record, including zero-quantity ones, must carry four required
  fields (`producer`, `wine_name`, `color` as non-empty strings, `quantity`
  as a non-negative int excluding `bool`); ten further fields are optional
  (`vintage` as an int 1800–2100 or the exact string `"NV"`, `country`,
  `region`, `style`, `grapes` as a list of non-empty strings in authored
  order, `estimated_price` paired with `price_currency` — both present or
  both absent, `vivino_rating` from 0 through 5, `drinking_window`,
  `notes`, and `special_occasion` as a bool, rendered only when `true`). This
  field schema and its validation logic (`validate_cellar_record()`) live in
  `capabilities/wine/cellar_schema.py`, not in `capability.py` — a shared,
  domain-level module with no dependency on prompts, provider calls, or
  fallback behavior. `WineCapability` imports and calls it; it does not
  duplicate the rules. An invalid required or recognized-optional field
  raises `ValueError` naming the record and field, before the provider is
  ever called; unknown fields are ignored and nothing is coerced. After
  validation, zero-quantity records are excluded and the remaining active
  records are sorted by record key only — never merged, deduplicated, or
  ranked by price, rating, producer, vintage, or model inference. A private
  constant caps a single prompt at 100 active records; above that, no
  partial inventory is sent — an honest section states the count, the
  limit, and that the model should ask the user to narrow the request
  instead of claiming to have evaluated the whole cellar, and the provider
  is still called exactly once. A "how many bottles of X do I have"
  question reaches this fallback, and is answered by the model from the
  structured cellar context, only when it does not match one of
  Deterministic Cellar Lookup v1's conservative phrasings above — when it
  does, the deterministic layer answers it directly instead.

  The model call is scoped to wine expertise by `prompts/wine/fallback.md`,
  which distinguishes the durable personal profile, the real but read-only
  cellar inventory, and recent, possibly-unrelated conversation context,
  and adds explicit everyday-versus-special-occasion guidance: never
  assume an occasion is special unless the request clearly says so, prefer
  lower-priced or lower-rated bottles for everyday requests, reserve
  `special_occasion: true` bottles for clearly stated special occasions,
  and never claim a bottle was consumed or its quantity decremented.
  `handle()` returns that call's real `ModelResponse` unchanged.
  `WineCapability` does not persist or write anything itself — the
  orchestrator remains responsible for writing memory after `handle()`
  returns, and nothing in this capability ever writes to the knowledge
  store. There is still no bottle-level purchase/ratings history beyond a
  cellar record's own fields, import or editing workflow, or web access.
  Covered by an automated pytest suite
  (`tests/capabilities/wine/test_capability.py`) and one end-to-end
  orchestrator test (`tests/kernel/orchestrator/test_orchestrator.py`).

Each capability is meant to be a self-contained domain expert that uses
kernel services (memory, knowledge, tools, models) to do its job. Capabilities
do not talk to interfaces directly, and they do not talk to each other
directly — all cross-capability coordination goes through the orchestrator.
Only `wine` exists so far; strategy, research, travel, and life administration
remain unimplemented.

### Prompts

`prompts/` holds prompt templates and instructions shared across the kernel
and capabilities, kept separate from code so they can be reviewed and
iterated on independently. `prompts/system.md` is implemented and loaded by
`kernel/prompts/builder.py` for the model-fallback path (see below).
`prompts/wine/fallback.md` holds the wine-expert-scoping instructions for
WineCapability's model-backed fallback; it is loaded directly by
`capabilities/wine/capability.py`, not by `kernel/prompts/`, since it is
wine-specific data owned by that capability.

### Storage

`storage/` is where persisted state actually lives: logs, memory, and
knowledge. The kernel's `memory` module defines *how* conversation data is
structured and stored (JSONL); `storage/` is *where* it is kept at rest
(`storage/memory/`, `storage/logs/`). `kernel/knowledge`'s
`JSONKnowledgeStore` reads from `storage/knowledge/` the same way — a real
personal wine profile would live at `storage/knowledge/wine_profile.json`,
and a real personal cellar inventory at
`storage/knowledge/wine_cellar.json` — but `storage/**/*.jsonl` and
`storage/knowledge/*.json` are gitignored, and no such files are committed
to this repository. Nothing in the runtime kernel writes to
`storage/knowledge/`; the two exceptions are `scripts/import_wine_cellar.py`
and `scripts/update_wine_cellar_quantity.py` (see Scripts and tests below),
human-invoked maintenance scripts that write `storage/knowledge/wine_cellar.json`
directly and outside the kernel entirely — neither goes through
`KnowledgeStore`, which stays read-only. Backups are not yet implemented.

### Scripts and tests

`scripts/` holds operational and maintenance scripts (setup, migrations,
utilities). Two are implemented today, both human-controlled, model-free
CLIs that write `storage/knowledge/wine_cellar.json` directly and outside
the runtime kernel:

- `scripts/import_wine_cellar.py` ("Safe Cellar Import v1") imports a CSV of
  wine holdings, replacing the entire destination document. It validates the
  complete CSV — headers, row-level type conversion, and every record
  through the shared `capabilities/wine/cellar_schema.py` validator — before
  writing anything. It defaults to a dry run that prints a summary (source
  path, destination path, holding counts, whether the destination already
  exists) without touching disk; a file is only written when the caller
  passes `--write` explicitly, which serves as the human confirmation —
  there is no interactive prompt. A `--write` run replaces the entire
  destination document (no merge, no partial update, no quantity
  decrementing) by writing to a temporary file in the destination directory
  and moving it into place with `os.replace()`, so the write is atomic and a
  failure at any point leaves an existing destination file byte-for-byte
  unchanged.
- `scripts/update_wine_cellar_quantity.py` ("Safe Cellar Quantity Update
  v1") changes only the `quantity` field of one existing holding, selected
  by exact, case-sensitive Cellar ID — no normalization, no producer/wine-name
  fallback, no fuzzy matching. `--set N` sets the quantity directly; `--decrement`
  (optionally followed by `N`, defaulting to 1) subtracts from the current
  quantity; a result below zero is rejected outright, never clamped, while
  decrementing exactly to zero is allowed and keeps the record (inactive,
  not deleted). It validates the complete existing cellar before computing
  the proposed quantity and the complete resulting cellar again before
  writing, using the same shared `cellar_schema.py` validator; one invalid
  record anywhere aborts the whole operation. The original parsed document
  is deep-copied and only the target record's `quantity` field is changed,
  so unrecognized fields and untouched records are preserved exactly rather
  than reconstructed from validated output. Like the importer, it defaults
  to a dry run, writes atomically only with an explicit `--write` flag, and
  a proposed quantity equal to the current one is a no-op that leaves the
  file byte-for-byte unchanged even with `--write`.

Both scripts never call a model and never run on their own — there is no
autonomous or scheduled write path for either. Cellar filtering, adding or
removing holdings, and editing any field other than quantity remain
unimplemented (deterministic *read-only* cellar lookup exists separately, in
`capabilities/wine/cellar_lookup.py` — see Capabilities above).

A third script, `scripts/wine_acceptance_check.py` ("Wine Data Readiness and
Acceptance Check v1"), is the committed, read-only half of a **hybrid
milestone**: this script — plus its automated tests — is committed code, but
onboarding real personal data (writing a real CSV and a real
`wine_profile.json`, running the importer with `--write`, and actually
executing this acceptance check against them) happens locally, after merge,
and is explicitly out of scope for what's committed here. Unlike the two
scripts above, it never writes anything at all — no `--write` flag exists.
It validates the local `wine_profile.json` and `wine_cellar.json` (default
paths under `storage/knowledge/`, injectable for testing) using the same
`capabilities/wine/cellar_schema.py` validator as the rest of the wine
stack, computes factual cellar statistics, and then runs two categories of
checks through the real `WineCapability.handle()`: deterministic
cellar-query cases (total bottle count, exact quantity, producer ownership,
producer holdings, vintage listing, region/country ownership, zero-quantity
behavior, an ambiguous wine name, multiple vintages of one wine, and an
unknown wine), selected from the real cellar data itself and reported
PASS/FAIL/SKIP; and a prompt-assembly case that exercises the model-backed
fallback's prompt construction structurally, without asserting on wording.
Deterministic checks run against a private fail-fast provider and fail-fast
memory object that raise immediately if touched, proving those paths remain
model- and memory-free; the prompt-assembly check runs against a private
recording fake provider (captures the assembled prompt without printing it
in full, since it contains personal data) and a private no-op memory object.
This no-op memory object is not the real `MemoryManager` — the script never
constructs or reads from the repository's persistent memory at all. An
explicit `--call-model` flag additionally runs a concise, fixed set of
prompts through the real, configured provider (constructed lazily, only
inside that opt-in path, via the existing `get_provider()` factory) and
prints the responses labeled `MANUAL REVIEW REQUIRED`, since the script
never asserts anything about a model's actual wording or pairing quality;
without that flag, no model provider is constructed or contacted, so a
plain run works even with no local model server running. This script
introduces no new capability, public interface, dependency, or change to
`WineCapability`, the router, the orchestrator, `KnowledgeStore`, or any
provider — it is read-only test-double-driven verification layered on top
of the existing wine stack. No real profile or cellar data is committed to
this repository.

`tests/` holds test suites that verify kernel and capability behavior;
today this covers `WineCapability` (`tests/capabilities/wine/test_capability.py`
and `tests/capabilities/wine/test_cellar_lookup.py`), the importer
(`tests/scripts/test_import_wine_cellar.py`), the quantity-update script
(`tests/scripts/test_update_wine_cellar_quantity.py`), and the acceptance
check (`tests/scripts/test_wine_acceptance_check.py`) — all four
`tests/scripts/` suites use only synthetic, dynamically constructed fixtures
under `tmp_path`; no real storage data is read or written by the test suite.

## Request flow

The flow below reflects what `Orchestrator.handle()` (`kernel/orchestrator/orchestrator.py`)
does today, run via the CLI entry point:

1. A prompt is passed to `python -m kernel.main "<prompt>"`.
2. The orchestrator asks the `CapabilityRouter` whether the prompt matches a
   capability.
3. **If it matches** (routed branch): the `CapabilityLoader` instantiates the
   matched capability, passing it the orchestrator's own provider, memory
   manager, and knowledge store (`capability_loader(capability_id,
   self._provider, self._memory, self._knowledge)`), and calls its `handle()`
   method. Today this only ever resolves to `wine`.
   If `handle()` returns a plain `str` (a deterministic response, no model
   call made), the orchestrator wraps it in a synthetic `ModelResponse` with
   `model="capability:<id>"` and zero token/latency counts. If `handle()`
   returns a `ModelResponse` (the capability called a model itself, as
   `WineCapability` does for wine requests outside its eight deterministic
   categories), the orchestrator uses that response unchanged, preserving
   its real model name, token counts, and latency. Any other return type is
   a programming error and raises `TypeError`.
4. **If it does not match** (fallback branch): `build_prompt()`
   (`kernel/prompts/builder.py`) assembles a prompt from the system prompt
   (`prompts/system.md`), the last 10 entries recalled from the
   `"conversation"` memory namespace, and the user's prompt; this combined
   prompt is sent to the configured model provider, and its response text is
   used.
5. Either way, the user's prompt and the response text are each appended as
   a separate entry to the `"conversation"` memory namespace
   (`storage/memory/conversation.jsonl`), and the full interaction (prompt,
   response text, model identifier, token counts, latency) is appended to the
   interaction log (`storage/logs/interactions.jsonl`).
6. The response text is printed to stdout.

There is currently no multi-turn session, streaming, retries, or tool use in
this flow — a single call to `handle()` is one full request/response cycle.

## Design boundaries

- **Interfaces are thin.** No domain logic or persistence lives in
  `interfaces/`.
- **The kernel is domain-agnostic.** No capability-specific logic lives in
  `kernel/`.
- **Capabilities are isolated.** They depend on the kernel, not on each
  other.
- **Prompts and storage are data, not code.** They are kept separate from
  implementation so they can evolve independently.

## Implementation status

**Implemented:**

- CLI entry point (`kernel/main.py`).
- Orchestrator: routing/fallback decision, wiring of provider, memory,
  knowledge store, and router.
- Model provider abstraction (`ModelProvider`, `get_provider()`), with an
  Ollama adapter configured as the active provider and exercised end-to-end.
  Adapter modules for Anthropic, OpenAI, and Gemini also exist in the
  codebase but are not the configured/verified active path.
- Memory: JSONL-backed conversation history, written and recalled on every
  request.
- Interaction logging to `storage/logs/interactions.jsonl`.
- System prompt (`prompts/system.md`), assembled with recalled memory for the
  model-fallback path.
- Capability contract (`handle(prompt) -> str | ModelResponse`), registry,
  explicit loader, and deterministic router.
- One capability: `WineCapability` — Wine Pairing v1 (eight deterministic
  food categories, no model, memory, or knowledge involvement), plus
  Deterministic Cellar Lookup v1 (`capabilities/wine/cellar_lookup.py`):
  total bottle count, exact quantity, exact ownership (by wine name,
  producer, producer + wine name, region, or country), producer holdings
  listing, and vintage listing, answered from validated `wine_cellar`
  records with no model call and only a `list_records("wine_cellar")`
  read, detected from a small set of conservative, literal phrasings
  before any knowledge access — plus a memory- and knowledge-aware,
  model-backed fallback for wine requests outside both of those: it reads
  an optional personal wine-preferences profile and a read-only personal
  cellar inventory from the knowledge store, recalls the last 10
  `"conversation"` memory entries as context, and is scoped by
  `prompts/wine/fallback.md` — with an automated pytest suite (including
  `tests/capabilities/wine/test_cellar_lookup.py`) plus one end-to-end
  orchestrator test per path. The model provider, memory manager, and
  knowledge store are all injected explicitly by `CapabilityLoader`,
  sourced from the orchestrator's own instances.
- Knowledge: a minimal, read-only `KnowledgeStore` contract
  (`kernel/knowledge/base.py`) and a `JSONKnowledgeStore` implementation
  (`kernel/knowledge/json_store.py`) that reads one keyed JSON document per
  namespace from an explicitly injected storage directory. Wired into
  `WineCapability`'s fallback for a personal wine-preferences profile
  (namespace `"wine_profile"`, key `"profile"`) and a read-only personal
  cellar inventory (namespace `"wine_cellar"`, one record per wine holding,
  validated by `capabilities/wine/cellar_schema.py` and formatted by
  `capabilities/wine/capability.py`); the same cellar inventory and
  validator are also read directly by Deterministic Cellar Lookup v1. No
  real profile or cellar data is committed to this repository.
- Safe Cellar Import v1 (`scripts/import_wine_cellar.py`): a human-invoked,
  model-free CLI, outside the runtime kernel, that validates a CSV against
  the shared `cellar_schema.py` rules and, only with an explicit `--write`
  flag, atomically replaces `storage/knowledge/wine_cellar.json` in full.
  Dry run is the default; `KnowledgeStore` is never used as a write
  interface.
- Safe Cellar Quantity Update v1 (`scripts/update_wine_cellar_quantity.py`):
  a second human-invoked, model-free CLI, outside the runtime kernel and not
  reachable from `WineCapability`, the router, or the orchestrator, that
  changes only the `quantity` field of one existing holding selected by
  exact, case-sensitive Cellar ID (`--set N` or `--decrement [N]`, default
  decrement of 1, zero allowed as a final quantity, below-zero rejected and
  never clamped). It validates the complete cellar with the same shared
  `cellar_schema.py` rules both before and after computing the proposed
  quantity, preserves unrecognized fields and untouched records exactly by
  deep-copying the original parsed document, and, only with an explicit
  `--write` flag, atomically replaces the destination file — a same-value
  `--set` is a no-op that leaves the file byte-for-byte unchanged even with
  `--write`. Dry run is the default; `KnowledgeStore` is never used as a
  write interface.
- Wine Data Readiness and Acceptance Check v1 (`scripts/wine_acceptance_check.py`):
  a human-invoked, read-only, model-free-by-default utility — the committed
  half of a hybrid milestone, with real-data onboarding and execution
  happening locally after merge. It validates the local `wine_profile.json`
  and `wine_cellar.json` with the shared `cellar_schema.py` validator,
  reports factual cellar statistics, and runs deterministic cellar-query and
  prompt-assembly checks through the real `WineCapability.handle()` using
  private fail-fast and recording-fake test doubles for the provider and
  memory — never the real `MemoryManager` and never the real provider unless
  the caller passes `--call-model`, which lazily constructs the configured
  provider via `get_provider()` and labels its responses `MANUAL REVIEW
  REQUIRED`. Writes nothing, adds no new capability or provider-architecture
  change, and commits no real profile or cellar data.

**Planned / not yet implemented:**

- Real interfaces (Claude, WhatsApp, web, voice) wired to the orchestrator —
  currently placeholder directories only.
- Adding or removing cellar holdings, editing any field other than
  quantity, automatic/conversation-driven quantity decrementing, backups, or
  change history (the importer is still a full-replacement snapshot and the
  quantity-update script is still quantity-only); recommendation ranking,
  pairing/suitability judgments, or model-assisted name resolution over the
  cellar (deterministic bottle-count lookup and exact wine-name matching are
  implemented — see Capabilities above — but only as exact, read-only
  lookups, never fuzzy matching, ranking, or writes); bottle-level
  purchase/ratings history beyond a cellar record's own fields; no search,
  embeddings, or vector retrieval; no write API on `KnowledgeStore` itself,
  and no autonomous or scheduled write path for the cellar.
- Tools (`kernel/tools/`).
- Additional capabilities (strategy, research, travel, life administration).
- Multi-turn sessions, streaming, retries, and any autonomous or
  multi-capability workflows.
