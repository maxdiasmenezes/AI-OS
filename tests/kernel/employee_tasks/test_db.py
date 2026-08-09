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


# --- Milestone 41 P2: schema v1 -> v2 migration -------------------------------

_V1_STATE_LIST_SQL = (
    "'created', 'planning', 'ready', 'running', 'waiting_for_confirmation', "
    "'completed', 'failed', 'cancelled'"
)


def _create_v1_database(db_path):
    """Build a real, historical schema-version-1 database by hand - the
    exact schema this package shipped with before Milestone 41 P2 (no
    plan_json column) - so migration can be tested against a genuine
    pre-migration file, not a description of one."""

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            f"""
            CREATE TABLE tasks (
                task_id          TEXT PRIMARY KEY,
                display_id       TEXT NOT NULL UNIQUE,
                state            TEXT NOT NULL CHECK (state IN ({_V1_STATE_LIST_SQL})),
                request_text     TEXT NOT NULL,
                source            TEXT NOT NULL,
                dedup_key        TEXT UNIQUE,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                started_at       TEXT,
                completed_at     TEXT,
                failure_code     TEXT,
                failure_summary  TEXT,
                metadata_json    TEXT NOT NULL DEFAULT '{{}}',
                protocol_version INTEGER NOT NULL,
                version          INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE task_transitions (
                transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id       TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                from_state    TEXT CHECK (from_state IS NULL OR from_state IN ({_V1_STATE_LIST_SQL})),
                to_state      TEXT NOT NULL CHECK (to_state IN ({_V1_STATE_LIST_SQL})),
                timestamp     TEXT NOT NULL,
                reason_code   TEXT,
                safe_summary  TEXT,
                task_version  INTEGER NOT NULL CHECK (task_version >= 1)
            )
            """
        )
        conn.execute("CREATE INDEX tasks_created_idx ON tasks(created_at, task_id)")
        conn.execute(
            "CREATE INDEX transitions_task_idx ON task_transitions(task_id, transition_id)"
        )
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _insert_v1_task(db_path, task_id, display_id):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO tasks (task_id, display_id, state, request_text, source, "
            "created_at, updated_at, metadata_json, protocol_version, version) "
            "VALUES (?, ?, 'created', 'do something', 'test', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00', '{}', 1, 1)",
            (task_id, display_id),
        )
        conn.execute(
            "INSERT INTO task_transitions (task_id, from_state, to_state, timestamp, "
            "reason_code, safe_summary, task_version) "
            "VALUES (?, NULL, 'created', '2026-01-01T00:00:00+00:00', 'task_created', NULL, 1)",
            (task_id,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def test_fresh_database_is_created_at_current_schema_version(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == (str(SCHEMA_VERSION),)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "plan_json" in columns
    finally:
        conn.close()


def test_existing_v1_database_migrates_to_current_schema_version(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    _create_v1_database(db_path)

    conn = open_writer_connection(db_path)
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == (str(SCHEMA_VERSION),)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "plan_json" in columns
    finally:
        conn.close()


def test_migration_preserves_existing_task_data(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    _create_v1_database(db_path)
    _insert_v1_task(db_path, "task-1", "TASK-AAAA1111")

    conn = open_writer_connection(db_path)
    try:
        row = conn.execute(
            "SELECT task_id, display_id, state, request_text, source, version "
            "FROM tasks WHERE task_id = ?",
            ("task-1",),
        ).fetchone()
        assert row == ("task-1", "TASK-AAAA1111", "created", "do something", "test", 1)

        transitions = conn.execute(
            "SELECT from_state, to_state FROM task_transitions WHERE task_id = ?", ("task-1",)
        ).fetchall()
        assert transitions == [(None, "created")]
    finally:
        conn.close()


def test_migration_gives_existing_tasks_a_null_plan_json(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    _create_v1_database(db_path)
    _insert_v1_task(db_path, "task-1", "TASK-AAAA1111")

    conn = open_writer_connection(db_path)
    try:
        plan_json = conn.execute(
            "SELECT plan_json FROM tasks WHERE task_id = ?", ("task-1",)
        ).fetchone()[0]
        assert plan_json is None
    finally:
        conn.close()


def test_migration_is_idempotent_on_repeated_initialization(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    _create_v1_database(db_path)
    _insert_v1_task(db_path, "task-1", "TASK-AAAA1111")

    open_writer_connection(db_path).close()
    # Second open against an already-migrated database must not error
    # (e.g. must not re-run "ALTER TABLE ... ADD COLUMN" against a column
    # that already exists) and must leave the data exactly as it was.
    conn = open_writer_connection(db_path)
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == (str(SCHEMA_VERSION),)
        task_row = conn.execute(
            "SELECT task_id, plan_json FROM tasks WHERE task_id = ?", ("task-1",)
        ).fetchone()
        assert task_row == ("task-1", None)
    finally:
        conn.close()


def test_newer_unsupported_schema_version_still_fails_closed(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    conn = open_writer_connection(db_path)
    conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    conn.close()

    with pytest.raises(TaskSchemaIncompatibleError):
        open_writer_connection(db_path)
