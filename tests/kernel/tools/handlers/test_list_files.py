"""Tests for kernel/tools/handlers/list_files.py: containment and caps."""

import os

import pytest

from kernel.tools.config import ToolsConfig
from kernel.tools.handlers import list_files
from kernel.tools.types import ActionRequest


def _config(approved_directories):
    return ToolsConfig(
        approved_directories=approved_directories, approved_applications={}, approved_scripts={}
    )


def test_unregistered_key_is_rejected(tmp_path):
    config = _config({})

    result = list_files.run(ActionRequest(action="list_files", resource_key="documents"), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_missing_configured_directory_fails_safely(tmp_path):
    config = _config({"documents": str(tmp_path / "does_not_exist")})

    result = list_files.run(ActionRequest(action="list_files", resource_key="documents"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_empty_directory_reports_empty(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config = _config({"documents": str(root)})

    result = list_files.run(ActionRequest(action="list_files", resource_key="documents"), config)

    assert result.success is True
    assert "empty" in result.message


def test_lists_immediate_entries_only_non_recursive(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a")
    sub = root / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested")
    config = _config({"documents": str(root)})

    result = list_files.run(ActionRequest(action="list_files", resource_key="documents"), config)

    assert result.success is True
    assert "a.txt" in result.message
    assert "subdir" in result.message
    assert "nested.txt" not in result.message


def test_output_is_capped_at_max_entries(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for i in range(150):
        (root / f"file_{i:03d}.txt").write_text("x")
    config = _config({"documents": str(root)})

    result = list_files.run(ActionRequest(action="list_files", resource_key="documents"), config)

    assert result.success is True
    listed_names = [line for line in result.message.splitlines()[1:]]
    assert len(listed_names) == list_files.MAX_ENTRIES
    assert "showing first 100" in result.message


def test_symlink_escaping_the_approved_root_is_excluded(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "visible.txt").write_text("visible")

    try:
        os.symlink(outside, root / "escape_link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this system")

    config = _config({"documents": str(root)})

    result = list_files.run(ActionRequest(action="list_files", resource_key="documents"), config)

    assert "visible.txt" in result.message
    assert "escape_link" not in result.message
    assert "secret.txt" not in result.message


def test_resource_key_is_used_only_as_a_lookup_never_as_a_path_fragment(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a")
    config = _config({"documents": str(root)})

    # Even a traversal-shaped key must fail closed - it's never appended to
    # any path, only compared as a whole string against the allowlist.
    result = list_files.run(
        ActionRequest(action="list_files", resource_key="../../etc"), config
    )

    assert result.success is False
    assert result.outcome == "rejected"
