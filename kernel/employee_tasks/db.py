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

# Schema version 3 (Milestone 42 P1): the closed set of durable step-status
# values - mirrors kernel/employee_tasks/types.py:StepStatus exactly.
# Deliberately no 'not_started' value - see that enum's own docstring.
_STEP_STATUS_LIST_SQL = "'in_progress', 'succeeded', 'failed'"

# Schema version 3 (Milestone 42 P1): durable per-step execution progress -
# see kernel/employee_tasks/types.py:TaskStepProgress. The (task_id,
# step_position) PRIMARY KEY is the atomic claim mechanism
# TaskRepository.claim_step() relies on: SQLite itself guarantees that of
# any number of concurrent INSERTs targeting the same pair, exactly one
# succeeds. No separate index is needed for per-task lookups
# (list_step_progress()) - the PRIMARY KEY's own index already covers the
# task_id-only prefix. Defined once, as a single constant, and reused by
# BOTH _SCHEMA_STATEMENTS (a fresh v3+ database) and
# _MIGRATION_2_TO_3_STATEMENTS (an existing v2 database being migrated) -
# unlike _migrate_v1_to_v2's ALTER TABLE (which only makes sense against an
# existing table), this is a wholly new table, so the fresh-schema path and
# the migration path need byte-identical CREATE TABLE text; defining it
# once here, rather than as two independently-maintained literal strings,
# removes any risk of the two ever drifting apart - matching this module's
# own established convention of sharing a repeated fragment as one module
# constant (see _STATE_LIST_SQL/_STEP_STATUS_LIST_SQL above).
_TASK_STEP_PROGRESS_TABLE_SQL = f"""
    CREATE TABLE IF NOT EXISTS task_step_progress (
        task_id         TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        step_position   INTEGER NOT NULL CHECK (step_position >= 1),
        status          TEXT NOT NULL CHECK (status IN ({_STEP_STATUS_LIST_SQL})),
        started_at      TEXT NOT NULL,
        completed_at    TEXT,
        result_json     TEXT,
        failure_code    TEXT,
        failure_summary TEXT,
        task_version    INTEGER NOT NULL CHECK (task_version >= 1),
        PRIMARY KEY (task_id, step_position)
    )
    """

# Schema version 4 (Milestone 42 P2): durable, task-scoped pending
# confirmations - see kernel/employee_tasks/types.py:PendingTaskConfirmation.
# task_id is the PRIMARY KEY (at most one pending confirmation per task,
# guaranteed by execution being strictly sequential); confirmation_id is
# separately UNIQUE (a single-use identity token, never reused across two
# different proposals for the same task, even sequential ones - see that
# type's own docstring for why task_id alone is not enough).
#
# Deliberately NOT shared with _MIGRATION_3_TO_4_STATEMENTS below (unlike
# _TASK_STEP_PROGRESS_TABLE_SQL/_TASK_LIFECYCLE_OUTBOX_TABLE_SQL's own
# "one definition, reused by both the fresh-schema and migration paths"
# discipline): this constant represents the table's CURRENT (v6) shape,
# decision/decided_at/decision_attempt_count/decision_next_attempt_at
# columns included, for a fresh database only. A database migrating
# v3 -> v4 must get the table in its ORIGINAL v4 shape (see
# _TASK_PENDING_CONFIRMATION_TABLE_V4_SQL below) - reusing this current
# definition there would ALTER TABLE ADD COLUMN the exact same columns a
# later v5 -> v6 step then tries to add again, which SQLite rejects
# outright ("duplicate column name").
#
# decision_attempt_count/decision_next_attempt_at (Milestone 47 P2
# adversarial-review correction): the same bounded retry-scheduling shape
# task_lifecycle_outbox's own attempt_count/next_attempt_at already
# established - see TaskRepository.find_recoverable_confirmation_decision()'s
# own docstring for why this is what keeps one repeatedly-failing durable
# decision from starving every later one. Since v6 has not shipped yet,
# these are added directly to this schema version rather than a new v7.
_TASK_PENDING_CONFIRMATION_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS task_pending_confirmation (
        task_id                  TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
        confirmation_id          TEXT NOT NULL UNIQUE,
        step_position            INTEGER NOT NULL CHECK (step_position >= 1),
        action_name              TEXT NOT NULL,
        resource_key             TEXT,
        created_at               TEXT NOT NULL,
        expires_at               TEXT NOT NULL,
        decision                 TEXT CHECK (decision IS NULL OR decision IN ('confirm', 'reject')),
        decided_at               TEXT,
        decision_attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (decision_attempt_count >= 0),
        decision_next_attempt_at TEXT
    )
    """

# The table's ORIGINAL v4 shape (Milestone 42 P2), frozen exactly as
# first shipped - used ONLY by _MIGRATION_3_TO_4_STATEMENTS below, so a
# v3 -> v4 -> v5 -> v6 chain adds decision/decided_at exactly once, via
# _MIGRATION_5_TO_6_STATEMENTS' own ALTER TABLE, never twice. Never
# changed again for any future schema version - if task_pending_confirmation
# ever needs another new column, it goes on
# _TASK_PENDING_CONFIRMATION_TABLE_SQL (the current shape) and its own
# ALTER TABLE migration, exactly like decision/decided_at did, and this
# historical constant stays exactly as it is here, forever.
_TASK_PENDING_CONFIRMATION_TABLE_V4_SQL = """
    CREATE TABLE IF NOT EXISTS task_pending_confirmation (
        task_id         TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
        confirmation_id TEXT NOT NULL UNIQUE,
        step_position   INTEGER NOT NULL CHECK (step_position >= 1),
        action_name     TEXT NOT NULL,
        resource_key    TEXT,
        created_at      TEXT NOT NULL,
        expires_at      TEXT NOT NULL
    )
    """

# Schema version 5 (Milestone 47 P1): the closed set of durable
# lifecycle-outbox event kinds - mirrors
# kernel/employee_tasks/types.py:LifecycleEventKind exactly. No other
# event_kind string is ever accepted at the schema level, matching
# _STEP_STATUS_LIST_SQL's own "close the set in SQL too, not just in the
# enum" discipline above.
_LIFECYCLE_EVENT_KIND_LIST_SQL = (
    "'confirmation_required', 'task_completed', 'task_failed', 'task_cancelled'"
)

# Schema version 5 (Milestone 47 P1): the durable, at-least-once
# lifecycle-notification outbox - see
# kernel/employee_tasks/types.py:LifecycleOutboxEvent's own docstring for
# the full field-by-field rationale, and repository.py's own module
# docstring for the atomicity invariant every writer of this table must
# uphold (one row, created in the SAME transaction as the
# task_transitions row it corresponds to - never a second, later
# transaction). transition_id is UNIQUE: a given task_transitions row can
# never have more than one corresponding outbox event, structurally, not
# merely by convention. Defined once and reused by BOTH _SCHEMA_STATEMENTS
# and _MIGRATION_4_TO_5_STATEMENTS, matching every earlier new-table
# migration's own "one definition, never two independently-maintained
# copies" discipline (see _TASK_STEP_PROGRESS_TABLE_SQL/
# _TASK_PENDING_CONFIRMATION_TABLE_SQL above).
_TASK_LIFECYCLE_OUTBOX_TABLE_SQL = f"""
    CREATE TABLE IF NOT EXISTS task_lifecycle_outbox (
        event_id        TEXT PRIMARY KEY,
        task_id         TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        transition_id   INTEGER NOT NULL UNIQUE
                            REFERENCES task_transitions(transition_id) ON DELETE CASCADE,
        channel         TEXT NOT NULL,
        event_kind      TEXT NOT NULL CHECK (event_kind IN ({_LIFECYCLE_EVENT_KIND_LIST_SQL})),
        payload_json    TEXT,
        created_at      TEXT NOT NULL,
        delivered_at    TEXT,
        attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        next_attempt_at TEXT NOT NULL
    )
    """

# Schema version 5 (Milestone 47 P1): the due-event lookup index - a
# partial index (SQLite has supported `WHERE` on `CREATE INDEX` since
# 3.8.0) covering only undelivered rows, ordered for exactly the query
# recovery/immediate-delivery retry logic runs
# (next_attempt_at, event_id ASC - event_id as the stable tiebreaker for
# equal next_attempt_at values). Already-delivered rows are excluded from
# this index entirely rather than merely skipped by a WHERE clause at
# query time, keeping the recovery scan cheap regardless of how large the
# table eventually grows.
_TASK_LIFECYCLE_OUTBOX_PENDING_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS task_lifecycle_outbox_pending_idx "
    "ON task_lifecycle_outbox(next_attempt_at, event_id) WHERE delivered_at IS NULL"
)

# Schema version 6 (Milestone 47 P2 adversarial-review correction): the
# minimal durable receipt a WINNING CONFIRM/REJECT provider message leaves
# behind - see TaskRepository.record_confirmation_decision()'s own
# docstring for the exact atomicity contract (this row and the decision
# write commit in the SAME transaction, or neither does). Deliberately
# NOT a foreign key to task_pending_confirmation, because that row is
# intentionally deleted the moment the decision is consumed - this
# receipt's whole purpose is to survive that deletion, so a provider's
# later, exact redelivery of the SAME winning message can still be
# recognized (DUPLICATE_INGRESS) even after the pending confirmation row,
# and even after a process restart, is long gone. `dedup_key` is the
# PRIMARY KEY - a SHA-256 digest of the provider message ID, never the
# raw ID itself (mirrors compute_dedup_key()'s own /task-ingress
# discipline) - so SQLite itself enforces "one winning provider message
# can create at most one receipt," not merely application-level
# discipline. This is transport-dedup HISTORY only, never execution
# authority: current task/pending-confirmation state and current
# ToolsConfig/ActionRegistry remain the sole authority for whether
# anything may execute - see dispatch_confirmation_work()'s own docstring.
# No raw message text, phone number, recipient, or provider secret is
# ever stored here - only the four already-bounded identifiers already
# durable elsewhere (task_id, confirmation_id, decision, source) plus one
# timestamp.
_TASK_CONFIRMATION_INGRESS_RECEIPTS_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS task_confirmation_ingress_receipts (
        dedup_key       TEXT PRIMARY KEY,
        task_id         TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        confirmation_id TEXT NOT NULL,
        decision        TEXT NOT NULL CHECK (decision IN ('confirm', 'reject')),
        source          TEXT NOT NULL,
        accepted_at     TEXT NOT NULL
    )
    """

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
        version          INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
        plan_json        TEXT
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
    _TASK_STEP_PROGRESS_TABLE_SQL,
    _TASK_PENDING_CONFIRMATION_TABLE_SQL,
    _TASK_LIFECYCLE_OUTBOX_TABLE_SQL,
    _TASK_LIFECYCLE_OUTBOX_PENDING_INDEX_SQL,
    _TASK_CONFIRMATION_INGRESS_RECEIPTS_TABLE_SQL,
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


# Milestone 41 P2: adds the nullable plan_json column a fresh v2+ database
# already has via _SCHEMA_STATEMENTS above. Existing rows implicitly get
# plan_json = NULL (SQLite's default for a column added without an
# explicit DEFAULT), which is exactly the correct value for every
# pre-existing task - none of them has ever had a plan. Runs inside the
# same transaction as the schema_meta version bump, so a database can
# never be left claiming version 2 while still missing the column, or
# vice versa. Bumps to the fixed literal "2" - NOT str(SCHEMA_VERSION),
# which now points at the current version (3) - because this migration's
# only job is v1 -> v2; reaching a still-newer version is
# _ensure_schema()'s chaining responsibility below, not this function's.
_MIGRATION_1_TO_2_STATEMENTS = ("ALTER TABLE tasks ADD COLUMN plan_json TEXT",)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _MIGRATION_1_TO_2_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            ("2",),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise TaskStorageUnavailableError("task database is unavailable") from exc


# Milestone 42 P1: adds the task_step_progress table a fresh v3+ database
# already has via _SCHEMA_STATEMENTS above - no ALTER TABLE needed, since
# this is a wholly new table rather than a new column on an existing one.
# Same single-transaction discipline as _migrate_v1_to_v2: the new table
# and the schema_meta version bump commit together, or neither does.
# Bumps to the fixed literal "3" for the same reason _migrate_v1_to_v2
# bumps to "2" - see that function's own comment. Reuses
# _TASK_STEP_PROGRESS_TABLE_SQL (defined once, above _SCHEMA_STATEMENTS) -
# see that constant's own comment for why the fresh-schema and migration
# paths must never risk drifting apart on this table's definition.
_MIGRATION_2_TO_3_STATEMENTS = (_TASK_STEP_PROGRESS_TABLE_SQL,)


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _MIGRATION_2_TO_3_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            ("3",),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise TaskStorageUnavailableError("task database is unavailable") from exc


# Milestone 42 P2: adds the task_pending_confirmation table, in its
# ORIGINAL v4 shape - same "wholly new table, no ALTER TABLE" shape as
# _migrate_v2_to_v3. Bumps to the fixed literal "4" for the same reason
# the earlier migrations bump to their own fixed literals - see
# _migrate_v1_to_v2's comment. Reuses _TASK_PENDING_CONFIRMATION_TABLE_V4_SQL
# (defined once, above _SCHEMA_STATEMENTS) - deliberately NOT the current
# _TASK_PENDING_CONFIRMATION_TABLE_SQL, which already includes the v6
# decision/decided_at columns _MIGRATION_5_TO_6_STATEMENTS' own ALTER
# TABLE adds later in the same chain - see that historical constant's own
# comment for why reusing the current shape here would double-add them.
_MIGRATION_3_TO_4_STATEMENTS = (_TASK_PENDING_CONFIRMATION_TABLE_V4_SQL,)


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _MIGRATION_3_TO_4_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            ("4",),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise TaskStorageUnavailableError("task database is unavailable") from exc


# Milestone 47 P1: adds the task_lifecycle_outbox table (and its partial
# pending-event index) a fresh v5+ database already has via
# _SCHEMA_STATEMENTS above - same "wholly new table, no ALTER TABLE" shape
# as _migrate_v2_to_v3/_migrate_v3_to_v4. Deliberately creates the table
# EMPTY - no historical backfill for any M40-M46 terminal or
# waiting_for_confirmation transition that already exists in an upgraded
# database: AI-OS has no durable evidence of whether any of those messages
# were already delivered, and fabricating outbox rows for them risks
# resending stale/duplicate WhatsApp notifications for events that may
# have been sent (or superseded) months ago. Outbox-backed delivery only
# ever applies to a lifecycle transition created AFTER this migration is
# active - existing durable task state is otherwise completely untouched
# by this migration. Reuses _TASK_LIFECYCLE_OUTBOX_TABLE_SQL/
# _TASK_LIFECYCLE_OUTBOX_PENDING_INDEX_SQL (defined once, above
# _SCHEMA_STATEMENTS).
_MIGRATION_4_TO_5_STATEMENTS = (
    _TASK_LIFECYCLE_OUTBOX_TABLE_SQL,
    _TASK_LIFECYCLE_OUTBOX_PENDING_INDEX_SQL,
)


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _MIGRATION_4_TO_5_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            ("5",),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise TaskStorageUnavailableError("task database is unavailable") from exc


# Milestone 47 P2 (finalized during adversarial-review correction, before
# ever shipping - so these land directly in v6 rather than a new v7): adds
# decision/decided_at/decision_attempt_count/decision_next_attempt_at to a
# fresh v5 database's EXISTING task_pending_confirmation table, and the
# wholly new task_confirmation_ingress_receipts table - unlike most earlier
# migrations in this module (which add a wholly new table with no ALTER
# TABLE needed), the first four statements here are genuinely
# ALTER TABLE ADD COLUMN, since task_pending_confirmation itself already
# exists on any v4+ database. SQLite permits a CHECK constraint (and a
# constant DEFAULT) on an ADD COLUMN as long as it references only the new
# column itself (never another column) - true for all four columns below,
# so none needs a table rebuild. decision/decided_at/decision_next_attempt_at
# are nullable with no DEFAULT, so every existing row (whatever its current
# state) implicitly gets NULL for all three - exactly "no decision has ever
# been recorded for this historical row," never a fabricated one.
# decision_attempt_count gets DEFAULT 0, matching every existing row having
# made zero attempts (consistent with it never having a decision to retry
# in the first place). task_confirmation_ingress_receipts is created EMPTY,
# for the same reason task_lifecycle_outbox was in the v4 -> v5 migration:
# no durable evidence exists for whether any historical CONFIRM/REJECT
# provider message ever "won," so nothing is fabricated. Runs inside the
# same transaction as the schema_meta version bump, same as every other
# migration here.
_MIGRATION_5_TO_6_STATEMENTS = (
    "ALTER TABLE task_pending_confirmation ADD COLUMN decision TEXT "
    "CHECK (decision IS NULL OR decision IN ('confirm', 'reject'))",
    "ALTER TABLE task_pending_confirmation ADD COLUMN decided_at TEXT",
    "ALTER TABLE task_pending_confirmation ADD COLUMN decision_attempt_count "
    "INTEGER NOT NULL DEFAULT 0 CHECK (decision_attempt_count >= 0)",
    "ALTER TABLE task_pending_confirmation ADD COLUMN decision_next_attempt_at TEXT",
    _TASK_CONFIRMATION_INGRESS_RECEIPTS_TABLE_SQL,
)


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _MIGRATION_5_TO_6_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            ("6",),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise TaskStorageUnavailableError("task database is unavailable") from exc


def _ensure_schema(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None  # schema_meta doesn't exist yet - a fresh database.

    if row is not None:
        current_version = row[0]
        # Chain every migration this package still supports, in order, so
        # a writer opening ANY previously-shipped version (v1, v2, v3, or
        # already-current v4) reaches the current schema deterministically
        # in one open_writer_connection() call - never a version this
        # chain doesn't recognize as a starting point along the way.
        if current_version == "1":
            _migrate_v1_to_v2(conn)
            current_version = "2"
        if current_version == "2":
            _migrate_v2_to_v3(conn)
            current_version = "3"
        if current_version == "3":
            _migrate_v3_to_v4(conn)
            current_version = "4"
        if current_version == "4":
            _migrate_v4_to_v5(conn)
            current_version = "5"
        if current_version == "5":
            _migrate_v5_to_v6(conn)
            current_version = "6"
        if current_version == str(SCHEMA_VERSION):
            return  # already current - idempotent no-op
        raise TaskSchemaIncompatibleError("task database schema is incompatible")

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
