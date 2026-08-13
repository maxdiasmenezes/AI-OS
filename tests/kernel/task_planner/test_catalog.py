"""Tests for kernel/task_planner/catalog.py: deterministic catalog
construction. Every test uses a synthetic in-memory ToolsConfig - never the
real, gitignored kernel/config/tools.yaml."""

import pytest

from kernel.task_planner.catalog import build_catalog
from kernel.tools.config import (
    ApplicationSpec,
    ApprovedPageSpec,
    DesktopControlSpec,
    DesktopTargetSpec,
    DirectoryCreationSpec,
    FileCopySpec,
    FileSpec,
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
        ("list_processes", None),
    }
    assert len(catalog) == 8


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
    # Only the two FORBIDDEN-resource-key actions (system_status,
    # list_processes) survive an entirely empty configuration - every
    # other action needs a configured key.
    assert [e.action_name for e in catalog] == ["system_status", "list_processes"]


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
        "list_processes",
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


# --- Milestone 43 P1: file_metadata / read_text_file / list_processes -------


@pytest.fixture
def full_tools_config_with_files(full_tools_config):
    return ToolsConfig(
        approved_directories=full_tools_config.approved_directories,
        approved_applications=full_tools_config.approved_applications,
        approved_scripts=full_tools_config.approved_scripts,
        approved_repositories=full_tools_config.approved_repositories,
        approved_backups=full_tools_config.approved_backups,
        approved_files={
            "resume_pdf": FileSpec(path="/f/resume.pdf"),
            "notes_txt": FileSpec(path="/f/notes.txt"),
        },
    )


def test_catalog_covers_file_metadata_and_read_text_file_per_approved_files_key(
    registry, full_tools_config_with_files
):
    catalog = build_catalog(registry, full_tools_config_with_files)
    pairs = {(e.action_name, e.resource_key) for e in catalog}

    assert ("file_metadata", "resume_pdf") in pairs
    assert ("file_metadata", "notes_txt") in pairs
    assert ("read_text_file", "resume_pdf") in pairs
    assert ("read_text_file", "notes_txt") in pairs

    file_metadata_entries = [e for e in catalog if e.action_name == "file_metadata"]
    read_text_file_entries = [e for e in catalog if e.action_name == "read_text_file"]
    assert len(file_metadata_entries) == 2
    assert len(read_text_file_entries) == 2


def test_list_processes_gets_exactly_one_entry_with_no_resource_key(
    registry, full_tools_config_with_files
):
    catalog = build_catalog(registry, full_tools_config_with_files)
    process_entries = [e for e in catalog if e.action_name == "list_processes"]

    assert len(process_entries) == 1
    assert process_entries[0].resource_key is None
    assert process_entries[0].sensitive is False
    assert process_entries[0].requires_capability_grounding is False


def test_list_processes_has_exactly_one_candidate_even_with_no_config_at_all(registry):
    empty = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
        approved_files={},
    )
    catalog = build_catalog(registry, empty)
    process_entries = [e for e in catalog if e.action_name == "list_processes"]

    assert len(process_entries) == 1
    assert process_entries[0].resource_key is None


def test_file_metadata_and_read_text_file_are_non_sensitive_in_the_catalog(
    registry, full_tools_config_with_files
):
    catalog = build_catalog(registry, full_tools_config_with_files)
    by_action = {e.action_name: e.sensitive for e in catalog}

    assert by_action["file_metadata"] is False
    assert by_action["read_text_file"] is False
    assert by_action["list_processes"] is False


def test_file_actions_require_grounding_even_with_a_single_configured_file(registry):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
        approved_files={"resume_pdf": FileSpec(path="/f/resume.pdf")},
    )
    catalog = build_catalog(registry, config)
    by_action = {e.action_name: e for e in catalog}

    # Exactly one configured file - a "genus" action with a single resource
    # would not require grounding (see the repo_health precedent above),
    # but an exact-file action is intrinsically a SPECIES (like
    # open_application/run_registered_script) - naming the sole registered
    # file is still a narrowing "read the file" alone never justifies.
    assert by_action["file_metadata"].requires_capability_grounding is True
    assert by_action["read_text_file"].requires_capability_grounding is True


def test_catalog_never_exposes_a_path_for_file_actions(registry, full_tools_config_with_files):
    catalog = build_catalog(registry, full_tools_config_with_files)
    for entry in catalog:
        if entry.action_name in ("file_metadata", "read_text_file"):
            assert "/f/" not in entry.summary
            assert ".pdf" not in entry.summary
            assert ".txt" not in entry.summary


def test_catalog_ids_remain_opaque_and_sequential_with_file_actions_included(
    registry, full_tools_config_with_files
):
    catalog = build_catalog(registry, full_tools_config_with_files)
    assert [e.catalog_id for e in catalog] == [f"action_{i}" for i in range(1, len(catalog) + 1)]


# --- Milestone 43 P2: create_directory / copy_file ---------------------------


@pytest.fixture
def full_tools_config_with_p2(full_tools_config_with_files):
    base = full_tools_config_with_files
    return ToolsConfig(
        approved_directories=base.approved_directories,
        approved_applications=base.approved_applications,
        approved_scripts=base.approved_scripts,
        approved_repositories=base.approved_repositories,
        approved_backups=base.approved_backups,
        approved_files=base.approved_files,
        approved_directory_creations={
            "project_exports": DirectoryCreationSpec(
                parent_directory_key="documents", directory_name="exports"
            )
        },
        approved_copies={
            "monthly_report_archive": FileCopySpec(
                source_file_key="resume_pdf",
                destination_directory_key="downloads",
                destination_name="resume.pdf",
            )
        },
    )


def test_catalog_covers_create_directory_and_copy_file_per_configured_operation(
    registry, full_tools_config_with_p2
):
    catalog = build_catalog(registry, full_tools_config_with_p2)
    pairs = {(e.action_name, e.resource_key) for e in catalog}

    assert ("create_directory", "project_exports") in pairs
    assert ("copy_file", "monthly_report_archive") in pairs

    create_directory_entries = [e for e in catalog if e.action_name == "create_directory"]
    copy_file_entries = [e for e in catalog if e.action_name == "copy_file"]
    assert len(create_directory_entries) == 1
    assert len(copy_file_entries) == 1


def test_create_directory_and_copy_file_are_sensitive_in_the_catalog(
    registry, full_tools_config_with_p2
):
    catalog = build_catalog(registry, full_tools_config_with_p2)
    by_action = {e.action_name: e.sensitive for e in catalog}

    assert by_action["create_directory"] is True
    assert by_action["copy_file"] is True


def test_create_directory_and_copy_file_require_grounding_even_with_a_single_configured_operation(
    registry,
):
    config = ToolsConfig(
        approved_directories={"documents": "/x/documents"},
        approved_applications={},
        approved_scripts={},
        approved_files={"resume_pdf": FileSpec(path="/f/resume.pdf")},
        approved_directory_creations={
            "project_exports": DirectoryCreationSpec(
                parent_directory_key="documents", directory_name="exports"
            )
        },
        approved_copies={
            "monthly_report_archive": FileCopySpec(
                source_file_key="resume_pdf",
                destination_directory_key="documents",
                destination_name="resume.pdf",
            )
        },
    )
    catalog = build_catalog(registry, config)
    by_action = {e.action_name: e for e in catalog}

    # Exactly one configured operation each - a "genus" action with a
    # single resource would not require grounding, but a pre-authorized
    # composite operation is intrinsically a SPECIES (like
    # open_application/file_metadata) - naming the sole registered
    # operation is still a narrowing "create a directory"/"copy the file"
    # alone never justifies.
    assert by_action["create_directory"].requires_capability_grounding is True
    assert by_action["copy_file"].requires_capability_grounding is True


def test_catalog_never_exposes_a_path_for_create_directory_or_copy_file(
    registry, full_tools_config_with_p2
):
    catalog = build_catalog(registry, full_tools_config_with_p2)
    for entry in catalog:
        if entry.action_name in ("create_directory", "copy_file"):
            # Only the composite operation's own key ("project_exports",
            # "monthly_report_archive") may appear - never any nested
            # field the spec references (parent/destination directory
            # key, directory/destination name) or a raw path.
            assert "/x/" not in entry.summary
            assert "/f/" not in entry.summary
            assert "documents" not in entry.summary
            assert "downloads" not in entry.summary
            assert "resume_pdf" not in entry.summary
            assert ".pdf" not in entry.summary


def test_create_directory_has_no_entries_when_unconfigured(registry, full_tools_config):
    catalog = build_catalog(registry, full_tools_config)
    assert [e for e in catalog if e.action_name == "create_directory"] == []
    assert [e for e in catalog if e.action_name == "copy_file"] == []


def test_catalog_ids_remain_opaque_and_sequential_with_p2_actions_included(
    registry, full_tools_config_with_p2
):
    catalog = build_catalog(registry, full_tools_config_with_p2)
    assert [e.catalog_id for e in catalog] == [f"action_{i}" for i in range(1, len(catalog) + 1)]


# --- Milestone 44 P1: browser_read_page --------------------------------------


@pytest.fixture
def full_tools_config_with_pages(full_tools_config):
    return ToolsConfig(
        approved_directories=full_tools_config.approved_directories,
        approved_applications=full_tools_config.approved_applications,
        approved_scripts=full_tools_config.approved_scripts,
        approved_repositories=full_tools_config.approved_repositories,
        approved_backups=full_tools_config.approved_backups,
        approved_pages={
            "example_docs": ApprovedPageSpec(url="https://example.com:443/docs"),
            "another_page": ApprovedPageSpec(url="https://another.example.com:443/page"),
        },
    )


def test_catalog_covers_browser_read_page_per_approved_pages_key(
    registry, full_tools_config_with_pages
):
    catalog = build_catalog(registry, full_tools_config_with_pages)
    pairs = {(e.action_name, e.resource_key) for e in catalog}

    assert ("browser_read_page", "example_docs") in pairs
    assert ("browser_read_page", "another_page") in pairs

    entries = [e for e in catalog if e.action_name == "browser_read_page"]
    assert len(entries) == 2


def test_browser_read_page_is_non_sensitive_in_the_catalog(registry, full_tools_config_with_pages):
    catalog = build_catalog(registry, full_tools_config_with_pages)
    by_action = {e.action_name: e.sensitive for e in catalog}

    assert by_action["browser_read_page"] is False


def test_browser_read_page_requires_grounding_even_with_a_single_configured_page(registry):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
        approved_pages={"example_docs": ApprovedPageSpec(url="https://example.com:443/docs")},
    )
    catalog = build_catalog(registry, config)
    by_action = {e.action_name: e for e in catalog}

    # Exactly one configured page - an approved_pages key is intrinsically a
    # SPECIES (a single, exact web page), like an approved_files entry -
    # naming the sole registered page is still a narrowing "read the page"
    # alone never justifies.
    assert by_action["browser_read_page"].requires_capability_grounding is True


def test_catalog_never_exposes_a_url_for_browser_read_page(registry, full_tools_config_with_pages):
    catalog = build_catalog(registry, full_tools_config_with_pages)
    for entry in catalog:
        if entry.action_name == "browser_read_page":
            assert "example.com" not in entry.summary
            assert "https://" not in entry.summary
            assert "http://" not in entry.summary


def test_browser_read_page_has_no_entries_when_unconfigured(registry, full_tools_config):
    catalog = build_catalog(registry, full_tools_config)
    assert [e for e in catalog if e.action_name == "browser_read_page"] == []


# --- Milestone 45 P1: desktop_target_status / desktop_control_status ---------


@pytest.fixture
def full_tools_config_with_desktop(full_tools_config):
    return ToolsConfig(
        approved_directories=full_tools_config.approved_directories,
        approved_applications=full_tools_config.approved_applications,
        approved_scripts=full_tools_config.approved_scripts,
        approved_repositories=full_tools_config.approved_repositories,
        approved_backups=full_tools_config.approved_backups,
        approved_desktop_targets={
            "fixture_window": DesktopTargetSpec(
                application_key="notepad",
                process_executable="/e/notepad.exe",
                window_class_name="Notepad",
            ),
            "other_window": DesktopTargetSpec(
                application_key="notepad",
                process_executable="/e/notepad.exe",
                window_class_name="OtherClass",
            ),
        },
        approved_desktop_controls={
            "fixture_refresh": DesktopControlSpec(
                target_key="fixture_window",
                control_automation_id="5001",
                control_type="Button",
            ),
        },
    )


def test_catalog_covers_desktop_target_status_per_approved_desktop_targets_key(
    registry, full_tools_config_with_desktop
):
    catalog = build_catalog(registry, full_tools_config_with_desktop)
    pairs = {(e.action_name, e.resource_key) for e in catalog}

    assert ("desktop_target_status", "fixture_window") in pairs
    assert ("desktop_target_status", "other_window") in pairs
    assert len([e for e in catalog if e.action_name == "desktop_target_status"]) == 2


def test_catalog_covers_desktop_control_status_per_approved_desktop_controls_key(
    registry, full_tools_config_with_desktop
):
    catalog = build_catalog(registry, full_tools_config_with_desktop)
    pairs = {(e.action_name, e.resource_key) for e in catalog}

    assert ("desktop_control_status", "fixture_refresh") in pairs
    assert len([e for e in catalog if e.action_name == "desktop_control_status"]) == 1


def test_desktop_status_actions_are_non_sensitive_in_the_catalog(
    registry, full_tools_config_with_desktop
):
    catalog = build_catalog(registry, full_tools_config_with_desktop)
    by_action = {e.action_name: e.sensitive for e in catalog}

    assert by_action["desktop_target_status"] is False
    assert by_action["desktop_control_status"] is False


def test_desktop_target_status_requires_grounding_even_with_a_single_configured_target(registry):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
        approved_desktop_targets={
            "fixture_window": DesktopTargetSpec(
                application_key="notepad",
                process_executable="/e/notepad.exe",
                window_class_name="Notepad",
            )
        },
    )
    catalog = build_catalog(registry, config)
    by_action = {e.action_name: e for e in catalog}

    assert by_action["desktop_target_status"].requires_capability_grounding is True


def test_desktop_control_status_requires_grounding_even_with_a_single_configured_control(registry):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
        approved_desktop_targets={
            "fixture_window": DesktopTargetSpec(
                application_key="notepad",
                process_executable="/e/notepad.exe",
                window_class_name="Notepad",
            )
        },
        approved_desktop_controls={
            "fixture_refresh": DesktopControlSpec(
                target_key="fixture_window",
                control_automation_id="5001",
                control_type="Button",
            )
        },
    )
    catalog = build_catalog(registry, config)
    by_action = {e.action_name: e for e in catalog}

    assert by_action["desktop_control_status"].requires_capability_grounding is True


def test_catalog_never_exposes_desktop_locator_metadata(registry, full_tools_config_with_desktop):
    catalog = build_catalog(registry, full_tools_config_with_desktop)
    for entry in catalog:
        if entry.action_name in ("desktop_target_status", "desktop_control_status"):
            for forbidden in ("notepad.exe", "Notepad", "5001", "Button", "/e/"):
                assert forbidden not in entry.summary


def test_desktop_status_actions_have_no_entries_when_unconfigured(registry, full_tools_config):
    catalog = build_catalog(registry, full_tools_config)
    assert [e for e in catalog if e.action_name == "desktop_target_status"] == []
    assert [e for e in catalog if e.action_name == "desktop_control_status"] == []
