# Tasks

Persistent AI-employee task lifecycle data used by `kernel/employee_tasks/`
(Milestone 40).

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
timestamps, the original request text, and bounded metadata) plus an
append-only journal of every state transition. It never stores chain of
thought, model reasoning, plans, execution steps, tool results, secrets,
or arbitrary Python objects — see `kernel/employee_tasks/types.py` for the
exact persisted fields and their size limits.

This is deliberately distinct from `capabilities/tasks/`, which
implements the existing, unrelated `/task ...` WhatsApp command
capability, and has no database of its own. See
`kernel/employee_tasks/__init__.py` for the full disambiguation.

As of Milestone 40, this subsystem persists task identity and lifecycle
state only. It never plans or executes a task, never calls a model or a
tool, and is not wired into the orchestrator, any capability, or
WhatsApp.
