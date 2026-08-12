"""Tests for kernel/tools/handlers/copy_file.py: a bounded, exact,
no-clobber copy of one pre-authorized composite operation (Milestone 43
P2)."""

import os
from datetime import datetime, timezone

import pytest

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_execution.observation import build_action_observation, serialize_observation
from kernel.tools.config import FileCopySpec, FileSpec, ToolsConfig
from kernel.tools.handlers import copy_file
from kernel.tools.types import ActionRequest


def _config(approved_directories, approved_files, approved_copies):
    return ToolsConfig(
        approved_directories=approved_directories,
        approved_applications={},
        approved_scripts={},
        approved_files=approved_files,
        approved_copies=approved_copies,
    )


def _request(resource_key):
    return ActionRequest(action="copy_file", resource_key=resource_key)


def _setup(tmp_path, content=b"hello world"):
    source_dir = tmp_path / "documents"
    source_dir.mkdir()
    dest_dir = tmp_path / "archive"
    dest_dir.mkdir()
    source = source_dir / "report.pdf"
    source.write_bytes(content)
    config = _config(
        {"archive": str(dest_dir)},
        {"monthly_report": FileSpec(path=str(source))},
        {
            "monthly_report_archive": FileCopySpec(
                "monthly_report", "archive", "monthly_report.pdf"
            )
        },
    )
    return source, dest_dir, config


def test_exact_successful_copy(tmp_path):
    source, dest_dir, config = _setup(tmp_path, content=b"the exact bytes")

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is True
    assert result.outcome == "executed"
    dest = dest_dir / "monthly_report.pdf"
    assert dest.exists()
    assert "Bytes copied: 15" in result.message


def test_destination_bytes_equal_source(tmp_path):
    source, dest_dir, config = _setup(tmp_path, content=b"x" * 50_000)

    copy_file.run(_request("monthly_report_archive"), config)

    dest = dest_dir / "monthly_report.pdf"
    assert dest.read_bytes() == source.read_bytes()


def test_source_unchanged_after_copy(tmp_path):
    source, dest_dir, config = _setup(tmp_path, content=b"unchanged content")
    original = source.read_bytes()

    copy_file.run(_request("monthly_report_archive"), config)

    assert source.read_bytes() == original


def test_unknown_resource_is_rejected(tmp_path):
    config = _config({}, {}, {})

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_missing_resource_key_is_rejected(tmp_path):
    config = _config({}, {}, {})

    result = copy_file.run(_request(None), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_source_unavailable_fails_safely(tmp_path):
    dest_dir = tmp_path / "archive"
    dest_dir.mkdir()
    config = _config(
        {"archive": str(dest_dir)},
        {"monthly_report": FileSpec(path=str(tmp_path / "does_not_exist.pdf"))},
        {
            "monthly_report_archive": FileCopySpec(
                "monthly_report", "archive", "monthly_report.pdf"
            )
        },
    )

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_source_symlink_is_rejected(tmp_path):
    source_dir = tmp_path / "documents"
    source_dir.mkdir()
    real_file = source_dir / "real.pdf"
    real_file.write_bytes(b"real content")
    link = source_dir / "report.pdf"
    try:
        os.symlink(real_file, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this system")

    dest_dir = tmp_path / "archive"
    dest_dir.mkdir()
    config = _config(
        {"archive": str(dest_dir)},
        {"monthly_report": FileSpec(path=str(link))},
        {
            "monthly_report_archive": FileCopySpec(
                "monthly_report", "archive", "monthly_report.pdf"
            )
        },
    )

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert not (dest_dir / "monthly_report.pdf").exists()


def test_destination_parent_unavailable_fails_safely(tmp_path):
    source_dir = tmp_path / "documents"
    source_dir.mkdir()
    source = source_dir / "report.pdf"
    source.write_bytes(b"content")
    config = _config(
        {"archive": str(tmp_path / "does_not_exist_dir")},
        {"monthly_report": FileSpec(path=str(source))},
        {
            "monthly_report_archive": FileCopySpec(
                "monthly_report", "archive", "monthly_report.pdf"
            )
        },
    )

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_destination_parent_symlink_is_rejected(tmp_path):
    source_dir = tmp_path / "documents"
    source_dir.mkdir()
    source = source_dir / "report.pdf"
    source.write_bytes(b"content")
    real_dest = tmp_path / "real_archive"
    real_dest.mkdir()
    dest_link = tmp_path / "archive_link"
    try:
        os.symlink(real_dest, dest_link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this system")

    config = _config(
        {"archive": str(dest_link)},
        {"monthly_report": FileSpec(path=str(source))},
        {
            "monthly_report_archive": FileCopySpec(
                "monthly_report", "archive", "monthly_report.pdf"
            )
        },
    )

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert not (real_dest / "monthly_report.pdf").exists()


def test_existing_final_destination_is_never_overwritten(tmp_path):
    source, dest_dir, config = _setup(tmp_path, content=b"new content")
    existing = dest_dir / "monthly_report.pdf"
    existing.write_bytes(b"PREEXISTING - must survive untouched")

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert existing.read_bytes() == b"PREEXISTING - must survive untouched"


def test_oversized_source_is_rejected_before_any_destination_work(tmp_path, monkeypatch):
    source, dest_dir, config = _setup(tmp_path, content=b"small enough")
    monkeypatch.setattr(copy_file, "MAX_COPY_SIZE_BYTES", 3)

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    # No temp file and no partial/final destination artifact left behind.
    assert list(dest_dir.iterdir()) == []


def test_source_exactly_at_the_size_limit_is_accepted(tmp_path, monkeypatch):
    # Exact-boundary coverage via a monkeypatched small limit - proves the
    # limit is inclusive (<=), not exclusive, without needing a real
    # 256 MiB fixture.
    monkeypatch.setattr(copy_file, "MAX_COPY_SIZE_BYTES", 10)
    content = b"x" * 10
    source, dest_dir, config = _setup(tmp_path, content=content)

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is True
    assert (dest_dir / "monthly_report.pdf").read_bytes() == content


def test_source_one_byte_over_the_size_limit_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(copy_file, "MAX_COPY_SIZE_BYTES", 10)
    source, dest_dir, config = _setup(tmp_path, content=b"x" * 11)

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert list(dest_dir.iterdir()) == []


def test_bounded_chunked_copy_reassembles_correctly_with_a_small_chunk_size(tmp_path, monkeypatch):
    content = b"y" * 5000
    source, dest_dir, config = _setup(tmp_path, content=content)
    # Forces the copy loop to iterate ~50 times for one 5000-byte file -
    # only possible to reassemble correctly if the loop genuinely streams
    # in chunks rather than reading the whole file in one call.
    monkeypatch.setattr(copy_file, "_COPY_CHUNK_SIZE", 100)

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is True
    assert (dest_dir / "monthly_report.pdf").read_bytes() == content


def test_source_change_before_finalize_is_detected(tmp_path, monkeypatch):
    source, dest_dir, config = _setup(tmp_path, content=b"original content")

    real_stream_copy = copy_file._stream_copy

    def _mutate_then_copy(source_path, fd, max_bytes):
        result = real_stream_copy(source_path, fd, max_bytes)
        # Mutate the source's mtime AFTER streaming completes but before
        # run()'s own post-copy identity re-check - simulates a source
        # that changed mid-operation. A fixed, clearly-different distant
        # timestamp (year ~2096) avoids any flakiness from real-clock
        # mtime resolution being too coarse to differ from "now".
        distant_ns = 4_000_000_000 * 10**9
        os.utime(source_path, ns=(distant_ns, distant_ns))
        return result

    monkeypatch.setattr(copy_file, "_stream_copy", _mutate_then_copy)

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert not (dest_dir / "monthly_report.pdf").exists()
    # No leftover temp file either.
    assert list(dest_dir.iterdir()) == []


def test_atomic_no_replace_collision_at_finalize_time(tmp_path, monkeypatch):
    source, dest_dir, config = _setup(tmp_path)

    def _raise_collision(temp_path, final_path):
        # Simulate a same-name file appearing between the pre-check and
        # finalize - the pre-check alone cannot be race-free (see
        # file_safety.py's own module docstring).
        raise copy_file.FinalizeCollisionError()

    monkeypatch.setattr(copy_file, "atomic_finalize_no_replace", _raise_collision)

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert list(dest_dir.iterdir()) == []  # temp file cleaned up, no leftover


def _failing_stream_copy(source_path, fd, max_bytes):
    # A realistic stream-copy failure double: the real _stream_copy()
    # always closes its fd in a finally block regardless of outcome (see
    # copy_file.py), so a fake that leaves it open would leak a real OS
    # handle - on Windows specifically, unlink() cannot remove a file that
    # still has an open handle, which would make run()'s own cleanup
    # spuriously fail for a reason that could never happen in production.
    import os as _os

    _os.close(fd)
    return None


def test_temp_cleanup_on_handled_stream_failure(tmp_path, monkeypatch):
    source, dest_dir, config = _setup(tmp_path)
    monkeypatch.setattr(copy_file, "_stream_copy", _failing_stream_copy)

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert list(dest_dir.iterdir()) == []


def test_final_destination_never_contains_a_partial_copy(tmp_path, monkeypatch):
    source, dest_dir, config = _setup(tmp_path)
    monkeypatch.setattr(copy_file, "_stream_copy", _failing_stream_copy)

    copy_file.run(_request("monthly_report_archive"), config)

    assert not (dest_dir / "monthly_report.pdf").exists()


def test_stream_copy_never_raises_when_close_fails(tmp_path, monkeypatch):
    """A Python `finally` block's own exception silently REPLACES a
    pending `return` from the `try` block above it - an unguarded
    file_obj.close() failure would previously discard an already-decided
    return value and propagate uncaught out of _stream_copy(), skipping
    run()'s own temp-file cleanup entirely. Proves the fix: _stream_copy()
    always returns (never raises), even when close() itself fails."""

    source = tmp_path / "source.bin"
    source.write_bytes(b"content")

    real_fdopen = copy_file.os.fdopen

    class _CloseFailsFile:
        def __init__(self, real_file):
            self._real = real_file

        def write(self, data):
            return self._real.write(data)

        def flush(self):
            return self._real.flush()

        def fileno(self):
            return self._real.fileno()

        def close(self):
            self._real.close()
            raise OSError("close failed: deferred write error")

    def _fake_fdopen(fd, mode):
        return _CloseFailsFile(real_fdopen(fd, mode))

    monkeypatch.setattr(copy_file.os, "fdopen", _fake_fdopen)

    import os as real_os

    fd = real_os.open(
        tmp_path / "dest.tmp", real_os.O_CREAT | real_os.O_WRONLY, 0o600
    )

    result = copy_file._stream_copy(source, fd, copy_file.MAX_COPY_SIZE_BYTES)

    assert result is None  # failure reported through the return value, never a raised exception


def test_no_absolute_paths_leak_into_the_result(tmp_path):
    source, dest_dir, config = _setup(tmp_path)

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert str(tmp_path) not in result.message
    assert str(source) not in result.message
    assert str(dest_dir) not in result.message


def test_fixed_safe_message_on_stream_os_error(tmp_path, monkeypatch):
    source, dest_dir, config = _setup(tmp_path)

    def _raise_open(*args, **kwargs):
        raise OSError("permission denied: C:/secret/machine/path")

    # Patches copy_file.py's own module-global "open" name only - a bare
    # open() call inside _stream_copy() resolves against the module's
    # globals before falling back to builtins, so this intercepts exactly
    # that one call site without touching the real builtins.open used by
    # pytest/the rest of the test process.
    monkeypatch.setattr(copy_file, "open", _raise_open, raising=False)

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert "secret" not in result.message
    assert "C:/" not in result.message


def test_runtime_defensive_recheck_of_unsafe_destination_name(tmp_path):
    source_dir = tmp_path / "documents"
    source_dir.mkdir()
    source = source_dir / "report.pdf"
    source.write_bytes(b"content")
    dest_dir = tmp_path / "archive"
    dest_dir.mkdir()
    config = _config(
        {"archive": str(dest_dir)},
        {"monthly_report": FileSpec(path=str(source))},
        {"monthly_report_archive": FileCopySpec("monthly_report", "archive", "../escape.pdf")},
    )

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert not (tmp_path / "escape.pdf").exists()


def test_oversized_symbolic_keys_fail_closed_before_any_copy_is_attempted(tmp_path):
    """The gap this correction closes: a hand-built ToolsConfig with
    absurdly long symbolic keys previously let copy_file succeed (the
    real copy only ever touches the short, validated source/destination
    paths) while producing an ActionResult.message too large to fit
    MAX_STEP_RESULT_JSON_CHARS once wrapped in a StepObservation -
    discovered only AFTER the real copy already happened. Proves the fix:
    this now fails closed, and no destination or temp file is created at
    all."""

    source, dest_dir, _base_config = _setup(tmp_path)
    long_key = "k" * 3000
    config = _config(
        {"archive": str(dest_dir)},
        {"monthly_report": FileSpec(path=str(source))},
        {long_key: FileCopySpec("monthly_report", "archive", "monthly_report.pdf")},
    )

    result = copy_file.run(_request(long_key), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert list(dest_dir.iterdir()) == []


def test_oversized_source_reference_fails_closed_before_any_copy_is_attempted(tmp_path):
    source, dest_dir, _base_config = _setup(tmp_path)
    long_source_key = "s" * 3000
    config = _config(
        {"archive": str(dest_dir)},
        {long_source_key: FileSpec(path=str(source))},
        {
            "monthly_report_archive": FileCopySpec(
                long_source_key, "archive", "monthly_report.pdf"
            )
        },
    )

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert list(dest_dir.iterdir()) == []


def test_oversized_destination_directory_reference_fails_closed_before_any_copy_is_attempted(
    tmp_path,
):
    source, dest_dir, _base_config = _setup(tmp_path)
    long_dest_key = "d" * 3000
    config = _config(
        {long_dest_key: str(dest_dir)},
        {"monthly_report": FileSpec(path=str(source))},
        {
            "monthly_report_archive": FileCopySpec(
                "monthly_report", long_dest_key, "monthly_report.pdf"
            )
        },
    )

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert list(dest_dir.iterdir()) == []


def test_manually_constructed_spec_referencing_an_entirely_missing_source_key_fails_safely(
    tmp_path,
):
    # load_tools_config() guarantees source_file_key always exists in
    # approved_files (referential integrity enforced at config-load time)
    # - this can only happen through a directly hand-constructed
    # ToolsConfig. Must fail safely, never crash, never broaden authority.
    dest_dir = tmp_path / "archive"
    dest_dir.mkdir()
    config = _config(
        {"archive": str(dest_dir)},
        {},  # "monthly_report" is not registered at all
        {
            "monthly_report_archive": FileCopySpec(
                "monthly_report", "archive", "monthly_report.pdf"
            )
        },
    )

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert list(dest_dir.iterdir()) == []


def test_manually_constructed_spec_referencing_an_entirely_missing_destination_key_fails_safely(
    tmp_path,
):
    source_dir = tmp_path / "documents"
    source_dir.mkdir()
    source = source_dir / "report.pdf"
    source.write_bytes(b"content")
    config = _config(
        {},  # "archive" is not registered at all
        {"monthly_report": FileSpec(path=str(source))},
        {
            "monthly_report_archive": FileCopySpec(
                "monthly_report", "archive", "monthly_report.pdf"
            )
        },
    )

    result = copy_file.run(_request("monthly_report_archive"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_real_step_observation_serialization_proof(tmp_path):
    source, dest_dir, config = _setup(tmp_path, content=b"z" * 1000)

    result = copy_file.run(_request("monthly_report_archive"), config)
    assert result.success is True

    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())
    serialized = serialize_observation(observation)

    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS
