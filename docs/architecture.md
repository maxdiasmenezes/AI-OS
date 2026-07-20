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

  kernel/knowledge, kernel/tools: directories exist; planned, no code yet.
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
  model provider, a memory manager, and a capability router once per run, then
  owns the per-request routing/fallback decision described in
  [Request flow](#request-flow). On the routed branch it passes its own
  provider and memory manager instances to the capability loader
  (`capability_loader(capability_id, self._provider, self._memory)`), so a
  capability can reuse the same provider and memory manager the orchestrator
  already built, rather than constructing or configuring its own.
- **memory** — conversation history persisted across requests. Implemented:
  `MemoryManager` (`kernel/memory/manager.py`) backed by a JSONL file per
  namespace (`kernel/memory/jsonl.py`), stored under the directory configured
  in `kernel/config/config.yaml` (`storage/memory/` by default).
- **knowledge** — the shared knowledge base infrastructure (storage and
  retrieval) capabilities would use to look up domain knowledge. Not yet
  implemented — `kernel/knowledge/` contains only a README describing intent.
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
  (active provider, provider settings, memory and log locations), secrets load
  from `.env` (`kernel/config/config.py`).

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
  model_provider, memory_manager)` takes the model provider and memory
  manager explicitly and passes both to the capability's constructor — the
  loader does not construct or configure either itself.
- **Router** (`kernel/orchestrator/router.py`) — deterministic prompt-to-id
  matching; currently a single rule (`\bwine\b`, case-insensitive) routes to
  `"wine"`, otherwise returns `None`.
- **WineCapability** (`capabilities/wine/capability.py`) — Wine Pairing v1
  plus a memory-aware, model-backed fallback. Constructed with two explicit
  dependencies, `WineCapability(model_provider, memory_manager)`, both
  injected rather than self-constructed. Wine Pairing v1 is deterministic,
  keyword-based food-to-wine pairing across eight food categories with a
  defined priority order for overlapping matches (e.g. "spicy shrimp"
  resolves to spicy, not shellfish) — no model calls, no memory recall, and
  `handle()` returns a plain `str` for these, immediately on match. A
  wine-related prompt that matches none of the eight categories falls back
  to the injected `ModelProvider` (`kernel/models/base.py`): it first
  recalls the last 10 entries from the existing `"conversation"` memory
  namespace via the injected `MemoryManager`, in chronological order, and
  includes them in the fallback prompt as plain conversational history
  (role and content per turn) only when entries exist, followed by the
  current request. The model call is scoped to wine expertise by
  `prompts/wine/fallback.md`, and `handle()` returns that call's real
  `ModelResponse` unchanged. `WineCapability` does not persist anything
  itself — the orchestrator remains responsible for writing memory after
  `handle()` returns. This is recalled conversation history only, not
  structured preferences or personal cellar knowledge; there is still no
  knowledge retrieval, tools, web access, or personal cellar database.
  Covered by an automated pytest suite
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

`storage/` is where persisted state actually lives: logs and memory. The
kernel's `memory` module defines *how* conversation data is structured and
stored (JSONL); `storage/` is *where* it is kept at rest
(`storage/memory/`, `storage/logs/`). A knowledge base and backups are not
yet implemented.

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
   matched capability, passing it the orchestrator's own provider and memory
   manager (`capability_loader(capability_id, self._provider, self._memory)`),
   and calls its `handle()` method. Today this only ever resolves to `wine`.
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
- Orchestrator: routing/fallback decision, wiring of provider, memory, router.
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
  food categories, no model or memory involvement) plus a memory-aware,
  model-backed fallback for wine requests outside those categories,
  recalling the last 10 `"conversation"` memory entries as context and
  scoped by `prompts/wine/fallback.md` — with an automated pytest suite.
  Both the model provider and memory manager are injected explicitly by
  `CapabilityLoader`, sourced from the orchestrator's own instances.

**Planned / not yet implemented:**

- Real interfaces (Claude, WhatsApp, web, voice) wired to the orchestrator —
  currently placeholder directories only.
- Knowledge base storage and retrieval (`kernel/knowledge/`).
- Tools (`kernel/tools/`).
- Additional capabilities (strategy, research, travel, life administration).
- Multi-turn sessions, streaming, retries, and any autonomous or
  multi-capability workflows.
