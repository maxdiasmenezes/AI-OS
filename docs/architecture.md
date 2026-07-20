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
  wine profile. kernel/tools: directory exists; planned, no code yet.
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
  consumer, reading an optional personal wine-preferences profile (see
  Capabilities below); there is still no write API, search, embeddings,
  vector retrieval, or web access.
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
  `\bwine\b`, or on a small, explicit set of natural wine-selection and
  food-pairing phrases (e.g. "which bottle should I open", or a
  pairing/selection verb combined with a small set of router-level food
  cues such as "pair this with chicken"), otherwise returns `None`. These
  phrase rules are plain compiled regexes with no model calls, fuzzy
  matching, scoring, or configuration involved, and are intentionally
  conservative: generic words like "drink", "bottle", "pair", "open",
  "suitable", or "food" never route on their own, only specific phrases or
  combinations do. The router does not depend on or import from
  `capabilities/wine/capability.py`.
- **WineCapability** (`capabilities/wine/capability.py`) — Wine Pairing v1
  plus a memory- and knowledge-aware, model-backed fallback. Constructed
  with three explicit dependencies, `WineCapability(model_provider,
  memory_manager, knowledge_store)`, all injected rather than
  self-constructed. Wine Pairing v1 is deterministic, keyword-based
  food-to-wine pairing across eight food categories with a defined priority
  order for overlapping matches (e.g. "spicy shrimp" resolves to spicy, not
  shellfish) — no model calls, no memory recall, no knowledge-store access,
  and `handle()` returns a plain `str` for these, immediately on match. A
  wine-related prompt that matches none of the eight categories falls back
  to the injected `ModelProvider` (`kernel/models/base.py`): it first reads
  an optional personal wine-preferences profile via
  `knowledge_store.get("wine_profile", "profile")` (the only knowledge
  access it performs), then recalls the last 10 entries from the existing
  `"conversation"` memory namespace via the injected `MemoryManager`, in
  chronological order. The fallback prompt is assembled in a fixed order:
  wine-expert instructions, the personal profile (only when it contains at
  least one recognized, non-empty field), recalled conversation history
  (role and content per turn, only when entries exist), then the current
  request. The recognized profile fields — `preferred_styles`,
  `disliked_styles`, `budget_range`, `priorities`, `notes` — are validated
  by small private logic inside `capabilities/wine/capability.py`; an
  unrecognized field is ignored, and a recognized field with an invalid
  type or list value raises `ValueError` rather than being silently
  coerced. The model call is scoped to wine expertise by
  `prompts/wine/fallback.md`, which distinguishes the durable personal
  profile from recent, possibly-unrelated conversation context, and
  `handle()` returns that call's real `ModelResponse` unchanged.
  `WineCapability` does not persist or write anything itself — the
  orchestrator remains responsible for writing memory after `handle()`
  returns, and nothing in this capability ever writes to the knowledge
  store. There is still no cellar inventory, bottle-level data, tools, or
  web access. Covered by an automated pytest suite
  (`tests/capabilities/wine/test_capability.py`).

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
personal wine profile would live at `storage/knowledge/wine_profile.json` —
but `storage/**/*.jsonl` and `storage/knowledge/*.json` are gitignored, and
no such file is committed to this repository. Nothing writes to
`storage/knowledge/` either; a human would place that file there directly.
Backups are not yet implemented.

### Scripts and tests

`scripts/` holds operational and maintenance scripts (setup, migrations,
utilities). `tests/` holds test suites that verify kernel and capability
behavior; today this covers `WineCapability`
(`tests/capabilities/wine/test_capability.py`).

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
  food categories, no model, memory, or knowledge involvement) plus a
  memory- and knowledge-aware, model-backed fallback for wine requests
  outside those categories: it reads an optional personal wine-preferences
  profile from the knowledge store, recalls the last 10 `"conversation"`
  memory entries as context, and is scoped by `prompts/wine/fallback.md` —
  with an automated pytest suite. The model provider, memory manager, and
  knowledge store are all injected explicitly by `CapabilityLoader`, sourced
  from the orchestrator's own instances.
- Knowledge: a minimal, read-only `KnowledgeStore` contract
  (`kernel/knowledge/base.py`) and a `JSONKnowledgeStore` implementation
  (`kernel/knowledge/json_store.py`) that reads one keyed JSON document per
  namespace from an explicitly injected storage directory. Wired into
  `WineCapability`'s fallback for a personal wine-preferences profile
  (namespace `"wine_profile"`, key `"profile"`); no real profile data is
  committed to this repository.

**Planned / not yet implemented:**

- Real interfaces (Claude, WhatsApp, web, voice) wired to the orchestrator —
  currently placeholder directories only.
- Cellar inventory, bottle-level data, and purchase/ratings history in the
  knowledge store; no search, embeddings, or vector retrieval.
- Tools (`kernel/tools/`).
- Additional capabilities (strategy, research, travel, life administration).
- Multi-turn sessions, streaming, retries, and any autonomous or
  multi-capability workflows.
