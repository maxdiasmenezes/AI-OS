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
  kernel/employee_tasks: implemented (Milestone 40; schema version 2,
  adding plan persistence, in Milestone 41 P2) - a separate,
  dedicated SQLite database (storage/tasks/tasks.sqlite3) giving durable
  task identity and lifecycle state; not wired into the orchestrator, any
  capability, kernel/action_protocol, or kernel/tools/confirmation.py -
  deliberately distinct from capabilities/tasks/TasksCapability, an
  unrelated, existing command capability. As of Milestone 41 P2 it has
  exactly one caller, kernel/task_orchestration/ below - still no
  production caller upstream of that.
  kernel/task_planner: implemented (Milestone 41 P1 - Bounded Task
  Planner) - takes one persisted employee_tasks Task and a deterministic
  action catalog and produces a bounded, validated plan via exactly one
  structured-output model call; never executes anything. See Bounded Task
  Planner below.
  kernel/task_orchestration: implemented (Milestone 41 P2 - Planning
  Orchestration) - connects kernel/task_planner to kernel/employee_tasks
  without either depending on the other, running one task through
  created -> planning -> {ready | failed}. See Planning Orchestration
  below. No production caller yet.
  kernel/task_execution: implemented (Milestone 42 - Autonomous Execution
  Loop) - drives a ready/running task one step at a time (deterministic,
  model-free eligibility; action execution through SafeTaskExecutor;
  durable confirmation for sensitive actions; RESPOND synthesis through an
  injected conversational ModelProvider) plus a bounded runner that
  repeats that one-step primitive until a blocking/terminal condition.
  See Autonomous Execution Loop below. No production caller yet.
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
- **server** (`server.py`) — the HTTP layer and the one place
  authorization and deduplication happen; see WhatsApp Server Lifecycle
  below for how it starts, is composed, and shuts down. `MessageHandler`
  itself holds no dedup cache or allow-list — see handler above. Exactly
  two operations exist -
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
  every item including the shutdown sentinel.

Covered by an automated pytest suite (`tests/interfaces/whatsapp/`, one
file per module) that makes no real network call — `WhatsAppClient` tests
inject a fake `urlopen`; server tests exercise the real HTTP server only
over loopback on an OS-assigned ephemeral port, including a few raw-socket
tests for Content-Length edge cases `urllib` cannot express.

#### WhatsApp Server Lifecycle

The WhatsApp interface starts as its own process, `python -m
interfaces.whatsapp.server`, independent of the CLI's `kernel/main.py`.
`server.py`'s `build_orchestrator(config, capability_loader)` constructs
the real `MemoryManager`, wraps it in `FixedNamespaceMemory`, and builds
an `Orchestrator`; `build_server()` wires that together with
`WhatsAppClient` and a `MessageHandler` into a `WhatsAppServer`.
`server.py` owns this composition and the server's full lifecycle —
startup, request dispatch, and shutdown — with no separate lifecycle
manager.

The server itself is `http.server.ThreadingHTTPServer`, bound to
`whatsapp_config.host` (validated loopback-only by `config.py` — never
bindable to a public or LAN address, since exposing it directly would put
the raw Cloud API access token and an unauthenticated webhook path
straight on the network; a real deployment terminates TLS and exposes it
publicly through a separate reverse proxy or tunnel, which this
repository does not provide). An IPv6 loopback host (`::1`) binds through
a dedicated `AF_INET6` server variant rather than being silently
mishandled by an IPv4-only socket. `WhatsAppServer.stop()` shuts the HTTP
server down, then enqueues a shutdown sentinel *after* whatever is
already queued — so already-queued tasks are drained before the worker
sees it — and joins the background worker thread with a bounded timeout,
for a graceful exit with no in-flight or already-queued message
abandoned mid-processing.

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

  Normally, after a routed capability returns, `handle()`
  unconditionally writes the prompt and the response to memory
  (`remember("conversation", ...)`, twice — once per role) and to the
  interaction log (`log_interaction()`). See Ephemeral Knowledge Results
  and Privacy below for `EphemeralResult` (Milestone 38), the mechanism a
  capability uses to opt one specific response out of that tail.
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
  never a second storage-location setting. See Knowledge Ingestion and
  Knowledge Search below for how ingestion and search actually behave.
  `kernel/knowledge_base/
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

  `kernel/knowledge_base/query.py` factors the lexical
  query mechanics `search()` already had (query validation, FTS5 literal
  transformation, source-filter validation, limit validation, and
  deterministic ranking tie-break order) out into one shared,
  package-internal module, so `search.py` and
  `kernel/knowledge_base/evidence.py` never duplicate them — `search.py`'s
  public API and behavior are unchanged. See Ask-Specific Minimum-Term
  Matching and Grounded Knowledge Answers below for `evidence.py` and
  `answer.py`.
- **models** — the abstraction layer over language models, so capabilities
  and the orchestrator do not depend on a specific model provider directly.
  The `ModelProvider` contract and a `get_provider()` factory are implemented
  (`kernel/models/base.py`, `kernel/models/factory.py`), and adapter modules
  exist for Ollama, Anthropic, OpenAI, and Gemini. Of these, only Ollama is
  selected as the active provider in `kernel/config/config.yaml` and exercised
  end-to-end today; the other adapters are present in the codebase but not
  verified as the active path. **Milestone 39** adds `ModelRequestOptions`
  (`kernel/models/base.py`) — a frozen, keyword-only, per-request options
  object (`require_json`, `json_schema`, `temperature_override`) that
  `send_prompt(prompt, *, options=None)` now accepts on every provider.
  `options=None` (every call site written before Milestone 39: the
  orchestrator's fallback, `WineCapability`'s fallback, and
  `KnowledgeCommandsCapability._execute_ask()`) reproduces the exact prior
  request byte-for-byte — nothing about ordinary prose calls changed.
  `OllamaProvider` (`kernel/models/ollama.py`) is the only provider that
  acts on `options`: it conditionally adds a `format` field (`"json"`, or
  the supplied JSON Schema) and substitutes `temperature_override` into
  that one request's payload only — `self.temperature`/`self.model`/
  `self.max_tokens` are never mutated, and a structured request has no
  effect on any later call on the same provider instance. An out-of-range
  `temperature_override` (outside `OllamaProvider`'s own
  `MIN_TEMPERATURE_OVERRIDE`/`MAX_TEMPERATURE_OVERRIDE`, 0.0-2.0) is
  rejected before any HTTP request is made.
- **tools** — reusable tools (actions, integrations, lookups) that
  capabilities could invoke. Implemented (Milestone 33; extended in
  Milestone 34; extended again in Milestone 35; extended again in
  Milestone 43; extended again in Milestone 44; extended again in
  Milestone 45 P1): the safe computer task
  execution layer, transport-agnostic
  and consumed today only by
  `capabilities/tasks/TasksCapability` — see Capabilities below for the
  full command surface. `kernel/tools/types.py` defines `ActionRequest`
  (an action name plus an optional symbolic `resource_key` — never a raw
  path or argument list) and `ActionResult`. `kernel/tools/registry.py`'s
  `ActionRegistry` is the fixed, non-configurable allowlist of exactly
  fourteen actions (`system_status`, `list_files`, `open_application`,
  `run_registered_script`, `repo_health`, `repository_backup`, —
  Milestone 43 P1 — `file_metadata`, `read_text_file`, `list_processes`,
  — Milestone 43 P2 — `create_directory`, `copy_file`, — Milestone 44 —
  `browser_read_page`, and — Milestone 45 P1 — `desktop_target_status`,
  `desktop_control_status`) and which five of them are sensitive
  (`open_application`, `run_registered_script`,
  `repository_backup`, `create_directory`, `copy_file`) — `repo_health` is
  read-only and, like `system_status`/`list_files`, is not sensitive;
  `repository_backup`/`create_directory`/`copy_file` each write a new
  filesystem entry, so all three are sensitive; the three Milestone 43 P1
  actions, `browser_read_page`, and the two Milestone 45 P1 actions are all
  read-only and not sensitive
  either; no action beyond
  these fourteen is ever reachable, no matter what a caller asks for.
  `kernel/tools/git_safety.py`
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
  See Task Confirmation Storage below for `kernel/tools/confirmation.py`'s
  `ConfirmationStore`. `kernel/tools/process_control.py` is
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
  `repository_backup.py` — see Repository Health Checks and Repository
  Backup below for their full behavior. **Milestone 39** adds a small,
  read-only descriptor view to `ActionRegistry`:
  `descriptors() -> tuple[ActionDescriptor, ...]`, in fixed declaration
  order, where `ActionDescriptor` (`kernel/tools/registry.py`) carries
  `name`, `resource_key_requirement` (a closed
  `ResourceKeyRequirement` enum — `FORBIDDEN`/`OPTIONAL`/`REQUIRED`;
  `system_status` is the only `FORBIDDEN` one, the other five are
  `REQUIRED`), `resource_key_description` (a short, generic phrase, never
  a real key), and `sensitive`. No handler, path, or configuration value
  is ever exposed through it — see `kernel/action_protocol/` below, its
  only consumer so far.
- **action_protocol** (Milestone 39 — Reliable Action Protocol) — the
  machine-readable protocol through which a model returns exactly one
  validated decision, without ever generating or altering a tool name, a
  resource key, or an arguments object itself. A new package,
  `kernel/action_protocol/`, implementing the approved two-stage
  Deterministic Candidate Protocol; **not yet wired to any caller** — no
  changes to `kernel/orchestrator/`, any `capabilities/`, or
  `interfaces/whatsapp/` in this milestone.

  **Stage A - deterministic candidate resolution**
  (`candidates.py:resolve_action_candidates()`). Never calls a model,
  never executes an action. Matches the raw request against a small,
  conservative, hand-written grammar over the six `ActionRegistry`
  actions (favoring false negatives over false positives throughout),
  resolves any named target against the real `ToolsConfig` with exact,
  case-insensitive symbolic-key lookup only (no aliases, no fuzzy or
  semantic matching, no default target — matching
  `kernel/config/tools.yaml`'s own schema exactly), and returns a
  `CandidateResolution`: either zero or more immutable `ActionCandidate`
  objects, or — when a supported single-action intent is recognized but
  its required target is absent — a deterministic
  `RequestClarificationDecision` returned without ever calling the model.
  A target-looking token that fails a narrow shape check
  (`[A-Za-z0-9_-]`, 1-64 characters) or does not match a real configured
  key produces zero candidates, never a guess — this is what keeps a
  destructive command, a drive-letter path, or a quoted shell fragment
  from ever reaching a registry lookup at all. A recognized action
  combined with further content (`and`/`then`/`also`/`after that`/
  `before that`/`;`/a newline, with real content on both sides) also
  produces zero candidates — a compound request is never narrowed to its
  safe-looking first clause.

  **Stage B - constrained model decision** (`prompt.py` + `parser.py`).
  `build_prompt()`/`build_schema()` build one flat prompt and one dynamic
  JSON Schema per request: the model sees the request text and, for each
  candidate, only an opaque `candidate_id`, a code-generated
  `user_summary`, and whether it is `sensitive` — never a raw path,
  command, executable, or the underlying action/resource-key fields
  themselves. The schema's `oneOf` permits exactly `respond`,
  `request_clarification`, `cannot_complete`, and — only when at least
  one candidate exists — `select_candidate`, whose `candidate_id` is an
  `enum` of exactly that request's live candidate IDs; with zero
  candidates, `select_candidate` is entirely absent from the schema, so
  the model is structurally unable to select a tool, not merely
  instructed not to. `parser.py:parse_decision()` never raises for a
  malformed or unsafe model response (mirrors
  `kernel/knowledge_base/answer.py`'s `parse_structured_answer()`
  contract): complete-response parsing only (`json.loads()` requires the
  whole string be one JSON value, so prose before/after or multiple
  objects already fail), at most one whole-response Markdown fence
  stripped, duplicate JSON keys and `NaN`/`Infinity`/`-Infinity` rejected,
  JSON nesting deeper than 8 rejected before the recursive decoder ever
  sees it, an exact field set required per decision branch (so a field
  from another branch, or a `tool_name`/`resource_key`/`arguments`/
  confirmation field, is always rejected), and a `select_candidate`
  decision's `candidate_id` resolved only against the exact, request-local
  candidate tuple supplied to the parser — an unknown ID fails closed even
  though the dynamic schema should already make it unreachable. The
  parser performs no I/O and imports no executor or confirmation store;
  nothing a request's text claims (an already-approved candidate ID, a
  claim that confirmation was already granted) has any channel of effect.

  See `kernel/action_protocol/README.md` for the full design and its
  documented limitation: the conservative grammar's false negatives are
  intentional, not a defect — broader natural-language coverage is a
  later-milestone tradeoff.
- **employee_tasks** (Milestone 40 — Persistent Task Lifecycle) — a new
  package, `kernel/employee_tasks/`, giving AI-OS durable task identity
  and lifecycle state across multiple messages. Named `employee_tasks`,
  not `tasks`, because "task" is already spoken for twice in this
  codebase: `capabilities/tasks/TasksCapability` (Milestone 33) is the
  existing, synchronous `/task ...` command capability, and
  `interfaces/whatsapp/handler.py`'s `TextTask`/`FixedReplyTask` are an
  ephemeral, never-persisted per-message classification. This package is
  a third, new concept — a durable, multi-message unit of work — and has
  no dependency on either of those, on `kernel/action_protocol/`,
  `kernel/tools/`, `kernel/models/`, any `capabilities/`, or
  `interfaces/whatsapp/`; none of those depend on it either, in this
  milestone. See Persistent Task Lifecycle below for the full design.
- **config** — settings that govern how the kernel and its components
  behave. Implemented: non-secret settings load from `kernel/config/config.yaml`
  (active provider, provider settings, memory, knowledge, and log locations),
  secrets load from `.env` (`kernel/config/config.py`). `Config.knowledge_storage_dir`
  is resolved to an absolute `Path` the same way `log_path` is — relative to
  the repository root — so `JSONKnowledgeStore` can be constructed directly
  from it without any further path handling.

#### Task Confirmation Storage

Pending task confirmations are stored in `kernel/tools/confirmation.py`'s
`ConfirmationStore`: a single-slot, TTL-bound (2 minutes), thread-safe,
in-process store — not a file or database, so a server restart clears
whatever was pending rather than persisting it. `propose()` registers one
pending action; `consume()` atomically reads and clears it in one step,
before the caller acts on the result, so a confirmation can never be
replayed even if execution afterward fails, and separately reports
whether what it found had expired. It is a process-wide singleton
(`default_store`), not an attribute on any capability instance, since
`CapabilityLoader` constructs a new capability object on every request.
`capabilities/tasks/TasksCapability` proposes into this store for
`/task backup <key>`, `/task open <key>`, and `/task run <key>`;
`/task confirm` and `/task cancel` consume or clear it — see Repository
Backup below for the specific confirmation this store enforces before a
backup is created.

#### Persistent Task Lifecycle

`kernel/employee_tasks/` (Milestone 40; schema version 2 in Milestone 41
P2) persists task identity, lifecycle state, and — once planning
succeeds — a validated plan, in a dedicated SQLite database,
`storage/tasks/tasks.sqlite3`, whose location is derived from the
`tasks.storage_dir` setting in `kernel/config/config.yaml` the same way
`kernel/knowledge_base/db.py` derives its own database path — read
directly from the YAML file, independently of `kernel/config/config.py`,
so this package needs no wiring into `Config` or `Orchestrator` to be
usable. It is **not** wired into either as of Milestone 41: nothing in
`kernel/orchestrator/`, any `capabilities/`, or `interfaces/whatsapp/`
calls it, and it still calls no model and no tool itself. As of Milestone
41 P2 it does have one caller, `kernel/task_orchestration/` (see Planning
Orchestration below), which is itself not wired into anything upstream
yet either.

- **Identity.** `task_id` is a code-generated UUID7
  (`uuid.uuid7()`, Python 3.14 stdlib, no new dependency) — time-ordered,
  unique for one installation, never generated by a model.
  `display_id` (`TASK-XXXXXXXX`) is an 8-character code deterministically
  derived from `task_id`'s raw bytes using a Crockford-style alphabet
  with visually ambiguous characters (`0`, `1`, `I`, `L`, `O`) removed —
  safe to read or copy over WhatsApp, but not a secret and never used for
  authorization. Because the derivation is lossy (128 bits down to
  ~40 bits), `TaskRepository.create_task()` treats a `display_id` UNIQUE
  violation as a signal to retry with a freshly generated `task_id`/
  `display_id` pair, bounded to `MAX_DISPLAY_ID_ATTEMPTS` (5) attempts,
  rather than ever overwriting an existing row.
- **States.** A closed, 8-value `TaskState` enum: `created`, `planning`,
  `ready`, `running`, `waiting_for_confirmation`, and the terminal
  `completed`/`failed`/`cancelled`. `waiting_for_confirmation` is only a
  persisted lifecycle state in this milestone — it is not wired to
  `kernel/tools/confirmation.py`'s pending-action store. The exact,
  closed transition table (`kernel/employee_tasks/types.py:
  ALLOWED_TRANSITIONS`) allows cancellation from every non-terminal
  state; terminal states accept no further transition, enforced both
  before any SQL runs and again by the conditional `UPDATE`'s `WHERE`
  clause.
- **Schema (version 2).** A `tasks` table (current row per task: state,
  the original `request_text`, `source`, an optional `dedup_key`,
  timestamps, failure fields, an opaque size-capped `metadata_json`
  string, an optimistic `version` counter, and — schema version 2,
  Milestone 41 P2 — an opaque, size-capped (`MAX_PLAN_JSON_CHARS`,
  16,384) `plan_json` column) plus an append-only `task_transitions`
  journal (one row per state change, including a synthetic
  `NULL -> created` row recorded at creation) — current-row update and
  journal insert always commit together in one transaction, matching
  `kernel/knowledge_base/db.py`'s `schema_meta` version-tracking
  convention. Version 1 → 2 migration (`kernel/employee_tasks/db.py:
  _migrate_v1_to_v2()`) adds the column to an existing database with a
  single `ALTER TABLE` committed atomically with the `schema_meta` version
  bump; every pre-existing row implicitly gets `plan_json = NULL`, which
  is exactly correct since no task from before this milestone has ever
  had a plan.
- **Concurrency.** `BEGIN IMMEDIATE` on every write, WAL journal mode and
  a fixed busy timeout on the writer connection, `PRAGMA query_only=ON`
  on the reader connection — the same split `kernel/knowledge_base/db.py`
  established. Every transition requires the caller's expected current
  state; a stale or racing writer gets a typed `InvalidTransitionError`
  or `TaskAlreadyTerminalError` and modifies neither table.
- **Plan persistence (Milestone 41 P2).**
  `TaskRepository.persist_plan_and_ready()` writes `plan_json` and
  transitions `planning -> ready` atomically, in the same transaction as
  the journal entry — a task is never left `ready` without a plan, and a
  plan is never persisted without that transition. The underlying
  conditional `UPDATE` requires both `state = 'planning'` AND
  `plan_json IS NULL`, so a plan may be persisted exactly once per task;
  a second attempt, or one made against the wrong state, fails closed
  with the same typed errors (`InvalidTransitionError`/
  `TaskAlreadyTerminalError`) every other transition already uses — no
  new error taxonomy was introduced for this. `plan_json`'s content is
  treated exactly as opaquely as `metadata_json`: this layer validates
  only that it parses as JSON and stays within its size bound, and never
  imports `kernel.task_planner` or any `TaskPlan` type.
- **Scope.** This package persists identity, lifecycle state, and a
  validated plan once planning succeeds — it still does not execute a
  task, call a model, or call a tool itself (planning happens in
  `kernel/task_planner/`, orchestrated by `kernel/task_orchestration/` —
  see below). Execution of a plan's steps is `kernel/task_execution/`'s
  responsibility (Milestone 42 — see Autonomous Execution Loop below).

See `kernel/employee_tasks/__init__.py` and `storage/tasks/README.md` for
the full design and the "task" naming disambiguation.

#### Bounded Task Planner

`kernel/task_planner/` (Milestone 41 P1) takes one `kernel/employee_tasks`
`TaskRecord` and a deterministic action catalog and produces a bounded,
validated plan (`TaskPlan`) via exactly one structured-output model call —
never executing anything, never calling a tool, never granting
confirmation. It has no dependency on `kernel/employee_tasks`' `db.py` or
`repository.py` (only the plain, I/O-free `TaskRecord` type and, for
serialization, the `MAX_PLAN_JSON_CHARS` bound), and no dependency on
`kernel/action_protocol/` — enforced mechanically by an AST-based
import-boundary test, not just by convention.

- **Action catalog** (`catalog.py:build_catalog()`) — the model-facing
  action-reference strategy: the full, request-independent cross product
  of `ActionRegistry.descriptors()` (Milestone 39) and each action's real
  configured resource keys, giving each `(action, resource_key)` pair an
  opaque `catalog_id`. Deliberately **not** `kernel/action_protocol/
  candidates.py`'s `resolve_action_candidates()`: that resolver is a
  per-request natural-language grammar that, by design, returns zero
  candidates for anything compound, which a bounded multi-step planner
  cannot be built on top of — confirmed empirically before this milestone
  was designed (see below).
- **Prompt/schema** (`prompt.py`) — a fixed set of five ordered precedence
  rules (full coverage of every requested operation; never guess from a
  genuinely ambiguous request; the step limit is a hard representability
  limit, never a truncation target; one action per action step; a
  `respond` step is synthesis only, never an implied action) plus six
  compact few-shot examples, and a two-branch (`"plan"`/`"cannot_plan"`)
  dynamic JSON Schema whose `action` step branch's `catalog_id` enum is
  exactly the offered catalog — the model is structurally unable to
  reference an action outside that set. Uses the same request-specific
  `require_json=True` / dynamic schema / `temperature_override`
  structured-output path Milestone 39 proved
  (`kernel/models/base.py:ModelRequestOptions`), fixed at
  `PLANNER_TEMPERATURE_OVERRIDE = 0.0`.
- **Parser** (`parser.py:parse_plan_response()`) — strict, fail-closed,
  never raises: complete-response-only JSON parsing, duplicate-key and
  `NaN`/`Infinity` rejection, a nesting-depth pre-check, an exact
  required-field set per branch, and per-step validation including a
  backward-only `depends_on` bound (`1 <= dependency < position`) that
  makes a dependency cycle structurally unreachable rather than merely
  checked-for. `requires_confirmation` is derived deterministically from
  the selected catalog entry's registry sensitivity — never a field the
  model can supply; no such field exists anywhere in the schema.
- **Capability grounding** (`grounding.py:validate_capability_grounding()`) —
  a small, deterministic validation boundary that runs strictly after a
  structurally valid plan already exists, never inside the parser. A real,
  correctly-referenced catalog action is necessary but not sufficient: an
  action whose resource key names one specific, narrowly-purposed
  capability (`open_application`, `run_registered_script` — always) or
  whose action has more than one configured resource in the catalog (any
  action, once ambiguous among multiple real options) must have every
  word of its selected resource key textually present in the request —
  purely lexical whole-word containment, never semantic/NLP matching, no
  model call. This closes a real failure mode found during model
  evaluation: a generic request ("run the tests") must never silently
  authorize a specifically-named capability ("the `whatsapp_test`
  script") the request never mentioned, while an explicit request naming
  it legitimately may. A step that fails this check produces a
  `PlannerFailure` (`UNGROUNDED_CAPABILITY`), never a `TaskPlan`.
- **Planner model selection.** `llama3.1:8b` (the general conversational
  provider's own model) and `qwen3:14b` were both evaluated as candidate
  structured-planner models against a fixed 33-request corpus (single-
  step, multi-step, ambiguous, and adversarial requests) using this exact
  prompt/schema/parser, `require_json=True`, and
  `temperature_override=0.0` — both scored 87.9% strict semantic-plan
  accuracy, below the required 90% reliability gate.
  `gemma3:12b` scored 97.0% (32/33) and passed every acceptance
  criterion (100% strict-schema-valid, 100% correct action-reference
  accuracy, zero invented actions, zero confirmation-policy violations,
  zero ungrounded-capability false-positives on legitimate requests). It
  is recorded as the dedicated planner model in `kernel/config/
  config.yaml`'s `planner` section (Milestone 41 P3) — structured the
  same way as the general `provider`/`providers` section, so a future
  caller can construct a `ModelProvider` from
  `config.planner_provider_settings` the same way `get_provider()`
  already does for the general one. **The general conversational
  provider is unchanged** — it remains `ollama`/`llama3.1:8b`; nothing
  currently constructs or calls a planner-specific `ModelProvider`, and
  no runtime caller invokes planning from a real request.
- **Serialization** (`serialization.py`) — `serialize_plan()`/
  `deserialize_plan()` convert a `TaskPlan` to and from the deterministic
  (sorted-key, compact) JSON string `kernel/employee_tasks/` persists
  opaquely as `plan_json` (see Persistent Task Lifecycle above). A
  malformed persisted string is a distinct, typed
  `PlanDeserializationError` — a storage/corruption concern — never
  converted into a `PlannerErrorCode`/`PlannerFailure`, which describe a
  problem with a model's response, not a database row. `TaskPlan.task_id`
  round-trips exactly like every other field; a future execution consumer
  (Milestone 42) that loads a `TaskRecord` and its `plan_json`
  independently must verify
  `deserialize_plan(record.plan_json).task_id == record.task_id` before
  treating any step as executable — `kernel/employee_tasks/` itself never
  cross-checks this, since `plan_json` is opaque to it.

Boundary: Milestone 41 decides and persists **what** should be done. It
never decides **when** or **next**, and never performs the doing.
Milestone 42 (see Autonomous Execution Loop below) owns execution.

#### Planning Orchestration

`kernel/task_orchestration/` (Milestone 41 P2) connects the pure planner
above to the persistent task lifecycle, without either depending on the
other — it depends on both `kernel.employee_tasks` and
`kernel.task_planner` (plus `kernel.models` for the injected
`ModelProvider` type), and neither of those depends on it. An AST-based
import-boundary test confirms it never imports a tool executor, a tool
execution handler, process-control execution, confirmation execution, or
`kernel.tools` at all.

`advance_task_planning(task, repository, catalog, model_provider)` runs
the complete `created -> planning -> {ready | failed}` sequence for one
task, one invocation:

- The caller must supply a task already in `created`; a task in any other
  state is rejected before any repository write or model call.
  `TaskRepository.transition_task()`'s own database-level race check is
  the real safety net against a stale in-hand `TaskRecord` — its
  `InvalidTransitionError`/`TaskAlreadyTerminalError` propagate uncaught,
  never concealed or retried.
- Exactly one call to `kernel.task_planner.plan_task()` is made, wrapped
  in a deliberately broad `except Exception` — `kernel.models.base.
  ModelProvider.send_prompt()` is an abstract method with no declared
  exception contract, and different concrete adapters (Ollama, Anthropic,
  OpenAI, Gemini) can raise entirely different types, so no narrower catch
  is available without this layer reaching into provider-specific
  internals it must not know about. Every other call in this function —
  `transition_task()`, `mark_failed()`, `persist_plan_and_ready()` — is
  deliberately **not** wrapped in any `try`/`except`: a concurrency
  conflict or a genuine defect in any of them propagates as itself, proven
  by dedicated tests rather than by inspection alone.
- The four-way `PlanOutcome` maps to lifecycle actions as: `TaskPlan` →
  serialize, then atomically persist `plan_json` and transition to
  `ready`; `CannotPlan`/`RequiresClarification` (the latter reserved,
  not yet producible by the current two-branch wire protocol) → `failed`
  with a fixed, code-authored failure code/summary; `PlannerFailure` →
  `failed` with `failure_code = outcome.error.value` and a **fixed,
  code-authored** summary looked up by error code — never the model's own
  `CannotPlan.reason`/`RequiresClarification.question` text, and never
  `PlannerFailure.detail` directly (at least one detail string embeds a
  model-supplied integer, so it cannot be treated as uniformly
  code-authored). `failure_summary`/`safe_summary` are trusted-display
  fields; this is the one place that distinction is actually enforced,
  not merely intended.
- No retries anywhere. Crash recovery for a process that dies while a
  task sits in `planning` is explicitly out of scope for this milestone —
  later persistence/recovery work, not this one.

As of Milestone 41 P3, nothing calls `advance_task_planning()` from a
real request — there is still no runtime caller, no wiring into
`kernel/orchestrator/`, any capability, or `interfaces/whatsapp/`, and no
task is ever created outside a test. `tests/kernel/task_orchestration/
test_integration.py` proves the complete non-executing chain — real
`create_task()` → real `advance_task_planning()` → a durable `TaskPlan` →
closing and reopening the database connection → reloading the
`TaskRecord` → `deserialize_plan()` → `task_id` integrity — works
end to end.

#### Autonomous Execution Loop

`kernel/task_execution/` (Milestone 42) is the layer that decides *when*/
*next* and performs the doing that Milestones 40-41 deliberately left
undone. It takes one persisted `employee_tasks` `TaskRecord` (whose
`plan_json` is a Milestone 41 `TaskPlan`), the task's durable per-step
progress, and the CURRENT `ActionRegistry`/`ToolsConfig`, and
deterministically drives that task from `ready` through however many
steps it can safely complete, one step at a time. Every dependency
(`TaskRepository`, `ActionRegistry`, `ToolsConfig`, `SafeTaskExecutor`,
and a conversational `ModelProvider`) is injected explicitly by the
caller — this package never opens a database connection, never loads
`kernel/config/tools.yaml`, and never constructs an `ActionRegistry`,
`SafeTaskExecutor`, or model provider itself.

- **Durable step progress (schema version 3).** A new `task_step_progress`
  table records one row per claimed plan step: absence of a row means
  "not started" (no separate `not_started` status is ever persisted); a
  claimed row starts `in_progress` and moves exactly once to the
  write-once terminal `succeeded` or `failed` (enforced by a conditional
  `UPDATE ... WHERE status = 'in_progress'`, mirroring `tasks`' own
  conditional-transition discipline). Claiming
  (`TaskRepository.claim_step()`) is a plain `INSERT` whose
  `(task_id, step_position)` PRIMARY KEY is the atomic exactly-one-winner
  mechanism for two callers racing the same step, wrapped in the same
  `BEGIN IMMEDIATE` transaction as a fresh re-read of the task's CURRENT
  state (never a stale caller-held `TaskRecord`) — a step may only be
  claimed while the task is actually `running`.
- **Deterministic, model-free eligibility**
  (`eligibility.py:evaluate_next_step()`) — a pure function with no
  database, tool, or model access of its own: deserializes the persisted
  plan, verifies its embedded `task_id` matches the task record, rejects
  a plan whose step count exceeds `MAX_PLAN_STEPS` (8) as a plan-integrity
  violation (the planner's own step-count bound is enforced only at
  plan-parse time, never re-checked by `deserialize_plan()`, so this is
  independent defense against a hand-crafted or corrupted persisted
  plan), scans steps in ascending position order, and returns the first
  not-yet-succeeded one whose declared dependencies have all durably
  succeeded. An earlier step that durably `failed`, or one still
  `in_progress` (an uncertain outcome — see Exactly-once/unknown-outcome
  limitation below), blocks the whole task closed rather than being
  retried or skipped. For an `action` step, `action_name`/`resource_key`
  are revalidated exactly as persisted against the CURRENT
  `ActionRegistry`/`ToolsConfig`, and sensitivity is always re-derived
  from `ActionRegistry.is_sensitive()` at evaluation time — never trusted
  from the persisted `PlanStep.requires_confirmation` (which reflects the
  registry's state at *planning* time) or from `catalog_id`, which
  carries no execution authority anywhere in this package.
- **Action execution.** `kernel.tools.executor.SafeTaskExecutor.execute()`
  is the ONLY action-execution boundary this package ever calls — never a
  handler, `kernel.tools.process_control`, or any application-launch/
  script/repository-backup implementation directly. The `ActionRequest`
  sent to it is built only from an already-revalidated, persisted
  `PlanStep` — never from request text or model interpretation.
- **Durable confirmation (schema version 4).** A sensitive eligible action
  proposes a `task_pending_confirmation` row and transitions
  `running -> waiting_for_confirmation` atomically
  (`propose_confirmation()`) — wholly independent of
  `kernel/tools/confirmation.py`'s pre-existing in-memory, single-slot
  `ConfirmationStore`, which still only serves the older `/task ...`
  command path and which this package never imports. The invariant
  `task.state == waiting_for_confirmation` **iff** exactly one
  `task_pending_confirmation` row exists is enforced mechanically, not
  just by convention: the generic `transition_task()`/`mark_cancelled()`/
  `mark_failed()` repository methods mechanically refuse any call that
  would enter or leave that state, because none of them know the pending
  table exists. Only `propose_confirmation()` (in) and
  `consume_confirmation_and_claim_step()`/`deny_confirmation()`/
  `fail_pending_confirmation()` (out) may cross that boundary, since only
  they atomically keep the pending row in sync with task state. Each
  pending confirmation is task-scoped, step-scoped, single-use, bounded by
  its own 120-second TTL (`TASK_CONFIRMATION_TTL_SECONDS`), and durable —
  a server restart does not lose it, unlike the older in-memory store.
  - **Authorization chain.** Approval never trusts the pending row as
    execution authority by itself — it is correlation/check state only.
    `approve_task_confirmation()` re-reads the CURRENT `TaskRecord`,
    re-resolves the pending row's `step_position` against the immutable
    **persisted `TaskPlan`** (`resolve_persisted_plan_step()`), and
    verifies the resolved step is an `action` step whose
    position/action_name/resource_key match the pending row EXACTLY
    before ever revalidating against the CURRENT `ActionRegistry`/
    `ToolsConfig`. Only after that chain succeeds does one atomic
    transaction consume the confirmation, transition
    `waiting_for_confirmation -> running`, and claim the exact step —
    and only after that transaction commits does `SafeTaskExecutor`
    ever get called, with an `ActionRequest` built from the verified
    persisted `PlanStep`, never from the pending row's own fields. A
    second approval attempt with the same `confirmation_id` can never
    claim or execute twice: the first call already deleted the pending
    row and moved the task out of `waiting_for_confirmation`.
  - **Claim = authorization boundary.** A successful step claim (via
    `claim_step()` for a non-sensitive step, or via
    `consume_confirmation_and_claim_step()` for an approved sensitive
    one) is the point of authorization for that one step. Cancellation
    *before* a claim commits prevents it outright — the executor or model
    is never invoked. Cancellation *after* a claim commits cannot
    retroactively revoke an already-authorized external side effect that
    may already be in flight: the already-claimed unit of work (the
    action call, or the RESPOND model call) may still finish, and its
    real, known result is still durably recorded for audit — but the task
    itself is never overwritten out of whatever terminal state a
    concurrent writer already committed it to, and no further step is
    ever started once a task is no longer `running`. There is no
    additional `executing` task state; an in-flight claimed step is
    represented purely by its `task_step_progress` row being `in_progress`
    while the task itself may independently already be terminal.
- **Exactly-once/unknown-outcome limitation.** SQLite and an arbitrary
  external side effect (a subprocess, an application launch, a real
  repository backup) cannot form one atomic transaction. If the process
  running this code crashes after a claim commits but before the step's
  terminal result is persisted, `task_step_progress` is left durably
  `in_progress` forever — a real, deliberate uncertain-outcome state, not
  a bug. This layer never automatically retries it, never clears it, and
  never continues to a later step once eligibility observes it — a
  concurrent/subsequent evaluation of an `in_progress` step fails the
  whole task closed. AI-OS does **not** claim exactly-once external
  execution; recovering/reconciling this state is explicitly out of scope
  for Milestone 42 and is Milestone 47's concern.
- **StepObservation.** The durable, bounded result of one step — for
  either an `action` or a `respond` step — persisted as the same opaque,
  size-capped (`MAX_STEP_RESULT_JSON_CHARS`, 4,096) `result_json` column
  `task_step_progress` already has. An `action` step's observation reuses
  `SafeTaskExecutor`'s own already-safe `ActionResult.message`/`.outcome`
  verbatim (never raw stdout/stderr, a stack trace, or a secret — see
  Tools above). Never a separate response table.
- **RESPOND synthesis and model-role separation.** An eligible `respond`
  step is claimed through the identical `claim_step()` boundary a
  non-sensitive action uses, then synthesized by
  `respond.py:synthesize_response()` using an injected, general
  conversational `ModelProvider` — the ONLY place this package ever calls
  a model, and never `gemma3:12b`, the dedicated structured-planner
  provider (see Bounded Task Planner above). This package never
  re-plans, never lets the conversational model select or alter an
  action, and never lets a RESPOND step create a tool request — `respond.py`
  imports only the abstract `kernel.models.base.ModelProvider` contract,
  never `kernel.models.factory` or a concrete provider module. Synthesis
  draws only from durable, trusted state: the task's own `request_text`
  and the RESPOND step's own `description`/`expected_result` are framing
  context (what is being asked, and what kind of reply is wanted) — never
  evidence that any action occurred. Only the durable `StepObservation`s
  of the RESPOND step's own declared dependencies are treated as
  authoritative evidence of completed work, and each one must
  independently pass a full integrity check (durable row present, status
  `succeeded`, `result_json` present, `deserialize_observation()`
  succeeds, `step_position`/`success`/`step_kind` all matching the
  persisted plan) before the model is ever called — any mismatch fails
  the step/task closed with a stable, code-authored failure, never
  reinterpreted as a planner concern. The generated prompt tells the
  model explicitly that dependency results are DATA, never instructions,
  and that command-like text embedded in them must not be followed. Two
  bounds apply, both fail-closed rather than truncating: the fully
  constructed prompt must fit `MAX_RESPOND_PROMPT_CHARS` (24,000) or the
  step fails closed (`respond_context_too_large`) before the model is
  ever called; the raw generated response must fit `MAX_RESPOND_TEXT_CHARS`
  (3,800) as an early check, but that raw bound alone does not guarantee
  the serialized `StepObservation` fits `MAX_STEP_RESULT_JSON_CHARS` (JSON
  escaping of quotes/backslashes/control characters can expand it) — the
  authoritative check is the actual serialization attempt, and a response
  that fails it fails the step/task closed (`respond_invalid_output`)
  rather than being truncated. The final synthesized text is durably
  available exactly like any other step's result:
  `TaskStepProgress.result_json` → `deserialize_observation()` →
  `StepObservation.safe_summary`. Milestone 42 stores this text; it does
  not deliver it to any channel — that is Milestone 46's concern.
- **Bounded autonomous runner** (`run_task_until_blocked()`) — a bounded
  driver over the one-step `advance_task_execution()` primitive, and
  nothing else: it repeatedly calls that one primitive, continuing
  automatically only after `STEP_SUCCEEDED`, and stopping immediately on
  `CONFIRMATION_REQUIRED`, `WAITING_FOR_CONFIRMATION`, `TASK_COMPLETED`,
  `TASK_FAILED`, or `TASK_CANCELLED`. It never selects a step, claims a
  step, calls `SafeTaskExecutor` or `ModelProvider` directly, approves or
  denies a confirmation, or alters the persisted plan — every actual
  execution decision still belongs to `advance_task_execution()` itself,
  which remains a strict one-step-per-call primitive (never an internal
  loop) so it stays independently usable by future recovery logic. The
  runner's only direct mutation is a deterministic, code-owned hard
  ceiling on how many times it may call that primitive in one invocation:
  `MAX_EXECUTION_ADVANCES = MAX_PLAN_STEPS + 1` (9) — an `N`-step plan
  needs at most `N` successful advances plus one further advance to
  observe all steps complete, so this bound is a pure loop/progression
  safety guard, never a substitute for the plan-size integrity check
  above (an oversized plan is already rejected before this bound is ever
  relevant). Exceeding it fails the task closed
  (`execution_advance_limit_exceeded`) rather than ever returning a
  still-running task as though it had finished.
- **Task lifecycle at end of Milestone 42.** `created -> planning -> ready
  -> running`, then from `running`: a successful ordinary step leaves the
  task `running`; a sensitive eligible action moves it to
  `waiting_for_confirmation`; approval consumes the confirmation and
  returns it to `running` with the exact approved step claimed; denial or
  a durable step failure moves it to `cancelled`/`failed` respectively; a
  confirmation that expires or no longer matches the persisted plan fails
  the task; and once every step has durably succeeded the next advance
  observes it and completes the task. No additional `executing` state was
  introduced — see Claim = authorization boundary above for how an
  in-flight step is represented instead.

As of Milestone 42, this package still has no runtime caller — nothing in
`kernel/orchestrator/`, any `capabilities/`, or `interfaces/whatsapp/`
calls `advance_task_execution()`/`run_task_until_blocked()`/
`approve_task_confirmation()`/`deny_task_confirmation()` from a real
request; nothing creates a task, triggers planning, or begins execution
outside a test. Real task submission, runtime/orchestrator wiring, a
scheduler/background service, WhatsApp result delivery and confirmation-
reply routing, crash-recovery reconciliation of an uncertain `in_progress`
step, and any new action type are explicitly out of scope here — see
Milestone boundaries in Implementation status below.

**Threat boundary.** Milestone 42 protects against: a stale caller-held
task/plan reference, two workers racing the same claim or the same
confirmation, confirmation replay, partial/inconsistent persisted
confirmation state, action substitution attempted through model output or
runtime code, a stale registry/config assumption, a malformed or
oversized persisted plan or observation, accidental partial corruption of
a confirmation row, and automatic re-execution of an action whose outcome
is uncertain. It does **not** provide cryptographic tamper resistance
against an attacker who already has arbitrary direct write access to the
SQLite database file — such an attacker could, in principle, rewrite
multiple mutually-consistent records (including the persisted `TaskPlan`
itself) to describe a different, coordinated, but still internally
self-consistent state. Defending against that specific threat would
require an independent trust boundary (e.g. signed proposals, or storage
this process does not itself have unrestricted write access to) that no
part of this milestone's threat model calls for.

#### Knowledge Ingestion

Ingestion (`kernel/knowledge_base/ingest.py:ingest_source()`) reads one
approved local source by symbolic key — never a caller-supplied path —
traverses it with symlink/junction/reparse-point rejection and fixed
safety limits (`kernel/knowledge_base/traversal.py`), then normalizes and
deterministically chunks each file's text with SHA-256-derived stable
identifiers (`kernel/knowledge_base/chunking.py`). The whole source
ingests inside one `BEGIN IMMEDIATE` SQLite transaction, diffed against
the prior generation by content hash so unchanged documents are left
completely untouched; any failure — an invalid file, a safety-limit
violation, an unexpected database error — rolls the whole transaction
back, leaving the prior generation searchable. An empty source is a
valid ingestion that atomically removes any previously indexed documents
for that source.

As of Milestone 38.2B, `chunk_normalized_text()` takes an `is_markdown`
keyword, which `ingest_source()` derives solely from the candidate's
suffix (`.md`, case-insensitively — never content sniffing or a config
flag). When true, a valid ATX heading outside a fenced code block is a
hard chunk-packing boundary: text is split into sections at every
heading's line start first, then each section is paragraph-packed
independently, so a heading always opens its section's first chunk, no
chunk ever straddles a real heading, and overlap seeding is clamped to
the current section's start. `is_markdown=False` (every non-`.md` file,
and the default) packs the whole document as one section — byte-for-byte
identical to chunking before this milestone. This changes boundaries,
offsets, and chunk IDs only for `.md` content; because unchanged
documents are skipped by content hash, already-indexed `.md` files keep
their pre-38.2B boundaries until their content changes or the index is
deliberately rebuilt — see `kernel/knowledge_base/README.md`. This does
not add or change ranking: `search()` and `retrieve_evidence()` are
unmodified, and there is no heading-based ranking or heading metadata
exposed anywhere.

#### Knowledge Search

`kernel/knowledge_base/search.py:search()` is read-only end to end and
never invokes a model or the network. It transforms plain query text
into a safe FTS5 `MATCH` expression: every extracted alphanumeric term
becomes an individually quoted string literal, joined with `AND`, so
caller-supplied quotes, wildcards, `NEAR`/`OR`/`NOT`, or column filters
can never behave as FTS5 operators, only as literal text. Ranking uses
SQLite's built-in BM25 with deterministic tie-breaking, and results carry
a bounded `snippet()` excerpt rather than full chunk text. This strict
all-terms-`AND` matching is unchanged by Ask-Specific Minimum-Term
Matching below — `/knowledge search` still requires every extracted
term to be present.

#### Historical Natural-Question Retrieval Limitation

Before Milestone 38.1, `retrieve_evidence()` reused `search()`'s strict,
all-terms-`AND` `build_match_expression()`. This made an ordinary
natural question — for example, "How does the repository backup feature
work?" — retrieve nothing: no chunk contained every generic framing word
("how", "does", "the", "feature", "work") alongside the real content
terms. This paragraph describes retrieval history, not how the
repository backup feature itself behaves — see Repository Backup below
for that. `search()` and `build_match_expression()` were not changed by
the fix that followed, and are still exactly the same today.

#### Ask-Specific Minimum-Term Matching

Natural-question evidence retrieval for `/knowledge ask` uses ask-specific
minimum-term matching: `retrieve_evidence()` (`kernel/knowledge_base/
evidence.py`) builds its own MATCH expression, used only by `/knowledge
ask`. Generic framing, interrogative, auxiliary, pronoun, and negation
terms are removed from the extracted terms first (English and
Portuguese); if nothing useful remains, `[]` is returned immediately,
without opening a database connection. The remaining useful terms
combine by a deterministic minimum-match rule performed entirely by
FTS5's own tokenizer: one useful term stands alone; two useful terms
require both; three or more useful terms require any two of them,
expressed as every two-term `AND` combination `OR`'d together.
`/knowledge search` remains strict all-term matching, unaffected by this
rule. This remains exactly one read-only, parameterized SQL query,
selecting bounded full chunk text (at most 5 chunks, 1,500 characters
each, 7,500 total) instead of a short `snippet()` excerpt, ranked by the
same BM25 and tie-break order `search()` uses.

#### Ephemeral Knowledge Results and Privacy

A capability can opt one response out of the orchestrator's normal
memory-write and interaction-log tail by returning `EphemeralResult`
(`kernel/capabilities/base.py`) instead of a plain `str` — a `str`
subclass, so every existing `isinstance(result, str)` check keeps
working unchanged. `Orchestrator.handle()` checks for `EphemeralResult`
before its plain-`str` check, still wraps the text in the usual
`ModelResponse`, but skips writing the prompt and response to memory and
to `storage/logs/interactions.jsonl` for that one request.
`capabilities/knowledge_commands/` returns it for `/knowledge search`
and `/knowledge ask` only, since query/question text and any
model-generated answer must never be persisted; `/knowledge status`,
`ingest`, `confirm`, and `cancel` are unaffected, and so is every other
capability.

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
  immediately. See Task Confirmation Storage above for how this pending
  state is actually stored.
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
  cancelled, timed out, failed) is audited via `kernel.tools.audit`. See
  Repository Backup and Repository Health Checks below for the full
  `repository_backup` and `repo_health` handler behavior.

#### Repository Backup

`repository_backup` (`kernel/tools/handlers/repository_backup.py`)
creates a verified, local-only Git bundle of one registered repository's
committed history in a preapproved local destination directory, reached
through `/task backup <key>`. It is sensitive, so it only proposes the
action first — explicit confirmation (`/task confirm`) within 2 minutes
is required before anything is written (see Task Confirmation Storage
above). Before creating a backup, the handler runs repository safety
checks: it independently canonicalizes both paths, re-verifies the
repository path is the worktree's actual root, and rejects the
destination being inside the repository, the repository being inside the
destination, or the two being equal. The bundle is streamed directly to
an exclusively created file, then verified with `git bundle verify` and
re-hashed (SHA-256) before an atomic, no-overwrite finalize; an existing
`.bundle` file is never deleted, truncated, or overwritten. On success
the reply names only the generated filename, size, and SHA-256 digest —
never the destination path.

`/task backup <key>` accepts only the symbolic
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
`kernel/tools/process_control.py`'s `run_streaming_stdout_to_file()`
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

#### Repository Health Checks

`repo_health` (`kernel/tools/handlers/repo_health.py`) is a read-only
status/sync check for one registered repository, reached through
`/task repo <key>`; unlike backup, it is not sensitive and executes
immediately, with no confirmation required. Repository health checks
report the current branch (or `detached`), whether the working tree is
clean or dirty (from `git status --porcelain`, used only to decide
clean-versus-dirty), the latest commit's short hash and a sanitized
subject line, whether `HEAD` matches the configured local main branch's
tip commit exactly, and whether the local main branch matches GitHub's:
`up to date`, `differs`, `remote branch unavailable`, or `GitHub
unreachable`. Every git subprocess runs local-only except one
deliberately isolated, read-only `git ls-remote` call against a strictly
validated `github.com` origin.

`repo_health`
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
- **Grounded answering (`ask`).** See Grounded Knowledge Answers below
  for the full `/knowledge ask` retrieval, prompt, and
  citation-validation flow.
- **Confirmation (`ingest`).** See Knowledge Ingestion Confirmation below
  for how `/knowledge ingest`'s pending state is proposed, confirmed, and
  stored.
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

#### Grounded Knowledge Answers

`/knowledge ask` retrieves bounded local evidence via `retrieve_evidence()`
(see Ask-Specific Minimum-Term Matching above) and, only if evidence was
found, makes exactly one call to the injected model provider.
`kernel/knowledge_base/answer.py` assembles one flat prompt — fixed
instructions plus the untrusted question and evidence as one JSON object
between fixed marker lines — and strictly parses the model's required
`{"answer", "used_citations", "sufficient"}` response: any wrong shape,
invented or missing citation, inline/`used_citations` mismatch, or empty
answer is rejected outright, never partially trusted. Citation labels
(`S1`–`S5`) are assigned by code in retrieval order, never by the model,
and the appended "Sources:" section is generated entirely from
code-owned metadata. A provider exception (including a request timeout)
is caught and mapped to one fixed reply; the raw exception never reaches
the reply, the audit record, memory, or the interaction log. The
explicit `/knowledge ask` command is itself sufficient consent today,
since the only implemented provider is local Ollama — this must be
revisited before a remote provider is ever enabled for this operation.

#### Knowledge Ingestion Confirmation

`/knowledge ingest <key>` is sensitive and uses its own confirmation
flow, entirely separate from Task Confirmation Storage above: the
pending action lives in `default_knowledge_confirmation_store`, a
distinct `kernel.tools.confirmation.ConfirmationStore` instance from
`capabilities/tasks/TasksCapability`'s `default_store`, so a pending
`/knowledge ingest` proposal can never collide with, or be silently
evicted by, a pending `/task` action, and vice versa. The symbolic
source key is checked against the current approved-source allowlist
immediately; if approved, the reply proposes the action, naming only the
source key. `/knowledge confirm` within 2 minutes runs the existing,
unmodified `ingest_source()` exactly once, re-checking the allowlist at
execute time so a source removed after proposal but before confirmation
fails closed; `/knowledge cancel` or a 2-minute timeout discards it
instead. `ask` does not use this store, or Task Confirmation Storage, at
all — the two confirmation mechanisms are not interchangeable.

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
`storage/tasks/` (Milestone 40) holds `kernel/employee_tasks/`'s SQLite
database (`tasks.sqlite3`, plus its WAL/shared-memory/journal sidecar
files while a write is in progress) — `storage/tasks/*.sqlite3*` is
gitignored the same way `storage/knowledge/*.sqlite3*` is; only
`storage/tasks/README.md` is committed, and the database itself is never
created automatically.

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
- Milestone 38.2B — Heading-Aware Markdown Chunk Boundaries:
  `kernel/knowledge_base/chunking.py:chunk_normalized_text()` gains an
  `is_markdown` keyword (default `False`); `ingest.py` passes
  `is_markdown=True` only when a candidate's suffix is exactly `.md`
  (case-insensitively) — derived solely from the traversed file's
  suffix, never content sniffing or a config flag. When true, a valid
  ATX heading (`^ {0,3}#{1,6}[ \t]+\S.*$`) outside a fenced code block
  (3+ backticks/tildes, closed only by a same-or-longer run of the same
  fence character) is a hard chunk-packing boundary: the document is
  partitioned into sections at every heading's line start first, then
  each section is paragraph-packed independently — a heading always
  opens its section's first chunk, no chunk ever contains text from both
  before and after a real heading (even with no blank line before it),
  and overlap seeding is clamped to never search or start before the
  current section. `is_markdown=False` (every non-`.md` file, and this
  function's default) packs the whole document as a single section,
  byte-for-byte identical to chunking before this milestone. This is
  purely a chunk-boundary change — it does not touch `search.py`,
  `query.py`, `evidence.py`, the database schema, any provider, prompt,
  capability, command, or configuration, and it does not add
  heading-based ranking or expose heading metadata anywhere. Because
  unchanged documents are skipped by content hash (see Knowledge
  Ingestion above), already-indexed `.md` documents keep their
  pre-38.2B chunk boundaries, offsets, and chunk IDs until their content
  changes or a deliberate one-time index rebuild is performed after
  deployment — there is no chunker-version field and no automatic
  forced re-ingestion in this milestone. See Kernel above and
  `kernel/knowledge_base/README.md` for the full detail.
- Milestone 39 — Reliable Action Protocol: the deterministic-candidate
  action protocol described under Kernel above
  (`kernel/action_protocol/`), plus `ModelRequestOptions`
  (`kernel/models/base.py`) and the `ActionRegistry.descriptors()` view
  (`kernel/tools/registry.py`) it consumes. Model-generated tool names,
  resource keys, and arguments objects are prohibited by construction —
  the model may only ever select a code-generated `candidate_id` already
  offered to it for that request, never invent or alter one. This
  milestone has **no production caller**: nothing in
  `kernel/orchestrator/`, any `capabilities/`, or `interfaces/whatsapp/`
  invokes this package, no tool is executed by it, and no
  natural-language WhatsApp task is reachable through it yet — it is a
  self-contained, tested library. Persisted task state, a bounded
  planner, and planning orchestration are Milestones 40 and 41; the
  autonomous execution loop that acts on them is Milestone 42 (see those
  bullets below).
- Milestone 40 — Persistent Task Lifecycle: `kernel/employee_tasks/`, a
  new package giving durable task identity and lifecycle state — see
  Kernel above (Persistent Task Lifecycle) for the full design. Like
  Milestone 39, this milestone has **no production caller** upstream of
  it: nothing in `kernel/orchestrator/`, any `capabilities/`,
  `kernel/action_protocol/`, or `interfaces/whatsapp/` invokes this
  package. It persists task identity and lifecycle state (and, as of
  Milestone 41 P2, a validated plan) — no execution, no tool call, and no
  wiring to `kernel/tools/confirmation.py`'s pending-action store
  (`waiting_for_confirmation` is only a persisted state).
- Milestone 41 — Bounded Task Planner (P1) and Planning Orchestration
  (P2): `kernel/task_planner/` and `kernel/task_orchestration/` — see
  Kernel above (Bounded Task Planner, Planning Orchestration) for the
  full design. Takes one persisted `employee_tasks` Task and produces a
  bounded, validated plan via exactly one structured-output model call,
  then persists it and transitions the task to `ready` (or `failed`) —
  never executing anything. `gemma3:12b` is the empirically-selected,
  dedicated planner model (97.0% strict semantic-plan accuracy against a
  33-request corpus, versus 87.9% for both `llama3.1:8b` and
  `qwen3:14b`, below the required 90% gate); it is recorded in
  `kernel/config/config.yaml`'s `planner` section but not yet constructed
  or called by anything — the general conversational provider remains
  unchanged. Like Milestones 39 and 40, this milestone has **no
  production caller**: nothing creates a task or triggers planning from a
  real request. The autonomous execution loop that decides *when*/*next*
  and performs the doing is Milestone 42, below.
- Milestone 42 — Autonomous Execution Loop: `kernel/task_execution/` —
  see Kernel above (Autonomous Execution Loop) for the full design.
  Delivered across three phases: durable per-step progress and
  deterministic, model-free next-step eligibility (P1); action execution
  through `SafeTaskExecutor` and durable, task-scoped confirmation for
  sensitive actions, replacing nothing of the existing in-memory
  `kernel/tools/confirmation.py` store (P2); RESPOND-step synthesis
  through an injected general conversational `ModelProvider` — never the
  dedicated planner provider — plus `run_task_until_blocked()`, a bounded
  driver over the one-step `advance_task_execution()` primitive (P3). Like
  Milestones 39-41, this milestone has **no production caller**: nothing
  in `kernel/orchestrator/`, any `capabilities/`, or
  `interfaces/whatsapp/` invokes it, and no task is ever advanced outside
  a test. Real task submission, runtime wiring, a scheduler, WhatsApp
  result delivery/confirmation routing (Milestone 46), and recovery/
  reconciliation of an uncertain `in_progress` step from a process crash
  mid-action (Milestone 47) are explicitly not this milestone's concern —
  a claimed step whose terminal result was never persisted is left
  durably `in_progress` and is never retried or skipped by this layer.
- Milestone 43 — Core Computer Worker: **implemented**. Adds five
  registered actions to `kernel/tools/registry.py` — `ActionRegistry` now
  holds eleven actions in total. P1 (Read-Only Inspection) adds three
  non-sensitive actions — `file_metadata`, `read_text_file`,
  `list_processes` — plus a new exact-file `approved_files` resource
  section on `ToolsConfig` (`kernel/tools/config.py`,
  `kernel/tools/file_safety.py`). P2 (Bounded File Mutations) adds two
  sensitive, create-only actions — `create_directory` and `copy_file` —
  each authorized by one pre-configured composite `resource_key`
  (`create_directory.approved_directory_creations`,
  `copy_file.approved_copies`) naming an entire operation (parent
  directory + child name, or source + destination directory + destination
  name) by reference to already-approved `approved_directories`/
  `approved_files` entries — never a caller/model-supplied path,
  filename, or overwrite flag — and reusing `kernel/tools/
  atomic_finalize.py`'s existing no-replace finalize and
  `kernel/tools/file_safety.py`'s symlink/reparse-point rejection rather
  than introducing new safety primitives. Both flow through M42's
  existing durable confirmation unchanged. P3 (Integration, Security
  Acceptance, and Closure) adds no new registered action: it consists of
  whole-milestone acceptance and security regression tests proving the
  P1+P2 capability set is correctly bounded, planner-visible,
  execution-safe, confirmation-safe, config-authorized, persistence-safe,
  and audit-private as one coherent milestone, plus this documentation
  closure.

  **Process termination/control was deliberately evaluated and excluded**
  from Milestone 43 (see the P3 design pass): `list_processes` remains
  informational only, carrying no execution authority; AI-OS retains no
  durable process-launch provenance (`open_application` discards the PID
  it receives, so a later "process matching this approved application" is
  never provably the instance AI-OS itself launched rather than one the
  user opened); on Windows, `psutil`'s `terminate()` is documented as an
  alias for `kill()` — an immediate, forceful termination with no
  cooperative graceful-shutdown path an application could use to save
  unsaved state; and terminating an arbitrary application could destroy
  pre-existing user work with no undo. Safe launch ownership and
  reconciliation would require a broader lifecycle design outside this
  milestone's scope. Explicitly deferred, not part of Milestone 43:
  process termination/control, rename, move, delete/trash, arbitrary
  text/file writing, arbitrary shell/PowerShell, browser automation, and
  native GUI automation.

- Milestone 44 — Browser Worker: **IMPLEMENTED.** Adds one registered
  action to `kernel/tools/registry.py` — `ActionRegistry` now holds twelve
  actions in total — `browser_read_page`, non-sensitive, requiring no
  confirmation, the same read-only trust tier as `read_text_file`/
  `file_metadata`. A new `kernel/tools/browser_safety.py` (URL/origin
  parsing and normalization, private/local-network rejection, DNS
  defense-in-depth, and the pure request-allowlist function shared by
  production and by tests) and a new `ToolsConfig.approved_pages` section
  (`kernel/tools/config.py`, `ApprovedPageSpec`) authorize exactly one
  canonical HTTPS page URL per symbolic key, plus an optional, small
  (at most `browser_safety.MAX_STYLESHEET_ORIGINS` = 5), explicit list of
  additional HTTPS origins authorized for stylesheet `GET` requests
  ONLY — never for document navigation, not even on the page's own
  origin (an admin who wants the page's own origin to also serve its
  stylesheet must list it explicitly). `browser_read_page`
  (`kernel/tools/handlers/browser_read_page.py`) renders that one page in
  an isolated, headless, single-use Playwright/Chromium browser context
  with `java_script_enabled=False` (fixed, code-owned, not
  configurable — this is the design's core safety property: no page
  script can ever execute, so no `fetch()`/XHR/WebSocket construction/
  `navigator.serviceWorker.register()`/JS navigation/`window.open()` can
  ever be attempted in the first place) and `service_workers="block"` as
  additional defense in depth (empirically: this does not make a page's
  own `register()` call reject, but does prevent a worker from ever
  gaining page-control or fetch-interception authority). WebSockets are
  unsupported specifically *because* page JavaScript is disabled — not
  because Playwright's own `route_web_socket()` mechanism is used (it was
  found, empirically, to hang the browser context in this exact
  environment/version and is not used anywhere in this codebase).

  **Exact document identity is enforced through a semantic URL comparison,
  never raw string equality** — an adversarial security review found that
  Chromium's own reported `Request.url` omits a default HTTPS port even
  when the configured URL included it explicitly, which a naive string
  comparison would have silently and completely broken for any ordinarily-
  written config. `kernel/tools/browser_safety.py`'s
  `NormalizedDocumentURL`/`parse_document_url()` parse both the configured
  page URL and every live request URL into the same structured
  (scheme, normalized host, default-filled port, path, query) form before
  comparison, so an omitted and an explicit default port are correctly
  treated as identical while a genuinely different port remains distinct.
  A configured path containing a `.`/`..` segment (including
  case-insensitive percent-encoded forms) or a backslash is rejected
  outright at config-validation time, rather than this module attempting
  to emulate Chromium's own dot-segment-resolution/backslash-to-slash
  rewriting.

  **HTTP redirects are categorically unsupported — a deliberate design
  choice, not a temporary limitation**, found necessary after Playwright's
  `context.route()` was empirically shown not to reliably re-invoke for a
  redirected request's target (a confirmed, currently-open upstream
  Playwright limitation). Every request the context-wide gate permits —
  exactly one main-frame `GET` document (the configured URL, matched in
  full, exactly once per action — no HTTP redirect, meta refresh, or any
  other secondary main-frame navigation is ever followed) or a `GET`
  stylesheet from an explicitly authorized origin — is fetched via
  `route.fetch(max_redirects=0, timeout=browser_read_page._FETCH_TIMEOUT_MS)`
  (an explicit, code-owned 5-second timeout — Playwright's own `route.fetch()`
  default is an unrelated 30 seconds, confirmed empirically to be governed
  by neither `page.set_default_timeout()` nor
  `set_default_navigation_timeout()`) and inspected before ever being
  fulfilled into Chromium; any redirect, non-2xx status, wrong
  Content-Type, or oversized response fails the read closed. Every other
  resource class (script, image, font, media, iframe/subframe document,
  XHR, fetch, beacon, prefetch, object, embed, manifest, favicon,
  WebSocket, or anything else) is denied by the same small, fixed
  ALLOWLIST — never a denylist that could omit a class its authors never
  thought of.

  **An action-wide stylesheet count and cumulative-byte bound close a gap
  the per-response cap alone did not**: a real-browser reproduction during
  adversarial review proved a 20-level recursive CSS `@import` chain and a
  50-`<link>` page both passed through completely unrestricted before
  this. `browser_safety.MAX_STYLESHEET_REQUESTS` (8) is checked and
  consumed *before* `route.fetch()` is ever called for the next
  stylesheet — an excess request is aborted before any network dispatch —
  and `browser_safety.MAX_TOTAL_STYLESHEET_BYTES` (256 KiB) tracks the
  authoritative, decompressed byte total of every stylesheet fulfilled so
  far in the action (never `Content-Length`, which is advisory-only:
  compression makes it diverge arbitrarily from the actual decompressed
  size `APIResponse.body()` returns — confirmed empirically with a
  519-byte response decompressing to 500,000 bytes). Exceeding either
  bound fails the *whole* action closed once navigation completes, never a
  silent partial-styling success. No CSS parsing of any kind happens in
  Python — the request gate itself is the bound.

  Output (`Page:`/`Location:`/`Title:`/`Content:`) is bounded (title/
  location/visible-text each independently capped, empirically proven via
  the real `build_action_observation()`/`serialize_observation()` pipeline
  to fit `MAX_STEP_RESULT_JSON_CHARS` even in the worst JSON-escaping
  case) and excludes query strings, fragments, cookies, headers, raw HTML,
  iframe text, and any raw exception detail. A response-buffering residual
  risk is documented honestly: Playwright 1.62.0's `route.fetch()` has no
  hard pre-buffering byte ceiling, so the actual, decompressed
  `len(body)` — never `Content-Length` — is the sole authoritative bound,
  checked after fetch and before fulfillment; neither it nor the request-
  count/cumulative-byte bounds above prevent the underlying fetch's own
  network/memory/decompression cost inside Playwright's Node driver
  process for any *one* response, which has already been paid by the time
  any check runs — accepted, bounded in scope and duration (not
  eliminated), for a personal, single-action-at-a-time system whose whole
  browser process is destroyed at the end of every action.

  **DNS defense-in-depth resolution runs in a real, killable OS
  subprocess, not a Python thread.** `kernel/tools/dns_resolver_worker.py`
  is a small, fixed, zero-project-import leaf script invoked via
  `kernel/tools/process_control.py`'s `run_capturing_stdout()` — the same
  `shell=False`, list-form-argv, full-process-tree-kill-and-reap primitive
  already used by `open_application`/`run_registered_script`/
  `repo_health`/`repository_backup`. This replaced an initial
  `concurrent.futures.ThreadPoolExecutor`-based implementation that
  adversarial review proved did NOT actually bound wall-clock time: a hung
  resolver call left the executor's own context-manager exit
  (`shutdown(wait=True)`) blocking for the resolver's full real duration
  regardless of the configured timeout, since a Python thread blocked
  inside a C-level socket call cannot be cancelled from another thread — a
  real OS process can be, and now is. A DNS-rebinding residual risk is
  documented honestly regardless: this defense-in-depth check is not full
  DNS pinning — Chromium performs its own, independent resolution
  afterward. No authenticated or persistent session of any kind exists —
  one fresh, isolated browser context per action, always fully closed at
  the end of that one action, never the user's real Chrome/Edge profile;
  a server may set a cookie reused by a later *permitted* request within
  that same action's own request sequence (ordinary browser behavior),
  but the entire context (and any such cookie state) is destroyed
  afterward. No `TaskPlan`/planner-schema change was needed —
  `browser_read_page` fits `action_name` + one `resource_key` exactly like
  every action since Milestone 33, and joins the planner's named-
  capability-grounding set (`kernel/task_planner/catalog.py`) for the same
  reason `file_metadata`/`read_text_file` do.

  **Milestone 44 closes here, deliberately, without bounded browser
  interaction.** Clicks, form submission, downloads, uploads, screenshots,
  a separate `browser_open_page`/`browser_list_links` action, arbitrary
  URLs or selectors, authenticated browsing, and headed mode were all
  evaluated and are not implemented. This is not an unfinished phase of
  this milestone — it is this milestone's completed scope. Safe execution
  authority for JavaScript-enabled or otherwise interactive browser
  behavior (page JavaScript, WebSockets, workers, dynamic DOM identity,
  selectors, clicks, forms, state-changing navigation, authenticated
  sessions, downloads/uploads) requires its own explicit security design;
  it is deferred beyond the current Browser Worker milestone, not
  "scheduled" as a numbered phase of it.

- Milestone 45 — Windows Desktop Worker: **IN PROGRESS.** P1 (Windows
  Desktop Foundation and Exact Target/Control Status) is **implemented.**
  Adds two registered, read-only, non-sensitive actions to
  `kernel/tools/registry.py` — `ActionRegistry` now holds fourteen actions
  in total — `desktop_target_status` and `desktop_control_status`. Each
  reports one of a small set of fixed, code-owned states
  (`"available"`/`"unavailable"`/`"ambiguous"`, plus two distinct
  failure states — see CHECK_FAILED/AUTOMATION_UNAVAILABLE below) for one
  already-approved, exactly-matched Windows desktop target/control —
  never a window title,
  control text, AutomationId, ClassName, PID, HWND, process path, or match
  count. A new `kernel/tools/desktop_safety.py` (Windows UI Automation
  identity resolution and config-load-time locator validation) and two new
  flat, top-level `ToolsConfig` sections
  (`approved_desktop_targets`/`approved_desktop_controls`,
  `kernel/tools/config.py`) authorize exactly one Windows window/control
  per symbolic key, following the same "shared, not duplicated" and
  composite-operation precedent `browser_safety.py`/`approved_pages` and
  `create_directory`/`copy_file` already established.

  **Foundation: `pywinauto` (`uia` backend), added as a new project
  dependency.** Chosen after empirically proving, on the project's actual
  Python 3.14.6 interpreter, that it imports and constructs cleanly, that
  its low-level `findwindows.find_elements()` API performs exact,
  complete-enumeration property matching (`class_name`, `control_type`,
  `auto_id`) with no dependency on any fuzzy/best-match/index-based
  convenience API, and that a direct `comtypes`/UIA COM fallback also
  works independently (proving a future pywinauto-wrapper-specific failure
  would mean "wrapper incompatible," never "UIA unavailable"). Coordinates,
  mouse movement, and keyboard/text-input APIs are never imported or
  called anywhere in this milestone's production code — proven
  mechanically, not just by design intent (see NO MUTATION below).

  **Window/control identity model — exact, config-owned, never
  fuzzy/positional/text-based.** A target is
  `(application launch reference [`approved_applications`] + a SEPARATE,
  independently-required `process_executable` runtime-image path +
  `window_class_name` + optional `window_automation_id`)`; a control is
  `(target reference + REQUIRED `control_automation_id` + `control_type` +
  optional `control_class_name`)`. There is no title field, no title
  matching of any kind, and no control Name/displayed-text matching of any
  kind anywhere in this model — both are treated as untrusted, potentially
  private runtime data, never authority (see `desktop_safety.py`'s own
  module docstring, AUTHORITY MODEL and TITLE/TEXT EXCLUSION sections).
  `control_automation_id` is **required**, not optional: empirical
  validation proved a real Tkinter application's sibling controls of the
  same type share an *empty* `automation_id` and an *identical*
  `class_name`/`control_type` — class/type alone cannot distinguish them.
  An application/control that exposes no stable AutomationId is
  unsupported by this milestone; this module never falls back to visible
  text, sibling index, or position. `control_type` is validated at
  config-load time against a small, closed, code-owned allowlist of UIA
  control types (`desktop_safety.SUPPORTED_CONTROL_TYPES`), extended
  deliberately, never implicitly.

  **Launch path is not runtime process identity — a load-bearing
  distinction, proven empirically, not assumed.** A configured
  application's *launch* path
  (`approved_applications[*].executable`) can be a real, separate
  executable from the one Windows reports as actually *owning* a live
  window's process (`psutil.Process(pid).exe()`) — e.g. a virtual
  environment's `Scripts\python.exe` launcher versus its own base
  interpreter, empirically reproduced during this milestone's validation
  pass. `approved_desktop_targets[*].process_executable` is therefore a
  separate, independently-required, config-owned field — never derived
  from `approved_applications`. The runtime comparison
  (`desktop_safety.compare_windows_executable_identity()`) never uses raw
  string equality or `os.path.realpath()`/`Path.resolve()` (neither
  reliably resolves a Windows launcher/redirector's real target) — it
  opens both paths read-only (metadata query only, zero content bytes
  read) and compares Windows' own file identity
  (`GetFileInformationByHandle`'s volume-serial-number + file-index
  triple), which correctly treats a launcher and the distinct file it
  starts as different files, exactly as they are, while correctly
  treating a hard link to the same NTFS file as the SAME identity (it is,
  byte-for-byte, the same file under another name — never a content hash,
  so a byte-identical copy at a different path is correctly NOT the same
  identity). This three-way comparison — `MATCH`/`NO_MATCH`/`CHECK_FAILED`
  — is deliberately not a bare boolean at the point it matters (see
  COMPLETE AUTHORITY BEFORE CARDINALITY below): an inspection failure
  (access denied, an unexpected Win32 error) must never be silently
  reported as a proven `NO_MATCH`. `same_windows_executable()` remains
  available as a boolean convenience wrapper for simple callers that
  collapses `CHECK_FAILED` into `False`.

  **Complete authority before cardinality — corrected after an
  adversarial review found and reproduced the original ordering bug.**
  The configured target authority is the FULL combination of (exact UIA
  window locator) AND (configured runtime executable identity) AND
  (usable state — visible, not minimized). The initial implementation
  computed cardinality on the raw UIA-locator match count *before* ever
  checking process identity or minimized state — reproduced live: two raw
  candidates sharing a configured window class, only one of which
  belonged to the correct process (or only one of which was not
  minimized), were both reported ambiguous without the process-identity/
  minimized check ever running on either one, meaning an unrelated
  process merely exposing the same window class could suppress a
  legitimately-available target's status. `desktop_safety.py`'s
  `_resolve_target_internal()` now evaluates **every** raw UIA candidate
  against the complete authority first, classifying each as `QUALIFIED`,
  `NON_QUALIFYING` (wrong process, minimized, or its owning process
  cleanly exited — `psutil.NoSuchProcess`), or `INSPECTION_FAILED` (an
  unexpected `AccessDenied`, Win32, or file-identity-comparison failure —
  see `_evaluate_target_candidate()`) — cardinality is computed only over
  the `QUALIFIED` set: zero → unavailable, exactly one → available, two or
  more → ambiguous. If **any** candidate is `INSPECTION_FAILED`, the whole
  result is `CHECK_FAILED` unconditionally, regardless of how many other
  candidates already qualified — exact cardinality cannot be proven while
  any candidate remains unresolved. A control can only be available if its
  target independently resolves to exactly `AVAILABLE` first; an ambiguous,
  unavailable, check-failed, or automation-unavailable target now
  **propagates that exact status unchanged** to the control (a control
  scoped to an ambiguous target is itself ambiguous — it cannot be
  uniquely resolved, for a documented reason — rather than being
  collapsed to a less-accurate "unavailable"). Hidden (withdrawn) targets
  are not resolvable through this path at all (proven empirically — a raw
  Win32 `EnumWindows` cross-check confirms the window still exists at the
  OS level; UIA's own element tree simply never surfaces it), so no
  special case was needed to reject them.

  **`DesktopStatus` has five values, not three — infrastructure failure is
  never ordinary absence.** `AVAILABLE`/`UNAVAILABLE`/`AMBIGUOUS` all mean
  the check itself *succeeded*. `CHECK_FAILED` (the system could not
  reliably perform the configured check — an unexpected Win32/COM/psutil
  failure, or a malformed spec caught by runtime revalidation before any
  live call) and `AUTOMATION_UNAVAILABLE` (the platform/UIA foundation
  itself is unavailable — not running on Windows) are now distinct,
  reachable outcomes (`ActionResult(success=False, outcome="failed", ...)`)
  — an adversarial review proved the original implementation collapsed
  every one of these into ordinary `UNAVAILABLE`, meaning a fully broken
  UIA stack was silently indistinguishable from "the application isn't
  there." Only genuinely-unregistered symbolic keys or a broken
  `application`/`target` reference remain `outcome="rejected"`, unchanged.

  **Runtime spec revalidation closes the manually-built-`ToolsConfig`
  gap.** `_revalidate_target_spec()`/`_revalidate_control_spec()` re-run
  the exact same field validation `kernel/tools/config.py` runs at
  config-load time, again at execution time, before any live UIA call —
  never trusting that a caller-supplied spec necessarily passed through
  `load_tools_config()`. This closes a gap an adversarial review
  reproduced live: a hand-built `DesktopControlSpec` with an empty
  `control_automation_id` (which `load_tools_config()` already rejects,
  but a manually-constructed one could bypass) reached
  `find_elements(auto_id="")` directly, which genuinely matches real
  controls with an empty AutomationId (proven against the fixture — 8 real
  matches, including window-chrome buttons). An invalid manually-built
  spec now fails `CHECK_FAILED` before `find_elements()` is ever called,
  for every locator field, not merely "probably won't match anything."

  **NO MUTATION — empirically evaluated and REJECTED for this
  milestone.** `desktop_invoke_control`, `desktop_click`,
  `desktop_type_text`, `desktop_hotkey`, `desktop_set_control_value`, and
  `desktop_close_window` are not implemented. UI Automation's
  `InvokePattern` was proven callable, via a pure COM code path with no
  mouse/keyboard/focus call anywhere in pywinauto's own implementation —
  but invoking it against the validation fixture's native Win32 button
  empirically (a) changed the foreground window despite no explicit
  activation call, and (b) did not reliably trigger the target
  application's actual behavior at all. A dedicated AST-based static
  acceptance test
  (`tests/kernel/tools/test_desktop_no_mutation_static_acceptance.py`)
  proves this milestone's production code contains no reference,
  anywhere, to `click`/`click_input`/`invoke`/`iface_invoke`/`type_keys`/
  `send_keys`/`SendInput`/`SetForegroundWindow`/`set_focus`/
  `set_edit_text`/`set_value`/`screenshot`/`capture`/`close`/`kill`/
  `terminate`, and no import of `keyboard`/`mouse`/`pyautogui` — this is a
  mechanical property of the code, not merely current behavior. **P3
  (Security Acceptance and Closure) has not yet run — Milestone 45 is not
  complete.**

**Planned / not yet implemented:**

- The remainder of Milestone 45 (P3 — Security Acceptance and Closure;
  no new capability), Milestone 46 (WhatsApp
  Task Control — real task submission, result delivery, and
  confirmation-reply routing), Milestone 47 (persistence
  recovery/reconciliation, especially an uncertain `in_progress` external
  action left behind by a process crash), and Milestone 48 (employee
  acceptance/launch). None of this exists yet; do not treat any of these
  names as implemented. Milestone 44 does not, and must not, absorb any
  of this scope — a future JavaScript-enabled or interactive browser
  capability is a new, separately-designed feature, not a hidden part of
  any of these four. Milestone 45 will close as a read-only Windows
  Desktop Worker — no mutation phase is planned (see above).
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
