"""Tests for kernel/tools/handlers/create_directory.py: a bounded,
no-clobber, pre-authorized directory creation (Milestone 43 P2)."""

import os
from datetime import datetime, timezone

import pytest

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_execution.observation import build_action_observation, serialize_observation
from kernel.tools.config import DirectoryCreationSpec, ToolsConfig
from kernel.tools.handlers import create_directory
from kernel.tools.types import ActionRequest


def _config(approved_directories, approved_directory_creations):
    return ToolsConfig(
        approved_directories=approved_directories,
        approved_applications={},
        approved_scripts={},
        approved_directory_creations=approved_directory_creations,
    )


def _request(resource_key):
    return ActionRequest(action="create_directory", resource_key=resource_key)


def test_successful_exact_configured_creation(tmp_path):
    parent = tmp_path / "documents"
    parent.mkdir()
    config = _config(
        {"documents": str(parent)},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert (parent / "exports").is_dir()
    assert "project_exports" in result.message
    assert "exports" in result.message


def test_unknown_resource_is_rejected(tmp_path):
    config = _config({}, {})

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_missing_resource_key_is_rejected(tmp_path):
    config = _config({}, {})

    result = create_directory.run(_request(None), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_parent_unavailable_fails_safely(tmp_path):
    config = _config(
        {"documents": str(tmp_path / "does_not_exist")},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_parent_that_is_a_file_is_rejected(tmp_path):
    parent_file = tmp_path / "documents"
    parent_file.write_bytes(b"not a directory")
    config = _config(
        {"documents": str(parent_file)},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_parent_symlink_is_rejected(tmp_path):
    real_dir = tmp_path / "real_documents"
    real_dir.mkdir()
    link = tmp_path / "documents_link"
    try:
        os.symlink(real_dir, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this system")

    config = _config(
        {"documents": str(link)},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert not (real_dir / "exports").exists()


def test_target_existing_directory_is_rejected(tmp_path):
    parent = tmp_path / "documents"
    parent.mkdir()
    (parent / "exports").mkdir()
    config = _config(
        {"documents": str(parent)},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_target_existing_file_is_rejected(tmp_path):
    parent = tmp_path / "documents"
    parent.mkdir()
    (parent / "exports").write_bytes(b"already here")
    config = _config(
        {"documents": str(parent)},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert (parent / "exports").read_bytes() == b"already here"


def test_target_dangling_symlink_is_rejected(tmp_path):
    parent = tmp_path / "documents"
    parent.mkdir()
    dangling_target = tmp_path / "does_not_exist_target"
    link = parent / "exports"
    try:
        os.symlink(dangling_target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this system")

    config = _config(
        {"documents": str(parent)},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_runtime_defensive_recheck_of_unsafe_directory_name(tmp_path):
    # Bypasses config-load validation entirely by constructing the spec
    # directly - proves the handler's own redundant is_valid_child_name()
    # re-check, not just kernel/tools/config.py's.
    parent = tmp_path / "documents"
    parent.mkdir()
    config = _config(
        {"documents": str(parent)},
        {"project_exports": DirectoryCreationSpec("documents", "../escape")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert not (tmp_path / "escape").exists()


def test_oversized_symbolic_keys_fail_closed_before_the_real_mkdir_call(tmp_path):
    """The gap this correction closes: a hand-built ToolsConfig with
    absurdly long symbolic keys previously let create_directory succeed
    (the real mkdir() only ever touches the short, validated
    directory_name) while producing an ActionResult.message too large to
    fit MAX_STEP_RESULT_JSON_CHARS once wrapped in a StepObservation -
    discovered only AFTER the real directory was already created. Proves
    the fix: this now fails closed, and no directory is created at all."""

    parent = tmp_path / "documents"
    parent.mkdir()
    long_key = "k" * 3000
    config = _config(
        {"documents": str(parent)},
        {long_key: DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request(long_key), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert not (parent / "exports").exists()


def test_oversized_parent_reference_fails_closed_before_the_real_mkdir_call(tmp_path):
    parent = tmp_path / "documents"
    parent.mkdir()
    long_parent_key = "p" * 3000
    config = _config(
        {long_parent_key: str(parent)},
        {"project_exports": DirectoryCreationSpec(long_parent_key, "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert not (parent / "exports").exists()


def test_manually_constructed_spec_referencing_an_entirely_missing_parent_key_fails_safely(
    tmp_path,
):
    # load_tools_config() guarantees parent_directory_key always exists in
    # approved_directories (referential integrity enforced at config-load
    # time) - this can only happen through a directly hand-constructed
    # ToolsConfig, e.g. in a test, or a caller that never went through
    # load_tools_config() at all. Must fail safely, never crash, never
    # broaden authority.
    config = _config(
        {},  # "documents" is not registered at all
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_no_absolute_path_leaks_into_the_result(tmp_path):
    parent = tmp_path / "documents"
    parent.mkdir()
    config = _config(
        {"documents": str(parent)},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)

    assert str(tmp_path) not in result.message
    assert str(parent) not in result.message


def test_fixed_safe_message_on_os_error(tmp_path, monkeypatch):
    parent = tmp_path / "documents"
    parent.mkdir()
    config = _config(
        {"documents": str(parent)},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    def _raise(*args, **kwargs):
        raise OSError("disk full: C:/secret/machine/path")

    monkeypatch.setattr("pathlib.Path.mkdir", _raise)

    result = create_directory.run(_request("project_exports"), config)

    assert result.success is False
    assert "secret" not in result.message
    assert "C:/" not in result.message


def test_real_step_observation_serialization_proof(tmp_path):
    parent = tmp_path / "documents"
    parent.mkdir()
    config = _config(
        {"documents": str(parent)},
        {"project_exports": DirectoryCreationSpec("documents", "exports")},
    )

    result = create_directory.run(_request("project_exports"), config)
    assert result.success is True

    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())
    serialized = serialize_observation(observation)

    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS
