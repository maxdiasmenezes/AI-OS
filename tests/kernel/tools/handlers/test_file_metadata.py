"""Tests for kernel/tools/handlers/file_metadata.py: bounded, safe
metadata for exactly one registered file (Milestone 43 P1)."""

import os
from datetime import datetime, timezone

import pytest

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_execution.observation import (
    ObservationSerializationError,
    build_action_observation,
    serialize_observation,
)
from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH, FileSpec, ToolsConfig
from kernel.tools.handlers import file_metadata
from kernel.tools.types import ActionRequest


def _config(approved_files):
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_files=approved_files,
    )


def _request(resource_key):
    return ActionRequest(action="file_metadata", resource_key=resource_key)


def test_registered_exact_file_returns_bounded_safe_metadata(tmp_path):
    target = tmp_path / "resume.pdf"
    target.write_bytes(b"%PDF-1.4 fake pdf bytes")
    config = _config({"resume_pdf": FileSpec(path=str(target))})

    result = file_metadata.run(_request("resume_pdf"), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "resume_pdf" in result.message
    assert "Size: 23 bytes" in result.message
    assert "Extension: .pdf" in result.message
    assert "Type: regular file" in result.message


def test_unregistered_key_is_rejected(tmp_path):
    config = _config({})

    result = file_metadata.run(_request("resume_pdf"), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_missing_resource_key_is_rejected():
    config = _config({})

    result = file_metadata.run(_request(None), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_missing_configured_file_fails_safely(tmp_path):
    config = _config({"resume_pdf": FileSpec(path=str(tmp_path / "does_not_exist.pdf"))})

    result = file_metadata.run(_request("resume_pdf"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_directory_target_is_rejected(tmp_path):
    target_dir = tmp_path / "a_directory"
    target_dir.mkdir()
    config = _config({"resume_pdf": FileSpec(path=str(target_dir))})

    result = file_metadata.run(_request("resume_pdf"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_symlink_target_is_rejected(tmp_path):
    real_file = tmp_path / "real.pdf"
    real_file.write_bytes(b"real content")
    link = tmp_path / "link.pdf"
    try:
        os.symlink(real_file, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this system")

    config = _config({"resume_pdf": FileSpec(path=str(link))})

    result = file_metadata.run(_request("resume_pdf"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_no_resolved_absolute_path_leaks_into_message(tmp_path):
    target = tmp_path / "resume.pdf"
    target.write_bytes(b"content")
    config = _config({"resume_pdf": FileSpec(path=str(target))})

    result = file_metadata.run(_request("resume_pdf"), config)

    assert str(tmp_path) not in result.message
    assert str(target) not in result.message


def test_handler_never_reads_file_contents_merely_to_provide_metadata(tmp_path):
    # Invalid UTF-8 bytes - if this handler ever attempted to decode file
    # content (which metadata has no reason to do), that decode would
    # fail. Metadata must succeed regardless, proving content is never
    # touched.
    target = tmp_path / "binary.dat"
    target.write_bytes(b"\xff\xfe\x00\x01not valid utf-8 \xff")
    config = _config({"binary_dat": FileSpec(path=str(target))})

    result = file_metadata.run(_request("binary_dat"), config)

    assert result.success is True
    assert f"Size: {target.stat().st_size} bytes" in result.message


def test_oversized_file_metadata_itself_remains_bounded(tmp_path):
    target = tmp_path / "large.bin"
    target.write_bytes(b"x" * (5 * 1024 * 1024))
    config = _config({"large_bin": FileSpec(path=str(target))})

    result = file_metadata.run(_request("large_bin"), config)

    assert result.success is True
    assert f"Size: {5 * 1024 * 1024} bytes" in result.message
    # The metadata report itself is a handful of short, fixed lines -
    # never proportional to the file's own size.
    assert len(result.message) < 500


def test_largest_realistic_result_serializes_within_the_real_observation_bound(tmp_path, monkeypatch):
    """The authoritative proof required for this correction: even the
    largest realistic file_metadata result (a long, but realistic,
    configured symbolic key, an astronomically large file size - faked via
    a monkeypatched resolve_approved_file() rather than actually writing
    that many bytes to disk, since only the DIGIT COUNT of the size
    matters here - and a long extension) survives the REAL
    build_action_observation()/serialize_observation() pipeline within
    MAX_STEP_RESULT_JSON_CHARS - not an estimated overhead formula.
    file_metadata's own output is a handful of fixed-shape lines with no
    unbounded, externally-influenced content (unlike read_text_file's file
    content or list_processes' process names), so this is expected to hold
    with very large margin - this test proves that expectation rather than
    merely asserting it in prose."""

    from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH
    from kernel.tools.file_safety import ResolvedFile

    # The largest ALLOWED configured symbolic key - a longer one is now
    # rejected before resolve_approved_file() is even reached (see this
    # module's own MAX_SYMBOLIC_NAME_LENGTH defense-in-depth check, added
    # by the Milestone 43 P3 pre-push review correction).
    long_key = "a" * MAX_SYMBOLIC_NAME_LENGTH
    fake_resolved = ResolvedFile(
        key=long_key,
        path=tmp_path / ("x" * 50 + "." + "y" * 20),
        size_bytes=10**19,  # near the largest value a 64-bit size could hold
        modified_at=datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setattr(file_metadata, "resolve_approved_file", lambda *a, **k: fake_resolved)

    result = file_metadata.run(_request(long_key), _config({}))
    assert result.success is True

    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())
    serialized = serialize_observation(observation)

    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS


def test_oversized_resource_key_fails_closed_before_resolve_approved_file_is_reached(tmp_path):
    """Milestone 43 P3 pre-push review correction (Finding A): a
    hand-constructed ToolsConfig that bypasses load_tools_config()'s own
    MAX_SYMBOLIC_NAME_LENGTH enforcement (see kernel/tools/config.py's
    _parse_approved_files()) must never let file_metadata succeed with an
    oversized key - this handler's own defense-in-depth check must reject
    it BEFORE resolve_approved_file() is ever called, exactly like
    create_directory.py/copy_file.py already do for their own composite
    keys."""

    target = tmp_path / "notes.txt"
    target.write_bytes(b"hello")
    long_key = "k" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    config = _config({long_key: FileSpec(path=str(target))})

    result = file_metadata.run(_request(long_key), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_oversized_resource_key_pre_fix_shape_would_have_overflowed_the_observation_bound(tmp_path):
    """Documents WHY the bound in the test above exists: reproduces the
    exact pre-fix overflow shape directly against the real
    build_action_observation()/serialize_observation() pipeline, bypassing
    this handler's own defense-in-depth check to isolate what the
    serialization layer alone would do with an oversized key echoed
    verbatim into a successful ActionResult.message (the M43 P3 pre-push
    review's Finding A)."""

    from kernel.tools.types import ActionResult

    long_key = "k" * 4000
    pre_fix_message = "\n".join(
        [
            f"File: '{long_key}'",
            "Type: regular file",
            "Size: 5 bytes",
            "Modified: 2026-08-11T00:00:00+00:00",
            "Extension: .txt",
        ]
    )
    result = ActionResult(True, pre_fix_message, "executed")

    observation = build_action_observation(1, result, "2026-08-11T00:00:00+00:00")
    with pytest.raises(ObservationSerializationError):
        serialize_observation(observation)
