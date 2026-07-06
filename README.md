# AI Operating System (AI-OS)

A personal AI Operating System: a kernel platform that runs a set of specialized AI employees, each dedicated to a specific domain of work and life.

## Mission

Build a personal AI Operating System that augments knowledge work.

AI-OS provides one intelligent platform with shared memory, shared knowledge,
shared tools, and multiple specialized capabilities that can be accessed
through different interfaces such as Claude Code, WhatsApp, Web, and Voice.

## Structure

| Folder | Purpose |
|---|---|
| `kernel/` | Core layer: orchestration, memory, knowledge, models, tools, config |
| `capabilities/` | Individual AI employees, one per domain |
| `interfaces/` | Entry points (Claude, WhatsApp, web, voice) |
| `prompts/` | Shared prompt templates and instructions |
| `storage/` | Persisted logs, memory, knowledge, and backups |
| `scripts/` | Operational and maintenance scripts |
| `tests/` | Test suites |
| `docs/` | Architecture and design documentation |

See [`docs/architecture.md`](docs/architecture.md) for how these pieces fit together, and [`docs/principles.md`](docs/principles.md) for the engineering principles behind the project.

## Status

This project is in early scaffolding. The folder structure and documentation are in place; implementation has not started yet.
