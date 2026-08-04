# Orchestrator

Coordinates requests across capabilities, routing work to the right AI employee and managing task flow.

## Persistence

After a routed capability handles a request, `Orchestrator.handle()`
normally writes the prompt and the response to memory (`conversation`
namespace) and to the interaction log (`storage/logs/interactions.jsonl`)
unconditionally. As of Milestone 38, a capability can opt a specific
response out of both writes by returning `EphemeralResult`
(`kernel/capabilities/base.py`) instead of a plain `str` - a `str`
subclass, so it behaves like an ordinary string everywhere else. This is
request-scoped, not capability-scoped: `capabilities/knowledge_commands/`
uses it for `/knowledge search` and `/knowledge ask` (query/question text
and, for `ask`, a model-generated answer must never be persisted), while
`/knowledge status`, `/knowledge ingest`, `/knowledge confirm`, and
`/knowledge cancel` are unaffected and keep being remembered/logged as
plain strings, exactly as before. Denied computer-action responses and
ordinary model-fallback responses are also unaffected.
