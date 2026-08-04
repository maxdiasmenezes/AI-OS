# Knowledge Commands

Milestone 37's safe, deterministic `/knowledge` command layer over the
Milestone 36 local knowledge base (`kernel/knowledge_base/`): status,
lexical search, and ingestion, reachable only through a strict command
grammar - never natural language. Milestone 38 adds one explicit verb,
`ask`, that performs safe, explicit, knowledge-grounded answering: it
retrieves bounded local evidence and makes exactly one call to the
configured model provider to answer a question from that evidence alone.

This is a distinct package from `capabilities/knowledge/`, which remains
reserved for a future, different, higher-level AI-employee capability
focused on managing and surfacing personal knowledge and learning. This
package (`capabilities/knowledge_commands/`) is the deterministic
`/knowledge` command layer - it never performs *automatic* retrieval or
retrieval-augmented generation, and it is never triggered by anything
other than an explicit `/knowledge ...` command.

**`KnowledgeCommandsCapability` is not model-free.** `status`, `search`,
`ingest`, `confirm`, `cancel`, and `help` remain fully deterministic - no
model call, ever - and none of them read memory or the `KnowledgeStore`
(`kernel/knowledge/`) capabilities like `WineCapability` use. `ask` is the
one exception: it explicitly calls the model provider already injected by
`CapabilityLoader`, exactly once per request, to answer a question
grounded only in retrieved local excerpts. It still never reads memory,
never reads the `KnowledgeStore`, and never recalls conversation history.
Every verb delegates all actual retrieval/ingestion work to the existing,
unmodified Milestone 36 core functions
(`kernel/knowledge_base/status.py:get_status`,
`kernel/knowledge_base/search.py:search`,
`kernel/knowledge_base/ingest.py:ingest_source`) plus, for `ask`, the
Milestone 38 additions (`kernel/knowledge_base/evidence.py:retrieve_evidence`,
`kernel/knowledge_base/answer.py`) - this capability only parses the
`/knowledge` command, enforces the confirmation step for ingestion, makes
the one model call for `ask`, formats bounded replies, and audits.

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
/knowledge ask -- <question>
/knowledge ask --source <source-key> -- <question>
/knowledge ask --limit <1-5> -- <question>
/knowledge ask --source <source-key> --limit <1-5> -- <question>
/knowledge ask --limit <1-5> --source <source-key> -- <question>
/knowledge ingest <source-key>
/knowledge confirm
/knowledge cancel
```

`<source-key>` is a symbolic key already approved in
`kernel/config/knowledge_base.yaml` - never a path, and never validated
by shape alone (a shape-valid but unapproved key is still rejected by the
existing Milestone 36 allowlist). `search` and `ask` each require exactly
one literal bare `--` delimiter; everything after the first one is
query/question text, never re-parsed as options, even if it itself
contains `--`-shaped tokens. `ask`'s question text must be 1-200
characters after whitespace-run normalization; its `--limit` is a
1-5 evidence-chunk count, a distinct concept from `search`'s 1-10
result-count limit. Any extra token, missing token, unknown option,
duplicate option, missing option value, missing delimiter, blank
query/question, an oversized question, or a malformed source key is
rejected with one fixed, generic reply
(`Invalid knowledge command. Use /knowledge help.`) - nothing is guessed.
No command accepts a path, `..`, a SQL fragment, an FTS expression, a
database location, a provider or model name, a system-prompt override, a
temperature or token-limit setting, a timeout value, or a shell command.

## Read-only operations

`status` and `search` never write anything and never propose a
confirmation. `search` calls the existing Milestone 36 `search()`
function unmodified, requesting at most 10 results
(`MAX_INTERFACE_RESULT_LIMIT`, tighter than that function's own
service-level cap of 50) and passing the raw query text only transiently
through the parser and this call stack - it is never logged or audited,
and, as of Milestone 38, its result is returned as `EphemeralResult`
(`kernel/capabilities/base.py`) so the orchestrator does not persist the
query or the formatted results to memory or the interaction log either
(see kernel/orchestrator/README.md). It is never stored in pending
confirmation state, and is garbage-collected with the request once
`handle()` returns.

## Grounded answering (`ask`, Milestone 38)

`ask` retrieves bounded local evidence via
`kernel/knowledge_base/evidence.py:retrieve_evidence` (returning full
bounded chunk text instead of a short excerpt - up to 5 chunks, 1,500
characters each, 7,500 characters total). As of Milestone 38.1,
`retrieve_evidence` shares `kernel/knowledge_base/query.py`'s validation
and term-extraction with `search`, but builds its own ask-only,
minimum-term FTS5 MATCH expression rather than `search`'s strict
all-terms-AND one, so an ordinary natural question ("How does the
repository backup feature work?") can still retrieve relevant evidence -
see `kernel/knowledge_base/README.md` for the exact matching rule. This
is still exactly one read-only, parameterized SQL query, and if any
evidence was found, makes exactly one call to the model provider already
injected by `CapabilityLoader`. The prompt is built by
`kernel/knowledge_base/answer.py`: fixed instructions from
`prompts/knowledge/ask_system.md`, followed by the question and evidence
as one untrusted `json.dumps(..., ensure_ascii=False)` object between
fixed marker lines. The model must return exactly one JSON object
(`{"answer", "used_citations", "sufficient"}`); any response that doesn't
strictly match - wrong shape, an invented or missing citation, an
inline/`used_citations` mismatch, an empty answer - is rejected outright,
never partially trusted. Citation labels (`S1`..`S5`) are assigned by
code in retrieval order, never by the model, and the final "Sources:"
section is generated entirely from code-owned metadata (symbolic source
key, relative path, chunk ordinal - never a chunk ID, a rank, or an
absolute path).

**Consent (v1):** the explicit `/knowledge ask` command is itself
sufficient consent to send the question and selected local excerpts to
the configured model provider - no second confirmation step, and nothing
is placed in `PendingAction`. This is acceptable today because the only
implemented provider is local Ollama (`kernel/models/ollama.py`,
`http://localhost:11434`) - nothing leaves the machine. **This must be
revisited before a remote provider (Anthropic/OpenAI/Gemini) is ever
enabled for this operation**; the current `ModelProvider` contract has no
way to detect whether a configured provider is local or remote, and this
milestone deliberately does not add one.

Like `search`, `ask`'s result is returned as `EphemeralResult` - the
question, the evidence, and the generated answer are never written to
memory or the interaction log by the orchestrator. This capability itself
never calls `memory_manager.recall()`/`remember()` for `ask` either - no
conversation history is read or injected into the prompt, and each `ask`
is fully stateless.

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

For `ask`: the model's `answer` field is bounded to 2,500 characters, the
source section displays at most 5 entries, and the total reply is bounded
to the same 3,500-character budget as every other command. If the answer
and its still-referenced source entries don't fit even after reducing the
answer, `ask` fails closed to the fixed unverifiable-answer reply rather
than dropping a source entry whose `[S#]` citation remains visible in the
answer text.

## Model-provider invocation (`ask` only)

`ask` uses the `ModelProvider` instance already injected by
`CapabilityLoader` - it never instantiates a provider or provider factory
itself, never lets the caller choose a provider or model, never sets a
temperature/token/timeout option from the command, and calls
`send_prompt()` exactly once per request: no retry, no streaming, no
second "critique" or citation-repair model call. A provider exception
(including a request timeout - see `kernel/models/ollama.py`'s fixed
`OLLAMA_REQUEST_TIMEOUT_SECONDS`) is caught here and mapped to one fixed
reply; the raw exception is never in the reply, the audit record, memory,
or the interaction log.

## Privacy and audit

Reuses `kernel/tools/audit.py` (a separate, distinct instance of
`ConfirmationStore`, but the *same* audit module and log file
`/task` uses). Audit records carry only a fixed action name, the symbolic
source key when applicable, and a fixed outcome (`executed`/`failed`/
`rejected`) - never query/question text, evidence, an excerpt, an answer,
a path, SQL, an FTS expression, a prompt, a provider response, an
exception, a sender identity, or a message body. Every recognized failure
maps to one fixed message by exception type
(`kernel/knowledge_base/messages.py`) or, for `ask`'s model-specific
outcomes, one of a small set of fixed strings defined in this package -
never `str(exc)`.

## Out of scope (see docs/architecture.md for the full list)

No automatic RAG, no automatic or intent-detected retrieval before a
model call, no prompt-context injection into ordinary prompts, no
embeddings or vector search, no semantic reranking, no hybrid retrieval,
no web search, no external connectors, no model-generated summaries or
ingestion metadata, no path-based or partial-source ingestion, no
scheduled or background ingestion, no deletion commands, no document
content display, no multi-turn grounded conversation, no answer caching,
no self-critique or citation-repair model call, no streaming, no
remote-provider detection or per-provider consent logic. The typed core
retrieval APIs this capability calls
(`kernel/knowledge_base/search.py:search`,
`kernel/knowledge_base/evidence.py:retrieve_evidence`) are the same ones
an explicit, future semantic/embedding-based retrieval integration could
sit behind - nothing here forecloses that; `ask` is explicit
retrieval-augmented generation, never automatic.
