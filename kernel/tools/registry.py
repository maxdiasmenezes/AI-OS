"""
ActionRegistry: the fixed allowlist of computer actions kernel/tools will
ever execute. This list of six action names is not user- or
machine-configurable - only which *resources* (directories, applications,
scripts, repositories) each action may touch is configurable, via
kernel/config/tools.yaml (see kernel/tools/config.py). Nothing outside
these six names is reachable through kernel/tools, no matter what a
caller asks for.
"""

from kernel.tools.handlers import (
    list_files,
    open_application,
    repo_health,
    repository_backup,
    run_registered_script,
    system_status,
)

# Sensitive actions require an explicit confirmation step (see
# kernel/tools/confirmation.py and capabilities/tasks/capability.py) before
# they run. Read-only/informational actions do not - repo_health is
# read-only (Milestone 34) and is deliberately not in this set.
# repository_backup (Milestone 35) writes a file, so it is sensitive.
_SENSITIVE_ACTIONS = frozenset(
    {"open_application", "run_registered_script", "repository_backup"}
)

_HANDLERS = {
    "system_status": system_status.run,
    "list_files": list_files.run,
    "open_application": open_application.run,
    "run_registered_script": run_registered_script.run,
    "repo_health": repo_health.run,
    "repository_backup": repository_backup.run,
}


class ActionRegistry:
    """Read-only lookup over the fixed set of known actions."""

    def is_known(self, action: str) -> bool:
        return action in _HANDLERS

    def is_sensitive(self, action: str) -> bool:
        return action in _SENSITIVE_ACTIONS

    def handler_for(self, action: str):
        return _HANDLERS.get(action)
