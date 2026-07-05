# Architecture

## Overview

AI-OS is organized as a **kernel** that provides shared infrastructure, a set of
**capabilities** that act as independent AI employees, and a set of
**interfaces** through which those employees are reached. Everything is tied
together through shared **prompts** and **storage**.

```
                    +------------------+
                    |    interfaces    |
                    | claude / whatsapp|
                    | web / voice      |
                    +--------+---------+
                             |
                    +--------v---------+
                    |      kernel      |
                    |  orchestrator    |
                    +--------+---------+
                             |
        +--------------------+--------------------+
        |                    |                     |
  +-----v-----+       +------v------+       +------v------+
  |  memory   |       |  knowledge  |       |    tools    |
  +-----------+       +-------------+       +-------------+
                             |
                    +--------v---------+
                    |   capabilities   |
                    | strategy/research|
                    | travel/wine/...  |
                    +------------------+
```

## Layers

### Interfaces

`interfaces/` holds the entry points through which a human interacts with
AI-OS: Claude, WhatsApp, a web app, and voice. Interfaces are responsible for
translating a channel-specific message into a request the kernel can route,
and translating the kernel's response back into that channel's format.
Interfaces contain no business logic of their own.

### Kernel

`kernel/` is the shared core that every capability depends on:

- **orchestrator** — receives a request from an interface, decides which
  capability (or capabilities) should handle it, and returns the result.
  It owns routing, not domain logic.
- **memory** — short- and long-term context: conversation history, user
  facts, and state that needs to persist across sessions.
- **knowledge** — the shared knowledge base infrastructure (storage and
  retrieval) that capabilities use to look up domain knowledge.
- **models** — the abstraction layer over language models, so capabilities
  and the orchestrator do not depend on a specific model provider directly.
- **tools** — reusable tools (actions, integrations, lookups) that
  capabilities can invoke.
- **config** — settings that govern how the kernel and its components
  behave.

The kernel is domain-agnostic. It knows how to run a capability; it does not
know what wine, travel, or strategy mean.

### Capabilities

`capabilities/` holds the AI employees themselves: strategy, research, wine,
travel, knowledge, and life administration. Each capability is a self
-contained domain expert that uses kernel services (memory, knowledge, tools,
models) to do its job. Capabilities do not talk to interfaces directly, and
they do not talk to each other directly — all cross-capability coordination
goes through the orchestrator.

### Prompts

`prompts/` holds prompt templates and instructions shared across the kernel
and capabilities, kept separate from code so they can be reviewed and
iterated on independently.

### Storage

`storage/` is where persisted state actually lives: logs, memory snapshots,
knowledge base contents, and backups. The kernel's `memory` and `knowledge`
modules define *how* data is structured and accessed; `storage/` is *where*
it is kept at rest.

### Scripts and tests

`scripts/` holds operational and maintenance scripts (setup, migrations,
utilities). `tests/` holds test suites that verify kernel and capability
behavior.

## Request flow

1. A human sends a message through an interface (e.g. WhatsApp).
2. The interface normalizes the message and hands it to the kernel
   orchestrator.
3. The orchestrator determines which capability should handle the request,
   using memory/knowledge as needed for context.
4. The capability processes the request, using kernel services (models,
   tools, knowledge) as needed.
5. The result flows back through the orchestrator to the originating
   interface, which formats it for the channel.
6. Relevant state (memory, logs) is persisted to `storage/`.

## Design boundaries

- **Interfaces are thin.** No domain logic or persistence lives in
  `interfaces/`.
- **The kernel is domain-agnostic.** No capability-specific logic lives in
  `kernel/`.
- **Capabilities are isolated.** They depend on the kernel, not on each
  other.
- **Prompts and storage are data, not code.** They are kept separate from
  implementation so they can evolve independently.

## Status

This document describes the intended shape of the system. No implementation
exists yet; this is the reference for how future work should be organized.
