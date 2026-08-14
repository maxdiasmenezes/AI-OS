# Tools

Reusable tools and integrations that AI employees can invoke to take action or fetch information.

## Safe computer task execution (Milestone 33; extended in Milestone 34, 35, 43, 44, and 45)

The implementation here is a small, explicitly allowlisted set of computer
actions on this machine: `system_status`, `list_files`, `open_application`,
`run_registered_script`, `repo_health` (repository health checks, Milestone
34), `repository_backup` (repository backup, Milestone 35), — Milestone 43
P1 — `file_metadata`, `read_text_file`, and `list_processes` (read-only
computer inspection), — Milestone 43 P2 — `create_directory` and
`copy_file` (bounded, create-only filesystem mutations, each authorized by
one pre-configured composite `resource_key` naming an entire operation -
never a caller/model-supplied path, filename, or overwrite flag), —
Milestone 44 (**IMPLEMENTED** — see `docs/architecture.md`'s Milestone 44
section for the full design) — `browser_read_page`: a bounded, read-only
render of exactly one pre-authorized HTTPS page
(`ToolsConfig.approved_pages`) in an isolated, single-use, headless,
JavaScript-disabled Playwright/Chromium browser context with a
default-deny request gate (`browser_safety.py`,
`handlers/browser_read_page.py`) — HTTP redirects, meta refresh, and every
resource class other than the one authorized main-frame document and
explicitly-authorized stylesheet origins are categorically unsupported;
never a caller/model-supplied URL, selector, or JavaScript. Milestone 44
closes without bounded browser interaction (clicks, forms, downloads,
screenshots, JavaScript) by deliberate design — see `docs/architecture.md`
for why. — and Milestone 45 (**IMPLEMENTED — see
`docs/architecture.md`'s Milestone 45 section for the full design**) —
`desktop_target_status` and
`desktop_control_status`: bounded, read-only Windows UI Automation
presence checks for one already-approved desktop window/control
(`ToolsConfig.approved_desktop_targets`/`approved_desktop_controls`,
`desktop_safety.py`, `handlers/desktop_target_status.py`,
`handlers/desktop_control_status.py`) — each reports a fixed status
(`"available"`/`"unavailable"`/`"ambiguous"`, or a distinct failure state
when the check itself could not be reliably performed — see
`desktop_safety.DesktopStatus`), never a window
title, control text, AutomationId, ClassName, PID, HWND, process path, or
match count. Cardinality is computed only over candidates that satisfy
the COMPLETE configured authority (window locator AND runtime executable
identity AND usable state) — never over raw UIA-locator matches alone
(corrected after an adversarial review found an unrelated process sharing
a configured window class could otherwise suppress a legitimately-available
target's status). Mutation (`desktop_invoke_control` and similar) was
empirically evaluated during this milestone's design/validation passes and
**rejected** — semantic UI Automation `InvokePattern` invocation against
the validation fixture changed the foreground window and did not reliably
trigger the target application's actual behavior; Milestone 45 closes
as a read-only Windows Desktop Worker — no mutation capability is planned
for a later phase of it or for Milestone 46. See `docs/architecture.md`'s
Capabilities section — Repository Backup and Repository Health Checks in
particular — for the full design, and `capabilities/tasks/` for the only
current caller.

Highlights:

- `ActionRegistry` (`registry.py`) is a fixed, non-configurable list of
  exactly those fourteen actions - nothing else is ever reachable.
- `browser_safety.py` is the shared URL/origin parsing, normalization,
  private/local-network rejection, and DNS defense-in-depth module for
  `browser_read_page` - config-load-time validation (`config.py`) and the
  runtime request gate (`handlers/browser_read_page.py`) both use it,
  rather than duplicating URL parsing; the request allowlist itself
  (`is_request_permitted()`) is a pure function with no browser
  dependency, shared by production and by network-policy unit tests.
- `desktop_safety.py` is the shared Windows UI Automation identity module
  for `desktop_target_status`/`desktop_control_status` (Milestone 45 P1) -
  config-load-time locator validation (`config.py`) and the runtime
  resolution functions (`resolve_target_status()`/
  `resolve_control_status()`) both live here. Built on `pywinauto`'s `uia`
  backend, using only its low-level, exact-property `find_elements()` API
  - never `best_match`, title matching, or index-based selection.
  `same_windows_executable()` proves two paths reference the *same* file
  on disk via Windows' own file identity (never raw string equality or
  path normalization alone - see its own docstring for the empirically
  proven venv-launcher-versus-real-interpreter gap this exists to handle
  correctly). Every resolution is fresh - nothing here persists a
  pywinauto wrapper, HWND, PID, or COM handle for reuse across calls.
- Real, machine-specific paths (approved directories/applications/scripts)
  live only in the gitignored `kernel/config/tools.yaml`, copied from the
  committed `kernel/config/tools.example.yaml` placeholder
  (`config.py`). Loading fails closed: missing file → empty allowlists;
  present-but-invalid file → `ToolsConfigError`, never partially applied.
- `SafeTaskExecutor` (`executor.py`) is the single choke point every
  action passes through, and always audits the outcome
  (`audit.py` → `storage/logs/task_actions.jsonl`, symbolic fields only -
  never a secret, token, or resolved private path).
- `ConfirmationStore` (`confirmation.py`) gates the five sensitive
  actions (`open_application`, `run_registered_script`,
  `repository_backup`, `create_directory`, `copy_file`) behind an
  explicit, TTL-bound, consume-once confirmation step. See Task
  Confirmation Storage in `docs/architecture.md` for the full mechanism.
  `desktop_target_status`/`desktop_control_status` are non-sensitive and
  never reach this store.
- `process_control.py` is the only place a real process is spawned -
  always `shell=False` with a list-form `argv` sourced from `tools.yaml`,
  never from a caller. Timeouts kill the full process tree via `psutil`.

Nothing here performs its own caller authorization - see
`kernel/orchestrator/context.py`'s `RequestContext` and
`Capability.requires_computer_actions` for how access to this layer is
gated at the orchestrator level, before a capability that uses it is ever
called.
