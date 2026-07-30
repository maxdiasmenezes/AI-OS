# Tools

Reusable tools and integrations that AI employees can invoke to take action or fetch information.

## Safe computer task execution (Milestone 33)

The only implementation here today is a small, explicitly allowlisted set
of computer actions on this machine: `system_status`, `list_files`,
`open_application`, `run_registered_script`. See `docs/architecture.md`'s
Kernel section for the full design, and `capabilities/tasks/` for the only
current caller.

Highlights:

- `ActionRegistry` (`registry.py`) is a fixed, non-configurable list of
  exactly those four actions - nothing else is ever reachable.
- Real, machine-specific paths (approved directories/applications/scripts)
  live only in the gitignored `kernel/config/tools.yaml`, copied from the
  committed `kernel/config/tools.example.yaml` placeholder
  (`config.py`). Loading fails closed: missing file → empty allowlists;
  present-but-invalid file → `ToolsConfigError`, never partially applied.
- `SafeTaskExecutor` (`executor.py`) is the single choke point every
  action passes through, and always audits the outcome
  (`audit.py` → `storage/logs/task_actions.jsonl`, symbolic fields only -
  never a secret, token, or resolved private path).
- `ConfirmationStore` (`confirmation.py`) gates the two sensitive actions
  (`open_application`, `run_registered_script`) behind an explicit,
  TTL-bound, consume-once confirmation step.
- `process_control.py` is the only place a real process is spawned -
  always `shell=False` with a list-form `argv` sourced from `tools.yaml`,
  never from a caller. Timeouts kill the full process tree via `psutil`.

Nothing here performs its own caller authorization - see
`kernel/orchestrator/context.py`'s `RequestContext` and
`Capability.requires_computer_actions` for how access to this layer is
gated at the orchestrator level, before a capability that uses it is ever
called.
