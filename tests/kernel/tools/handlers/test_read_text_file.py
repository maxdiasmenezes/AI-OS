"""Tests for kernel/tools/handlers/read_text_file.py: a bounded, exact
read of one registered file (Milestone 43 P1)."""

import os
from datetime import datetime, timezone

import pytest

from kernel.task_execution.observation import (
    ObservationSerializationError,
    build_action_observation,
    serialize_observation,
)
from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH, FileSpec, ToolsConfig
from kernel.tools.handlers import read_text_file
from kernel.tools.types import ActionRequest


def _config(approved_files):
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_files=approved_files,
    )


def _request(resource_key):
    return ActionRequest(action="read_text_file", resource_key=resource_key)


def test_registered_utf8_text_file_can_be_read(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("Hello, world! Héllo again.", encoding="utf-8")
    config = _config({"notes_txt": FileSpec(path=str(target))})

    result = read_text_file.run(_request("notes_txt"), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Hello, world! Héllo again." in result.message


def test_exact_content_is_returned_within_the_safe_bound(tmp_path):
    content = "line one\nline two\nline three"
    target = tmp_path / "notes.txt"
    # write_bytes (not write_text) - avoids platform newline translation,
    # since read_text_file.py reads raw bytes with no such translation of
    # its own and this test asserts byte-exact round-tripping.
    target.write_bytes(content.encode("utf-8"))
    config = _config({"notes_txt": FileSpec(path=str(target))})

    result = read_text_file.run(_request("notes_txt"), config)

    assert result.message.endswith(content)


def test_unregistered_key_is_rejected(tmp_path):
    config = _config({})

    result = read_text_file.run(_request("notes_txt"), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_missing_configured_file_fails_safely(tmp_path):
    config = _config({"notes_txt": FileSpec(path=str(tmp_path / "does_not_exist.txt"))})

    result = read_text_file.run(_request("notes_txt"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_directory_target_is_rejected(tmp_path):
    target_dir = tmp_path / "a_directory"
    target_dir.mkdir()
    config = _config({"notes_txt": FileSpec(path=str(target_dir))})

    result = read_text_file.run(_request("notes_txt"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_symlink_target_is_rejected(tmp_path):
    real_file = tmp_path / "real.txt"
    real_file.write_text("real content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(real_file, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this system")

    config = _config({"notes_txt": FileSpec(path=str(link))})

    result = read_text_file.run(_request("notes_txt"), config)

    assert result.success is False
    assert result.outcome == "failed"


def test_oversized_file_is_rejected_without_silent_truncation(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text("x" * (read_text_file.MAX_TEXT_FILE_BYTES + 1), encoding="utf-8")
    config = _config({"big_txt": FileSpec(path=str(target))})

    result = read_text_file.run(_request("big_txt"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert "too large" in result.message
    assert "x" not in result.message


def test_file_exactly_at_the_bound_is_accepted(tmp_path):
    content = "y" * read_text_file.MAX_TEXT_FILE_BYTES
    target = tmp_path / "exact.txt"
    target.write_text(content, encoding="utf-8")
    config = _config({"exact_txt": FileSpec(path=str(target))})

    result = read_text_file.run(_request("exact_txt"), config)

    assert result.success is True
    assert content in result.message


def test_invalid_utf8_is_rejected(tmp_path):
    target = tmp_path / "invalid.txt"
    target.write_bytes(b"\xff\xfe not valid utf-8")
    config = _config({"invalid_txt": FileSpec(path=str(target))})

    result = read_text_file.run(_request("invalid_txt"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert "could not be read as text" in result.message


def test_nul_containing_content_is_rejected_as_binary_like_even_though_it_would_otherwise_decode(
    tmp_path,
):
    # A NUL byte is technically a valid UTF-8 codepoint (U+0000), so this
    # would decode successfully - the explicit binary-like policy in
    # read_text_file.py rejects it anyway, before decoding is even
    # attempted.
    target = tmp_path / "has_nul.txt"
    target.write_bytes(b"before\x00after")
    config = _config({"has_nul": FileSpec(path=str(target))})

    result = read_text_file.run(_request("has_nul"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert "could not be read as text" in result.message


@pytest.mark.parametrize(
    "control_byte",
    [
        b"\x01",  # the exact byte that previously produced an oversized
        # StepObservation once JSON-escaped to a 6-character unicode
        # escape sequence - see
        # test_worst_case_disallowed_control_byte_would_have_overflowed_
        # the_observation_bound_before_this_policy below for the direct
        # proof against the pre-fix policy
        b"\x02",
        b"\x08",  # backspace
        b"\x0b",  # vertical tab
        b"\x0c",  # form feed
        b"\x1f",
        b"\x7f",  # DEL
    ],
)
def test_disallowed_control_bytes_are_rejected_as_binary_like(tmp_path, control_byte):
    target = tmp_path / "has_control.txt"
    target.write_bytes(b"before" + control_byte + b"after")
    config = _config({"has_control": FileSpec(path=str(target))})

    result = read_text_file.run(_request("has_control"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert "could not be read as text" in result.message


def test_tab_newline_and_carriage_return_remain_allowed(tmp_path):
    target = tmp_path / "whitespace.txt"
    target.write_bytes(b"col1\tcol2\r\nline2")
    config = _config({"whitespace": FileSpec(path=str(target))})

    result = read_text_file.run(_request("whitespace"), config)

    assert result.success is True
    assert "col1\tcol2\r\nline2" in result.message


def test_worst_case_disallowed_control_byte_would_have_overflowed_the_observation_bound_before_this_policy():
    """Direct proof of the bug this correction fixes: repeated U+0001 at
    exactly the byte bound, run through the REAL StepObservation pipeline
    with no read_text_file.py policy in the way, exceeds
    MAX_STEP_RESULT_JSON_CHARS. This is why read_text_file.py must reject
    such bytes itself rather than relying on downstream serialization
    failure - see kernel/task_execution/service.py's
    _finalize_action_step(), which does NOT catch
    ObservationSerializationError the way the RESPOND path does."""

    from kernel.task_execution.observation import ObservationSerializationError
    from kernel.tools.types import ActionResult

    content = "\x01" * read_text_file.MAX_TEXT_FILE_BYTES
    message = f"File: 'k'\nContent:\n{content}"
    result = ActionResult(True, message, "executed")
    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())

    with pytest.raises(ObservationSerializationError):
        serialize_observation(observation)


def test_worst_case_permitted_content_serializes_within_the_real_observation_bound(tmp_path):
    """The authoritative proof required for this correction: the largest,
    worst-case-for-JSON-escaping content read_text_file.py can actually
    return on success (every byte a double quote, at exactly
    MAX_TEXT_FILE_BYTES) survives the REAL
    build_action_observation()/serialize_observation() pipeline within
    MAX_STEP_RESULT_JSON_CHARS - not an estimated overhead formula."""

    target = tmp_path / "worst_case.txt"
    target.write_bytes(b'"' * read_text_file.MAX_TEXT_FILE_BYTES)
    config = _config({"worst_case": FileSpec(path=str(target))})

    result = read_text_file.run(_request("worst_case"), config)
    assert result.success is True

    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())
    serialized = serialize_observation(observation)

    from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS

    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS


def test_no_raw_decoder_exception_text_appears_in_message(tmp_path):
    target = tmp_path / "invalid.txt"
    target.write_bytes(b"\xff\xfe not valid utf-8")
    config = _config({"invalid_txt": FileSpec(path=str(target))})

    result = read_text_file.run(_request("invalid_txt"), config)

    assert "UnicodeDecodeError" not in result.message
    assert "codec" not in result.message
    assert "0x" not in result.message


def test_no_absolute_path_leaks_into_the_result(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    config = _config({"notes_txt": FileSpec(path=str(target))})

    result = read_text_file.run(_request("notes_txt"), config)

    assert str(tmp_path) not in result.message
    assert str(target) not in result.message


def test_oversized_resource_key_fails_closed_before_resolve_approved_file_is_reached(tmp_path):
    """Milestone 43 P3 pre-push review correction (Finding A): a
    hand-constructed ToolsConfig that bypasses load_tools_config()'s own
    MAX_SYMBOLIC_NAME_LENGTH enforcement (see kernel/tools/config.py's
    _parse_approved_files()) must never let read_text_file succeed with an
    oversized key - this handler's own defense-in-depth check must reject
    it BEFORE resolve_approved_file() is ever called, exactly like
    create_directory.py/copy_file.py already do for their own composite
    keys."""

    target = tmp_path / "notes.txt"
    target.write_bytes(b"hello")
    long_key = "k" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    config = _config({long_key: FileSpec(path=str(target))})

    result = read_text_file.run(_request(long_key), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_oversized_resource_key_pre_fix_shape_would_have_overflowed_the_observation_bound():
    """Documents WHY the bound in the test above exists: reproduces the
    exact pre-fix overflow shape directly against the real
    build_action_observation()/serialize_observation() pipeline, bypassing
    this handler's own defense-in-depth check to isolate what the
    serialization layer alone would do with an oversized key echoed
    verbatim into a successful ActionResult.message (the M43 P3 pre-push
    review's Finding A)."""

    from kernel.tools.types import ActionResult

    long_key = "k" * 4000
    pre_fix_message = f"File: '{long_key}'\nContent:\nhello"
    result = ActionResult(True, pre_fix_message, "executed")

    observation = build_action_observation(1, result, "2026-08-11T00:00:00+00:00")
    with pytest.raises(ObservationSerializationError):
        serialize_observation(observation)
