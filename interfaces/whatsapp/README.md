# WhatsApp

Interface for interacting with the AI Operating System through WhatsApp, via
Meta's WhatsApp Business Cloud API. Two message families are handled here:

- **Ordinary chat** - turned into a call into the kernel `Orchestrator` and
  the response turned back into an outbound WhatsApp text message. No
  domain logic of its own for this path.
- **Task control** (`/task ...`, `CONFIRM <id>`, `REJECT <id>` - Milestone
  46) - this interface owns transport-specific durable task *ingress*,
  confirmation-command *parsing*, and worker *dispatch wiring* for this
  channel. It does **not** own task business logic, execution policy, or
  action authority - those remain entirely with `kernel/employee_tasks/`
  (`TaskRepository`), `kernel/task_planner/`, `kernel/task_execution/`, and
  `kernel/tools/` (`ActionRegistry`, `ToolsConfig`, `SafeTaskExecutor`). See
  "Task control" below for the full command surface and security model.

**Single-user only**: exactly one WhatsApp sender is authorized - there is
no allow-list and no multi-user support.

## Running it

```
python -m interfaces.whatsapp.server
```

This starts a **loopback-only** HTTP server, bound to `WHATSAPP_HOST`
(default `127.0.0.1`; any non-loopback address is rejected at startup -
see Configuration). Meta's Cloud API requires a publicly reachable
**HTTPS** webhook URL, so a real deployment puts a TLS-terminating reverse
proxy or tunnel in front of this process; the server itself never speaks
TLS and is never meant to be reachable directly from the internet.

## Configuration

All configuration is environment variables, validated at startup by
`interfaces/whatsapp/config.py` - a missing or invalid value fails
immediately with a clear error rather than partway through handling a
webhook request. See `.env.example` for the full list (every value there
is a synthetic placeholder):

- `WHATSAPP_VERIFY_TOKEN` - shared secret used only for the GET webhook
  verification handshake.
- `WHATSAPP_APP_SECRET` - Meta app secret; used to validate the
  `X-Hub-Signature-256` HMAC-SHA256 signature on every inbound POST.
- `WHATSAPP_ACCESS_TOKEN` - bearer token for outbound Cloud API calls.
- `WHATSAPP_PHONE_NUMBER_ID` - the business phone number ID this server
  answers for; also used to reject webhooks addressed to a different
  number.
- `WHATSAPP_AUTHORIZED_SENDER_ID` - the single WhatsApp phone number
  (digits only, wire format) authorized to message this bot. Matched by
  exact string equality only - no normalization, no list, no second user.
  Any other sender is dropped without a reply.
- `WHATSAPP_API_VERSION` - **required, no code default.** There is no
  hardcoded fallback Cloud API version anywhere in this interface -
  verify the currently supported version in Meta's developer docs during
  operational setup and set it explicitly (e.g. `v23.0`).
- `WHATSAPP_HOST` - optional, defaults to `127.0.0.1`. Validated as
  loopback-only at startup: `0.0.0.0`, LAN addresses, public addresses,
  and arbitrary hostnames are all rejected. Only a loopback IP literal
  (`127.0.0.0/8`, `::1`) or `localhost` is accepted. An IPv6 loopback host
  (`::1`) is bound with a genuine `AF_INET6` server (`server.py` selects
  it automatically) - not silently mishandled by an IPv4-only socket.
- `WHATSAPP_PORT` - optional, defaults to `8000`.

Task control (below) additionally uses `kernel/config/tools.yaml` (the
current `ActionRegistry`/`ToolsConfig` authority - reloaded fresh before
every execution or approval, never cached from startup) and the same
task database `kernel/employee_tasks/` already uses for every other
interface - there is no WhatsApp-specific task store.

`WhatsAppConfig` itself is a frozen dataclass (`@dataclass(frozen=True)`)
- assigning to a field after construction raises `FrozenInstanceError` -
and its `repr()` deliberately excludes `verify_token`, `app_secret`,
`access_token`, `phone_number_id`, and `authorized_sender_id`; only
`host`, `port`, and `api_version` are visible if the object is ever
logged or printed by accident. Validation error messages never echo the
invalid value back for those same secret/personal fields (`host`, `port`,
and `api_version` are not secrets, so their error messages may include
the offending value).

## Endpoints

Only `GET /webhook` and `POST /webhook` exist. Every other path returns
`404`. `PUT`, `DELETE`, `PATCH`, `HEAD`, and `OPTIONS` on `/webhook` return
`405`; the same methods on any other path return `404` or `405`. Neither
case ever falls through to `http.server`'s default error page (which
would echo the request method/path back into an HTML body) - every
response, success or failure, has an empty body produced by this
interface's own code.

- `GET /webhook` - Meta's webhook verification handshake: requires
  `hub.mode=subscribe`, a present `hub.challenge`, and `hub.verify_token`
  matching `WHATSAPP_VERIFY_TOKEN` via `hmac.compare_digest` (constant-time,
  not `==`) - and echoes back the raw challenge as `text/plain` on
  success. Anything else gets `403`. The query string (which carries the
  token and challenge) is never logged, including in the default request
  log line.
- `POST /webhook` - inbound message delivery. Processing order, all
  synchronous within the request (see `interfaces/whatsapp/server.py`'s
  module docstring for the full rationale):
  1. Reject if `Content-Length` is missing (`411`), malformed or negative
     (`400`), over the configured body-size limit (`413`, without reading
     the body), or the body actually received is shorter than declared
     (`400`).
  2. Verify the raw-body `X-Hub-Signature-256` signature - accepted only
     as `sha256=` followed by exactly 64 hex characters, compared with
     `hmac.compare_digest` (`403` if invalid, malformed, or missing) -
     **before** any JSON parsing, and before any task/confirmation
     operation of any kind.
  3. Parse the JSON body (`400` if malformed).
  4. For each parsed message: validate the destination phone number ID,
     validate the sender against `WHATSAPP_AUTHORIZED_SENDER_ID` (exact
     match only). An unauthorized sender or wrong destination is dropped
     silently (`200` - not `403`; from Meta's perspective delivery still
     succeeded, since retrying wouldn't change anything) - **no dedup
     reservation, no durable acceptance, and no queueing ever happens for
     a message that fails this check.**
  5. The message is classified (see "Task control" below for the full
     grammar). A `/task <request>` message is durably accepted here,
     synchronously, before this request can return success - see
     "Durable `/task` acceptance". Every other message (ordinary chat, a
     fixed `/task`/`CONFIRM`/`REJECT` reply, `CONFIRM <id>`/`REJECT <id>`
     itself) is deduplicated via the in-memory `SeenMessageCache` and
     submitted to the bounded worker queue.
  6. If the queue is full (or, for a `/task` message, if durable
     acceptance itself fails), any reservation already taken is released
     and the response is `503` - never `200` - so Meta retries the
     delivery. Processing of the rest of the batch stops at that point;
     work already queued/accepted earlier in the same batch is
     unaffected, so a full redelivery doesn't reprocess it twice.

  The response only ever waits on these fast, synchronous checks (plus,
  for `/task`, one bounded database write) - never on the orchestrator,
  the planner, task execution, action execution, or an outbound Cloud API
  call, all of which happen afterward, in the background worker.

## Task control (Milestone 46)

A second command family, alongside ordinary chat, for durable,
multi-turn tasks that may need to execute a real computer action.

### Command surface

| You send | What happens |
|---|---|
| any ordinary text | conversational path (`Orchestrator.handle()`) |
| `/task <what you want done>` | durable task-control path - accepted, planned, and executed in the background |
| `/task` or `/task help` | fixed help reply - never touches the task database |
| `/task confirm` or `/task cancel` | fixed migration notice pointing at `CONFIRM`/`REJECT` - **not** a call into the legacy Milestone 33 `kernel/tools/confirmation.py`/`TasksCapability` confirmation system, which this channel no longer reaches at all |
| `CONFIRM <confirmation_id>` | approve the exact pending confirmation identified by that id |
| `REJECT <confirmation_id>` | reject the exact pending confirmation identified by that id |

Recognition is a deterministic, case-insensitive prefix match on a fixed
grammar (`interfaces/whatsapp/task_control.py:classify_task_text()` /
`classify_confirmation_text()`) - **no LLM involvement, no fuzzy
matching**. There is no fuzzy/synonym confirmation vocabulary: `yes`,
`y`, `ok`, `approve`, `no`, `cancel`, or any other bare word is never
treated as a decision - only the exact `CONFIRM <id>` / `REJECT <id>`
shape, with the id copied verbatim from the confirmation request message,
is recognized. Anything else falls through to ordinary chat.

### Durable `/task` acceptance

A new `/task <request>` is durably persisted as a `TaskRecord`
**before** this interface ever returns HTTP success for it - the SQLite
`UNIQUE` constraint on `tasks.dedup_key` (derived from the provider
message ID, never the raw ID itself) is the authoritative concurrent-dedup
boundary for this path, not `SeenMessageCache`. A redelivered `/task` with
the same provider message ID always resolves to the same durable task
rather than creating a duplicate. Planning and execution then happen
asynchronously, entirely on the background worker thread - never on the
request thread.

### Execution and the confirmation gate

Once accepted, the worker plans the task, then executes it step by step
exclusively through `kernel.tools.executor.SafeTaskExecutor` - the one and
only action-execution boundary this interface (or the task engine itself)
ever calls; nothing here calls a tool handler directly. Before every
non-sensitive step, and again at every approval, the **current**
`ActionRegistry`/`ToolsConfig` (reloaded fresh from `tools.yaml`, never a
stale planning-time snapshot) is what decides whether an action/resource
is valid and whether it is sensitive - never the persisted plan's own
recorded flag.

A step the current registry marks sensitive **never executes
automatically**. Instead the task durably enters
`WAITING_FOR_CONFIRMATION` and you receive a message like:

```
Confirmation required.

Action: open_application
Resource: notepad

Reply:
CONFIRM 018f...  (an opaque, single-use id)
or
REJECT 018f...
```

- `CONFIRM <id>` re-validates the action/resource against the **current**
  configuration one more time (a resource approved when the task was
  planned may have since been removed), then - during normal, live-process
  handling - invokes the approved action once for that now-consumed
  confirmation. The confirmation id itself is **consume-once**: a replay
  can never re-authorize a second invocation (see the durability section
  below for the separate, weaker question of what M46 guarantees about
  that one invocation's side effects surviving an actual process crash). A
  task's plan may contain more than one sensitive step; each proposes a
  **new, distinct** confirmation id when the worker reaches it - there is
  no limit on how many confirmation rounds one task can go through.
- `REJECT <id>` cancels the task. It never executes anything - denial
  never needs current-config revalidation.
- A confirmation id is **consume-once**: once resolved (approved,
  rejected, or expired), replaying the exact same `CONFIRM`/`REJECT`
  text again produces the same generic reply as an unknown id - it can
  never approve or cancel a second time.
- Multiple tasks may be simultaneously pending at once (e.g. two separate
  `/task` requests each waiting on their own confirmation) - each is
  addressed by its own distinct confirmation id, independently.
- A `CONFIRM`/`REJECT` is only ever honored if the id resolves to a task
  whose `source` is `"whatsapp"` **and** whose state is still
  `WAITING_FOR_CONFIRMATION` - a valid id belonging to some other
  channel's task, or one already resolved, is rejected exactly like an
  unknown id.
- Every one of these failure cases - malformed command shape, unknown id,
  wrong-source id, or an id whose pending row is already gone (already
  approved, already rejected, or already expired-and-resolved by an
  earlier check) - produces the **identical** fixed reply ("That
  confirmation is no longer valid.") so a reply can never be used to
  probe which case actually happened. This is distinct from a `CONFIRM`
  that arrives for a task still genuinely `WAITING_FOR_CONFIRMATION` but
  whose confirmation window has *just* passed: that is resolved through
  the engine's own expiry handling - the task fails closed
  (`confirmation_expired`) and you receive a normal, bounded
  task-failure message, not the generic invalid reply. `REJECT` is
  asymmetric here: a REJECT against that same still-pending, expired row
  still cancels the task, since denial never authorizes execution.

Whatever the task's execution produces next - a final result, a failure,
a new confirmation request, or a cancellation - is delivered as exactly
one WhatsApp message, through the same delivery path P2A's terminal
results already use. There is never a separate "confirmation accepted"
acknowledgement on top of that.

### Security boundaries

- A `CONFIRM`/`REJECT` (like every message) is only ever processed after
  a valid Meta signature and the exact configured `WHATSAPP_AUTHORIZED_SENDER_ID`
  have already been checked - a bare confirmation id, by itself, proves
  nothing and is never sufficient on its own (it must also resolve to a
  `source="whatsapp"`, still-`WAITING_FOR_CONFIRMATION` task).
- The outbound recipient for every task-control message is always the
  one fixed, configured authorized sender - never derived from task data,
  the confirmation lookup, or model output. The model never selects a
  recipient, and never interprets a `CONFIRM`/`REJECT` command at all.
- Resolving, authorizing, approving, denying, and executing a
  confirmation all happen exclusively on the background worker thread,
  never the webhook request thread - `approve_task_confirmation()` may
  run a sensitive action synchronously and must never be reachable from
  a request handler.
- A replayed `CONFIRM` can never execute an action twice - the durable
  consume step and the claim of the step to execute happen atomically in
  the same database transaction, before the action ever runs.

### Durability and crash-window language (read precisely)

- For **`/task`**: durable acceptance happens *before* this interface
  ever returns HTTP success, so the request is never lost as an
  unidentified/unrecorded message - the `TaskRecord` itself always
  survives a crash at that point. If the process dies after that HTTP
  success but before the worker actually processes the queued
  `TaskExecutionWork`, the queued item itself does not survive the crash
  (it only ever existed in memory) and the task can sit `CREATED` (or
  `planning`/`ready`/`running`) durably, with nothing left to process
  it - but, as of Milestone 47 P3, this is no longer indefinite and no
  longer requires an external re-dispatch trigger: a periodic, bounded
  recovery checkpoint (`run_task_state_recovery_checkpoint()`) discovers
  and resumes a stranded task automatically, through the exact same
  `dispatch_task_work()` path a normal dispatch uses - never a separate
  recovery-specific execution path or executor. This discovery is based
  entirely on durable database state, not on inspecting the worker's own
  in-memory queue - outside the pure post-crash case just described, an
  ordinary, currently-queued `TaskExecutionWork` item for the SAME task
  may legitimately coexist with a P3-eligible row (see "What this
  interface does not guarantee" below for why that is safe, not a race).
  A task stranded mid-`planning` is
  explicitly re-armed (`planning -> created`) for a genuinely fresh
  planning attempt, never assuming the interrupted model call's outcome.
  A retried/redelivered `/task` with the same provider message ID still
  resolves to the same durable row and re-enqueues it too, exactly as
  before - the two mechanisms coexist safely (`dispatch_task_work()` is
  safe to call more than once for the same task). A `running` task whose
  step was left `in_progress` by a crash mid-action is discovered the
  same way, but is deliberately **never retried**: the engine already
  treats an in-progress step as permanently uncertain and fails the task
  closed (`step_execution_uncertain`) rather than ever calling the action
  handler again - see "What this interface does not guarantee" below.
- For **`CONFIRM`/`REJECT`**: as of Milestone 47 P2, an **accepted, new**
  command's decision is durably recorded *before* this interface may
  ever return HTTP `200` for it - `record_confirmation_decision()`
  atomically checks source/state/first-decision-wins and commits the
  decision in one transaction. This does not mean every `200` this
  endpoint ever returns for a `CONFIRM`/`REJECT` represents a fresh
  recording happening on that exact request - see `DUPLICATE_INGRESS`
  just below, which also returns `200`, precisely because the decision
  was *already* durably recorded by an earlier request, not because this
  one recorded anything new. The in-memory worker queue item carries
  only the confirmation id, never the decision itself - the worker always
  reloads the durable decision fresh, so the queue is never authority,
  and a crash between a genuine recording's own HTTP `200` and worker
  pickup no longer loses the decision: a periodic, bounded recovery
  checkpoint (`run_confirmation_decision_recovery_checkpoint()`) picks it
  up and processes it, with capped-exponential backoff if a recovery
  attempt itself fails, so one repeatedly-failing decision can never
  permanently block a later one. An exact redelivery of the SAME
  provider message - even one that arrives after the original decision
  has already been fully consumed and its own pending-confirmation row
  deleted - is durably recognized and safely ignored (`DUPLICATE_INGRESS`:
  HTTP `200`, no re-enqueue, no reply, no further mutation), via a
  separate, restart-surviving ingress receipt keyed on a hashed provider
  message id - never the raw id itself. This is distinct from a genuine
  **manual resend**: the user (or an operator) re-sending the identical
  `CONFIRM`/`REJECT` text arrives as a NEW provider message (its own,
  different provider message id) and is therefore never itself classified
  `DUPLICATE_INGRESS` - it is evaluated fresh against whatever the durable
  confirmation state actually is right now, returning `RECORDED` (a
  genuine, first-ever recording, with a real effect - e.g. if the original
  attempt never actually reached this interface at all), `ALREADY_DECIDED`
  (a true no-op - the original attempt already won, whether or not it was
  ever visibly acknowledged), or `NOT_ELIGIBLE` (if the task has moved on)
  - never a silent DUPLICATE_INGRESS-style ignore in any of these three
  cases. A manual resend remains available while the confirmation is
  still pending, subject to the same expiry rule described next, but is
  now simply redundant with automatic recovery for the crash-window case
  specifically, not the only way forward for it.
- **`CONFIRM` vs `REJECT` expiry is not symmetric.** A `CONFIRM` resent
  after the confirmation's own TTL (120 seconds) has passed reaches the
  engine's existing expiry check and fails the task closed
  (`confirmation_expired`) - it is not silently accepted. A `REJECT`
  resent after that same window still succeeds and cancels the task,
  because denial never authorizes or executes anything, so honoring it
  late is safe.

### What this interface does not guarantee

Milestone 47 (P1-P3) closed most of what Milestone 46 originally stated as
an open scope boundary here - durable confirmation-decision recording,
provider-message redelivery deduplication, and stranded-task restart
recovery, all described above. What remains true, deliberately, even
after that work:

- No exactly-once guarantee for an external action's side effects across
  a process crash, ever. A crash after a step is claimed but before or
  during the actual external call is genuinely uncertain, and is always
  resolved by failing the task closed - never by inferring success or
  failure, and never by retrying the action.
- No exactly-once guarantee for outbound lifecycle message delivery -
  the outbox retry mechanism (Milestone 47 P1) is at-least-once, so a
  duplicate WhatsApp message around a crash boundary remains possible.
- The Milestone 47 P3 recovery backoff governs its own discovery
  ordering only - it is not a global execution lock. A legitimate,
  already-queued `TaskExecutionWork` item may still process a
  currently-backed-off task sooner than its own next scheduled recovery
  attempt; this is intentional, not a gap.
- Single-worker recovery may be delayed by one long-running task,
  network call, or model call ahead of it in the same checkpoint.
- This remains a personal-scale architecture - no generalized scheduler,
  no multi-worker recovery, and no exactly-once delivery ledger of any
  kind (the outbox retry mechanism above is a bounded at-least-once
  redelivery schedule, not an exactly-once guarantee).

## Message handling

A pre-authorized, already-classified item is processed by a single
background worker thread (`interfaces/whatsapp/handler.py`), which
performs no authorization or deduplication of its own:

- A non-text message, an empty/whitespace-only text message, or a text
  message over the configured inbound limit gets one of three fixed
  replies without ever reaching the orchestrator or the task engine.
- A `/task <request>` (`TaskExecutionWork`) or `CONFIRM`/`REJECT`
  (`TaskConfirmationWork`) item is delegated entirely to
  `interfaces/whatsapp/task_control.py`'s `dispatch_task_work()` /
  `dispatch_confirmation_work()` - see "Task control" above.
- Otherwise, the text is passed to `Orchestrator.handle()`. Both of its
  possible return shapes are handled correctly: a plain `str` is used
  directly, and a `ModelResponse` has its `.text` field used. `None`, any
  other return type, an empty or whitespace-only plain string, or a
  `ModelResponse` with empty/whitespace-only text all count as a
  processing failure - as does the orchestrator (or the model provider it
  calls) raising an exception. Either way, exactly one fixed reply is
  attempted: "I could not process that message. Please try again later."
  A failure is logged only as a generic `processing_error` category -
  never the exception object, its message, or a traceback, since even a
  synthetic/malicious exception message could carry a sender ID, user
  text, or a secret. If that fixed reply also fails to send, that's
  logged separately as a generic `outbound_failure` category, with the
  same no-detail rule.
- If a genuinely valid response exceeds the configured outbound
  application limit, it is **discarded outright** - never truncated,
  never sent partially - and replaced with exactly one fixed notice: "The
  response was too long to send through this interface. Please ask a
  narrower question." Task-control lifecycle messages follow the same
  discard-outright discipline via their own bounded-text helper.

The background worker itself is a second, independent safety net: if
`handle_task()` raises in a way that escapes the handling above (a bug,
not an expected orchestrator/client/task-engine failure), the worker logs
a generic `worker_error` category - again no traceback, no exception
detail - and moves on to the next queued item. A single misbehaving item
never stops the worker thread or blocks the rest of the queue.

These are AI-OS application limits, not claims about any WhatsApp
platform limit, and are constructor/function parameters throughout the
implementation (with the defaults below) so tests can inject different
values:

| Limit | Default |
|---|---|
| Raw webhook body | 1,000,000 bytes |
| Inbound text | 4,096 Unicode characters |
| Outbound text | 4,096 Unicode characters |
| Worker queue capacity | 16 |
| Dedup cache capacity | 256 |
| Outbound Cloud API timeout | 10 seconds |

There are no retries anywhere in this interface - a failed outbound send
is logged once and dropped, not queued or retried; the worker processes
one item at a time, in FIFO order, and a failure in one item never stops
the worker from processing the next.

## Memory

Every request through this interface uses `FixedNamespaceMemory`
(`interfaces/whatsapp/memory.py`), which wraps the kernel's real
`MemoryManager` and pins every `remember()`/`recall()` call to a single
fixed namespace, regardless of what namespace the orchestrator or a
capability requests. This keeps all WhatsApp conversation history in one
place, isolated from the CLI or any other interface sharing the same
underlying storage - see the `memory_manager` injection seam documented in
`kernel/orchestrator/orchestrator.py` and `docs/architecture.md`.

## Logging and privacy

Nothing in this interface ever logs: the sender ID (full, masked, or
partial), the destination ID, the raw message ID, message text, the AI
response text, the request payload, the verification token, the
signature, the access token, the app secret, a confirmation id, or any
detail of an exception. A log line may include a short, non-reversible
SHA-256-derived reference for a message ID, for correlating log entries -
never the ID itself. An unauthorized-sender event logs only a category
("dropping message: unauthorized sender"), never any portion of the
sender ID. Task-control log events (`task_ingress_storage_error`,
`task_control_outbound_failure`, `task_control_missing_pending_confirmation`)
follow the identical fixed-category, no-detail discipline. The default
request logger is overridden so the query string of a `GET` request -
which can carry `hub.verify_token` - is never logged either. This is
separate from the kernel's own interaction log
(`storage/logs/interactions.jsonl`), which is unchanged by this
interface.

## Testing

`tests/interfaces/whatsapp/` covers every module, including the full
Milestone 46 task-control surface. No test makes a real network call or a
real durable side effect against a shared database: `WhatsAppClient`
takes an injectable `urlopen`, the server tests exercise the real HTTP
server only over loopback (`127.0.0.1`, an OS-assigned ephemeral port) -
never against Meta's actual Cloud API - and task-control tests use an
isolated, temporary SQLite database. A few edge cases (a missing/malformed
`Content-Length`, a body shorter than declared) are exercised with raw
sockets, since `urllib` cannot express them.
