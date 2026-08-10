# Tasks

Persistent AI-employee task lifecycle data used by `kernel/employee_tasks/`
(Milestone 40; schema version 2, plan persistence, added in Milestone 41 P2;
schema versions 3 and 4, durable execution progress and confirmation, added
in Milestone 42).

`tasks.sqlite3` — a local SQLite database (plus its WAL/shared-memory/
journal sidecar files while a write is in progress) holding the durable
task table and its append-only transition journal. Its location is not a
separate setting: it is always `<tasks.storage_dir>/tasks.sqlite3`, i.e.
this same directory, derived from the `tasks.storage_dir` setting in
`kernel/config/config.yaml`. It is **local and gitignored**
(`storage/tasks/*.sqlite3*`) — never committed, and never created
automatically; this directory must already exist before
`kernel/employee_tasks/db.py` will place the database file here.

The database holds one row per task (identity, lifecycle state,
timestamps, the original request text, bounded metadata, and — as of
schema version 2 — an optional persisted plan) plus an append-only journal
of every state transition. It never stores chain of thought, model
reasoning, secrets, or arbitrary Python objects — see
`kernel/employee_tasks/types.py` for the exact persisted fields and their
size limits.

**Plan persistence (schema version 2, Milestone 41 P2).** A task's `tasks`
row has a `plan_json` column: an opaque, bounded (`MAX_PLAN_JSON_CHARS`)
JSON string produced by `kernel/task_planner/serialization.py:
serialize_plan()` from a validated `TaskPlan`. `kernel/employee_tasks/`
itself never interprets, deserializes, or acts on this string's
content — it validates only that it parses as JSON and stays within the
size bound, exactly like the existing `metadata_json` column, and never
imports `kernel.task_planner` or any `TaskPlan` type. Writing `plan_json`
and transitioning the task from `planning` to `ready` happen atomically,
in one transaction with the journal entry
(`TaskRepository.persist_plan_and_ready()`) — a task is never left `ready`
without a plan, and a plan is never persisted without that same
transition. `plan_json` is **write-once** under the current design: the
underlying update requires both `state = 'planning'` and
`plan_json IS NULL`, so a second write for the same task fails closed
rather than overwriting the first plan; there is no general plan-update
or replanning API.

A validated plan describes *what* should be done — an ordered, bounded
list of steps referencing only real, registered actions — never *what
happened*. Milestone 42's `kernel/task_execution/` is what turns a `ready`
plan into a record of what actually happened, by adding two further
tables to this same database (schema versions 3 and 4 — see below).

**Durable step progress (schema version 3, Milestone 42 P1).** A
`task_step_progress` table records one row per plan step a task has
started executing: absence of a row for a `(task_id, step_position)` pair
means "not started" — there is no separate `not_started` status. A row
starts `in_progress` when the step is claimed and moves exactly once to
the write-once terminal `succeeded` or `failed`, at which point its
`result_json` column holds an opaque, bounded (`MAX_STEP_RESULT_JSON_CHARS`,
4,096) `StepObservation` — the durable, safe-to-relay summary of that
step's outcome (never raw stdout/stderr, a stack trace, or a secret),
produced by `kernel/task_execution/observation.py`. This table is
plan-agnostic: `step_position` is stored as an opaque, positive integer
this package never validates against any particular `TaskPlan` — knowing
what a plan step is, and which position to claim next, belongs entirely to
`kernel/task_execution/`.

**Durable confirmation (schema version 4, Milestone 42 P2).** A
`task_pending_confirmation` table holds at most one row per task — a
durable, task-scoped, single-use, 120-second-TTL-bound record of a
proposed sensitive action, created when a task enters
`waiting_for_confirmation` and consumed (approved, denied, or expired)
whenever it leaves that state. This is wholly independent of
`kernel/tools/confirmation.py`'s pre-existing in-memory, single-slot
`ConfirmationStore`, which still only serves the older, unrelated
`/task ...` command path and is never read or written by this database.
The invariant `task.state == waiting_for_confirmation` **iff** exactly one
`task_pending_confirmation` row exists is enforced at the repository API
boundary itself: the generic transition methods this database's own
`tasks`/`task_transitions` tables use mechanically refuse to enter or
leave that state, since none of them know this table exists.

**Migration (`kernel/employee_tasks/db.py`).** `open_writer_connection()`
walks a database at any prior version through the full chain —
v1 → v2 → v3 → v4 — in one call; each step is its own independently
committed transaction (schema change plus the `schema_meta` version bump
together), so a failure partway through leaves the database at the last
successfully completed version, safely resumable by the next call rather
than half-migrated. A `schema_meta` version newer than this code
recognizes is never modified or guessed at — it fails closed
(`TaskSchemaIncompatibleError`).

This is deliberately distinct from `capabilities/tasks/`, which
implements the existing, unrelated `/task ...` WhatsApp command
capability, and has no database of its own. See
`kernel/employee_tasks/__init__.py` for the full disambiguation.

As of Milestone 42, this subsystem persists task identity, lifecycle
state, a validated plan, durable per-step execution progress and results,
and durable confirmation state — and `kernel/task_execution/` (not this
package) is what actually reads and acts on all of it, one step at a
time. It still is not wired into the orchestrator, any capability, or
WhatsApp; nothing in this repository currently creates a task, triggers
planning, or advances execution from a real request. If a step's external
action begins but the process crashes before its terminal result is
persisted, its `task_step_progress` row is left durably `in_progress` —
a genuine uncertain-outcome state this database records faithfully but
never resolves on its own; recovering/reconciling it is Milestone 47's
concern, not this one's.
