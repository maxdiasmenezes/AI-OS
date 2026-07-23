# WhatsApp

Interface for interacting with the AI Operating System through WhatsApp,
via Meta's WhatsApp Business Cloud API. This is a thin translation layer:
it turns an inbound WhatsApp text message into a call into the kernel
`Orchestrator` and turns the response back into an outbound WhatsApp text
message. No domain logic lives here. **Single-user only**: exactly one
WhatsApp sender is authorized - there is no allow-list and no multi-user
support.

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
     **before** any JSON parsing.
  3. Parse the JSON body (`400` if malformed).
  4. For each parsed message: validate the destination phone number ID,
     validate the sender against `WHATSAPP_AUTHORIZED_SENDER_ID` (exact
     match only), and reserve its message ID in the dedup cache - *all
     three before the message is ever queued*. An unauthorized sender or
     wrong destination is dropped silently (`200` - not `403`; from
     Meta's perspective delivery still succeeded, since retrying
     wouldn't change anything) and never touches the dedup cache. A
     duplicate message ID is dropped the same way and never reaches the
     queue.
  5. A message that clears authorization and dedup is classified into a
     task (valid text, or one fixed reply for an empty/oversized/
     unsupported message) and submitted to the bounded worker queue.
  6. If the queue is full, the just-reserved dedup entry is released
     (`SeenMessageCache.discard`) and the response is `503` - never
     `200` - so Meta retries the delivery. Processing of the rest of the
     batch stops at that point; messages already queued earlier in the
     same batch keep their dedup reservation, so a full redelivery
     doesn't reprocess them twice.

  The response only ever waits on these fast, synchronous checks -
  never on the orchestrator or an outbound Cloud API call, both of which
  happen afterward, in the background worker.

## Message handling

A pre-authorized, already-deduplicated task is processed by a single
background worker thread (`interfaces/whatsapp/handler.py`), which
performs no authorization or deduplication of its own:

- A non-text message, an empty/whitespace-only text message, or a text
  message over the configured inbound limit gets one of three fixed
  replies without ever reaching the orchestrator.
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
  narrower question."

The background worker itself is a second, independent safety net: if
`handle_task()` raises in a way that escapes the handling above (a bug,
not an expected orchestrator/client failure), the worker logs a generic
`worker_error` category - again no traceback, no exception detail - and
moves on to the next queued task. A single misbehaving task never stops
the worker thread or blocks the rest of the queue.

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
one task at a time, in FIFO order, and a failure in one task never stops
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
signature, the access token, or the app secret. A log line may include a
short, non-reversible SHA-256-derived reference for a message ID, for
correlating log entries - never the ID itself. An unauthorized-sender
event logs only a category ("dropping message: unauthorized sender"),
never any portion of the sender ID. The default request logger is
overridden so the query string of a `GET` request - which can carry
`hub.verify_token` - is never logged either. This is separate from the
kernel's own interaction log (`storage/logs/interactions.jsonl`), which
is unchanged by this interface.

## Testing

`tests/interfaces/whatsapp/` covers every module. No test makes a real
network call: `WhatsAppClient` takes an injectable `urlopen`, and the
server tests exercise the real HTTP server only over loopback
(`127.0.0.1`, an OS-assigned ephemeral port) - never against Meta's actual
Cloud API. A few edge cases (a missing/malformed `Content-Length`, a body
shorter than declared) are exercised with raw sockets, since `urllib`
cannot express them.
