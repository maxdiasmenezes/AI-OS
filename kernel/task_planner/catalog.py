"""
Deterministic action catalog builder for kernel/task_planner/ (Milestone
41). build_catalog() is the model-facing action-reference strategy: it is
the direct cross product of ActionRegistry.descriptors() (Milestone 39,
kernel/tools/registry.py) and each action's real, configured resource keys
(kernel/tools/config.py's ToolsConfig), independent of any one request's
text.

This is deliberately NOT kernel/action_protocol/candidates.py's
resolve_action_candidates(): that resolver runs a per-request natural-
language grammar and, by design, returns zero candidates for anything
compound (see its module docstring) - a bounded multi-step planner cannot
be built on top of it. build_catalog() instead exposes the full set of
what the model is ALLOWED to use, once, and the model itself picks zero or
more entries per plan by their opaque catalog_id (see prompt.py/parser.py) -
the same "opaque ID, never a raw tool name or path" discipline
kernel/action_protocol/candidates.py established for its own, differently-
scoped ActionCandidate.

Never calls a model, never executes an action, never performs I/O beyond
what the caller-supplied ToolsConfig already represents in memory.
"""

from kernel.task_planner.types import CatalogEntry
from kernel.tools.config import ToolsConfig
from kernel.tools.registry import ActionRegistry, ResourceKeyRequirement

# Maps each REQUIRED-resource-key action to the ToolsConfig field holding
# its real, configured resource keys - the same mapping
# kernel/action_protocol/candidates.py's _TOOL_GRAMMAR keys off of, kept as
# a small, independent copy here (this module has no dependency on
# kernel/action_protocol/) rather than imported, matching that module's own
# "no shared per-action mapping module exists yet, and one is not
# introduced for two callers" scope.
_RESOURCE_FIELD_BY_ACTION = {
    "list_files": "approved_directories",
    "open_application": "approved_applications",
    "run_registered_script": "approved_scripts",
    "repo_health": "approved_repositories",
    "repository_backup": "approved_backups",
    "file_metadata": "approved_files",
    "read_text_file": "approved_files",
    "create_directory": "approved_directory_creations",
    "copy_file": "approved_copies",
    "browser_read_page": "approved_pages",
}

_SUMMARY_TEMPLATES = {
    "system_status": lambda key: "Check the current system status.",
    "list_files": lambda key: f"List files in the registered '{key}' directory.",
    "open_application": lambda key: f"Open the registered '{key}' application.",
    "run_registered_script": lambda key: f"Run the registered '{key}' script.",
    "repo_health": lambda key: f"Check the health of the registered '{key}' repository.",
    "repository_backup": lambda key: f"Back up the registered '{key}' repository.",
    "file_metadata": lambda key: f"Check metadata for the registered '{key}' file.",
    "read_text_file": lambda key: f"Read the contents of the registered '{key}' file.",
    "list_processes": lambda key: "List currently running processes.",
    "create_directory": lambda key: f"Create the registered '{key}' directory.",
    "copy_file": lambda key: f"Copy the registered '{key}' file.",
    "browser_read_page": lambda key: f"Read the registered '{key}' web page.",
}

# Actions whose resource_key names one specific, narrowly-purposed
# capability (a SPECIES) rather than a broader resource/location type (a
# GENUS) - grounding-required for these UNCONDITIONALLY, independent of how
# many are configured (see _requires_grounding() below for the full,
# two-trigger rule, and CatalogEntry.requires_capability_grounding's
# docstring in types.py for the reasoning). A fixed, catalog-owned contract
# property - never derived from request text or model output, and never
# grown to cover a new action without the same species-vs-genus reasoning
# applying to it. file_metadata/read_text_file (Milestone 43 P1) join this
# set for the same reason open_application/run_registered_script do: an
# approved_files key names one specific, exact file (a SPECIES, not a
# location/category) - "read the file" without the request itself naming
# which one must never silently authorize a specifically-named file the
# request never mentioned, exactly like an unnamed script/application.
# create_directory/copy_file (Milestone 43 P2) join for the identical
# reason: each resource_key names one complete, pre-authorized composite
# operation (a specific parent+child pair, or a specific source+
# destination+name triple) - "create a directory" or "copy the file"
# without the request naming which preconfigured operation must never
# silently authorize one the request never identified.
# browser_read_page (Milestone 44 P1) joins for the same reason
# file_metadata/read_text_file do: an approved_pages key names one
# specific, exact web page (a SPECIES, not a location/category) - "read
# the page" without the request naming which one must never silently
# authorize a specifically-named page the request never mentioned.
_NAMED_CAPABILITY_ACTIONS = frozenset(
    {
        "open_application",
        "run_registered_script",
        "file_metadata",
        "read_text_file",
        "create_directory",
        "copy_file",
        "browser_read_page",
    }
)


def _requires_grounding(action_name: str, configured_resource_count: int) -> bool:
    """The two-trigger grounding rule: an entry needs textual grounding
    (see grounding.py) if EITHER its action is intrinsically a named
    capability (see _NAMED_CAPABILITY_ACTIONS above - true regardless of
    count), OR the catalog offers more than one configured resource for
    this action (a genus-type action - list_files/repo_health/
    repository_backup - stops being an unambiguous stand-in for generic
    language the moment there is more than one eligible referent to choose
    among; picking one without the request text resolving which is the
    exact same silent-narrowing failure class the M1 case demonstrated for
    scripts). A single configured resource for a genus-type action needs no
    grounding: "the repository" when only one is registered denotes exactly
    that one, losing no information - already correctly handled by
    prompt.py's Rule 2 ambiguity guidance."""

    return action_name in _NAMED_CAPABILITY_ACTIONS or configured_resource_count > 1


def build_catalog(
    registry: ActionRegistry, tools_config: ToolsConfig
) -> tuple[CatalogEntry, ...]:
    """Build the full model-facing catalog for one registry+config state:
    one CatalogEntry per (action, resource_key) pair actually configured,
    plus one entry for each action whose resource key is FORBIDDEN (only
    system_status today). Deterministic ordering: ActionRegistry's own
    fixed declaration order (see registry.descriptors()), then each
    action's resource keys in ToolsConfig's own dict iteration order (the
    order they appear in kernel/config/tools.yaml). catalog_id values are
    "action_1", "action_2", ... assigned in that same order - opaque,
    code-generated, and never influenced by request text or model output.

    Two passes: the first collects each REQUIRED action's configured
    resource keys (needed to compute _requires_grounding()'s cardinality
    trigger before any CatalogEntry for that action can be built); the
    second assigns catalog_id and requires_capability_grounding and builds
    the actual immutable entries, in the same deterministic order the
    original single-pass version used."""

    resource_keys_by_action: dict[str, list[str]] = {}
    for descriptor in registry.descriptors():
        if descriptor.resource_key_requirement is ResourceKeyRequirement.FORBIDDEN:
            continue
        config_field = _RESOURCE_FIELD_BY_ACTION[descriptor.name]
        resource_keys_by_action[descriptor.name] = list(getattr(tools_config, config_field))

    entries: list[CatalogEntry] = []
    index = 1
    for descriptor in registry.descriptors():
        if descriptor.resource_key_requirement is ResourceKeyRequirement.FORBIDDEN:
            entries.append(
                CatalogEntry(
                    catalog_id=f"action_{index}",
                    action_name=descriptor.name,
                    resource_key=None,
                    sensitive=descriptor.sensitive,
                    summary=_SUMMARY_TEMPLATES[descriptor.name](None),
                )
            )
            index += 1
            continue

        resource_keys = resource_keys_by_action[descriptor.name]
        requires_grounding = _requires_grounding(descriptor.name, len(resource_keys))
        for resource_key in resource_keys:
            entries.append(
                CatalogEntry(
                    catalog_id=f"action_{index}",
                    action_name=descriptor.name,
                    resource_key=resource_key,
                    sensitive=descriptor.sensitive,
                    summary=_SUMMARY_TEMPLATES[descriptor.name](resource_key),
                    requires_capability_grounding=requires_grounding,
                )
            )
            index += 1

    return tuple(entries)
