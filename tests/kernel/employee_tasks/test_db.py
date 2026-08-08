"""Tests for kernel/employee_tasks/db.py: schema, PRAGMAs, database-path
derivation, and integrity checking. Every test uses a tmp_path database
and a tmp_path config.yaml - never the real files under storage/tasks/."""

import sqlite3

import pytest
import yaml

from kernel.employee_tasks.db import (
    SCHEMA_VERSION,
    check_integrity,
    open_reader_connection,
    open_writer_connection,
    resolve_database_path,
)
from kernel.employee_tasks.types import (
    TaskSchemaIncompatibleError,
    TaskStorageCorruptError,
    TaskStorageUnavailableError,
)


def _write_config_yaml(tmp_path, storage_dir):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"tasks": {"storage_dir": str(storage_dir)}}),
        encoding="utf-8",
    )
    return config_path


# --- database path derivation ------------------------------------------------


def test_resolve_database_path_uses_storage_dir_and_fixed_filename(tmp_path):
    storage_dir = tmp_path / "storage" / "tasks"
    storage_dir.mkdir(parents=True)
    config_path = _write_config_yaml(tmp_path, storage_dir)

    db_path = resolve_database_path(config_path)

    assert db_path == storage_dir.resolve() / "tasks.sqlite3"


def test_resolve_database_path_rejects_missing_storage_dir(tmp_path):
    storage_dir = tmp_path / "storage" / "tasks"  # never created
    config_path = _write_config_yaml(tmp_path, storage_dir)

    with pytest.raises(TaskStorageUnavailableError):
        resolve_database_path(config_path)


def test_resolve_database_path_rejects_missing_config_file(tmp_path):
    with pytest.raises(TaskStorageUnavailableError):
        resolve_database_path(tmp_path / "does-not-exist.yaml")


def test_resolve_database_path_rejects_malformed_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("tasks: [this is not: a mapping", encoding="utf-8")

    with pytest.raises(TaskStorageUnavailableError):
        resolve_database_path(config_path)


def test_resolve_database_path_rejects_missing_tasks_section(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("provider: ollama\n", encoding="utf-8")

    with pytest.raises(TaskStorageUnavailableError):
        resolve_database_path(config_path)


def test_resolve_database_path_does_not_create_storage_dir(tmp_path):
    storage_dir = tmp_path / "storage" / "tasks"
    config_path = _write_config_yaml(tmp_path, storage_dir)

    with pytest.raises(TaskStorageUnavailableError):
        resolve_database_path(config_path)

    assert not storage_dir.exists()


# --- schema creation and versioning ------------------------------------------


def test_open_writer_connection_creates_schema(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == (str(SCHEMA_VERSION),)

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"schema_meta", "tasks", "task_transitions"} <= tables
    finally:
        conn.close()


def test_open_writer_connection_sets_wal_and_foreign_keys(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_open_writer_connection_is_idempotent(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    conn1 = open_writer_connection(db_path)
    conn1.close()
    conn2 = open_writer_connection(db_path)  # schema already exists - must verify, not fail
    try:
        row = conn2.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == (str(SCHEMA_VERSION),)
    finally:
        conn2.close()


def test_open_writer_connection_rejects_incompatible_schema_version(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    conn = open_writer_connection(db_path)
    conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    conn.close()

    with pytest.raises(TaskSchemaIncompatibleError):
        open_writer_connection(db_path)


def test_open_reader_connection_rejects_missing_file(tmp_path):
    db_path = tmp_path / "does-not-exist.sqlite3"
    with pytest.raises(TaskStorageUnavailableError):
        open_reader_connection(db_path)


def test_open_reader_connection_never_creates_file(tmp_path):
    db_path = tmp_path / "does-not-exist.sqlite3"
    with pytest.raises(TaskStorageUnavailableError):
        open_reader_connection(db_path)
    assert not db_path.exists()


def test_open_reader_connection_is_read_only(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    open_writer_connection(db_path).close()

    reader = open_reader_connection(db_path)
    try:
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO tasks (task_id) VALUES ('x')")
    finally:
        reader.close()


def test_open_reader_connection_rejects_incompatible_schema_version(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    writer = open_writer_connection(db_path)
    writer.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    writer.close()

    with pytest.raises(TaskSchemaIncompatibleError):
        open_reader_connection(db_path)


# --- integrity checking -------------------------------------------------------


def test_check_integrity_reports_ok_for_healthy_database(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        assert check_integrity(conn) is True
    finally:
        conn.close()


def test_check_integrity_detects_corruption(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    open_writer_connection(db_path).close()

    # Truncate the file to simulate real corruption - distinct from a
    # schema-version mismatch, which is handled separately above.
    with open(db_path, "r+b") as f:
        f.truncate(200)

    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(TaskStorageCorruptError):
            check_integrity(conn)
    finally:
        conn.close()
