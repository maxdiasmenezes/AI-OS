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


# --- Milestone 42 P1: schema v2 -> v3 migration -------------------------------


def _create_v2_database(db_path):
    """Build a real, historical schema-version-2 database by hand - the
    exact schema this package shipped with before Milestone 42 P1 (a
    plan_json column, but no task_step_progress table) - so migration can
    be tested against a genuine pre-migration file, not a description of
    one."""

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
                version          INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
                plan_json        TEXT
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
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _insert_v2_task(db_path, task_id, display_id, plan_json=None):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO tasks (task_id, display_id, state, request_text, source, "
            "created_at, updated_at, metadata_json, protocol_version, version, plan_json) "
            "VALUES (?, ?, 'ready', 'do something', 'test', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00', '{}', 1, 1, ?)",
            (task_id, display_id, plan_json),
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


def _table_names(conn) -> set:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


def test_fresh_database_has_task_step_progress_table(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        assert "task_step_progress" in _table_names(conn)
    finally:
        conn.close()


def test_existing_v2_database_migrates_to_current_schema_version(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    _create_v2_database(db_path)

    conn = open_writer_connection(db_path)
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == (str(SCHEMA_VERSION),)
        assert "task_step_progress" in _table_names(conn)
    finally:
        conn.close()


def test_existing_v1_database_reaches_current_schema_via_full_chain(tmp_path):
    """v1 must reach the current (v3) schema in one open_writer_connection()
    call, via the full v1 -> v2 -> v3 chain - not just v1 -> v2."""

    db_path = tmp_path / "tasks.sqlite3"
    _create_v1_database(db_path)

    conn = open_writer_connection(db_path)
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == (str(SCHEMA_VERSION),)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "plan_json" in columns
        assert "task_step_progress" in _table_names(conn)
    finally:
        conn.close()


def test_migration_v2_to_v3_preserves_task_data_and_plan_json(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    _create_v2_database(db_path)
    plan_json = '{"plan_version":1,"task_id":"task-1","objective":"x","created_at":"t","steps":[]}'
    _insert_v2_task(db_path, "task-1", "TASK-AAAA1111", plan_json=plan_json)

    conn = open_writer_connection(db_path)
    try:
        row = conn.execute(
            "SELECT task_id, display_id, state, plan_json, version FROM tasks WHERE task_id = ?",
            ("task-1",),
        ).fetchone()
        assert row == ("task-1", "TASK-AAAA1111", "ready", plan_json, 1)

        transitions = conn.execute(
            "SELECT from_state, to_state FROM task_transitions WHERE task_id = ?", ("task-1",)
        ).fetchall()
        assert transitions == [(None, "created")]
    finally:
        conn.close()


def test_migration_v2_to_v3_leaves_no_step_progress_rows(tmp_path):
    """A migrated v2 database has no task_step_progress rows for any
    pre-existing task - none of them has ever had execution progress,
    exactly like migration v1 -> v2 gives every pre-existing task a NULL
    plan_json."""

    db_path = tmp_path / "tasks.sqlite3"
    _create_v2_database(db_path)
    _insert_v2_task(db_path, "task-1", "TASK-AAAA1111")

    conn = open_writer_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM task_step_progress WHERE task_id = ?", ("task-1",)
        ).fetchall()
        assert rows == []
    finally:
        conn.close()


def test_migration_v2_to_v3_is_idempotent_on_repeated_initialization(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    _create_v2_database(db_path)
    _insert_v2_task(db_path, "task-1", "TASK-AAAA1111")

    open_writer_connection(db_path).close()
    # Second open against an already-migrated database must not error
    # (e.g. must not re-run "CREATE TABLE task_step_progress" in a way
    # that fails against a table that already exists) and must leave the
    # data exactly as it was.
    conn = open_writer_connection(db_path)
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == (str(SCHEMA_VERSION),)
        task_row = conn.execute(
            "SELECT task_id FROM tasks WHERE task_id = ?", ("task-1",)
        ).fetchone()
        assert task_row == ("task-1",)
    finally:
        conn.close()


def test_v2_to_v3_migration_failure_leaves_a_valid_resumable_v2_database(tmp_path, monkeypatch):
    """_migrate_v1_to_v2() and _migrate_v2_to_v3() are each their own
    independently-committed BEGIN IMMEDIATE ... COMMIT transaction, run in
    sequence by _ensure_schema() - never one transaction spanning both
    version bumps. If v2 -> v3 fails, only that migration's own
    (uncommitted) work rolls back; an already-committed v1 -> v2 result is
    untouched. This proves that by construction: it forces the v2 -> v3
    migration itself to fail with a broken statement appended to its own
    statement tuple, then confirms the database is left as a valid,
    correctly-labelled v2 database (not falsely claiming v3, not missing
    the plan_json column, not structurally corrupt) that a later writer
    open resumes and completes cleanly."""

    import kernel.employee_tasks.db as db_module

    db_path = tmp_path / "tasks.sqlite3"
    _create_v2_database(db_path)
    _insert_v2_task(db_path, "task-1", "TASK-AAAA1111")

    broken_statements = db_module._MIGRATION_2_TO_3_STATEMENTS + ("THIS IS NOT VALID SQL",)
    monkeypatch.setattr(db_module, "_MIGRATION_2_TO_3_STATEMENTS", broken_statements)

    with pytest.raises(TaskStorageUnavailableError):
        open_writer_connection(db_path)

    # Inspect the raw file directly - never through open_writer_connection()
    # again here, since that would itself retry the (still-monkeypatched)
    # broken migration.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == ("2",)

        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "plan_json" in columns

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "task_step_progress" not in tables

        task_row = conn.execute(
            "SELECT task_id FROM tasks WHERE task_id = ?", ("task-1",)
        ).fetchone()
        assert task_row == ("task-1",)
    finally:
        conn.close()

    monkeypatch.undo()

    # A later writer open (real statements restored) resumes from v2 and
    # completes the migration cleanly.
    conn2 = open_writer_connection(db_path)
    try:
        row = conn2.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        assert row == (str(SCHEMA_VERSION),)
        tables = {
            r[0]
            for r in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "task_step_progress" in tables
        task_row = conn2.execute(
            "SELECT task_id FROM tasks WHERE task_id = ?", ("task-1",)
        ).fetchone()
        assert task_row == ("task-1",)
    finally:
        conn2.close()
