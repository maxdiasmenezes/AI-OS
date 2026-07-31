# Tasks

AI employee for Milestone 33's safe computer task execution: a small,
explicitly allowlisted set of actions on this machine, reachable only
through the strict `/task ...` command grammar - never natural language,
never a fallback to the model.

`TasksCapability` is deterministic end-to-end: it never calls a model,
never reads memory, and never reads the knowledge store. It delegates all
actual execution to the kernel's safe task execution layer
(`kernel/tools/`) - this capability only parses the `/task` command,
enforces the confirmation step for sensitive actions, and formats the
result.

## Authorization

`TasksCapability.requires_computer_actions = True`. `Orchestrator.handle()`
refuses to call this capability's `handle()` at all unless the request
carries a `RequestContext(allow_computer_actions=True)` - see
`kernel/orchestrator/context.py`. The CLI and every other caller that
doesn't pass a context stay denied by default. Only
`interfaces/whatsapp/handler.py` constructs an authorizing context, and
only for a message that already passed WhatsApp's own exact-sender
authorization (in `interfaces/whatsapp/server.py`, before the message was
ever queued). This capability performs no authorization of its own and
duplicates none of WhatsApp's.

## Commands

```
/task status
/task files <directory>
/task open <application>
/task run <script>
/task confirm
/task cancel
/task help
```

`<directory>`, `<application>`, and `<script>` are symbolic keys
registered in `kernel/config/tools.yaml` (see
`kernel/config/tools.example.yaml`) - never a raw path. Any extra token,
missing token, or unrecognized verb is rejected with a fixed, generic
message; nothing is guessed.

## Confirmation

`open_application` and `run_registered_script` are sensitive: the first
message only proposes the action and replies with a prompt to confirm.
`/task confirm` within 2 minutes runs it; anything else (timeout, or
`/task cancel`) discards it. The pending action lives in a single,
process-wide, in-memory slot (`kernel/tools/confirmation.py`) - not on
this capability's own instance, since `CapabilityLoader` constructs a new
`TasksCapability` on every request. Confirming consumes the pending action
immediately, before it runs, so it can never be replayed even if
execution itself fails.

## Security

See `kernel/tools/` for the full safe execution layer: allowlist-only
actions, `shell=False` with list-form argv everywhere, no
sender-controlled paths or arguments, per-action timeouts (with full
process-tree termination on timeout for `run_registered_script`), and an
audit trail at `storage/logs/task_actions.jsonl` that never logs a
secret, a resolved private path, or a traceback.
