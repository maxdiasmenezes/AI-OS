# AI Operating System (AI-OS)

Developed by Max Dias Menezes for Murilo Dias Menezes's personal use.

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

AI-OS is a working personal AI employee platform, operating inside a **controlled personal-use launch envelope** (Milestone 48).

Through WhatsApp today, it handles ordinary conversation and durable, multi-step task requests: bounded LLM planning, execution through a single `SafeTaskExecutor` action boundary, explicit human confirmation before any sensitive action, a persistent SQLite-backed task lifecycle, and crash/restart recovery for that durable state. Execution capability includes bounded local filesystem/repository/script/application actions, a read-only browser worker, and a read-only Windows desktop-status worker. The conversational and planning model layer is provider-agnostic where already wired (a local Ollama model is the currently configured/validated path; Anthropic/OpenAI/Gemini adapters exist but are not the active configuration).

This is **not** a multi-user or multi-worker system, not a hosted SaaS or 24/7 high-availability service, not a generalized exactly-once execution engine, not an arbitrary-shell or unrestricted-automation platform, and not an enterprise scheduler. The validated launch shape is one operator, one Windows machine, one local runtime, one SQLite task database, one worker, and one authorized WhatsApp sender.

See [`interfaces/whatsapp/README.md`](interfaces/whatsapp/README.md) for the full operator launch runbook, and [`docs/architecture.md`](docs/architecture.md) for the complete architecture and milestone history, including exact acceptance evidence and known limitations.
