"""Tests for kernel/knowledge_base/db.py: schema, FTS5 probe, connection
PRAGMAs, and database-path derivation. Every test uses a tmp_path
database and a tmp_path config.yaml - never the real files."""

import sqlite3

import pytest
import yaml

from kernel.knowledge_base.db import (
    SCHEMA_VERSION,
    open_reader_connection,
    open_writer_connection,
    resolve_database_path,
)
from kernel.knowledge_base.types import DatabaseUnavailableError, SchemaIncompatibleError


def _write_config_yaml(tmp_path, storage_dir):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"knowledge": {"storage_dir": str(storage_dir)}}),
        encoding="utf-8",
    )
    return config_path


# --- database path derivation -----------------------------------------------


def test_resolve_database_path_uses_storage_dir_and_fixed_filename(tmp_path):
    storage_dir = tmp_path / "storage" / "knowledge"
    storage_dir.mkdir(parents=True)
    config_path = _write_config_yaml(tmp_path, storage_dir)

    db_path = resolve_database_path(config_path)

    assert db_path == storage_dir.resolve() / "knowledge_index.sqlite3"


def test_resolve_database_path_rejects_missing_storage_dir(tmp_path):
    storage_dir = tmp_path / "storage" / "knowledge"  # never created
    config_path = _write_config_yaml(tmp_path, storage_dir)

    with pytest.raises(DatabaseUnavailableError):
        resolve_database_path(config_path)


def test_resolve_database_path_rejects_missing_config_file(tmp_path):
    with pytest.raises(DatabaseUnavailableError):
        resolve_database_path(tmp_path / "does-not-exist.yaml")


def test_resolve_database_path_rejects_malformed_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("knowledge: [this is not: a mapping", encoding="utf-8")

    with pytest.raises(DatabaseUnavailableError):
        resolve_database_path(config_path)


def test_resolve_database_path_rejects_missing_knowledge_section(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("provider: ollama\n", encoding="utf-8")

    with pytest.raises(DatabaseUnavailableError):
        resolve_database_path(config_path)


def test_resolve_database_path_does_not_create_storage_dir(tmp_path):
    storage_dir = tmp_path / "storage" / "knowledge"
    config_path = _write_config_yaml(tmp_path, storage_dir)

    with pytest.raises(DatabaseUnavailableError):
        resolve_database_path(config_path)

    assert not storage_dir.exists()


# --- schema creation and versioning ------------------------------------------


def test_schema_creates_successfully(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"schema_meta", "sources", "documents", "chunks"} <= tables
    finally:
        conn.close()


def test_schema_version_row_matches_expected_version(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert row[0] == str(SCHEMA_VERSION)
    finally:
        conn.close()


def test_documents_table_has_source_key_and_foreign_key(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
        assert "source_key" in columns
        fks = conn.execute("PRAGMA foreign_key_list(documents)").fetchall()
        assert any(fk[2] == "sources" for fk in fks)
    finally:
        conn.close()


def test_indexes_exist(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "documents_source_idx" in indexes
        assert "chunks_document_idx" in indexes
    finally:
        conn.close()


def test_foreign_keys_enabled_on_writer_connection(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_foreign_keys_enabled_on_reader_connection(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    writer = open_writer_connection(db_path)
    writer.close()

    reader = open_reader_connection(db_path)
    try:
        assert reader.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        reader.close()


def test_wal_journal_mode_set_on_writer(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_reader_connection_is_query_only(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    writer = open_writer_connection(db_path)
    writer.close()

    reader = open_reader_connection(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT OR IGNORE INTO sources(source_key) VALUES ('x')")
    finally:
        reader.close()


def test_reader_connection_missing_database_raises(tmp_path):
    db_path = tmp_path / "does-not-exist.sqlite3"
    with pytest.raises(DatabaseUnavailableError):
        open_reader_connection(db_path)


def test_schema_version_mismatch_fails_closed_for_writer(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    conn = open_writer_connection(db_path)
    conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit() if hasattr(conn, "commit") else None
    conn.close()

    with pytest.raises(SchemaIncompatibleError):
        open_writer_connection(db_path)


def test_schema_version_mismatch_fails_closed_for_reader(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    conn = open_writer_connection(db_path)
    conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    conn.close()

    with pytest.raises(SchemaIncompatibleError):
        open_reader_connection(db_path)


def test_schema_creation_is_idempotent_across_writer_opens(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    first = open_writer_connection(db_path)
    first.close()

    second = open_writer_connection(db_path)  # must not error re-creating
    second.close()


# --- FTS5 probe --------------------------------------------------------------


def test_fts5_probe_leaves_no_persistent_table(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"
    conn = open_writer_connection(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert not any("probe" in name for name in tables)
    finally:
        conn.close()


def test_fts5_probe_uses_temp_schema(tmp_path):
    db_path = tmp_path / "knowledge_index.sqlite3"

    executed = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(executed.append)
        return conn

    original_connect = sqlite3.connect
    sqlite3.connect = spy_connect
    try:
        conn = open_writer_connection(db_path)
    finally:
        sqlite3.connect = original_connect
    conn.close()

    probe_statements = [s for s in executed if "fts5_probe" in s]
    assert probe_statements
    # Every statement this module itself issued against the probe table
    # (as opposed to FTS5's own internal shadow-table bookkeeping, shown
    # as "-- ..." comments) must target the temp schema.
    own_statements = [s for s in probe_statements if not s.startswith("--")]
    assert own_statements
    assert all("temp.__ai_os_fts5_probe" in s for s in own_statements)
    # And every statement anywhere - including FTS5's own internal shadow
    # tables - must live in the temp schema, never the persistent one.
    assert all("'temp'." in s or "temp." in s for s in probe_statements)
