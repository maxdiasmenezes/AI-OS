"""
Typed, read-only source-status queries for kernel/knowledge_base/
(Milestone 37).

A caller supplies either no source keys (every approved source, sorted)
or a list of already-approved symbolic keys - never a path, never SQL.
This is the single place that queries the `sources` table for status;
scripts/knowledge.py and capabilities/knowledge_commands/ both call
get_status() instead of querying it themselves, so this SQL exists in
exactly one place in the codebase.

Uses the same read-only connection discipline as search.py
(open_reader_connection(), PRAGMA query_only=ON) - nothing here ever
writes.
"""

from pathlib import Path

from kernel.knowledge_base.config import KnowledgeBaseConfig, load_knowledge_base_config
from kernel.knowledge_base.db import open_reader_connection, resolve_database_path
from kernel.knowledge_base.types import SourceStatus, UnknownSourceError


def _validate_source_keys(
    source_keys: list[str] | None, config: KnowledgeBaseConfig
) -> list[str]:
    if source_keys is None:
        return sorted(config.approved_sources)

    normalized: list[str] = []
    for key in source_keys:
        if not isinstance(key, str):
            raise UnknownSourceError("unknown source key")
        candidate_key = key.strip().casefold()
        if candidate_key not in config.approved_sources:
            raise UnknownSourceError("unknown source key")
        normalized.append(candidate_key)
    return normalized


def get_status(
    source_keys: list[str] | None = None,
    *,
    config: KnowledgeBaseConfig | None = None,
    db_path: Path | None = None,
) -> list[SourceStatus]:
    """Read-only status for one or more approved sources, sorted by
    symbolic key. source_keys=None means every currently approved source.
    Raises UnknownSourceError if an explicitly requested key is not
    currently approved (see types.py). A database that does not exist
    yet, or a source with no row in it, is not an error - it simply means
    "not yet ingested"."""

    resolved_config = config if config is not None else load_knowledge_base_config()
    keys = _validate_source_keys(source_keys, resolved_config)

    if not keys:
        return []

    resolved_db_path = db_path if db_path is not None else resolve_database_path()

    rows_by_key: dict[str, tuple] = {}
    if resolved_db_path.exists():
        conn = open_reader_connection(resolved_db_path)
        try:
            placeholders = ", ".join("?" for _ in keys)
            rows = conn.execute(
                f"""
                SELECT source_key, generation, document_count, chunk_count, last_ingested_at
                FROM sources
                WHERE source_key IN ({placeholders})
                """,
                keys,
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            rows_by_key[row[0]] = row

    statuses = []
    for key in sorted(keys):
        row = rows_by_key.get(key)
        if row is None:
            statuses.append(
                SourceStatus(
                    source_key=key,
                    ingested=False,
                    generation=0,
                    document_count=0,
                    chunk_count=0,
                    last_ingested_at=None,
                )
            )
        else:
            _, generation, document_count, chunk_count, last_ingested_at = row
            statuses.append(
                SourceStatus(
                    source_key=key,
                    ingested=True,
                    generation=generation,
                    document_count=document_count,
                    chunk_count=chunk_count,
                    last_ingested_at=last_ingested_at,
                )
            )
    return statuses
