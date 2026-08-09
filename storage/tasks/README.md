# Tasks

Persistent AI-employee task lifecycle data used by `kernel/employee_tasks/`
(Milestone 40; schema version 2, plan persistence, added in Milestone 41 P2).

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
happened*. As of Milestone 41, this database still does not store
execution results, tool output, or any record that a step actually ran;
`waiting_for_confirmation` remains a persisted lifecycle state only, not
wired to `kernel/tools/confirmation.py`'s pending-action store. Whether
and how execution results get recorded here is Milestone 42's concern, not
this one's.

This is deliberately distinct from `capabilities/tasks/`, which
implements the existing, unrelated `/task ...` WhatsApp command
capability, and has no database of its own. See
`kernel/employee_tasks/__init__.py` for the full disambiguation.

As of Milestone 41, this subsystem persists task identity, lifecycle
state, and — once planning succeeds — a validated, durable plan. It still
never executes a task, never calls a tool, and is not wired into the
orchestrator, any capability, or WhatsApp; nothing in this repository
currently creates a task or triggers planning from a real request (see
`kernel/task_orchestration/__init__.py` for the planning-orchestration
layer this milestone added, and its own "no production caller yet" note).
