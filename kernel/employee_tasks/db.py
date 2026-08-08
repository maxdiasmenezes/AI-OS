"""
SQLite connection management and schema for kernel/employee_tasks/
(Milestone 40) - the persistent AI-employee Task subsystem. See
kernel/employee_tasks/__init__.py for what this package is and, just as
importantly, what it deliberately is not.

Follows kernel/knowledge_base/db.py's established pattern:

- The database location is derived directly from a config.yaml setting
  (`tasks.storage_dir`), read here independently of
  kernel/config/config.py - exactly like kernel/knowledge_base/db.py
  reads `knowledge.storage_dir` itself, never through Config - so this
  package stays usable without being wired into Config or Orchestrator.
  Its filename is fixed: tasks.sqlite3.
- A writer connection (open_writer_connection()) sets WAL journal mode
  (only ever set here, during writable initialization), synchronous=
  NORMAL, and ensures/verifies the schema.
- A reader connection (open_reader_connection()) sets PRAGMA
  query_only=ON so a bug in a future read-only caller can never write,
  and never touches journal_mode - it only verifies the schema version
  it finds.

Both always set foreign_keys=ON and a fixed busy_timeout, so lock
contention (a concurrent writer) surfaces as the fixed "database locked"
condition rather than hanging indefinitely.

Both connections also pass check_same_thread=False: correctness across
concurrent writers comes from SQLite's own locking (BEGIN IMMEDIATE in
repository.py, plus busy_timeout above), not from confining a connection
object to the thread that created it. A caller that hands one connection
to multiple threads simultaneously is still responsible for not doing
so - sqlite3 does not serialize concurrent calls on the same connection
object for you.
"""

import os
import sqlite3
import stat as stat_module
from pathlib import Path

import yaml

from kernel.employee_tasks.types import (
    SCHEMA_VERSION,
    TaskSchemaIncompatibleError,
    TaskStorageCorruptError,
    TaskStorageUnavailableError,
)

# kernel/employee_tasks/db.py -> kernel/employee_tasks -> kernel -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_YAML_PATH = _PROJECT_ROOT / "kernel" / "config" / "config.yaml"

DATABASE_FILENAME = "tasks.sqlite3"
BUSY_TIMEOUT_MS = 5000

_STATE_LIST_SQL = (
    "'created', 'planning', 'ready', 'running', 'waiting_for_confirmation', "
    "'completed', 'failed', 'cancelled'"
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS tasks (
        task_id          TEXT PRIMARY KEY,
        display_id       TEXT NOT NULL UNIQUE,
        state            TEXT NOT NULL CHECK (state IN ({_STATE_LIST_SQL})),
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
    """,
    f"""
    CREATE TABLE IF NOT EXISTS task_transitions (
        transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id       TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        from_state    TEXT CHECK (from_state IS NULL OR from_state IN ({_STATE_LIST_SQL})),
        to_state      TEXT NOT NULL CHECK (to_state IN ({_STATE_LIST_SQL})),
        timestamp     TEXT NOT NULL,
        reason_code   TEXT,
        safe_summary  TEXT,
        task_version  INTEGER NOT NULL CHECK (task_version >= 1)
    )
    """,
    "CREATE INDEX IF NOT EXISTS tasks_created_idx ON tasks(created_at, task_id)",
    "CREATE INDEX IF NOT EXISTS transitions_task_idx ON task_transitions(task_id, transition_id)",
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(lst: os.stat_result) -> bool:
    attrs = getattr(lst, "st_file_attributes", None)
    if attrs is None:
        return False
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _validate_existing_directory(configured_path: Path) -> Path:
    """Validate that `configured_path` (already absolute) exists, is an
    actual directory, and is not a symlink/junction/reparse point/special
    file - a small, private copy of
    kernel/knowledge_base/traversal.py's validate_existing_directory(),
    duplicated rather than imported so this package has no dependency on
    kernel/knowledge_base/. Never creates the directory."""

    try:
        lst = os.lstat(configured_path)
    except OSError as exc:
        raise TaskStorageUnavailableError("task storage directory is not available") from exc

    if (
        stat_module.S_ISLNK(lst.st_mode)
        or _is_reparse_point(lst)
        or not stat_module.S_ISDIR(lst.st_mode)
    ):
        raise TaskStorageUnavailableError("task storage directory is not available")

    try:
        canonical = configured_path.resolve(strict=True)
    except OSError as exc:
        raise TaskStorageUnavailableError("task storage directory is not available") from exc

    if not canonical.is_dir():
        raise TaskStorageUnavailableError("task storage directory is not available")

    return canonical


def resolve_database_path(config_yaml_path: Path | None = None) -> Path:
    """Derive the task database path exclusively from the `tasks.storage_dir`
    setting in kernel/config/config.yaml - never a second, duplicate
    storage-location setting. The resolved storage directory must already
    exist and be an actual directory (never a symlink/junction/reparse
    point/special file); it is never created automatically."""

    resolved_config_path = config_yaml_path or _DEFAULT_CONFIG_YAML_PATH
    try:
        with open(resolved_config_path, "r", encoding="utf-8") as f:
            settings = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise TaskStorageUnavailableError("task database is unavailable") from exc

    try:
        storage_dir_setting = settings["tasks"]["storage_dir"]
    except (KeyError, TypeError) as exc:
        raise TaskStorageUnavailableError("task database is unavailable") from exc

    if not isinstance(storage_dir_setting, str) or not storage_dir_setting.strip():
        raise TaskStorageUnavailableError("task database is unavailable")

    storage_dir = _PROJECT_ROOT / storage_dir_setting
    canonical_storage_dir = _validate_existing_directory(storage_dir)
    return canonical_storage_dir / DATABASE_FILENAME


def _ensure_schema(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None  # schema_meta doesn't exist yet - a fresh database.

    if row is not None:
        if row[0] != str(SCHEMA_VERSION):
            raise TaskSchemaIncompatibleError("task database schema is incompatible")
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
        raise TaskStorageUnavailableError("task database is unavailable") from exc


def _verify_schema(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise TaskSchemaIncompatibleError("task database schema is incompatible") from exc

    if row is None or row[0] != str(SCHEMA_VERSION):
        raise TaskSchemaIncompatibleError("task database schema is incompatible")


def open_writer_connection(db_path: Path) -> sqlite3.Connection:
    """Open (creating the file if absent) a single-writer connection:
    foreign keys on, WAL journal mode, synchronous=NORMAL, a busy
    timeout, and schema creation/verification. Only ever one such
    connection should hold the write lock on one database at a time -
    BEGIN IMMEDIATE (used throughout repository.py) enforces that at the
    SQLite level."""

    try:
        conn = sqlite3.connect(
            str(db_path),
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
    except sqlite3.Error as exc:
        raise TaskStorageUnavailableError("task database is unavailable") from exc

    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _ensure_schema(conn)
    except Exception:
        conn.close()
        raise

    return conn


def open_reader_connection(db_path: Path) -> sqlite3.Connection:
    """Open a read-only-by-policy connection: foreign keys on, a busy
    timeout, and PRAGMA query_only=ON so a bug in a read-only caller can
    never write. Never sets journal_mode. Never creates the database
    file - a missing file is treated as "database unavailable", not
    silently created empty."""

    if not os.path.exists(db_path):
        raise TaskStorageUnavailableError("task database is unavailable")

    try:
        conn = sqlite3.connect(
            str(db_path),
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
    except sqlite3.Error as exc:
        raise TaskStorageUnavailableError("task database is unavailable") from exc

    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only = ON")
        _verify_schema(conn)
    except Exception:
        conn.close()
        raise

    return conn


def check_integrity(conn: sqlite3.Connection) -> bool:
    """Run PRAGMA integrity_check and raise TaskStorageCorruptError unless
    SQLite reports exactly "ok" - covering both an explicit non-"ok"
    result and the PRAGMA itself failing outright (e.g. a malformed file
    header). Returns True on success; corruption is always a raised,
    typed error, never a silent bool a caller could forget to check.
    Distinct from a schema version mismatch
    (TaskSchemaIncompatibleError), which is a perfectly readable file
    written by, or intended for, a different schema version."""

    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise TaskStorageCorruptError("task database failed integrity check") from exc

    if len(rows) == 1 and rows[0][0] == "ok":
        return True
    raise TaskStorageCorruptError("task database failed integrity check")
