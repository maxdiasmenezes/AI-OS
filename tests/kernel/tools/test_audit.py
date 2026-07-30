"""Tests for kernel/tools/audit.py: the task-action audit trail."""

import json
import logging

from kernel.tools import audit


def test_record_appends_one_jsonl_line_with_expected_fields(tmp_path):
    log_path = tmp_path / "task_actions.jsonl"

    audit.record("open_application", "notepad", "executed", log_path=log_path)

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "open_application"
    assert entry["resource"] == "notepad"
    assert entry["outcome"] == "executed"
    assert "timestamp" in entry


def test_record_with_none_resource_key_is_written_as_null(tmp_path):
    log_path = tmp_path / "task_actions.jsonl"

    audit.record("system_status", None, "executed", log_path=log_path)

    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert entry["resource"] is None


def test_multiple_records_append_rather_than_overwrite(tmp_path):
    log_path = tmp_path / "task_actions.jsonl"

    audit.record("system_status", None, "executed", log_path=log_path)
    audit.record("list_files", "documents", "rejected", log_path=log_path)

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_record_creates_parent_directories_as_needed(tmp_path):
    log_path = tmp_path / "nested" / "dir" / "task_actions.jsonl"

    audit.record("system_status", None, "executed", log_path=log_path)

    assert log_path.exists()


def test_record_never_raises_when_the_write_fails(tmp_path, caplog):
    # A directory in place of the expected file makes the open() call fail.
    log_path = tmp_path / "task_actions.jsonl"
    log_path.mkdir()

    with caplog.at_level(logging.DEBUG):
        audit.record("system_status", None, "executed", log_path=log_path)  # must not raise

    assert "audit_write_failed" in [r.getMessage() for r in caplog.records]
