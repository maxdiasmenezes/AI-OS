"""Tests for kernel/task_planner/catalog.py: deterministic catalog
construction. Every test uses a synthetic in-memory ToolsConfig - never the
real, gitignored kernel/config/tools.yaml."""

import pytest

from kernel.task_planner.catalog import build_catalog
from kernel.tools.config import (
    ApplicationSpec,
    RepoBackupSpec,
    RepoSpec,
    ScriptSpec,
    ToolsConfig,
)
from kernel.tools.registry import ActionRegistry


@pytest.fixture
def registry():
    return ActionRegistry()


@pytest.fixture
def full_tools_config():
    return ToolsConfig(
        approved_directories={"documents": "/x/documents", "downloads": "/x/downloads"},
        approved_applications={"notepad": ApplicationSpec(executable="/e/notepad", cwd="/e")},
        approved_scripts={
            "daily_report": ScriptSpec(
                interpreter="/i/python", script_path="/s/report.py", cwd="/s", timeout_seconds=30
            )
        },
        approved_repositories={"ai-os": RepoSpec(path="/r/ai-os", main_branch="main")},
        approved_backups={"ai-os": RepoBackupSpec(destination_directory="/d/backups")},
    )


def test_catalog_covers_every_configured_resource(registry, full_tools_config):
    catalog = build_catalog(registry, full_tools_config)
    pairs = {(e.action_name, e.resource_key) for e in catalog}
    assert pairs == {
        ("system_status", None),
        ("list_files", "documents"),
        ("list_files", "downloads"),
        ("open_application", "notepad"),
        ("run_registered_script", "daily_report"),
        ("repo_health", "ai-os"),
        ("repository_backup", "ai-os"),
    }
    assert len(catalog) == 7


def test_system_status_gets_exactly_one_entry_with_no_resource_key(registry, full_tools_config):
    catalog = build_catalog(registry, full_tools_config)
    status_entries = [e for e in catalog if e.action_name == "system_status"]
    assert len(status_entries) == 1
    assert status_entries[0].resource_key is None
    assert status_entries[0].sensitive is False


def test_sensitive_flag_matches_registry(registry, full_tools_config):
    catalog = build_catalog(registry, full_tools_config)
    by_action = {e.action_name: e.sensitive for e in catalog}
    assert by_action["system_status"] is False
    assert by_action["list_files"] is False
    assert by_action["repo_health"] is False
    assert by_action["open_application"] is True
    assert by_action["run_registered_script"] is True
    assert by_action["repository_backup"] is True


def test_catalog_ids_are_opaque_and_sequential(registry, full_tools_config):
    catalog = build_catalog(registry, full_tools_config)
    assert [e.catalog_id for e in catalog] == [f"action_{i}" for i in range(1, len(catalog) + 1)]


def test_empty_config_sections_yield_no_entries_for_that_action(registry):
    empty = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
    )
    catalog = build_catalog(registry, empty)
    # Only system_status (FORBIDDEN resource key) survives an entirely
    # empty configuration - every other action needs a configured key.
    assert [e.action_name for e in catalog] == ["system_status"]


def test_catalog_never_exposes_a_path_or_executable(registry, full_tools_config):
    catalog = build_catalog(registry, full_tools_config)
    for entry in catalog:
        assert "/" not in entry.summary
        assert "\\" not in entry.summary


def test_catalog_is_deterministic_across_calls(registry, full_tools_config):
    first = build_catalog(registry, full_tools_config)
    second = build_catalog(registry, full_tools_config)
    assert first == second


def test_catalog_order_follows_registry_declaration_order_then_config_order(
    registry, full_tools_config
):
    catalog = build_catalog(registry, full_tools_config)
    action_order = [e.action_name for e in catalog]
    # ActionRegistry's own fixed declaration order (see registry.py).
    assert action_order == [
        "system_status",
        "list_files",
        "list_files",
        "open_application",
        "run_registered_script",
        "repo_health",
        "repository_backup",
    ]
    list_files_keys = [e.resource_key for e in catalog if e.action_name == "list_files"]
    assert list_files_keys == ["documents", "downloads"]


# --- requires_capability_grounding: the two-trigger rule --------------------
# (intrinsic action type, OR more than one configured resource for that
# action - see catalog.py:_requires_grounding() and
# types.py:CatalogEntry.requires_capability_grounding for the full
# reasoning)


def test_genus_action_with_a_single_resource_does_not_require_grounding(registry):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={"ai_os": RepoSpec(path="/r/ai-os", main_branch="main")},
        approved_backups={},
    )
    catalog = build_catalog(registry, config)
    repo_entry = next(e for e in catalog if e.action_name == "repo_health")
    assert repo_entry.requires_capability_grounding is False


def test_genus_action_with_multiple_resources_requires_grounding(registry):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={
            "ai_os": RepoSpec(path="/r/ai-os", main_branch="main"),
            "navexis": RepoSpec(path="/r/navexis", main_branch="main"),
            "personal_finance": RepoSpec(path="/r/pf", main_branch="main"),
        },
        approved_backups={},
    )
    catalog = build_catalog(registry, config)
    repo_entries = [e for e in catalog if e.action_name == "repo_health"]
    assert len(repo_entries) == 3
    assert all(e.requires_capability_grounding is True for e in repo_entries)


def test_list_files_with_two_directories_requires_grounding_for_both(registry, full_tools_config):
    # full_tools_config already configures two directories.
    catalog = build_catalog(registry, full_tools_config)
    list_files_entries = [e for e in catalog if e.action_name == "list_files"]
    assert len(list_files_entries) == 2
    assert all(e.requires_capability_grounding is True for e in list_files_entries)


def test_named_capability_actions_require_grounding_even_with_a_single_resource(
    registry, full_tools_config
):
    catalog = build_catalog(registry, full_tools_config)
    by_action = {e.action_name: e for e in catalog}
    # full_tools_config configures exactly one application and one script.
    assert by_action["open_application"].requires_capability_grounding is True
    assert by_action["run_registered_script"].requires_capability_grounding is True


def test_named_capability_actions_require_grounding_even_with_multiple_resources(registry):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={
            "notepad": ApplicationSpec(executable="/e/notepad", cwd="/e"),
            "calculator": ApplicationSpec(executable="/e/calc", cwd="/e"),
        },
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
    )
    catalog = build_catalog(registry, config)
    app_entries = [e for e in catalog if e.action_name == "open_application"]
    assert len(app_entries) == 2
    assert all(e.requires_capability_grounding is True for e in app_entries)


def test_system_status_never_requires_grounding(registry, full_tools_config):
    catalog = build_catalog(registry, full_tools_config)
    status_entry = next(e for e in catalog if e.action_name == "system_status")
    assert status_entry.requires_capability_grounding is False
