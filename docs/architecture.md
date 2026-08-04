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
                    |    interfaces    |   whatsapp: implemented, own HTTP
                    | claude / whatsapp|   server + composition root
                    |   web / voice    |   (interfaces/whatsapp/server.py);
                    +--------+---------+   claude/web/voice: README stubs
                             |             only, not wired yet
                    +--------v---------+
                    | kernel.main /    |   implemented: two independent
                    | whatsapp.server  |   composition roots - the CLI and
                    |  (composition    |   WhatsApp each build their own
                    |     roots)       |   Orchestrator directly
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
  kernel/tools: implemented (Milestone 33; repo_health added in
  Milestone 34; repository_backup added in Milestone 35) - the safe
  computer task execution layer (allowlist-only actions, timeouts,
  confirmation, audit), consumed by capabilities/tasks/TasksCapability.
  kernel/knowledge_base: implemented (Milestone 36) - a separate, local-only
  SQLite FTS5 lexical search/ingestion service over approved .md/.txt
  sources, reachable only through scripts/knowledge.py (a human-invoked
  CLI outside the runtime kernel); not wired into the orchestrator, any
  capability, or kernel/knowledge above.
```

## Layers

### Interfaces

`interfaces/` holds the intended entry points through which a human will
interact with AI-OS: Claude, WhatsApp, a web app, and voice. `claude/`,
`web/`, and `voice/` each still exist only as a directory with a short
README describing intent — no code, not wired to the orchestrator. The
system's other entry point is still a CLI: `python -m kernel.main
"<prompt>"` (`kernel/main.py`), a composition root outside `interfaces/`.
Once a real interface is built, it translates a channel-specific message
into a call into the orchestrator, and translates the result back into
that channel's format — interfaces are meant to contain no business logic
of their own.

**WhatsApp** (`interfaces/whatsapp/`) is implemented: a self-contained
composition root and loopback-only HTTP server that reaches the same
`Orchestrator` the CLI does, without going through `kernel/main.py`.
**Single-user only** — it authorizes exactly one sender, by exact string
equality, with no allow-list, no multi-user support, and no
normalization. It is built from focused modules, each with a single
responsibility:

- **config** (`config.py`) — loads and validates WhatsApp-specific
  environment configuration (`WhatsAppConfig`, `load_whatsapp_config()`),
  separate from `kernel/config/config.py`: the Cloud API verify token, app
  secret, access token, phone number ID, the single authorized sender ID
  (`WHATSAPP_AUTHORIZED_SENDER_ID`, exact-match only), the Cloud API
  version (`WHATSAPP_API_VERSION` — **required, no code default**; a
  missing or malformed value fails startup, and this module embeds no
  real Graph API version anywhere), and two optional operational
  settings: `WHATSAPP_HOST` (default `127.0.0.1`, validated as
  loopback-only — `0.0.0.0`, LAN addresses, public addresses, and
  arbitrary hostnames are all rejected; only a loopback IP literal or
  `localhost` is accepted, and `server.py` binds a genuine `AF_INET6`
  socket when the validated host is IPv6, so `::1` actually works rather
  than failing to bind) and `WHATSAPP_PORT` (default `8000`). Every
  required value is validated at startup — a missing or invalid one
  raises `WhatsAppConfigError` immediately, before the HTTP server binds
  to anything; validation errors never echo back the invalid value for a
  secret or personal-identifier field (they may for `host`/`port`/
  `api_version`, none of which are secret). `.env` is loaded the same way
  `kernel/config/config.py` does (`override=False`, explicit rather than
  relying on the library default); an explicit `env` mapping can be
  passed in for testing. `WhatsAppConfig` itself is `@dataclass(frozen=True)`
  — assigning to a field raises `FrozenInstanceError` — with
  `verify_token`, `app_secret`, `access_token`, `phone_number_id`, and
  `authorized_sender_id` all marked `field(repr=False)`, so none of them
  appear if the config object is ever logged or printed by accident; only
  `host`, `port`, and `api_version` are visible in `repr()`.
- **signature** (`signature.py`) — `verify_signature(app_secret, raw_body,
  signature_header)` accepts only a header of the exact shape
  `sha256=<64 hex characters>`, and validates it (an HMAC-SHA256 of the
  exact raw request bytes) using `hmac.compare_digest`, over the body
  before any JSON parsing happens.
- **dedup** (`dedup.py`) — `SeenMessageCache` is a thread-safe, bounded
  FIFO cache of message-ID strings only (no sender IDs, text, timestamps,
  or other payload fields; nothing persisted across a restart), with two
  operations: `add_if_new(message_id) -> bool` atomically reserves an ID
  (returns whether it was new), and `discard(message_id) -> None`
  releases a reservation so a later redelivery of that same ID is
  accepted rather than treated as a duplicate forever. Default capacity
  256; the oldest ID is evicted once the bound is exceeded.
- **payload** (`payload.py`) — `parse_webhook_payload(body)`
  conservatively extracts every inbound message (`IncomingMessage`) from a
  webhook's `entry[].changes[].value.messages[]` batches; every access is
  defensive (`.get()`, `isinstance` checks) and a malformed or
  unrecognized entry is skipped rather than raising, since a single bad
  entry must never take down parsing of the rest of the batch. Only
  `type == "text"` messages get a populated `text` field; every other type
  (image, audio, status update, etc.) is still returned, with
  `text=None`, so the caller can reply with a fixed "unsupported" message
  rather than silently dropping it.
- **client** (`client.py`) — `WhatsAppClient.send_text_message(to, body)`
  is a thin `urllib.request`-based POST to the Cloud API's
  `/{api_version}/{phone_number_id}/messages` endpoint (mirroring
  `kernel/models/ollama.py`'s use of `urllib` rather than adding an HTTP
  dependency), with `urlopen` and the outbound timeout (default 10
  seconds) left as injectable constructor parameters so tests never make
  a real network call. It makes exactly one attempt — no retries — parses
  the response and returns the Cloud API's outbound message ID, and
  raises `WhatsAppClientError` if the request fails or the response
  doesn't contain a usable ID. Neither its exceptions nor its logs ever
  include the access token, recipient, request body, or response body.
- **handler** (`handler.py`) — performs no authorization or
  deduplication; both already happened in `server.py` before this module
  is ever involved. `classify_message(message)` is a pure function that
  turns an already-authorized `IncomingMessage` into a `TextTask` (valid
  text, ready for the orchestrator) or a `FixedReplyTask` (one of three
  fixed replies — unsupported type, empty text, or text over the
  configured inbound limit, default 4,096 Unicode characters — the
  orchestrator is never called for these). `MessageHandler.handle_task(task)`
  is what the background worker calls: for a `TextTask` it calls
  `Orchestrator.handle(text, context=_TRUSTED_CONTEXT)` inside a
  `try/except` — `_TRUSTED_CONTEXT` (Milestone 33,
  `RequestContext(allow_computer_actions=True, actor="whatsapp")`) is
  built once at module load and passed on every `TextTask`, since a
  `TextTask` only ever exists for a message that already passed
  `server.py`'s synchronous exact-sender authorization before being
  queued — this is the one and only place this interface grants
  computer-action trust, and it neither depends on nor duplicates that
  phone-number check itself (see kernel/orchestrator/orchestrator.py and
  kernel/orchestrator/context.py above). `FixedReplyTask`s never call the
  orchestrator at all, so no context is ever built for them. That
  `try/except` correctly handles both of `Orchestrator.handle()`'s
  possible return shapes — a plain `str` used directly, or a
  `ModelResponse` whose `.text` is used — and treats `None`, any other
  return type, empty/whitespace-only text (either shape), or a raised
  exception as an equivalent processing failure: exactly one fixed reply,
  `PROCESSING_FAILURE_REPLY` ("I could not process that message. Please
  try again later."), logged only as a generic `processing_error`
  category with no exception object, message, or traceback ever logged —
  a synthetic or real exception's text could itself carry a sender ID,
  user text, or a secret. A genuinely valid response is relayed, replaced
  outright with a fixed `LONG_RESPONSE_NOTICE` (never truncating, never
  appending a marker, and never describing 4,096 as a WhatsApp platform
  limit — it is an AI-OS application limit) if it exceeds the configured
  outbound limit; for a `FixedReplyTask` the reply is sent directly. No
  log line in this module ever includes a sender ID (masked or
  otherwise), message text, or AI response text; a failed reply (fixed or
  otherwise) is logged once as a generic `outbound_failure` category and
  dropped, never retried.
- **memory** (`memory.py`) — `FixedNamespaceMemory` is the concrete
  namespace adapter the `memory_manager` injection seam (see Orchestrator
  below) was built for: it wraps a real `MemoryManager` and pins every
  `remember()`/`recall()` call to one fixed namespace (`"whatsapp"` by
  default), regardless of what namespace the caller passes in, so all
  memory reachable through this interface — the orchestrator's own and
  every capability's — stays isolated from the CLI or any other interface
  sharing the same underlying storage.
- **server** (`server.py`) — the composition root, the HTTP layer, *and*
  the one place authorization and deduplication happen. `build_orchestrator
  (config, capability_loader)` constructs the real `MemoryManager`, wraps
  it in `FixedNamespaceMemory`, and constructs `Orchestrator(config,
  capability_loader=capability_loader, memory_manager=scoped_memory)` —
  the exact same `scoped_memory` object reaches the orchestrator's own
  recall/remember calls and every capability, since nothing about the
  seam copies or wraps it further. `build_server()` wires this together
  with `WhatsAppClient` and a `MessageHandler` (which itself holds no
  dedup cache or allow-list — see handler above) into a `WhatsAppServer`.
  The server itself is `http.server.ThreadingHTTPServer`, bound to
  `whatsapp_config.host` (validated loopback-only by `config.py` — never
  bindable to a public or LAN address), since exposing it directly would
  put the raw Cloud API access token and an unauthenticated webhook path
  straight on the network; a real deployment terminates TLS and exposes
  it publicly through a separate reverse proxy or tunnel, which this
  repository does not provide. Exactly two operations exist -
  `GET /webhook` and `POST /webhook`; every other path is `404`, and
  `PUT`/`DELETE`/`PATCH`/`HEAD`/`OPTIONS` on `/webhook` are `405` (the
  same methods elsewhere are `404` or `405`) — `send_error()` is
  overridden so no path ever falls through to `http.server`'s default
  error page, which would otherwise reflect the request method/path back
  into an HTML body. The default request logger is overridden so a `GET`
  request's query string (which can carry `hub.verify_token`) is never
  logged. `GET /webhook` answers Meta's verification handshake (requires
  `hub.mode=subscribe`, a present `hub.challenge`, and compares
  `hub.verify_token` with `hmac.compare_digest` rather than `==`, guarded
  against a missing token to avoid `compare_digest` raising on `None`).
  `POST /webhook` first rejects on `Content-Length` problems (missing →
  `411`; malformed or negative → `400`, and a negative value is checked
  before any `rfile.read()` call; over the configured body limit, default
  1,000,000 bytes → `413`, without reading the body; body shorter than
  declared → `400`), then verifies the raw-body signature (`403` if
  invalid) *before* parsing the JSON body (`400` if malformed) — and only
  then, for each parsed
  message, synchronously validates the destination phone number ID,
  validates the sender against the single configured
  `WHATSAPP_AUTHORIZED_SENDER_ID`, and reserves its message ID in
  `SeenMessageCache` — all three *before* the message is classified into
  a task and submitted to the bounded (`queue.Queue`, default capacity
  16) worker queue. An unauthorized sender or wrong destination is
  dropped without ever touching the dedup cache, and the response is
  still `200` (Meta's delivery succeeded; retrying wouldn't help). A
  duplicate message ID is dropped before a task is ever created, also
  `200`. If the queue is full, the just-made reservation is released via
  `SeenMessageCache.discard` — so a later redelivery of that ID is
  accepted rather than lost — processing of the rest of that batch stops
  immediately (messages already queued earlier in the same batch keep
  their reservation, so they aren't reprocessed if the whole batch is
  redelivered), and the response is `503`, never `200`. The response
  never waits on the orchestrator or an outbound Cloud API call — those
  happen afterward, in a single background worker thread that performs no
  authorization or deduplication itself, processes one task at a time in
  FIFO order, never dies on an exception (a `handle_task()` failure that
  escapes `MessageHandler`'s own handling is caught and logged only as a
  generic `worker_error` category, with no traceback or exception detail,
  and the worker moves on to the next task), and calls `task_done()` on
  every item including the shutdown sentinel. `WhatsAppServer.stop()`
  shuts the HTTP server down, then enqueues the shutdown sentinel *after*
  whatever is already queued (so already-queued tasks are drained before
  the worker sees it) and joins the worker with a bounded timeout, for a
  graceful exit with no in-flight or already-queued message abandoned
  mid-processing.

Covered by an automated pytest suite (`tests/interfaces/whatsapp/`, one
file per module) that makes no real network call — `WhatsAppClient` tests
inject a fake `urlopen`; server tests exercise the real HTTP server only
over loopback on an OS-assigned ephemeral port, including a few raw-socket
tests for Content-Length edge cases `urllib` cannot express.

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
  configuring its own. By default, `Orchestrator` still constructs its own
  `MemoryManager` from `config.memory_settings`, exactly as before. A
  composition root may instead inject an already-constructed memory
  dependency via an optional, keyword-only `memory_manager` constructor
  parameter (`Orchestrator(config, capability_loader, memory_manager=...)`);
  when supplied, that exact object — typed only against the small structural
  `SupportsMemory` protocol (`remember()`/`recall()`) so a delegating adapter
  need not inherit from `MemoryManager` — is used for Orchestrator's own
  recall/remember operations and passed to every capability via
  `capability_loader`, with no additional `MemoryManager` constructed and no
  mutation or wrapping of the supplied object. This seam exists so an
  interface composition root (e.g. a future WhatsApp interface) can supply an
  interface-specific namespace adapter without private-attribute mutation;
  omitting the parameter leaves CLI and all existing runtime behavior
  unchanged. `interfaces/whatsapp/memory.py`'s `FixedNamespaceMemory` is
  that adapter, wired in by `interfaces/whatsapp/server.py`'s
  `build_orchestrator()` — see Interfaces above.

  **Milestone 33 — computer-action authorization gate.** `handle()` also
  accepts an optional, keyword-capable `context: RequestContext | None`
  parameter (`kernel/orchestrator/context.py`), defaulting to
  `RequestContext()` — `allow_computer_actions=False` — when omitted, so
  every existing caller (the CLI, and any test that doesn't pass one)
  stays denied. `Capability` (`kernel/capabilities/base.py`) gained one new
  concrete (non-abstract) class attribute, `requires_computer_actions:
  bool = False`; every existing capability inherits `False` unchanged.
  When the routed capability's `requires_computer_actions` is `True` and
  the request's context doesn't grant `allow_computer_actions`,
  `Orchestrator.handle()` returns a fixed, deterministic denial
  (`COMPUTER_ACTIONS_DENIED_TEXT`) — the capability's `handle()` is never
  called, and the request never falls through to the model-fallback
  branch either. The denial still goes through the same
  remember/log tail as any other response. This mechanism is
  domain-agnostic: the orchestrator does not know that "tasks" exists or
  what a computer action is, only that some capability declared it needs
  extra trust. The only caller that constructs an authorizing context is
  `interfaces/whatsapp/handler.py`, and only for a message that already
  passed WhatsApp's own exact-sender authorization — see Interfaces above
  and Capabilities below.

  **Milestone 38 — `EphemeralResult` / non-persistent capability
  results.** Normally, after a routed capability returns, `handle()`
  unconditionally writes the prompt and the response to memory
  (`remember("conversation", ...)`, twice — once per role) and to the
  interaction log (`log_interaction()`). A capability can opt one specific
  response out of *both* writes by returning `EphemeralResult`
  (`kernel/capabilities/base.py`) instead of a plain `str` — a `str`
  subclass, so every existing `capability.handle(prompt) == "..."`
  comparison and `MessageHandler._extract_response_text()`'s
  `isinstance(result, str)` branch keep working unchanged.
  `Orchestrator.handle()` checks `isinstance(capability_result,
  EphemeralResult)` before its plain-`str` check, wraps the text in the
  usual `ModelResponse` the caller receives, but skips the remember/log
  tail for that one request. This is request-scoped, not
  capability-scoped: `capabilities/knowledge_commands/` returns it for
  `/knowledge search` and `/knowledge ask` only (query/question text and,
  for `ask`, a model-generated answer must never be persisted — see
  Capabilities below); `/knowledge status`, `ingest`, `confirm`, and
  `cancel` are unaffected, and so is every other capability, the
  computer-action denial response, and ordinary model-fallback responses.
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
  and no embeddings, vector retrieval, or web access. (Milestone 36 added a
  separate, sibling lexical-search service — `kernel/knowledge_base/`,
  below — rather than extending this contract; `get()`/`list_records()`
  don't naturally express search or ranking.)
- **knowledge_base** — a local-only, deterministic knowledge-base service
  (Milestone 36), separate from `kernel/knowledge` above. Ingests approved
  local `.md`/`.txt` sources (looked up only by a symbolic key configured in
  the gitignored, machine-local `kernel/config/knowledge_base.yaml` — a
  caller never supplies a path), traverses them with symlink/junction/
  reparse-point rejection and fixed safety limits
  (`kernel/knowledge_base/traversal.py`), normalizes and deterministically
  chunks the text with SHA-256-derived stable identifiers
  (`kernel/knowledge_base/chunking.py`), and stores document/chunk metadata
  plus a SQLite FTS5 lexical index (`kernel/knowledge_base/db.py`) at
  `<knowledge.storage_dir>/knowledge_index.sqlite3` — reusing the same
  `kernel/config/config.yaml` setting `JSONKnowledgeStore` already uses,
  never a second storage-location setting. Ingestion
  (`kernel/knowledge_base/ingest.py`) is atomic per source (one SQLite
  transaction, WAL mode, diffed by content hash so unchanged files are
  untouched); a failure of any kind rolls back completely, leaving the
  prior generation searchable. `kernel/knowledge_base/search.py` is
  read-only, never invokes a model or the network, and safely transforms
  plain query text into a quoted-literal FTS5 `MATCH` expression so caller
  input can never behave as an FTS operator. `kernel/knowledge_base/
  status.py` (`get_status()`, `SourceStatus`, Milestone 37) is the one
  typed, read-only status query both callers below share — neither
  duplicates its SQL. `kernel/knowledge_base/messages.py`
  (`message_for_error()`, Milestone 37) is likewise the one place that maps
  every error type to a fixed, privacy-safe message. There are exactly two
  callers: `scripts/knowledge.py` (`status`/`ingest`/`search`, a
  human-invoked offline CLI — see Scripts and tests below) and, as of
  Milestone 37, `capabilities/knowledge_commands/` (the `/knowledge`
  command capability — see Capabilities below). Neither the orchestrator
  nor this package's own code calls a model or the network; there is
  still no automatic RAG, no orchestrator-driven retrieval, no
  embeddings, and no semantic/vector search.

  **Milestone 38 additions**, for explicit knowledge-grounded answering
  (`/knowledge ask`): `kernel/knowledge_base/query.py` factors the lexical
  query mechanics `search()` already had (query validation, FTS5 literal
  transformation, source-filter validation, limit validation, and
  deterministic ranking tie-break order) out into one shared,
  package-internal module, so `search.py` and the new
  `kernel/knowledge_base/evidence.py` never duplicate them — `search.py`'s
  public API and behavior are unchanged.
  `evidence.py:retrieve_evidence()` is `search()`'s sibling: the same
  single, read-only, parameterized, ranked query, selecting bounded full
  chunk text instead of a short `snippet()` excerpt (at most 5 chunks,
  1,500 characters each, 7,500 total), with no second, caller-controlled
  chunk-ID lookup. `kernel/knowledge_base/answer.py` is pure — no I/O, no
  model call, no network — and only prepares the one flat prompt string
  (fixed instructions from `prompts/knowledge/ask_system.md` plus the
  untrusted question/evidence as one JSON object between fixed marker
  lines) and strictly parses/validates the model's structured response;
  the model call itself is made by
  `capabilities/knowledge_commands/KnowledgeCommandsCapability`, not by
  this package. This package's own code therefore still never calls a
  model or the network.
- **models** — the abstraction layer over language models, so capabilities
  and the orchestrator do not depend on a specific model provider directly.
  The `ModelProvider` contract and a `get_provider()` factory are implemented
  (`kernel/models/base.py`, `kernel/models/factory.py`), and adapter modules
  exist for Ollama, Anthropic, OpenAI, and Gemini. Of these, only Ollama is
  selected as the active provider in `kernel/config/config.yaml` and exercised
  end-to-end today; the other adapters are present in the codebase but not
  verified as the active path.
- **tools** — reusable tools (actions, integrations, lookups) that
  capabilities could invoke. Implemented (Milestone 33; extended in
  Milestone 34; extended again in Milestone 35): the safe computer task
  execution layer, transport-agnostic and consumed today only by
  `capabilities/tasks/TasksCapability` — see Capabilities below for the
  full command surface. `kernel/tools/types.py` defines `ActionRequest`
  (an action name plus an optional symbolic `resource_key` — never a raw
  path or argument list) and `ActionResult`. `kernel/tools/registry.py`'s
  `ActionRegistry` is the fixed, non-configurable allowlist of exactly
  six actions (`system_status`, `list_files`, `open_application`,
  `run_registered_script`, `repo_health`, `repository_backup`) and which
  three of them are sensitive (`open_application`, `run_registered_script`,
  `repository_backup`) — `repo_health` is read-only and, like
  `system_status`/`list_files`, is not sensitive; `repository_backup`
  writes a file, so it is sensitive; no seventh action is ever reachable,
  no matter what a caller asks for. `kernel/tools/git_safety.py`
  (Milestone 35) holds the local git-execution hardening shared by
  `repo_health.py` and `repository_backup.py` — `GIT_SAFE_PREFIX` and
  `sanitized_git_env()`, extracted out of `repo_health.py` (which
  originally defined them) so the two handlers cannot silently drift
  apart; `repo_health.py` re-exports both under its original private
  names for backward compatibility with its own existing tests.
  `kernel/tools/config.py`'s `load_tools_config()` reads the *machine-local,
  gitignored* `kernel/config/tools.yaml` (copied from the committed
  `kernel/config/tools.example.yaml` placeholder) — the only place real
  filesystem/application paths for this machine exist — and fails closed
  two different ways: a missing file yields an entirely empty
  `ToolsConfig` (every resource-scoped action then denies everything, by
  having nothing allowlisted), while a *present but invalid* file
  (malformed YAML, a duplicate key — including one that only collides
  case-insensitively, a relative path where an absolute one is required,
  or any unrecognized field) always raises `ToolsConfigError`, which
  `capabilities/tasks/capability.py` converts into a fixed, generic "task
  system unavailable" reply rather than ever treating the error as
  permission to proceed. Every path in `tools.yaml` (a directory, an
  executable, a script's interpreter/path/cwd, a repository's path) must be
  absolute, and every key is matched case-insensitively.
  `repo_health.approved_repositories[*].main_branch` is optional (defaults
  to `"main"`) and, when given, must match a strict git-branch-name
  pattern or the whole file fails to load. `kernel/tools/executor.py`'s
  `SafeTaskExecutor.execute()` is the single choke point every action
  passes through: it rejects an unknown action outright, calls the
  matched handler with the loaded `ToolsConfig`, converts any handler
  exception into a fixed, generic failure (no exception object, message,
  or traceback ever surfaces), and unconditionally calls
  `kernel/tools/audit.py`'s `record()` regardless of outcome.
  `kernel/tools/audit.py` appends one JSONL record per attempt to
  `storage/logs/task_actions.jsonl` — a symbolic action name, a symbolic
  resource key (e.g. `"notepad"`, never a resolved path), and one of a
  fixed set of outcome codes (`proposed`, `confirmed`, `executed`,
  `rejected`, `expired`, `cancelled`, `timed_out`, `failed`) — never a
  secret, token, phone number, message body, traceback, or resolved
  private filesystem path; a write failure there is caught and logged
  only as a generic category, never raised.
  `kernel/tools/confirmation.py`'s `ConfirmationStore` is a single-slot,
  TTL-bound (2 minutes), thread-safe, in-process store for the two
  sensitive actions: `propose()` registers a pending action,
  `consume()` atomically reads and clears it in one step — before the
  caller ever acts on the result — so a confirmation can never be replayed
  even if execution afterward fails, and separately reports whether what
  it found (if anything) had expired. It's a *process-wide* singleton
  (`default_store`), deliberately not an attribute on any capability
  instance, because `CapabilityLoader` constructs a brand new capability
  object on every single request. `kernel/tools/process_control.py` is
  the only place a real OS process is spawned, always
  `subprocess.Popen(argv, cwd=cwd, shell=False, ...)` with a list-form
  `argv` sourced entirely from `tools.yaml`: `launch_detached()` (used by
  `open_application`) starts a process and returns immediately, never
  waiting for it to exit; `run_with_timeout()` (used by
  `run_registered_script`) waits up to a per-script configured timeout
  and, on expiry, kills the *entire* process tree via `psutil` (every
  descendant the process spawned, not just the immediate child) before
  reporting a `timed_out` result; `run_capturing_stdout()` (used by
  `repo_health`, Milestone 34, and by two of `repository_backup`'s three
  git calls, Milestone 35) additionally captures the child's stdout —
  never stderr — through a dedicated background reader thread that drains
  the pipe continuously and discards anything past a caller-supplied byte
  bound as it streams, so neither a chatty child nor one producing
  megabytes of output can either block on a full pipe buffer or balloon
  this process's memory; on timeout it kills the full process tree the
  same way, then deterministically joins the reader thread and closes the
  pipe before returning. `run_streaming_stdout_to_file()` (Milestone 35,
  used only by `repository_backup`'s bundle-creation call) instead
  connects the child's stdout *directly* to an already-open, caller-owned
  binary file object via `subprocess.Popen(..., stdout=output_file)` —
  the payload is never read into this process at all, unlike
  `run_capturing_stdout()`'s deliberately bounded in-memory capture; it
  never writes to or closes that file object itself (the caller retains
  full ownership — flushing, `fsync`, closing), enforces the same hard
  timeout and full-process-tree kill on expiry, and returns only
  success/timed_out/returncode, since there is no stdout to hand back by
  design.

  All three timeout branches (`run_with_timeout()`, `run_capturing_stdout()`,
  `run_streaming_stdout_to_file()`) share one private helper,
  `_terminate_and_reap()` (Milestone 35 correctness fix), which never
  returns while the process is still alive: it calls `_kill_process_tree()`
  (unchanged), then does a second, bounded `proc.wait()` through the
  `subprocess` module itself (not just `psutil`) so `proc.returncode` is
  actually set; if the process is still alive after that, it calls
  `proc.kill()` directly as an independent fallback — tolerating
  `ProcessLookupError`/`OSError` only when the process has, by that point,
  already exited, never silently swallowing a real failure — followed by
  an unbounded `proc.wait()` that blocks until the process is actually
  reaped; finally it re-checks every descendant `_kill_process_tree()`
  found via `psutil`, killing and waiting for any straggler still
  reported as running. This replaced an earlier version of all three
  functions' timeout branches that could return `timed_out=True` after
  only a single best-effort kill-and-5-second-wait, silently swallowing a
  second `TimeoutExpired` and returning regardless — a real risk for
  `run_streaming_stdout_to_file()` specifically, since a still-alive child
  could keep writing to the caller-owned partial-bundle file after the
  helper claimed the timeout was handled. The six handlers under
  `kernel/tools/handlers/` each implement
  exactly one action: `system_status.py` reads no configuration at all
  (CPU/memory via `psutil`, disk via `shutil.disk_usage`, uptime from
  `psutil.boot_time()`, and short, hardcoded-timeout HTTP reachability
  checks against a local Ollama and a local ngrok API); `list_files.py`
  accepts only a registered symbolic directory key, canonicalizes the
  configured root once, lists its immediate (non-recursive) contents
  capped at 100 entries, and independently re-resolves every entry to
  exclude anything a symlink, junction, or other reparse point would make
  appear to live outside that canonical root; `open_application.py` and
  `run_registered_script.py` accept only a registered symbolic key —
  never a sender-supplied path, executable, working directory, or (for
  scripts) argument of any kind; `repo_health.py` and
  `repository_backup.py` — see Capabilities below for their full
  behavior.
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

**tasks** (Milestone 33; `repo` verb added in Milestone 34; `backup` verb
added in Milestone 35) is the second implemented capability —
`capabilities/tasks/TasksCapability` — a small, explicitly allowlisted set
of computer actions on this machine, reached only through a strict
`/task ...` command grammar, never natural language and never a model
fallback. `handle()` is fully deterministic: no model call, no memory
recall, no knowledge-store access, ever.

- **Authorization.** `TasksCapability.requires_computer_actions = True`
  (the new `Capability` class attribute — see Orchestrator above), so
  `Orchestrator.handle()` refuses to call this capability's `handle()` at
  all unless the request's `RequestContext` explicitly grants
  `allow_computer_actions`. This capability performs no authorization of
  its own and duplicates none of WhatsApp's — it trusts the orchestrator's
  gate completely, and knows nothing about phone numbers or any other
  interface-specific check.
- **Command grammar** (`capabilities/tasks/command_parser.py`) — exactly
  nine literal forms, matched case-insensitively on `/task` and the verb:
  `/task status`, `/task files <key>`, `/task open <key>`,
  `/task run <key>`, `/task repo <key>`, `/task backup <key>`,
  `/task confirm`, `/task cancel`, `/task help`. Any extra token, missing
  token, or unrecognized verb is a `ParseError` with a stable, symbolic
  reason — never guessed at, never partially honored. `<key>` is a
  registered symbolic name resolved through `kernel/config/tools.yaml`,
  never a path — `/task backup <key>` accepts only the symbolic
  repository key, never a path, filename, git ref, option, or
  destination.
- **Confirmation.** `open_application`, `run_registered_script`, and
  `repository_backup` (Milestone 35) are sensitive (per
  `kernel/tools/registry.py`): the first matching command only calls
  `ConfirmationStore.propose()` and replies with a prompt to confirm —
  nothing executes yet. `/task confirm` within 2 minutes calls
  `consume()` and, if something unexpired was pending, executes it exactly
  once; `/task cancel` clears it explicitly; letting it sit past 2 minutes
  reports as expired on the next `/task confirm`. `system_status`,
  `list_files`, and `repo_health` are not sensitive and execute
  immediately.
- **Execution.** A non-sensitive (or just-confirmed) command becomes one
  `kernel.tools.ActionRequest`, run through a fresh
  `kernel.tools.SafeTaskExecutor`; the `ActionResult.message` is returned
  as-is (already safe to relay — see kernel/tools above).
  `system_status` never touches `kernel/config/tools.yaml` at all (a
  hardcoded, empty `ToolsConfig`), so it stays available even if that file
  is missing or invalid; every other action loads it fresh on each call,
  and a `ToolsConfigError` becomes a fixed "task system unavailable"
  reply, audited as `failed`, never treated as permission to proceed.
  Every step (proposed, confirmed, executed, rejected, expired,
  cancelled, timed out, failed) is audited via `kernel.tools.audit`.
  `repo_health` (`kernel/tools/handlers/repo_health.py`, Milestone 34)
  reports, for one registered repository: the current branch (or
  `detached`), clean/dirty working tree (from `git status --porcelain`,
  used only to decide clean-vs-dirty — filenames are never relayed), the
  latest commit's short hash and a sanitized one-line, control-character-
  stripped, whitespace-normalized, 120-character-capped subject, whether
  `HEAD` matches the configured local main branch's tip commit exactly
  (compared as SHAs, independent of which branch is checked out — a
  feature branch pointing at the same commit as main still reports
  `yes`), and whether the local main branch matches `origin`'s main branch
  on GitHub: `up to date`, `differs`, `remote branch unavailable`
  (reachable but no matching ref), or `GitHub unreachable` (timeout,
  nonzero exit, or a protocol/transport the policy below rejects). The
  configured repository path must resolve to the worktree's actual root
  (`git rev-parse --show-toplevel`) — a path that is merely a
  subdirectory of a larger worktree is rejected.

  Every git subprocess this handler runs — local or remote — carries a
  fixed argv prefix (`--no-optional-locks`, `--no-pager`,
  `--no-replace-objects`, `-c core.fsmonitor=false`) and a sanitized
  environment (`_sanitized_git_env()`): starts from a full copy of this
  process's own environment (`PATH` and everything else ordinary stays
  available), then strips (case-insensitively) `GIT_CONFIG_PARAMETERS`,
  `GIT_CONFIG_COUNT`, every `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*` pair,
  `GIT_EXEC_PATH`, `GIT_ASKPASS`, `GIT_SSH`, `GIT_SSH_COMMAND`,
  `SSH_ASKPASS`, every repository/ref/object/index-redirection variable
  (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`,
  `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`,
  `GIT_NAMESPACE`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`,
  `GIT_CEILING_DIRECTORIES`, `GIT_REPLACE_REF_BASE`), and every transport-
  security/tracing/stdio-redirection variable (`GIT_SSL_NO_VERIFY`,
  `GIT_CURL_VERBOSE`, every `GIT_TRACE*` variable, `GIT_REDIRECT_STDIN`,
  `GIT_REDIRECT_STDOUT`, `GIT_REDIRECT_STDERR`), before setting
  `GIT_OPTIONAL_LOCKS=0`, `GIT_CONFIG_NOSYSTEM=1`, and
  `GIT_CONFIG_GLOBAL=os.devnull` for **every** call — no system- or
  machine-global git config is ever consulted, though a local call still
  reads the approved repository's own *local* config where needed (e.g.
  reading `remote.origin.url`), since `GIT_CEILING_DIRECTORIES` is
  stripped, not set, at this layer. All of this is supplied on the
  command line / in the process environment rather than left to any
  config file, so nothing in the target repository's own `.git/config` —
  or an inherited environment variable — can remove or override it: the
  argv flags above are global git options, not config keys, and a
  command-line `-c` always wins over repo config in git's own resolution
  order. `--no-replace-objects` means a repository-configured replace ref
  can never substitute a different object for the one actually reported.

  The one network call (`git ls-remote`) never targets the symbolic
  remote name `origin`, and the origin is accepted only when it validates
  as a credential-free `github.com` HTTPS repository URL: the
  repository's local `remote.origin.url` is read with a local, read-only
  `git config --local --no-includes --get-all -z` lookup — NUL-delimited
  so every configured value is positively enumerated, sidestepping
  `--get`'s inconsistent behavior on a multi-valued key — required to be
  exactly one non-empty, properly NUL-terminated value, and strictly
  parsed (`_parse_github_origin()`) — `https` scheme only, hostname
  `github.com` only (case-insensitively), no embedded username/password,
  no explicit port, no query string or fragment, no control characters or
  malformed percent-encoding, and a path shaped exactly like
  `/<owner>/<repo>` or `/<owner>/<repo>.git` with both components
  conservatively character-allowlisted. Anything else — absent,
  multi-valued, non-github, or malformed — reports `GitHub unreachable`
  without ever attempting a network connection, and no raw or normalized
  value is ever put in the reply or the audit log. Only the resulting
  normalized `https://github.com/<owner>/<repo>.git` is ever passed to
  `ls-remote`, as one fixed argv element.

  That call additionally runs from `_neutral_cwd()` — the real system
  temp directory, never the approved repository and never a directory
  created for this purpose, and only after verifying (via a real,
  unmocked `git rev-parse --is-inside-work-tree` probe) that it is not
  itself inside any git worktree — with its own controlled
  `GIT_CEILING_DIRECTORIES` pinned to that same directory (on top of the
  system/global isolation every call already gets), so the call is
  isolated from repository, global, *and* system git configuration:
  nothing configured anywhere on this machine — a rewritten URL via
  `url.*.insteadOf`, an injected `http.extraHeader`, a `credential.helper`,
  a `http.proxy` — can reach or influence it, because git never discovers
  the approved repository's `.git/config` for this subprocess in the
  first place. It also pins `-c protocol.allow=never -c
  protocol.https.allow=always` (rejecting file, ssh, git://, ext, and any
  custom remote helper outright — no test-only exception exists in
  production code), `-c credential.helper= -c core.askPass= -c
  http.extraHeader= -c http.proxy=` (empty values, which git treats as
  "use none of this"), `-c http.sslVerify=true` (so nothing inherited can
  disable TLS certificate verification for this call), strips any
  inherited `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` (any case) from its
  environment, and sets `GIT_TERMINAL_PROMPT=0`/`GCM_INTERACTIVE=Never` —
  run with a
  fixed timeout and full process-tree termination like every other git
  call here.

  No repository path, remote URL, credential, prompt, or raw git output
  ever reaches the reply or the audit log; a malformed or unvalidated
  branch name/commit hash from `git` causes the whole report to fail
  closed (`"That repository is not available."`) rather than being
  partially relayed — branch names are validated with
  `kernel/tools/config.py`'s `is_valid_git_branch_name()` (shared, not
  duplicated, with that module's own load-time validation of an
  admin-supplied `main_branch`; modeled on `git check-ref-format`'s real
  rules — rejects a leading `-`/`.`/`/`, a trailing `/`/`.`, any `..`,
  `//`, `@{`, a bare `@`, or a `.`-leading/`.lock`-suffixed path
  component, on top of a restrictive ASCII character allowlist).
  Covered by `tests/capabilities/tasks/` (command parser, confirmation
  flow, config-failure handling) and `tests/kernel/tools/` (every
  kernel/tools/ module and handler). `repo_health`'s own tests run every
  local git check — including the origin-URL read (via a real repository
  configured with `remote.origin.url` set twice, proving the multi-value
  case is positively detected and rejected end to end, not just via
  mocked output) and the neutral-directory work-tree probe — against
  real, temporary local repositories; the `ls-remote` network call itself
  is always injected by monkeypatching (matching/differing/missing SHA,
  malformed output, timeout, nonzero exit), since production only ever
  permits https and the suite must never depend on — or attempt — a real
  connection. Dedicated tests also configure a real repository's local
  `url.*.insteadOf`, `http.extraHeader`, `http.proxy`, and
  `credential.helper`, and set inherited `GIT_TRACE`/`GIT_TRACE_CURL`
  variables pointing at marker files, asserting none of it ever reaches
  the recorded `ls-remote` argv/cwd/env or creates the marker file, that
  proxy environment variables are stripped for that call, and that a
  non-github or unreachable origin still leaves the local portion of the
  report intact. No test ever contacts GitHub or any real network
  service. Plus dedicated `RequestContext`/authorization-gate tests in
  `tests/kernel/orchestrator/test_orchestrator.py`.

  `repository_backup` (`kernel/tools/handlers/repository_backup.py`,
  Milestone 35) creates a verified, local-only Git bundle of one
  registered repository's committed history in a preapproved local
  destination directory — `/task backup <key>` accepts only the symbolic
  repository key, sensitive like `open_application`/`run_registered_script`
  above, so it is proposed and requires `/task confirm` before anything is
  written. The repository path is looked up through the same
  `repo_health.approved_repositories` entry `repo_health` uses — never
  duplicated — while the destination directory comes from a new
  `repository_backup.approved_backups[<key>].destination_directory`
  section in `kernel/config/tools.yaml`, whose key must already exist as a
  `repo_health.approved_repositories` key (cross-validated at config-load
  time — `kernel/tools/config.py`'s `_parse_repository_backup()`) and must
  additionally be filename-safe (`is_valid_backup_key()`: lowercase ASCII
  letters/digits/`_`/`-` only, starting with a letter or digit, at most 64
  characters — the key is used directly inside the generated backup
  filename). At request time the handler independently canonicalizes both
  the repository and destination paths (`Path.resolve(strict=True)`),
  requires both to be existing directories, re-verifies the repository
  path is the worktree's actual root (the same `rev-parse
  --is-inside-work-tree` / `--show-toplevel` check `repo_health` performs),
  and rejects the destination being inside the repository, the repository
  being inside the destination, or the two being equal — comparing only
  canonical (symlink/junction-resolved) paths throughout, so a reparse
  point can never make either check pass falsely. Every git call this
  handler makes is local-only — no fetch, pull, push, checkout, reset,
  merge, commit, clone, remote, or network access of any kind — and reuses
  `kernel/tools/git_safety.py`'s `GIT_SAFE_PREFIX` and
  `sanitized_git_env()` (see kernel/tools above), extracted from
  `repo_health.py` in this milestone specifically so the two handlers
  share one hardening implementation rather than two that could drift
  apart.

  Ref-inclusion policy: `git bundle create - HEAD --branches --tags`.
  Included: the current `HEAD` (so a detached-`HEAD` checkout is still
  captured), every local branch (`refs/heads/*`), every tag
  (`refs/tags/*`), and every committed object those refs require.
  Excluded: `refs/remotes/*`, `refs/stash`, `refs/notes/*`,
  `refs/replace/*` (also neutralized globally by `--no-replace-objects`),
  any other custom ref, and — because a bundle can only ever contain
  committed objects reachable from the refs it records — every current
  uncommitted, untracked, and ignored file. A file that is untracked or
  gitignored *right now* is absent for that reason; a file that was ever
  actually committed to selected history is not additionally filtered by
  filename — the handler inspects refs, never filenames, and the
  user-facing reply is worded to reflect this precisely rather than
  overclaiming.

  The bundle is created by streaming, never buffering: `git bundle create
  -` writes the complete bundle to *stdout*, which
  `kernel/tools/process_control.py`'s new `run_streaming_stdout_to_file()`
  connects directly to an already-open file descriptor this handler
  creates with `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
  os.O_BINARY, 0o600)` inside the canonical destination directory —
  exclusive creation closes the time-of-check/time-of-use window a
  generate-then-reopen sequence would leave open, since the path cannot
  have been written to, replaced, or symlinked before this process's own
  call atomically created it, and the bundle payload is never held in
  this process's memory regardless of size. The `0o600` mode is enforced
  directly by the OS on POSIX (owner read/write only); on Windows,
  `os.open()`'s mode argument has no POSIX-permission-bit equivalent and
  only affects the read-only attribute bit — actual access control comes
  entirely from NTFS ACLs inherited from the destination directory, which
  this handler never reads, sets, or otherwise modifies. Restricting who
  can read the configured backup destination on Windows is a
  machine-configuration concern outside this milestone. The generated
  temporary name is `.{key}-{UTC timestamp}-{16-hex-character
  secrets.token_hex(8) suffix}.partial`; the eventual final name shares
  the exact same timestamp and suffix,
  `{key}-{same timestamp}-{same suffix}.bundle` — a name collision on
  creation retries with an entirely fresh timestamp and
  suffix, bounded, never reusing a collided one. Immediately after bundle
  creation completes, the handler records the partial file's `(st_dev,
  st_ino, st_size)` identity via `os.stat(path, follow_symlinks=False)`,
  requiring a regular file (never a symlink, junction/reparse point,
  directory, or other special file) with a hard-link count of 1 directly
  inside the canonical destination — then re-confirms that exact identity
  is unchanged after `git bundle verify`, after streaming SHA-256/size
  hashing (bounded chunked reads, the complete file never loaded into
  memory at once), and immediately before finalization; any mismatch at
  any checkpoint fails the whole operation closed. Finalization
  (`kernel/tools/atomic_finalize.py`'s `atomic_finalize_no_replace()`)
  never uses `os.replace()` and never relies on an informal "rename
  doesn't overwrite" assumption: on Windows it uses `os.rename()`, whose
  documented behavior is to raise `FileExistsError` if the destination
  already exists; elsewhere it uses an atomic hard-link-to-final followed
  by unlinking the source, the standard POSIX no-clobber-rename idiom. An
  existing `.bundle` is never deleted, truncated, or overwritten under any
  failure mode — including a final-name collision itself: rather than
  regenerating a fresh suffix and retrying bundle creation, a collision at
  finalization fails the whole attempt closed with the same generic
  creation-failure reply as any other creation failure (never a dedicated
  "already exists" message that might hint at the destination's
  contents), removes only the current execution's own partial file, and
  never touches the pre-existing final file it collided with — confirmed
  end to end by `test_existing_completed_bundle_is_never_overwritten`
  (`tests/kernel/tools/handlers/test_repository_backup.py`), which proves
  the original bundle's bytes are unchanged after a forced collision.
  Complex collision recovery (regenerating a new suffix and re-running
  bundle creation) is deliberately out of scope. Every failure path
  (registration, availability, creation, verification, hashing,
  finalization, or timeout) removes only the one
  temporary file this specific execution created — never a glob, never
  another file in the destination, and never an existing completed
  backup — and returns one of a small set of fixed, generic replies
  containing no path, filename beyond the generated one, git output,
  stderr, or traceback. On success, the reply names only the generated
  filename, size, and SHA-256 digest — never the absolute destination
  path:

  ```
  Repository backup created.
  Repository: ai_os
  File: ai_os-20260802T233800Z-a1b2c3d4e5f60718.bundle
  Size: 12.4 MB
  SHA-256: <64 lowercase hexadecimal characters>
  Included: committed HEAD, local branches, tags, and required Git history
  Not included: current uncommitted, untracked, or ignored files
  ```

  Restore, deletion, retention cleanup, cloud upload, encryption,
  scheduling, and automatic backup rotation are all out of scope for this
  milestone. Covered by `tests/kernel/tools/test_git_safety.py`,
  `tests/kernel/tools/test_atomic_finalize.py`, the
  `run_streaming_stdout_to_file()` additions in
  `tests/kernel/tools/test_process_control.py`, and
  `tests/kernel/tools/handlers/test_repository_backup.py` — the last runs
  real, temporary local git repositories and destination directories
  throughout (a committed `.env` proves the bundle is history-based, not
  filename-filtered; an untracked `.env`, an ignored would-be
  `tools.yaml`, and a real `git bundle list-heads` check that
  `refs/remotes/*`/`refs/stash`/`refs/notes/*`/custom refs are never
  advertised), never touches the real `kernel/config/tools.yaml` or a real
  backup destination, and includes an explicit proof that `git bundle
  create -` produces a valid bundle on the installed git version before
  the handler relies on that form. `tests/capabilities/tasks/` covers
  `/task backup <key>` proposing rather than executing immediately,
  `/task confirm` executing it exactly once, `/task cancel` and
  confirmation expiry both preventing execution.

**knowledge** (Milestone 37, extended in Milestone 38) is the third
implemented capability — `capabilities/knowledge_commands/
KnowledgeCommandsCapability` (registered under the id `"knowledge"`) — a
`/knowledge` command layer over the Milestone 36 local knowledge base
(`kernel/knowledge_base/` above), reached only through a strict command
grammar, never natural language and never automatic. Deliberately a
separate package from `capabilities/knowledge/`, which remains reserved
and unimplemented for a different, future, higher-level AI-employee
capability. **`handle()` is not fully model-free**: `status`, `search`,
`ingest`, `confirm`, `cancel`, and `help` are fully deterministic — no
model call, no memory recall, no `kernel/knowledge` `KnowledgeStore`
access, ever — but Milestone 38's `ask` verb explicitly calls the
already-injected model provider exactly once per request, to answer a
question grounded only in retrieved local evidence. `ask` still never
recalls memory or reads `KnowledgeStore`.

- **Authorization.** `KnowledgeCommandsCapability.requires_computer_actions
  = True`, identically to `TasksCapability` — `Orchestrator.handle()`
  refuses to call `handle()` at all unless the request's `RequestContext`
  grants `allow_computer_actions`. This applies uniformly to every verb,
  including the read-only `status`/`search` and the model-calling `ask` —
  there is no per-verb trust tier. A denied request never parses,
  retrieves evidence, opens the knowledge database, or calls the model.
  This capability performs no authorization of its own.
- **Command grammar**
  (`capabilities/knowledge_commands/command_parser.py`) — a hand-rolled
  tokenizer (not `argparse`, which remains reserved for the offline
  `scripts/knowledge.py` CLI grammar): `/knowledge`, `/knowledge help`,
  `/knowledge status [--source <key>]`,
  `/knowledge search [--source <key>] [--limit <1-10>] -- <query text>`,
  `/knowledge ask [--source <key>] [--limit <1-5>] -- <question>`,
  `/knowledge ingest <key>`, `/knowledge confirm`, `/knowledge cancel`.
  `search` and `ask` each require exactly one literal bare `--`
  delimiter; everything after the first one is query/question text, never
  re-parsed as options even if it contains `--`-shaped tokens. `<key>`
  must match the same conservative shape `kernel/knowledge_base/config.py`
  uses (`^[a-z0-9][a-z0-9_-]{0,63}$`) and is casefolded; a shape-valid but
  unapproved key is still rejected downstream by the existing Milestone 36
  allowlist, never by the parser itself. `search`'s `--limit` is bounded
  to 1–10 (`MAX_INTERFACE_RESULT_LIMIT`) — tighter than `search()`'s own
  service-level cap of 50. `ask`'s `--limit` is a separate 1–5
  evidence-chunk count (`MAX_ASK_EVIDENCE_LIMIT`, default 3) and its
  question text is bounded to 1–200 characters
  (`MAX_QUESTION_CHARACTERS`, a literal deliberately duplicated from
  `kernel/knowledge_base/query.py`'s `MAX_QUERY_CHARACTERS` rather than
  imported, matching this module's existing no-dependency policy). Any
  extra token, missing token, unknown or duplicate option, missing option
  value, missing delimiter, blank query/question, an oversized question,
  or a malformed key is a `KnowledgeParseError` with a stable, symbolic
  reason, surfaced as one fixed reply
  (`Invalid knowledge command. Use /knowledge help.`) — never guessed at.
- **Read-only execution.** `status` calls
  `kernel/knowledge_base/status.py:get_status()`; `search` calls the
  existing, unmodified `kernel/knowledge_base/search.py:search()`,
  requesting at most 10 results. Neither requires confirmation. The raw
  query text is passed only transiently through the parser and this call
  stack — never logged or audited — and, as of Milestone 38, `search`'s
  result is returned as `EphemeralResult` (see Orchestrator below) so the
  orchestrator does not persist it to memory or the interaction log
  either.
- **Grounded answering (`ask`, Milestone 38).** Retrieves bounded evidence
  via `kernel/knowledge_base/evidence.py:retrieve_evidence()` — the same
  lexical FTS5 mechanics `search()` uses, factored into
  `kernel/knowledge_base/query.py` so both share one implementation of
  query validation, source filtering, FTS literal transformation, and
  ranking order — returning full (still bounded) chunk text instead of a
  short excerpt: at most 5 chunks, 1,500 characters each, 7,500 total,
  dropped whole (never partially) once the running total would exceed
  budget. If no evidence is found, the model is never called. Otherwise
  `kernel/knowledge_base/answer.py` (pure — no I/O, no model call)
  assembles one flat prompt: fixed instructions from
  `prompts/knowledge/ask_system.md`, then the untrusted question and
  evidence as one `json.dumps(..., ensure_ascii=False)` object between
  fixed marker lines — `json.dumps()` escapes every newline inside string
  values, so the whole blob is always one line and retrieved evidence can
  never produce a standalone line matching the closing marker, without
  any zero-width-character or delimiter-mutation trick. The capability
  calls its injected `ModelProvider.send_prompt()` exactly once and
  strictly parses the required `{"answer", "used_citations",
  "sufficient"}` JSON response: wrong shape, an invented or unsupplied
  citation label, an inline/`used_citations` mismatch, or an empty answer
  all fail closed to a fixed "unverifiable answer" reply, distinct from
  the fixed "insufficient evidence" reply used when the model itself
  reports `sufficient: false`. Citation labels (`S1`..`S5`) are assigned
  by code in retrieval order, never by the model, and the appended
  "Sources:" section is generated entirely from code-owned metadata
  (symbolic source key, relative path, chunk ordinal — never a chunk ID,
  a rank, or an absolute path). A provider exception (including a
  request timeout — `kernel/models/ollama.py`'s fixed
  `OLLAMA_REQUEST_TIMEOUT_SECONDS = 120`) is caught and mapped to one
  fixed "service temporarily unavailable" reply; the raw exception never
  reaches the reply, the audit record, memory, or the interaction log.
  **Consent (v1):** the explicit `/knowledge ask` command is itself
  sufficient consent — no second confirmation, nothing in
  `PendingAction` — acceptable because the only implemented provider is
  local Ollama; this must be revisited before a remote provider is ever
  enabled for this operation, since `ModelProvider` has no
  locality-detection contract and this milestone deliberately does not
  add one. `ask`'s result, like `search`'s, is returned as
  `EphemeralResult`.
- **Confirmation.** `ingest` is sensitive: the symbolic key is checked
  against the current approved-source allowlist immediately (no source
  pre-scan, no document-count disclosure) and, if approved, only
  *proposed* — the reply names the source key and nothing else. `/knowledge
  confirm` within 2 minutes calls the existing, unmodified
  `kernel/knowledge_base/ingest.py:ingest_source()` exactly once;
  `/knowledge cancel`, an expired confirmation, or a re-check at execute
  time against a since-changed configuration all fail closed. The pending
  action lives in `default_knowledge_confirmation_store` — a *separate*
  `kernel.tools.confirmation.ConfirmationStore` instance from
  `capabilities/tasks/TasksCapability`'s `default_store`, so the two
  command families can never collide over the single-slot design
  `kernel/tools/confirmation.py` describes. `ask` does not use this store
  at all (see consent, above).
- **Output limits and privacy.** Fixed, interface-level limits in addition
  to Milestone 36's own service-level ones: at most 10 results requested,
  a 200-character excerpt, an 80-character path, and a 3,500-character
  total reply, all truncated deterministically with a visible ellipsis;
  when complete results don't all fit, whole results are dropped from the
  end and a fixed notice is appended — never a partial result, never split
  across multiple messages. `ask`'s generated answer is bounded to 2,500
  characters and at most 5 source entries are displayed, within the same
  3,500-character reply budget; if a citation's `[S#]` token is still
  present in the (possibly truncated) answer, its source entry is never
  dropped to make room — the answer is reduced further instead, or the
  fixed unverifiable-answer reply is returned. Every recognized failure
  maps to one fixed message by exception type via the shared
  `kernel/knowledge_base/messages.py:message_for_error()`, or, for `ask`'s
  model-specific outcomes, one of a small set of additional fixed strings
  — never `str(exc)`. Audit events reuse `kernel/tools/audit.py` (the
  same module and log file `/task` uses) with only a fixed action name,
  the symbolic source key when applicable, and a fixed outcome
  (`executed`/`failed`/`rejected`) — never query/question text, evidence,
  an excerpt, an answer, a path, SQL, an FTS expression, a prompt, a
  provider response, or an exception.
- **Out of scope**: automatic RAG, automatic or intent-detected retrieval
  before a model call, prompt-context injection into ordinary prompts,
  embeddings, semantic/vector search, hybrid retrieval, web search,
  external connectors, model-generated summaries or ingestion metadata,
  path-based or partial-source ingestion, scheduled or background
  ingestion, deletion commands, document-content display, multi-turn
  grounded conversation, answer caching, self-critique or citation-repair
  model calls, streaming, remote-provider detection, per-provider consent
  logic. `search()`'s and `retrieve_evidence()`'s typed core APIs are the
  same ones a future, explicit semantic/embedding-based retrieval
  integration could sit behind — nothing here forecloses that; `ask` is
  explicit retrieval-augmented generation, never automatic. The offline
  `scripts/knowledge.py` CLI remains available and unaffected by this
  capability's trust gating.

Each capability is meant to be a self-contained domain expert that uses
kernel services (memory, knowledge, tools, models) to do its job. Capabilities
do not talk to interfaces directly, and they do not talk to each other
directly — all cross-capability coordination goes through the orchestrator.
`wine` and `tasks` exist so far; strategy, research, travel, and life
administration remain unimplemented.

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

A fourth script, `scripts/knowledge.py` (Milestone 36), is the sole
operational interface to `kernel/knowledge_base/` (see Kernel above): a
strict `argparse` CLI — `status [--source KEY]`, `ingest KEY`, and
`search QUERY [--source KEY ...] [--limit N]` — invoked as
`uv run python -m scripts.knowledge <command> ...`. No command accepts a
filesystem path, a SQL fragment, or an FTS expression; `ingest`/`--source`
only ever take a symbolic key already approved in
`kernel/config/knowledge_base.yaml`. Every recognized failure prints one
fixed, privacy-safe message and exits `1`; argument-grammar errors exit
`2` (argparse's own default); success exits `0`, including a search with
zero results. Like the wine scripts above, it is human-invoked and lives
entirely outside the runtime kernel — never reached by the orchestrator,
a capability, or a model.

`tests/` holds test suites that verify kernel and capability behavior;
today this covers `WineCapability` (`tests/capabilities/wine/test_capability.py`
and `tests/capabilities/wine/test_cellar_lookup.py`), the importer
(`tests/scripts/test_import_wine_cellar.py`), the quantity-update script
(`tests/scripts/test_update_wine_cellar_quantity.py`), the acceptance
check (`tests/scripts/test_wine_acceptance_check.py`), and — Milestone 36
— `kernel/knowledge_base/` (`tests/kernel/knowledge_base/`, covering
config, traversal, chunking, the database/schema/FTS5 layer, atomic
ingestion, and search) plus its CLI
(`tests/scripts/test_knowledge_cli.py`). Every one of these suites uses
only synthetic, dynamically constructed fixtures under `tmp_path`; no real
storage data or real local configuration is read or written by the test
suite.

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
- WhatsApp interface (`interfaces/whatsapp/`): a second, independent
  composition root — alongside `kernel/main.py` — that reaches the same
  `Orchestrator` over a loopback-only, standard-library HTTP server bound
  to a validated-loopback-only host. **Single-user only**: exactly one
  authorized sender (`WHATSAPP_AUTHORIZED_SENDER_ID`), matched by exact
  equality, no allow-list. Own environment configuration and validation
  (`config.py`, including a required Cloud API version with no code
  default), raw-body HMAC-SHA256 webhook signature verification
  (`signature.py`), a bounded thread-safe FIFO message-ID dedup cache with
  atomic reserve/release (`dedup.py`, `SeenMessageCache`), conservative
  multi-message payload parsing (`payload.py`), a `urllib`-based Cloud API
  client with no retries that returns a usable outbound message ID
  (`client.py`), pure message classification plus task processing with no
  authorization or dedup of its own (`handler.py`), and a
  `FixedNamespaceMemory` adapter (`memory.py`) — the concrete
  implementation of the orchestrator's `memory_manager` injection seam
  (see Orchestrator above). `server.py` performs destination/sender
  authorization and dedup reservation synchronously, before a message is
  ever queued: exactly `GET /webhook` and `POST /webhook` exist (every
  other path is `404`); an unauthorized sender, wrong destination, or
  duplicate message ID gets `200` and never reaches the queue, dedup
  cache (for the first two), or orchestrator; a full queue releases its
  dedup reservation and returns `503`. A single background worker thread
  processes pre-authorized tasks only, in FIFO order, with graceful
  shutdown. Long orchestrator responses are replaced outright with a
  fixed notice, never truncated. Logging throughout excludes sender IDs
  (in any form), message text, AI response text, tokens, and secrets —
  including exception content: an orchestrator/provider failure or a
  worker-level exception is logged only as a generic category
  (`processing_error`, `outbound_failure`, `worker_error`), never the
  exception object, its message, or a traceback. `WhatsAppConfig` is a
  frozen dataclass with secrets/personal identifiers excluded from
  `repr()`. Both the GET token check and the POST signature check use
  `hmac.compare_digest`; only `GET`/`POST` on `/webhook` are handled, an
  IPv6 loopback host (`::1`) binds correctly via a dedicated `AF_INET6`
  server variant, and no request path ever reaches `http.server`'s
  default error page. No real network call in the test suite
  (`tests/interfaces/whatsapp/`), including raw-socket tests for HTTP
  edge cases `urllib` cannot express.
- Local knowledge base (Milestone 36): `kernel/knowledge_base/` — a
  local-only SQLite FTS5 lexical-search/ingestion service, separate from
  `kernel/knowledge` and not wired into the orchestrator, any capability,
  WhatsApp, memory, or a model. Approved sources are configured only in
  the gitignored `kernel/config/knowledge_base.yaml` (symbolic key ->
  absolute path + `recursive`, validated fail-closed by
  `kernel/knowledge_base/config.py` with its own small, private
  duplicate-key-safe YAML loader); the database always lives at
  `<knowledge.storage_dir>/knowledge_index.sqlite3`, deriving its location
  from the existing `kernel/config/config.yaml` setting rather than adding
  a second one. Ingestion supports only `.md`/`.txt`, UTF-8 or UTF-8 with
  BOM; traversal (`kernel/knowledge_base/traversal.py`) inspects a
  configured root's original, unresolved identity via `lstat` before ever
  resolving it, rejects any symlink/junction/reparse point/special file
  encountered anywhere (root or candidate), enforces fixed limits (file
  count, file size, total bytes, recursion depth, document/chunk counts —
  never configurable), and reads each candidate race-resistantly
  (fresh identity check immediately before opening, `O_NOFOLLOW` where
  available, `fstat`-verified before and after reading). Any invalid file
  fails the whole source's ingestion and rolls back — the prior generation
  stays fully searchable; an empty source is a valid ingestion that
  atomically removes previously indexed documents for that source.
  Chunking (`kernel/knowledge_base/chunking.py`) is deterministic
  (1,200-character chunks, 200-character overlap, paragraph-aware, no
  model call), with every identifier a SHA-256 digest of trusted inputs
  (content hash, a source+path-derived document key, chunk ordinal) —
  never Python's process-randomized `hash()`. The SQLite schema
  (`kernel/knowledge_base/db.py`) is version 1 (`schema_meta`, `sources`,
  `documents` with a `source_key` foreign key and a case-normalized
  `relative_path_key` for Windows-safe deduplication, `chunks`, and an
  external-content `chunks_fts` FTS5 table kept in sync by insert/delete
  triggers); FTS5 availability is verified at runtime via a temporary,
  non-persistent schema object, never assumed. Ingestion
  (`kernel/knowledge_base/ingest.py`) runs one `BEGIN IMMEDIATE`
  transaction per source (WAL journal mode, `synchronous=NORMAL`, a busy
  timeout mapped to a fixed "database locked" result), diffing against the
  prior generation by content hash so unchanged documents are left
  completely untouched. Search (`kernel/knowledge_base/search.py`) is
  read-only (`PRAGMA query_only=ON`), never invokes a model or the
  network, and transforms plain query text into a safe FTS5 `MATCH`
  expression by extracting alphanumeric terms and quoting each as an
  individual string literal joined with `AND` — caller-supplied quotes,
  wildcards, `NEAR`/`OR`/`NOT`, or column filters can therefore never
  behave as FTS5 operators, only as literal text; ranking uses SQLite's
  built-in BM25 with deterministic tie-breaking, and excerpts come from a
  bounded `snippet()`. `scripts/knowledge.py` (`status`/`ingest`/`search`)
  is a strict, human-invoked CLI outside the runtime kernel with stable
  exit codes (`0` success, `1` a recognized failure, `2` a grammar error)
  and a small set of fixed, privacy-safe messages — no path, SQL, database
  location, or traceback is ever exposed. As of Milestone 37, this package
  also has a second caller —
  `capabilities/knowledge_commands/KnowledgeCommandsCapability`, a
  deterministic, trusted-context-gated `/knowledge` command capability
  (see Capabilities and Milestone 37 below) — both callers share the
  package's typed `get_status()` and `message_for_error()` rather than
  each reimplementing status SQL or an error-message mapping. See
  `kernel/knowledge_base/README.md` for the full design. Explicitly out of
  scope: embeddings, semantic/vector search, automatic orchestrator/RAG
  integration, and every other format beyond `.md`/`.txt`.

- Milestone 33 — Safe Computer Task Execution: `kernel/orchestrator/context.py`'s
  `RequestContext` (default-deny `allow_computer_actions`) plus
  `Capability.requires_computer_actions` gate which capabilities
  `Orchestrator.handle()` will call at all; `kernel/tools/` (a fixed
  four-action `ActionRegistry`, `SafeTaskExecutor`, machine-local
  `tools.yaml` config with two-mode fail-closed loading, single-slot TTL
  `ConfirmationStore`, `psutil`-backed process-tree-killing timeout
  enforcement, and a symbolic-only `task_actions.jsonl` audit trail); and
  `capabilities/tasks/TasksCapability`, reachable only through the strict
  `/task ...` command grammar and only when
  `interfaces/whatsapp/handler.py`'s trusted context is present. See
  Kernel and Capabilities above for the full detail.
- Milestone 34 — Safe Repository Health Checks: a fifth `kernel/tools/`
  action, `repo_health` (read-only, not sensitive — no confirmation step),
  reachable via the new `/task repo <key>` verb under the same
  `RequestContext`/`ActionRegistry`/`SafeTaskExecutor`/audit machinery
  Milestone 33 established, with no changes to `interfaces/whatsapp/` or
  the orchestrator gate. Adds `kernel/tools/config.py`'s
  `repo_health.approved_repositories` section (an absolute repository
  path plus an optional `main_branch`, defaulting to `"main"` and
  validated by the shared `is_valid_git_branch_name()`) and
  `kernel/tools/process_control.py`'s `run_capturing_stdout()` — stdout
  capture bounded via a background reader thread that discards anything
  past the byte limit as it streams (never buffers unboundedly first),
  with the same timeout/process-tree-kill guarantees as
  `run_with_timeout()`, stderr always discarded. Every git subprocess the
  handler runs carries a fixed safety prefix (including
  `--no-replace-objects`) and a sanitized environment that strips
  git-injection/credential-redirection variables, every repository/ref/
  object/index-redirection variable (`GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_CEILING_DIRECTORIES`, `GIT_REPLACE_REF_BASE`, etc.), and every
  transport-security/tracing/stdio-redirection variable
  (`GIT_SSL_NO_VERIFY`, every `GIT_TRACE*`, `GIT_REDIRECT_STDOUT`/
  `STDERR`, etc.) regardless of what this process inherited, and sets
  `GIT_CONFIG_NOSYSTEM=1`/`GIT_CONFIG_GLOBAL=os.devnull` for every call.
  The one network call (`git ls-remote`) never targets the symbolic
  remote name `origin`: the repository's local `remote.origin.url` is
  read via `git config --local --no-includes --get-all -z` (positively
  rejecting a multi-valued key, not just relying on `--get`'s
  inconsistent behavior), strictly validated as a bare, credential-free
  `github.com` HTTPS repository URL, and normalized to
  `https://github.com/<owner>/<repo>.git` — only that fixed, normalized
  URL (never "origin", never anything read as-is) is ever passed to
  `ls-remote`, from a neutral, verified-non-worktree directory (the real
  system temp directory, never the approved repository) with its own
  controlled `GIT_CEILING_DIRECTORIES` pinned there, so the call is fully
  isolated from repository, global, and system git configuration — no
  `url.*.insteadOf` rewrite, injected `http.extraHeader`, `http.proxy`,
  or `credential.helper` configured anywhere on this machine can reach or
  influence it — with `protocol.allow=never` and only `https` re-allowed,
  `http.sslVerify=true` forced, credential helpers/askpass disabled on
  the command line, and any inherited proxy environment variable
  stripped. See Kernel and Capabilities above for the full detail.
- Milestone 37 — Safe Explicit Knowledge Commands: a deterministic
  `/knowledge` command layer over the Milestone 36 knowledge base, reached
  only through the strict `/knowledge ...` command grammar and only when
  `interfaces/whatsapp/handler.py`'s trusted context is present, matching
  Milestone 33's authorization pattern exactly (`requires_computer_actions
  = True`, uniformly across every verb, no per-verb trust tier). Adds
  `kernel/knowledge_base/status.py` (`get_status()`, `SourceStatus`) and
  `kernel/knowledge_base/messages.py` (`message_for_error()`) to the core
  package — the former eliminates the SQL `scripts/knowledge.py`'s
  `status` command used to run itself; the latter centralizes the fixed
  error-message mapping so `scripts/knowledge.py` and the new
  `capabilities/knowledge_commands/KnowledgeCommandsCapability` share one
  copy instead of two. Ingestion remains sensitive (routed through a
  *separate* `kernel.tools.confirmation.ConfirmationStore` instance from
  `capabilities/tasks/TasksCapability`'s own, so the two command families'
  single-slot pending state can never collide); status and search are
  read-only and require no confirmation. Adds fixed, interface-level
  output limits (at most 10 results, 200-character excerpts,
  80-character paths, a 3,500-character total reply, deterministic
  whole-result truncation with a fixed omission notice) on top of
  Milestone 36's own service-level limits. No changes to
  `interfaces/whatsapp/server.py`, the orchestrator's authorization gate,
  or any Milestone 36 ingestion/search/traversal/chunking logic — this
  milestone only adds a command-parsing and confirmation layer in front of
  the existing, unmodified core functions. See Kernel and Capabilities
  above for the full detail.
- Milestone 38 — Safe Explicit Knowledge-Grounded Answers: adds
  `/knowledge ask`, an explicit, single-shot, retrieval-grounded
  question-answering verb on `KnowledgeCommandsCapability`, behind the
  same `requires_computer_actions` trust gate as every other verb. Adds
  `kernel/knowledge_base/query.py` (lexical query mechanics factored out
  of `search.py` so `search.py` and the new `evidence.py` share one
  implementation, with no change to `search.py`'s public API or
  behavior), `kernel/knowledge_base/evidence.py` (`retrieve_evidence()` —
  bounded full-chunk-text retrieval, sharing `search()`'s ranking and
  validation), and `kernel/knowledge_base/answer.py` (pure prompt
  construction from `prompts/knowledge/ask_system.md` plus a
  `json.dumps(..., ensure_ascii=False)` untrusted-data block between fixed
  marker lines, and strict structured-response parsing/citation
  validation) — none of which invoke a model or the network themselves;
  the capability makes the one model call. Adds `EphemeralResult`
  (`kernel/capabilities/base.py`) and a matching `Orchestrator.handle()`
  branch so a capability response can opt out of the
  memory-write/interaction-log tail — used for `ask` (question, evidence,
  and answer must never be persisted) and, correcting a Milestone 37 gap,
  for `search` (query text and results were previously still reaching
  that unconditional tail even though the capability itself never logged
  or audited them). Adds a fixed `OLLAMA_REQUEST_TIMEOUT_SECONDS = 120` to
  `kernel/models/ollama.py`'s `urlopen()` call, so a provider timeout is
  possible to map to a fixed reply instead of hanging indefinitely.
  Consent v1: the explicit `/knowledge ask` command is itself sufficient
  consent, acceptable only because the sole implemented provider is local
  Ollama — revisit before any remote provider is enabled for this
  operation. See Kernel and Capabilities above for the full detail.

**Planned / not yet implemented:**

- Real interfaces for Claude, web, and voice wired to the orchestrator —
  currently placeholder directories only (WhatsApp is implemented; see
  above).
- Adding or removing cellar holdings, editing any field other than
  quantity, automatic/conversation-driven quantity decrementing, backups, or
  change history (the importer is still a full-replacement snapshot and the
  quantity-update script is still quantity-only); recommendation ranking,
  pairing/suitability judgments, or model-assisted name resolution over the
  cellar (deterministic bottle-count lookup and exact wine-name matching are
  implemented — see Capabilities above — but only as exact, read-only
  lookups, never fuzzy matching, ranking, or writes); bottle-level
  purchase/ratings history beyond a cellar record's own fields; no search,
  embeddings, or vector retrieval over the cellar itself; no write API on
  `KnowledgeStore` itself, and no autonomous or scheduled write path for
  the cellar. (`kernel/knowledge_base/`, Milestone 36, is a separate,
  unrelated lexical-search service over `.md`/`.txt` sources — see Kernel
  above — and does not change any of this.)
- Automatic RAG or orchestrator-driven retrieval from
  `kernel/knowledge_base/` before an *ordinary* model call; retrieval
  triggered by natural-language intent detection; prompt-context injection
  into ordinary prompts; embeddings or semantic/vector search over that
  index; hybrid retrieval; model-generated summaries or metadata;
  multi-turn grounded conversation; answer caching; self-critique or
  citation-repair model calls. (Milestone 37 added explicit,
  human-triggered `/knowledge status`/`search`/`ingest` commands, and
  Milestone 38 added explicit, human-triggered `/knowledge ask` —
  single-shot, lexical-retrieval-grounded answering behind the same
  `requires_computer_actions` trust gate, calling the model exactly once
  per request — see Capabilities above. Nothing calls a model or retrieves
  automatically for an ordinary prompt; a future semantic/embedding-based
  retrieval integration could reuse the same typed
  `evidence.py:retrieve_evidence()` API `ask` already calls, but that
  integration itself remains unimplemented.)
- Automatic (non-command-triggered) ingestion, scheduled ingestion, file
  watchers, web/remote-URL ingestion, deletion commands, document-content
  display, path-based or partial-source ingestion, or any format beyond
  `.md`/`.txt`.
- Tools (`kernel/tools/`).
- Additional capabilities (strategy, research, travel, life administration).
- Multi-turn sessions, streaming, retries, and any autonomous or
  multi-capability workflows.
