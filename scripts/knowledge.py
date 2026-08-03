"""
Local operational CLI for kernel/knowledge_base/ (Milestone 36).

Invoked as `uv run python -m scripts.knowledge <command> ...`, exactly
like the existing wine-domain scripts in this directory: a human-invoked,
offline surface entirely outside the runtime kernel, never reached by the
orchestrator, a capability, WhatsApp, or a model.

Grammar:

    status [--source KEY]
    ingest KEY
    search QUERY [--source KEY ...] [--limit N]

No command accepts a filesystem path, a SQL fragment, or an FTS
expression - `ingest`/`--source` only ever take a symbolic key already
approved in kernel/config/knowledge_base.yaml, and `search` only ever
takes plain query text. Every recognized failure prints one fixed,
privacy-safe message (never a path, SQL, or a traceback) and exits 1;
argument-grammar errors exit 2 (argparse's own default); success exits 0,
including a search with zero results.
"""

import argparse
import logging
import sys

from kernel.knowledge_base.config import load_knowledge_base_config
from kernel.knowledge_base.db import open_reader_connection, resolve_database_path
from kernel.knowledge_base.ingest import ingest_source
from kernel.knowledge_base.search import DEFAULT_RESULT_LIMIT, MAX_RESULT_LIMIT
from kernel.knowledge_base.search import search as knowledge_search
from kernel.knowledge_base.types import (
    DatabaseLockedError,
    DatabaseUnavailableError,
    FTS5UnavailableError,
    IngestionFailedError,
    InvalidQueryError,
    InvalidSourceContentError,
    InvalidSourceFilterError,
    KnowledgeBaseError,
    KnowledgeConfigError,
    SchemaIncompatibleError,
    SearchFailedError,
    SourceLimitExceededError,
    SourceUnavailableError,
    UnknownSourceError,
)

logger = logging.getLogger(__name__)

EXIT_SUCCESS = 0
EXIT_OPERATION_FAILED = 1
EXIT_USAGE_ERROR = 2

_ERROR_MESSAGES: dict[type, str] = {
    UnknownSourceError: "Unknown knowledge source.",
    SourceUnavailableError: "Knowledge source is not available.",
    InvalidSourceContentError: "Knowledge source contains unsupported or invalid content.",
    SourceLimitExceededError: "Knowledge source exceeds safety limits.",
    DatabaseUnavailableError: "Knowledge database is unavailable.",
    DatabaseLockedError: "Knowledge database is busy. Try again.",
    SchemaIncompatibleError: "Knowledge database schema is incompatible.",
    FTS5UnavailableError: "Knowledge search is unavailable on this system.",
    IngestionFailedError: "Ingestion failed.",
    InvalidQueryError: "Search query is invalid.",
    InvalidSourceFilterError: "Unknown knowledge source in filter.",
    SearchFailedError: "Search failed.",
    KnowledgeConfigError: "Knowledge base configuration is unavailable.",
}


def _message_for(exc: KnowledgeBaseError) -> str:
    return _ERROR_MESSAGES.get(type(exc), "Operation failed.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.knowledge",
        description="Local knowledge base operations (status, ingest, search).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show configured source status.")
    status_parser.add_argument(
        "--source", dest="source", default=None, help="Limit to one symbolic source key."
    )

    ingest_parser = subparsers.add_parser("ingest", help="Ingest one approved source.")
    ingest_parser.add_argument("source_key", help="Symbolic source key.")

    search_parser = subparsers.add_parser("search", help="Search indexed content.")
    search_parser.add_argument("query", help="Plain search text.")
    search_parser.add_argument(
        "--source",
        dest="sources",
        action="append",
        default=None,
        help="Limit to one symbolic source key (repeatable).",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_RESULT_LIMIT,
        help=f"Maximum results (1-{MAX_RESULT_LIMIT}).",
    )

    return parser


def _handle_status(args: argparse.Namespace) -> int:
    try:
        config = load_knowledge_base_config()
    except KnowledgeConfigError as exc:
        print(_message_for(exc))
        logger.warning("action=knowledge_status outcome=failed error_category=%s", type(exc).__name__)
        return EXIT_OPERATION_FAILED

    if args.source is not None:
        if args.source not in config.approved_sources:
            print(_message_for(UnknownSourceError()))
            logger.warning(
                "action=knowledge_status source_key=%s outcome=failed error_category=unknown_source",
                args.source,
            )
            return EXIT_OPERATION_FAILED
        keys = [args.source]
    else:
        keys = sorted(config.approved_sources)

    if not keys:
        print("No knowledge sources are configured.")
        logger.info("action=knowledge_status outcome=succeeded source_count=0")
        return EXIT_SUCCESS

    try:
        db_path = resolve_database_path()
    except KnowledgeBaseError as exc:
        print(_message_for(exc))
        logger.warning("action=knowledge_status outcome=failed error_category=%s", type(exc).__name__)
        return EXIT_OPERATION_FAILED

    statuses: dict[str, tuple | None] = {}
    if db_path.exists():
        try:
            conn = open_reader_connection(db_path)
        except KnowledgeBaseError as exc:
            print(_message_for(exc))
            logger.warning(
                "action=knowledge_status outcome=failed error_category=%s", type(exc).__name__
            )
            return EXIT_OPERATION_FAILED
        try:
            for key in keys:
                row = conn.execute(
                    "SELECT generation, document_count, chunk_count, last_ingested_at "
                    "FROM sources WHERE source_key = ?",
                    (key,),
                ).fetchone()
                statuses[key] = row
        finally:
            conn.close()
    else:
        statuses = {key: None for key in keys}

    for key in keys:
        row = statuses.get(key)
        if row is None:
            print(f"{key}: not yet ingested")
        else:
            generation, document_count, chunk_count, last_ingested_at = row
            print(
                f"{key}: {document_count} document(s), {chunk_count} chunk(s), "
                f"generation {generation}, last ingested {last_ingested_at}"
            )

    logger.info("action=knowledge_status outcome=succeeded source_count=%d", len(keys))
    return EXIT_SUCCESS


def _handle_ingest(args: argparse.Namespace) -> int:
    try:
        result = ingest_source(args.source_key)
    except KnowledgeBaseError as exc:
        print(_message_for(exc))
        logger.warning(
            "action=knowledge_ingest source_key=%s outcome=failed error_category=%s",
            args.source_key,
            type(exc).__name__,
        )
        return EXIT_OPERATION_FAILED

    print(f"source: {result.source_key}")
    print(f"documents indexed: {result.documents_indexed}")
    print(f"unchanged documents: {result.unchanged_documents}")
    print(f"removed documents: {result.removed_documents}")
    print(f"chunks indexed: {result.chunks_indexed}")
    print(f"generation: {result.generation}")
    print(f"duration: {result.elapsed_seconds:.3f}s")

    logger.info(
        "action=knowledge_ingest source_key=%s outcome=succeeded documents_indexed=%d "
        "chunks_indexed=%d elapsed_seconds=%.3f",
        result.source_key,
        result.documents_indexed,
        result.chunks_indexed,
        result.elapsed_seconds,
    )
    return EXIT_SUCCESS


def _handle_search(args: argparse.Namespace) -> int:
    try:
        results = knowledge_search(args.query, source_keys=args.sources, limit=args.limit)
    except KnowledgeBaseError as exc:
        print(_message_for(exc))
        logger.warning("action=knowledge_search outcome=failed error_category=%s", type(exc).__name__)
        return EXIT_OPERATION_FAILED

    if not results:
        print("No results found.")
        logger.info("action=knowledge_search outcome=succeeded result_count=0")
        return EXIT_SUCCESS

    for i, result in enumerate(results, start=1):
        print(f"{i}. [{result.source_key}] {result.relative_path} (chunk {result.chunk_ordinal})")
        print(f"   {result.excerpt}")

    logger.info("action=knowledge_search outcome=succeeded result_count=%d", len(results))
    return EXIT_SUCCESS


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        return _handle_status(args)
    if args.command == "ingest":
        return _handle_ingest(args)
    if args.command == "search":
        return _handle_search(args)

    parser.print_usage(sys.stderr)
    return EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
