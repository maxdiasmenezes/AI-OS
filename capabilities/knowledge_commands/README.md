# Knowledge Commands

Milestone 37's safe, deterministic `/knowledge` command layer over the
Milestone 36 local knowledge base (`kernel/knowledge_base/`): status,
lexical search, and ingestion, reachable only through a strict command
grammar - never natural language, never a model.

This is a distinct package from `capabilities/knowledge/`, which remains
reserved for a future, different, higher-level AI-employee capability
focused on managing and surfacing personal knowledge and learning. This
package (`capabilities/knowledge_commands/`) is the deterministic
Milestone 37 command layer only - it never invokes a model and never
performs retrieval-augmented generation.

`KnowledgeCommandsCapability` is deterministic end-to-end: it never calls
a model, never reads memory, and never reads the `KnowledgeStore`
(`kernel/knowledge/`) capabilities like `WineCapability` use. It delegates
all actual work to the existing, unmodified Milestone 36 core functions
(`kernel/knowledge_base/status.py:get_status`,
`kernel/knowledge_base/search.py:search`,
`kernel/knowledge_base/ingest.py:ingest_source`) - this capability only
parses the `/knowledge` command, enforces the confirmation step for
ingestion, formats bounded replies, and audits.

## Authorization

`KnowledgeCommandsCapability.requires_computer_actions = True`.
`Orchestrator.handle()` refuses to call this capability's `handle()` at
all unless the request carries a `RequestContext(allow_computer_actions=
True)` - see `kernel/orchestrator/context.py`. This applies to every verb
uniformly, including read-only `status` and `search` - there is no
per-verb trust tier. The CLI and every other caller that doesn't pass a
context stay denied by default. Only `interfaces/whatsapp/handler.py`
constructs an authorizing context today, and only for a message that
already passed WhatsApp's own exact-sender authorization. This capability
performs no authorization of its own.

The offline, human-invoked `uv run python -m scripts.knowledge ...` CLI
remains the local operational interface and is unaffected by this
milestone's trust gating - it is a separate surface, outside the runtime
kernel, never reached by the orchestrator.

## Commands

```
/knowledge
/knowledge help
/knowledge status
/knowledge status --source <source-key>
/knowledge search -- <query text>
/knowledge search --source <source-key> -- <query text>
/knowledge search --limit <1-10> -- <query text>
/knowledge search --source <source-key> --limit <1-10> -- <query text>
/knowledge search --limit <1-10> --source <source-key> -- <query text>
/knowledge ingest <source-key>
/knowledge confirm
/knowledge cancel
```

`<source-key>` is a symbolic key already approved in
`kernel/config/knowledge_base.yaml` - never a path, and never validated
by shape alone (a shape-valid but unapproved key is still rejected by the
existing Milestone 36 allowlist). `search` requires exactly one literal
bare `--` delimiter; everything after the first one is query text, never
re-parsed as options, even if it itself contains `--`-shaped tokens. Any
extra token, missing token, unknown option, duplicate option, missing
option value, missing delimiter, blank query, or malformed source key is
rejected with one fixed, generic reply
(`Invalid knowledge command. Use /knowledge help.`) - nothing is guessed.
No command accepts a path, `..`, a SQL fragment, an FTS expression, a
database location, or a shell command.

## Read-only operations

`status` and `search` never write anything and never propose a
confirmation. `search` calls the existing Milestone 36 `search()`
function unmodified, requesting at most 10 results
(`MAX_INTERFACE_RESULT_LIMIT`, tighter than that function's own
service-level cap of 50) and passing the raw query text only transiently
through the parser and this call stack - it is never logged, audited, or
stored in pending confirmation state, and is garbage-collected with the
request once `handle()` returns.

## Ingestion and confirmation

`ingest` is sensitive: the symbolic key is checked against the current
approved-source allowlist immediately (no pre-scan of the source, no
document-count disclosure), and if approved, only *proposes* the action -
it replies with a confirmation prompt naming the source key and nothing
else (no path, no database location, no document names). `/knowledge
confirm` within 2 minutes runs it, by calling the existing atomic
`ingest_source()` unmodified; anything else (timeout, or `/knowledge
cancel`) discards it. The pending action lives in
`default_knowledge_confirmation_store` - a **separate** instance of
`kernel.tools.confirmation.ConfirmationStore` from the one
`capabilities/tasks/TasksCapability` uses, so a pending `/knowledge
ingest` proposal can never collide with, or be silently evicted by, a
pending `/task` action (and vice versa). Confirming consumes the pending
action immediately, before it runs, so it can never be replayed even if
execution itself fails; the approval is re-checked against the current
configuration at execute time, so a source removed from the allowlist
after proposal but before confirmation fails closed.

## Output limits

Fixed, in addition to (and always at least as strict as) Milestone 36's
own service-level limits: at most 10 results requested per search, a
200-character excerpt, an 80-character path, and a 3,500-character total
reply, all truncated deterministically with a visible ellipsis; when
complete results don't all fit, whole results are dropped from the end
and a fixed notice is appended - never a partial result, never multiple
messages.

## Privacy and audit

Reuses `kernel/tools/audit.py` (a separate, distinct instance of
`ConfirmationStore`, but the *same* audit module and log file
`/task` uses). Audit records carry only a fixed action name, the symbolic
source key when applicable, and a fixed outcome - never query text, an
excerpt, a path, SQL, an FTS expression, an exception, a sender identity,
or a message body. Every recognized failure maps to one fixed message by
exception type (`kernel/knowledge_base/messages.py`), never `str(exc)`.

## Out of scope (see docs/architecture.md for the full list)

No automatic RAG, no automatic retrieval before a model call, no
prompt-context injection, no embeddings or vector search, no
model-generated summaries, no path-based or partial-source ingestion, no
scheduled or background ingestion, no deletion commands, no document
content display. The typed core search API
(`kernel/knowledge_base/search.py:search`) this capability already calls
is the same one an explicit, future RAG/retrieval integration could call
- nothing here forecloses that; it simply isn't wired to a model in this
milestone.
