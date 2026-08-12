"""Integration tests for Milestone 43 P2's two new actions
(create_directory, copy_file) through the REAL ActionRegistry + ToolsConfig
+ SafeTaskExecutor - never a fake registry or handler. Confirmation is NOT
covered here (see tests/kernel/task_execution/test_milestone_43_p2_e2e.py
for the real M42 confirmation flow) - these tests only prove routing and
config-driven rejection through the executor boundary itself."""

from kernel.tools.config import DirectoryCreationSpec, FileCopySpec, FileSpec, ToolsConfig
from kernel.tools.executor import SafeTaskExecutor
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest


def _config(
    approved_directories=None,
    approved_files=None,
    approved_directory_creations=None,
    approved_copies=None,
):
    return ToolsConfig(
        approved_directories=approved_directories or {},
        approved_applications={},
        approved_scripts={},
        approved_files=approved_files or {},
        approved_directory_creations=approved_directory_creations or {},
        approved_copies=approved_copies or {},
    )


def test_create_directory_known_operation_routes_to_the_real_handler(tmp_path):
    parent = tmp_path / "documents"
    parent.mkdir()
    config = _config(
        approved_directories={"documents": str(parent)},
        approved_directory_creations={
            "project_exports": DirectoryCreationSpec("documents", "exports")
        },
    )
    executor = SafeTaskExecutor(config, ActionRegistry())

    result = executor.execute(
        ActionRequest(action="create_directory", resource_key="project_exports")
    )

    assert result.success is True
    assert result.outcome == "executed"
    assert (parent / "exports").is_dir()


def test_copy_file_known_operation_routes_to_the_real_handler(tmp_path):
    source_dir = tmp_path / "documents"
    source_dir.mkdir()
    source = source_dir / "report.pdf"
    source.write_bytes(b"content")
    dest_dir = tmp_path / "archive"
    dest_dir.mkdir()
    config = _config(
        approved_directories={"archive": str(dest_dir)},
        approved_files={"monthly_report": FileSpec(path=str(source))},
        approved_copies={
            "monthly_report_archive": FileCopySpec(
                "monthly_report", "archive", "monthly_report.pdf"
            )
        },
    )
    executor = SafeTaskExecutor(config, ActionRegistry())

    result = executor.execute(
        ActionRequest(action="copy_file", resource_key="monthly_report_archive")
    )

    assert result.success is True
    assert result.outcome == "executed"
    assert (dest_dir / "monthly_report.pdf").read_bytes() == b"content"


def test_unknown_create_directory_operation_fails_closed_through_the_real_executor():
    config = _config()
    executor = SafeTaskExecutor(config, ActionRegistry())

    result = executor.execute(
        ActionRequest(action="create_directory", resource_key="not_registered")
    )

    assert result.success is False
    assert result.outcome == "rejected"


def test_unknown_copy_file_operation_fails_closed_through_the_real_executor():
    config = _config()
    executor = SafeTaskExecutor(config, ActionRegistry())

    result = executor.execute(ActionRequest(action="copy_file", resource_key="not_registered"))

    assert result.success is False
    assert result.outcome == "rejected"


def test_create_directory_and_copy_file_are_recognized_as_sensitive():
    registry = ActionRegistry()

    assert registry.is_sensitive("create_directory") is True
    assert registry.is_sensitive("copy_file") is True


def test_create_directory_and_copy_file_require_a_resource_key():
    config = _config()
    executor = SafeTaskExecutor(config, ActionRegistry())

    for action in ("create_directory", "copy_file"):
        result = executor.execute(ActionRequest(action=action, resource_key=None))
        assert result.success is False
        assert result.outcome == "rejected"
