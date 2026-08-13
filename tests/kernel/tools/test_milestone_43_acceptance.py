"""Milestone 43 (Core Computer Worker) whole-milestone acceptance and
security-closure tests for kernel/tools/ and kernel/task_planner/catalog.py.

This module does NOT re-prove what tests/kernel/tools/test_milestone_43_p1_
integration.py, test_milestone_43_p2_integration.py, and each handler's own
test_*.py already cover in detail (per-handler edge cases, per-handler
serialization proofs, per-field config validation). It instead proves a
small number of WHOLE-MILESTONE properties that no single P1/P2 file was
ever positioned to prove on its own: the final registry shape as one
matrix, the planner catalog surfacing all five M43 actions correctly, one
consolidated authority-boundary sweep across every M43 action, one
consolidated read-only/no-mutation proof, one consolidated serialization
proof spanning all five handlers' SUCCESS paths, referential-integrity/
length-bound acceptance for the composite config sections, and a positive
regression documenting that Milestone 43 deliberately implements NO
process-control (termination) capability (see docs/architecture.md's
Milestone 43 entry for the reasoning - psutil has no graceful Windows
termination primitive, and AI-OS retains no durable process-launch
provenance to safely scope one to)."""

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_execution.eligibility import revalidate_action
from kernel.task_execution.observation import build_action_observation, serialize_observation
from kernel.task_planner.catalog import build_catalog
from kernel.tools.config import (
    DirectoryCreationSpec,
    FileCopySpec,
    FileSpec,
    ToolsConfig,
    ToolsConfigError,
    load_tools_config,
)
from kernel.tools.executor import SafeTaskExecutor
from kernel.tools.handlers import list_processes as list_processes_handler
from kernel.tools.registry import ActionRegistry, ResourceKeyRequirement
from kernel.tools.types import ActionRequest

# ============================================================================
# 1. Final registry action set / sensitivity / resource-requirement matrix
# ============================================================================

_EXPECTED_MATRIX = {
    "system_status": (False, ResourceKeyRequirement.FORBIDDEN),
    "list_files": (False, ResourceKeyRequirement.REQUIRED),
    "open_application": (True, ResourceKeyRequirement.REQUIRED),
    "run_registered_script": (True, ResourceKeyRequirement.REQUIRED),
    "repo_health": (False, ResourceKeyRequirement.REQUIRED),
    "repository_backup": (True, ResourceKeyRequirement.REQUIRED),
    "file_metadata": (False, ResourceKeyRequirement.REQUIRED),
    "read_text_file": (False, ResourceKeyRequirement.REQUIRED),
    "list_processes": (False, ResourceKeyRequirement.FORBIDDEN),
    "create_directory": (True, ResourceKeyRequirement.REQUIRED),
    "copy_file": (True, ResourceKeyRequirement.REQUIRED),
    # Milestone 44 P1 added one further action to this same shared
    # registry after Milestone 43 closed - see
    # tests/kernel/tools/test_registry.py's own
    # test_milestone_44_p1_browser_read_page_is_not_sensitive for the
    # dedicated M44 coverage; included here only so this whole-registry
    # matrix stays accurate, since ActionRegistry is one shared allowlist,
    # not a milestone-scoped snapshot.
    "browser_read_page": (False, ResourceKeyRequirement.REQUIRED),
}


def test_final_registry_action_set_and_sensitivity_matrix():
    """The complete registry as of Milestone 44 P1: the eleven actions
    Milestone 43 closed with, plus Milestone 44 P1's one addition
    (browser_read_page) - twelve total, no more and no fewer, each with
    exactly the sensitivity and resource-key-requirement its own milestone
    established. ActionRegistry is a single shared allowlist, not a
    milestone-scoped snapshot, so this test's own exact-count assertion is
    expected to need updating again whenever a later milestone adds
    another action - that is not a regression."""

    registry = ActionRegistry()
    descriptors = registry.descriptors()

    assert {d.name for d in descriptors} == set(_EXPECTED_MATRIX)
    assert len(descriptors) == 12

    for descriptor in descriptors:
        expected_sensitive, expected_requirement = _EXPECTED_MATRIX[descriptor.name]
        assert descriptor.sensitive is expected_sensitive, descriptor.name
        assert descriptor.resource_key_requirement is expected_requirement, descriptor.name

    assert registry.is_sensitive("file_metadata") is False
    assert registry.is_sensitive("read_text_file") is False
    assert registry.is_sensitive("list_processes") is False
    assert registry.is_sensitive("create_directory") is True
    assert registry.is_sensitive("copy_file") is True


# ============================================================================
# 2. Planner catalog exposes all five M43 actions correctly
# ============================================================================


def _full_tools_config():
    return ToolsConfig(
        approved_directories={"documents": "C:\\docs", "archive": "C:\\archive"},
        approved_applications={},
        approved_scripts={},
        approved_files={
            "resume_pdf": FileSpec(path="C:\\docs\\resume.pdf"),
            "notes_txt": FileSpec(path="C:\\docs\\notes.txt"),
        },
        approved_directory_creations={
            "project_exports": DirectoryCreationSpec("documents", "exports"),
        },
        approved_copies={
            "resume_archive": FileCopySpec("resume_pdf", "archive", "resume.pdf"),
        },
    )


def test_planner_catalog_exposes_candidates_for_all_five_m43_actions_with_grounding():
    tools_config = _full_tools_config()
    registry = ActionRegistry()

    catalog = build_catalog(registry, tools_config)
    by_action: dict[str, list] = {}
    for entry in catalog:
        by_action.setdefault(entry.action_name, []).append(entry)

    # All five M43 actions appear.
    assert set(by_action) >= {
        "file_metadata",
        "read_text_file",
        "list_processes",
        "create_directory",
        "copy_file",
    }

    # file_metadata/read_text_file: two configured files each -> two entries,
    # both requiring grounding (named-capability action, unconditionally).
    assert len(by_action["file_metadata"]) == 2
    assert len(by_action["read_text_file"]) == 2
    for entry in by_action["file_metadata"] + by_action["read_text_file"]:
        assert entry.requires_capability_grounding is True
        assert entry.resource_key in ("resume_pdf", "notes_txt")

    # create_directory/copy_file: one configured operation each, also
    # unconditionally grounding-required as named capabilities.
    assert len(by_action["create_directory"]) == 1
    assert by_action["create_directory"][0].resource_key == "project_exports"
    assert by_action["create_directory"][0].requires_capability_grounding is True

    assert len(by_action["copy_file"]) == 1
    assert by_action["copy_file"][0].resource_key == "resume_archive"
    assert by_action["copy_file"][0].requires_capability_grounding is True

    # list_processes: FORBIDDEN resource key -> exactly one entry, no
    # resource_key, no grounding requirement (matches system_status).
    assert len(by_action["list_processes"]) == 1
    assert by_action["list_processes"][0].resource_key is None
    assert by_action["list_processes"][0].requires_capability_grounding is False

    # No entry's summary ever leaks a raw configured path or PID.
    for entry in catalog:
        assert "C:\\" not in entry.summary
        assert "docs" not in entry.summary.lower() or entry.action_name not in (
            "file_metadata",
            "read_text_file",
        )


# ============================================================================
# 3. Consolidated authority-boundary sweep (all M43 resource-bearing
#    actions, plus list_processes' no-resource contract)
# ============================================================================


@pytest.mark.parametrize(
    "action_name",
    ["file_metadata", "read_text_file", "create_directory", "copy_file"],
)
def test_unknown_resource_key_is_rejected_for_every_m43_resource_bearing_action(action_name):
    """One consolidated sweep, not four duplicated test files: every M43
    action that requires a resource_key must fail closed - never touching
    the filesystem - when given a resource_key that resolves to nothing in
    the CURRENT config, both through the real SafeTaskExecutor and through
    kernel.task_execution.eligibility.revalidate_action()."""

    tools_config = ToolsConfig(
        approved_directories={}, approved_applications={}, approved_scripts={}
    )
    registry = ActionRegistry()
    executor = SafeTaskExecutor(tools_config, registry)

    result = executor.execute(ActionRequest(action=action_name, resource_key="not_registered"))
    assert result.success is False
    assert result.outcome == "rejected"

    assert revalidate_action(action_name, "not_registered", registry, tools_config) is False
    assert revalidate_action(action_name, None, registry, tools_config) is False


def test_list_processes_no_resource_contract_holds_through_every_authority_layer(monkeypatch):
    """list_processes is the only M43 action with a FORBIDDEN resource key.
    Consolidated proof that the contract holds at every layer that could
    otherwise be bypassed: the handler itself, the real SafeTaskExecutor,
    and revalidate_action()."""

    monkeypatch.setattr(list_processes_handler.psutil, "process_iter", lambda: iter([]))
    tools_config = ToolsConfig(
        approved_directories={}, approved_applications={}, approved_scripts={}
    )
    registry = ActionRegistry()
    executor = SafeTaskExecutor(tools_config, registry)

    ok = executor.execute(ActionRequest(action="list_processes", resource_key=None))
    assert ok.success is True

    rejected = executor.execute(ActionRequest(action="list_processes", resource_key="anything"))
    assert rejected.success is False
    assert rejected.outcome == "rejected"

    assert revalidate_action("list_processes", None, registry, tools_config) is True
    assert revalidate_action("list_processes", "anything", registry, tools_config) is False


# ============================================================================
# 4. Read-only acceptance: file_metadata/read_text_file/list_processes never
#    mutate anything.
# ============================================================================


def test_read_only_m43_actions_never_create_modify_or_delete_anything(tmp_path, monkeypatch):
    # A dedicated subdirectory, isolated from tmp_path's own root - the
    # repo-wide autouse _redirect_task_audit_log fixture (tests/conftest.py)
    # redirects kernel.tools.audit's log file to directly inside tmp_path
    # itself, which would otherwise collide with a root-level "no new
    # entries" comparison.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"hello")
    original_mtime = target.stat().st_mtime_ns

    tools_config = ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_files={"notes_txt": FileSpec(path=str(target))},
    )
    registry = ActionRegistry()
    executor = SafeTaskExecutor(tools_config, registry)

    monkeypatch.setattr(list_processes_handler.psutil, "process_iter", lambda: iter([]))

    before_entries = set(workspace.iterdir())

    r1 = executor.execute(ActionRequest(action="file_metadata", resource_key="notes_txt"))
    r2 = executor.execute(ActionRequest(action="read_text_file", resource_key="notes_txt"))
    r3 = executor.execute(ActionRequest(action="list_processes", resource_key=None))

    assert r1.success is True
    assert r2.success is True
    assert r3.success is True

    assert set(workspace.iterdir()) == before_entries
    assert target.read_bytes() == b"hello"
    assert target.stat().st_mtime_ns == original_mtime


# ============================================================================
# 5. Consolidated serialization-bound proof across all five M43 handlers'
#    SUCCESS paths, using representative worst-safe values.
# ============================================================================


class _FakeProcess:
    def __init__(self, pid, name, status="running"):
        self.pid = pid
        self._name = name
        self._status = status

    def name(self):
        return self._name

    def status(self):
        return self._status


def test_all_five_m43_handlers_success_results_fit_the_persistence_bound(tmp_path, monkeypatch):
    """Each handler already has its own dedicated worst-case serialization
    regression (see each tests/kernel/tools/handlers/test_*.py's own
    test_real_step_observation_serialization_proof). This is the single
    whole-milestone assertion the individual files were never positioned to
    make: that ALL FIVE hold simultaneously, through the real
    build_action_observation()/serialize_observation() pipeline, using
    representative worst-safe (maximum-length-permitted) configured values
    rather than trivial short ones."""

    long_key = "k" * 64  # MAX_SYMBOLIC_NAME_LENGTH
    long_child_name = "n" * 60 + ".ext"  # near-maximum valid child name

    source_file = tmp_path / "source.txt"
    source_file.write_bytes(b"x" * 1400)  # near MAX_TEXT_FILE_BYTES (1500)
    parent_dir = tmp_path / "parent_dir_with_a_reasonably_long_name"
    parent_dir.mkdir()
    dest_dir = tmp_path / "dest_dir_with_a_reasonably_long_name"
    dest_dir.mkdir()

    tools_config = ToolsConfig(
        approved_directories={long_key: str(parent_dir), "dest": str(dest_dir)},
        approved_applications={},
        approved_scripts={},
        approved_files={long_key: FileSpec(path=str(source_file))},
        approved_directory_creations={
            long_key: DirectoryCreationSpec(long_key, long_child_name)
        },
        approved_copies={
            long_key: FileCopySpec(long_key, "dest", long_child_name),
        },
    )
    registry = ActionRegistry()

    monkeypatch.setattr(
        list_processes_handler.psutil,
        "process_iter",
        lambda: iter(
            _FakeProcess(1000 + i, f"process_name_{i}.exe") for i in range(50)
        ),
    )

    results = []
    executor = SafeTaskExecutor(tools_config, registry)
    results.append(executor.execute(ActionRequest(action="file_metadata", resource_key=long_key)))
    results.append(executor.execute(ActionRequest(action="read_text_file", resource_key=long_key)))
    results.append(executor.execute(ActionRequest(action="list_processes", resource_key=None)))

    # create_directory/copy_file each perform one real, observable side
    # effect - executed once each, sequentially, against the same config.
    results.append(
        executor.execute(ActionRequest(action="create_directory", resource_key=long_key))
    )
    results.append(executor.execute(ActionRequest(action="copy_file", resource_key=long_key)))

    assert all(r.success for r in results), results

    for position, result in enumerate(results, start=1):
        observation = build_action_observation(position, result, "2026-08-11T00:00:00+00:00")
        serialized = serialize_observation(observation)
        assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS


# ============================================================================
# 6. ToolsConfig composite referential integrity, length bounds, and
#    example-file parseability.
# ============================================================================


def test_tools_example_yaml_parses_cleanly_through_the_real_loader():
    """The committed placeholder kernel/config/tools.example.yaml must
    always be a valid, fully-loadable ToolsConfig - a real machine copies
    it verbatim as the starting point for its own tools.yaml."""

    example_path = (
        Path(__file__).resolve().parents[3] / "kernel" / "config" / "tools.example.yaml"
    )
    config = load_tools_config(example_path)

    assert isinstance(config, ToolsConfig)
    # Every M43 section is present in the example file (proves the example
    # was actually kept up to date, not just that SOME file parses).
    assert config.approved_files
    assert config.approved_directory_creations
    assert config.approved_copies


def test_create_directory_composite_reference_to_a_missing_parent_fails_closed(tmp_path):
    raw = yaml.safe_dump(
        {
            "list_files": {"approved_directories": {"documents": str(tmp_path)}},
            "create_directory": {
                "approved_directory_creations": {
                    "exports": {
                        "parent_directory": "does_not_exist",
                        "directory_name": "exports",
                    }
                }
            },
        }
    )
    config_path = tmp_path / "tools.yaml"
    config_path.write_text(raw, encoding="utf-8")

    with pytest.raises(ToolsConfigError):
        load_tools_config(config_path)


def test_copy_file_composite_reference_to_a_missing_source_or_destination_fails_closed(tmp_path):
    raw = yaml.safe_dump(
        {
            "list_files": {"approved_directories": {"archive": str(tmp_path)}},
            "copy_file": {
                "approved_copies": {
                    "report": {
                        "source_file": "does_not_exist",
                        "destination_directory": "archive",
                        "destination_name": "report.pdf",
                    }
                }
            },
        }
    )
    config_path = tmp_path / "tools.yaml"
    config_path.write_text(raw, encoding="utf-8")

    with pytest.raises(ToolsConfigError):
        load_tools_config(config_path)


def test_oversized_composite_operation_key_fails_closed_at_config_load_time(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"content")
    raw = yaml.safe_dump(
        {
            "approved_files": {"src": {"path": str(source)}},
            "list_files": {"approved_directories": {"dest": str(tmp_path)}},
            "copy_file": {
                "approved_copies": {
                    "k" * 3000: {
                        "source_file": "src",
                        "destination_directory": "dest",
                        "destination_name": "out.txt",
                    }
                }
            },
        }
    )
    config_path = tmp_path / "tools.yaml"
    config_path.write_text(raw, encoding="utf-8")

    with pytest.raises(ToolsConfigError):
        load_tools_config(config_path)


def test_unsafe_child_name_in_composite_sections_fails_closed_at_config_load_time(tmp_path):
    raw = yaml.safe_dump(
        {
            "list_files": {"approved_directories": {"documents": str(tmp_path)}},
            "create_directory": {
                "approved_directory_creations": {
                    "exports": {
                        "parent_directory": "documents",
                        "directory_name": "../escape",
                    }
                }
            },
        }
    )
    config_path = tmp_path / "tools.yaml"
    config_path.write_text(raw, encoding="utf-8")

    with pytest.raises(ToolsConfigError):
        load_tools_config(config_path)


# ============================================================================
# 7. Security decision documentation: NO process-control capability exists.
# ============================================================================


def test_no_process_control_capability_exists_anywhere_in_the_registry():
    """Documents Milestone 43 P3's explicit design decision (Option D - see
    docs/architecture.md's Milestone 43 entry): process termination/control
    was deliberately evaluated and excluded. This is a positive regression:
    it fails loudly if a future change ever quietly introduces process
    termination without a fresh design/security review."""

    registry = ActionRegistry()

    for forbidden_name in (
        "stop_process",
        "kill_process",
        "terminate_process",
        "end_process",
        "process_terminate",
    ):
        assert registry.is_known(forbidden_name) is False
        assert registry.handler_for(forbidden_name) is None

    action_names = {d.name for d in registry.descriptors()}
    assert not any("terminate" in name or "kill" in name or "stop_process" in name for name in action_names)


def test_no_process_control_capability_is_reachable_through_the_planner_catalog():
    """build_catalog() only ever emits entries for actions ActionRegistry
    already knows about (see catalog.py's own module docstring) - so the
    prior test's registry-level absence is sufficient to guarantee the
    planner can never expose one either. Verified directly here rather than
    only inferred."""

    tools_config = _full_tools_config()
    registry = ActionRegistry()
    catalog = build_catalog(registry, tools_config)

    action_names = {entry.action_name for entry in catalog}
    assert not any("terminate" in name or "kill" in name for name in action_names)


def test_tools_config_has_no_static_pid_or_process_resource_type():
    """ToolsConfig's schema itself has no field for a pre-authorized PID,
    process name, or process-identity resource of any kind - process
    control was not merely left unregistered, no configuration surface for
    it exists at all."""

    field_names = {f.name for f in fields(ToolsConfig)}
    assert not any("process" in name or "pid" in name for name in field_names)
