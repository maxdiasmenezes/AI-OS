"""
SQLite connection management, schema, and FTS5 availability for
kernel/knowledge_base/ (Milestone 36).

The database location is derived exclusively from the existing
`knowledge.storage_dir` setting in kernel/config/config.yaml (the same
canonical setting kernel/config/config.py itself uses for
JSONKnowledgeStore) - never a second, duplicate storage-location setting.
Its filename is fixed: knowledge_index.sqlite3. There is no
`database_path` override in kernel/config/knowledge_base.yaml in v1.

Two distinct connection modes exist:

- A writer connection (open_writer_connection()) sets WAL journal mode
  (only ever set here, once, during writable initialization - never on a
  read-only search connection) plus synchronous=NORMAL, runs the FTS5
  availability probe, and ensures the schema exists/matches.
- A reader connection (open_reader_connection()) sets PRAGMA
  query_only=ON so a bug in search.py can never write, and never touches
  journal_mode - it only verifies the schema version it finds.

Both always set foreign_keys=ON and a busy_timeout, so a lock contention
(another ingestion in progress) surfaces as the fixed "database locked"
condition rather than hanging indefinitely.
"""

import sqlite3
from pathlib import Path

import yaml

from kernel.knowledge_base.traversal import validate_existing_directory
from kernel.knowledge_base.types import (
    DatabaseUnavailableError,
    FTS5UnavailableError,
    SchemaIncompatibleError,
)

# kernel/knowledge_base/db.py -> kernel/knowledge_base -> kernel -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_YAML_PATH = _PROJECT_ROOT / "kernel" / "config" / "config.yaml"

DATABASE_FILENAME = "knowledge_index.sqlite3"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        source_key       TEXT PRIMARY KEY,
        generation       INTEGER NOT NULL DEFAULT 0,
        document_count   INTEGER NOT NULL DEFAULT 0,
        chunk_count      INTEGER NOT NULL DEFAULT 0,
        last_ingested_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
        id                INTEGER PRIMARY KEY,
        source_key        TEXT NOT NULL REFERENCES sources(source_key) ON DELETE CASCADE,
        relative_path     TEXT NOT NULL,
        relative_path_key TEXT NOT NULL,
        content_hash      TEXT NOT NULL,
        byte_size         INTEGER NOT NULL,
        mtime_ns          INTEGER NOT NULL,
        chunk_count       INTEGER NOT NULL,
        ingested_at       TEXT NOT NULL,
        UNIQUE(source_key, relative_path_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id            INTEGER PRIMARY KEY,
        document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        chunk_ordinal INTEGER NOT NULL,
        chunk_id      TEXT NOT NULL UNIQUE,
        text          TEXT NOT NULL,
        char_start    INTEGER NOT NULL,
        char_end      INTEGER NOT NULL,
        UNIQUE(document_id, chunk_ordinal)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        text,
        content='chunks',
        content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END
    """,
    "CREATE INDEX IF NOT EXISTS documents_source_idx ON documents(source_key)",
    "CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks(document_id)",
)


def resolve_database_path(config_yaml_path: Path | None = None) -> Path:
    """Derive the knowledge index database path exclusively from the
    existing `knowledge.storage_dir` setting in kernel/config/config.yaml.
    The resolved storage directory must already exist and be an actual
    directory (never a symlink/junction/reparse point/special file); it
    is never created automatically."""

    resolved_config_path = config_yaml_path or _DEFAULT_CONFIG_YAML_PATH
    try:
        with open(resolved_config_path, "r", encoding="utf-8") as f:
            settings = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise DatabaseUnavailableError("knowledge database is unavailable") from exc

    try:
        storage_dir_setting = settings["knowledge"]["storage_dir"]
    except (KeyError, TypeError) as exc:
        raise DatabaseUnavailableError("knowledge database is unavailable") from exc

    if not isinstance(storage_dir_setting, str) or not storage_dir_setting.strip():
        raise DatabaseUnavailableError("knowledge database is unavailable")

    storage_dir = _PROJECT_ROOT / storage_dir_setting
    canonical_storage_dir = validate_existing_directory(storage_dir)
    return canonical_storage_dir / DATABASE_FILENAME


def _probe_fts5(conn: sqlite3.Connection) -> None:
    """Verify FTS5 can create, populate, and query a table - using a
    temporary (connection-local, non-persistent) schema object, never the
    real chunks_fts table. Never falls back to LIKE/substring search or
    another dependency; raises FTS5UnavailableError instead."""

    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__ai_os_fts5_probe USING fts5(value)")
    except sqlite3.OperationalError as exc:
        raise FTS5UnavailableError("knowledge search is unavailable on this system") from exc

    try:
        conn.execute("INSERT INTO temp.__ai_os_fts5_probe(value) VALUES ('probe')")
        conn.execute(
            "SELECT value FROM temp.__ai_os_fts5_probe WHERE __ai_os_fts5_probe MATCH 'probe'"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise FTS5UnavailableError("knowledge search is unavailable on this system") from exc
    finally:
        conn.execute("DROP TABLE temp.__ai_os_fts5_probe")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None  # schema_meta doesn't exist yet - a fresh database.

    if row is not None:
        if row[0] != str(SCHEMA_VERSION):
            raise SchemaIncompatibleError("knowledge database schema is incompatible")
        return

    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise DatabaseUnavailableError("knowledge database is unavailable") from exc


def _verify_schema_version(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise SchemaIncompatibleError("knowledge database schema is incompatible") from exc

    if row is None or row[0] != str(SCHEMA_VERSION):
        raise SchemaIncompatibleError("knowledge database schema is incompatible")


def open_writer_connection(db_path: Path) -> sqlite3.Connection:
    """Open (creating the file if absent) a single-writer connection:
    foreign keys on, WAL journal mode, synchronous=NORMAL, a busy
    timeout, the FTS5 probe, and schema creation/verification. Only ever
    one such connection should hold the write lock on one database at a
    time - BEGIN IMMEDIATE (used by ingest.py) enforces that at the
    SQLite level."""

    try:
        conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError("knowledge database is unavailable") from exc

    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _probe_fts5(conn)
        _ensure_schema(conn)
    except Exception:
        conn.close()
        raise

    return conn


def open_reader_connection(db_path: Path) -> sqlite3.Connection:
    """Open a read-only-by-policy connection for search: foreign keys on,
    a busy timeout, and PRAGMA query_only=ON so a bug here can never
    write. Never sets journal_mode. Never creates the database file - a
    missing file is treated as "database unavailable", not silently
    created empty."""

    if not db_path.exists():
        raise DatabaseUnavailableError("knowledge database is unavailable")

    try:
        conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError("knowledge database is unavailable") from exc

    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only = ON")
        _verify_schema_version(conn)
    except Exception:
        conn.close()
        raise

    return conn
