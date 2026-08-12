"""
ActionRegistry: the fixed allowlist of computer actions kernel/tools will
ever execute. This list of action names is not user- or
machine-configurable - only which *resources* (directories, applications,
scripts, repositories, files) each action may touch is configurable, via
kernel/config/tools.yaml (see kernel/tools/config.py). Nothing outside
these names is reachable through kernel/tools, no matter what a caller
asks for.

Milestone 43 P1 (Core Computer Worker - Read-Only Inspection) adds three
read-only actions: file_metadata, read_text_file, and list_processes -
none of them added to _SENSITIVE_ACTIONS. Milestone 43 P2 (Bounded File
Mutations) adds two write actions - create_directory and copy_file - both
added to _SENSITIVE_ACTIONS, since both write a new filesystem entry
(matching repository_backup's own precedent).
"""

from dataclasses import dataclass
from enum import Enum

from kernel.tools.handlers import (
    copy_file,
    create_directory,
    file_metadata,
    list_files,
    list_processes,
    open_application,
    read_text_file,
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
# file_metadata/read_text_file/list_processes (Milestone 43 P1) are all
# read-only and deliberately not in this set either. create_directory/
# copy_file (Milestone 43 P2) both write a new filesystem entry, so both
# are sensitive.
_SENSITIVE_ACTIONS = frozenset(
    {
        "open_application",
        "run_registered_script",
        "repository_backup",
        "create_directory",
        "copy_file",
    }
)

_HANDLERS = {
    "system_status": system_status.run,
    "list_files": list_files.run,
    "open_application": open_application.run,
    "run_registered_script": run_registered_script.run,
    "repo_health": repo_health.run,
    "repository_backup": repository_backup.run,
    "file_metadata": file_metadata.run,
    "read_text_file": read_text_file.run,
    "list_processes": list_processes.run,
    "create_directory": create_directory.run,
    "copy_file": copy_file.run,
}


class ResourceKeyRequirement(Enum):
    """Whether an action's single symbolic resource_key (see
    kernel/tools/types.py's ActionRequest) is forbidden, optional, or
    required. Milestone 39: consumed by kernel/action_protocol/ to decide
    whether a deterministic candidate needs a resolved target before it
    can exist, and whether a missing target should trigger deterministic
    clarification instead of a model call. Every action today is either
    FORBIDDEN (system_status only) or REQUIRED (the other five) - OPTIONAL
    exists so this stays a closed three-way set if a future action is
    genuinely optional, without action_protocol/ needing to change."""

    FORBIDDEN = "forbidden"
    OPTIONAL = "optional"
    REQUIRED = "required"


@dataclass(frozen=True)
class ActionDescriptor:
    """A read-only, code-owned description of one registered action -
    everything a caller (in particular kernel/action_protocol/) needs to
    reason about or describe an action without touching a handler,
    kernel/config/tools.yaml, or any other machine-local configuration.
    Carries no path, command, executable, script content, or secret -
    only the fixed, non-configurable facts ActionRegistry itself owns."""

    name: str
    resource_key_requirement: ResourceKeyRequirement
    resource_key_description: str | None
    sensitive: bool


# Fixed, code-owned metadata behind descriptors() below - deliberately not
# derived from kernel/config/tools.yaml (that file only ever supplies
# which *resource keys* are valid for a given action, never whether the
# action itself takes one). resource_key_description is a short, generic
# phrase - never a real directory/application/script/repository name.
_RESOURCE_KEY_REQUIREMENTS = {
    "system_status": (ResourceKeyRequirement.FORBIDDEN, None),
    "list_files": (ResourceKeyRequirement.REQUIRED, "the registered directory key to list"),
    "open_application": (ResourceKeyRequirement.REQUIRED, "the registered application key to open"),
    "run_registered_script": (ResourceKeyRequirement.REQUIRED, "the registered script key to run"),
    "repo_health": (ResourceKeyRequirement.REQUIRED, "the registered repository key to check"),
    "repository_backup": (ResourceKeyRequirement.REQUIRED, "the registered repository key to back up"),
    "file_metadata": (ResourceKeyRequirement.REQUIRED, "the registered file key to inspect"),
    "read_text_file": (ResourceKeyRequirement.REQUIRED, "the registered file key to read"),
    "list_processes": (ResourceKeyRequirement.FORBIDDEN, None),
    "create_directory": (
        ResourceKeyRequirement.REQUIRED,
        "the registered directory-creation operation key",
    ),
    "copy_file": (ResourceKeyRequirement.REQUIRED, "the registered file-copy operation key"),
}


class ActionRegistry:
    """Read-only lookup over the fixed set of known actions."""

    def is_known(self, action: str) -> bool:
        return action in _HANDLERS

    def is_sensitive(self, action: str) -> bool:
        return action in _SENSITIVE_ACTIONS

    def handler_for(self, action: str):
        return _HANDLERS.get(action)

    def descriptors(self) -> tuple[ActionDescriptor, ...]:
        """Every known action as an immutable ActionDescriptor, in the
        same fixed order as _HANDLERS (declaration order - system_status,
        list_files, open_application, run_registered_script, repo_health,
        repository_backup, file_metadata, read_text_file, list_processes,
        create_directory, copy_file) - deterministic across calls and
        processes,
        never dependent on dict iteration happening to match by chance."""

        return tuple(
            ActionDescriptor(
                name=name,
                resource_key_requirement=_RESOURCE_KEY_REQUIREMENTS[name][0],
                resource_key_description=_RESOURCE_KEY_REQUIREMENTS[name][1],
                sensitive=self.is_sensitive(name),
            )
            for name in _HANDLERS
        )
