# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repository is

AI-OS is a personal AI Operating System: a kernel (`kernel/`) that provides
shared infrastructure — orchestration, memory, knowledge, models, tools,
config — and a set of capabilities (`capabilities/`) that act as specialized
AI employees (strategy, research, wine, travel, knowledge, life
administration), reached through several interfaces (`interfaces/`). See
[`docs/architecture.md`](docs/architecture.md) for the full layout and
[`docs/principles.md`](docs/principles.md) for the engineering principles
that govern design decisions.

## How to contribute here

- **Respect the layer boundaries.** Domain logic belongs in
  `capabilities/`, never in `kernel/` or `interfaces/`. Shared, reusable
  logic belongs in `kernel/`, never duplicated inside a capability.
  Capabilities do not call each other directly.
- **Follow `docs/principles.md`.** When a design choice isn't obvious,
  default to the principles documented there rather than inventing new
  conventions.
- **Keep the structure stable.** Don't create new top-level folders or
  reorganize existing ones without explicit instruction — treat the
  approved structure in `docs/architecture.md` as authoritative.
- **Do only what's asked.** Don't add code, dependencies, config files, or
  abstractions beyond the current request. This project grows
  incrementally and deliberately.
- **Keep docs honest.** If an architectural decision changes, update
  `docs/architecture.md` and/or `docs/principles.md` in the same change —
  don't let documentation drift from reality.
- **No secrets committed.** Nothing under `storage/` (logs, memory,
  knowledge, backups) should contain credentials or personal data that
  wasn't explicitly meant to be versioned.

## Working style

- Prefer small, focused changes over broad rewrites.
- Ask before making structural decisions that aren't already covered by
  `docs/architecture.md` or `docs/principles.md`.
- When a task specifies an exact list of files or changes, stick to that
  list exactly and stop — don't continue on to adjacent work uninvited.
