"""Tests for capabilities/knowledge_commands/capability.py: KnowledgeCommandsCapability."""

import logging

import pytest

from kernel.knowledge_base.config import KnowledgeBaseConfig, SourceSpec
from kernel.knowledge_base.ingest import ingest_source
from kernel.knowledge_base.types import (
    DatabaseUnavailableError,
    IngestResult,
    KnowledgeSearchResult,
    UnknownSourceError,
)
from kernel.tools.confirmation import ConfirmationStore

from capabilities.knowledge_commands.capability import (
    HELP_TEXT,
    KnowledgeCommandsCapability,
    default_knowledge_confirmation_store,
)


# --- test doubles ------------------------------------------------------


class _RecordingFn:
    """A fake status/search/ingest function that records every call and
    returns (or raises) a fixed, injected result."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class _ConfigHolder:
    def __init__(self, config):
        self.current = config


def _config(sources: dict = None) -> KnowledgeBaseConfig:
    sources = sources or {}
    return KnowledgeBaseConfig(
        approved_sources={key: SourceSpec(path=str(path), recursive=True) for key, path in sources.items()}
    )


def _make_capability(
    config=None,
    confirmation_store=None,
    status_fn=None,
    search_fn=None,
    ingest_fn=None,
    audit_record=None,
    db_path=None,
):
    if confirmation_store is None:
        confirmation_store = ConfirmationStore()
    if config is None:
        config = _config()
    capability = KnowledgeCommandsCapability(
        None,
        None,
        None,
        confirmation_store=confirmation_store,
        config_loader=lambda: config,
        db_path=db_path,
        status_fn=status_fn,
        search_fn=search_fn,
        ingest_fn=ingest_fn,
        audit_record=audit_record,
    )
    return capability, confirmation_store


def _audit_recorder():
    calls = []

    def record(action, resource_key, outcome):
        calls.append((action, resource_key, outcome))

    return record, calls


# --- basic identity -------------------------------------------------------


def test_requires_computer_actions_is_true():
    capability, _ = _make_capability()
    assert capability.requires_computer_actions is True
    assert capability.id == "knowledge"
    assert capability.name == "knowledge"


def test_parse_error_returns_fixed_reply():
    capability, _ = _make_capability()
    assert capability.handle("not a knowledge command") == "Invalid knowledge command. Use /knowledge help."


def test_help_returns_fixed_help_text():
    capability, _ = _make_capability()
    assert capability.handle("/knowledge help") == HELP_TEXT
    assert capability.handle("/knowledge") == HELP_TEXT


# --- routing/execution: status ----------------------------------------


def test_status_calls_get_status_with_no_confirmation():
    fake_status = _RecordingFn(result=[])
    capability, store = _make_capability(status_fn=fake_status)

    capability.handle("/knowledge status")

    assert len(fake_status.calls) == 1
    args, kwargs = fake_status.calls[0]
    assert args[0] is None  # no --source given
    pending, _ = store.consume()
    assert pending is None  # status never proposes anything


def test_status_with_source_passes_single_element_list():
    fake_status = _RecordingFn(result=[])
    capability, _ = _make_capability(status_fn=fake_status)

    capability.handle("/knowledge status --source ai_os_docs")

    args, kwargs = fake_status.calls[0]
    assert args[0] == ["ai_os_docs"]


def test_status_error_returns_fixed_message_and_audits_failed():
    fake_status = _RecordingFn(error=UnknownSourceError("unknown source key"))
    record, calls = _audit_recorder()
    capability, _ = _make_capability(status_fn=fake_status, audit_record=record)

    response = capability.handle("/knowledge status --source bogus")

    assert response == "Unknown knowledge source."
    assert ("knowledge_status", "bogus", "failed") in calls


# --- routing/execution: search -----------------------------------------


def test_search_calls_search_with_no_confirmation():
    fake_search = _RecordingFn(result=[])
    capability, store = _make_capability(search_fn=fake_search)

    capability.handle("/knowledge search -- repository backup")

    assert len(fake_search.calls) == 1
    args, kwargs = fake_search.calls[0]
    assert args[0] == "repository backup"
    assert kwargs["limit"] == 5  # default
    pending, _ = store.consume()
    assert pending is None


def test_search_passes_source_filter_and_limit():
    fake_search = _RecordingFn(result=[])
    capability, _ = _make_capability(search_fn=fake_search)

    capability.handle("/knowledge search --source ai_os_docs --limit 3 -- backup")

    args, kwargs = fake_search.calls[0]
    assert kwargs["source_keys"] == ["ai_os_docs"]
    assert kwargs["limit"] == 3


def test_search_never_requests_more_than_ten_results():
    fake_search = _RecordingFn(result=[])
    capability, _ = _make_capability(search_fn=fake_search)

    capability.handle("/knowledge search --limit 10 -- backup")

    args, kwargs = fake_search.calls[0]
    assert kwargs["limit"] == 10


def test_search_error_returns_fixed_message():
    fake_search = _RecordingFn(error=DatabaseUnavailableError("knowledge database is unavailable"))
    capability, _ = _make_capability(search_fn=fake_search)

    response = capability.handle("/knowledge search -- backup")

    assert response == "Knowledge database is unavailable."


# --- routing/execution: ingest / confirmation ---------------------------


def test_ingest_is_proposed_not_executed_immediately(tmp_path):
    fake_ingest = _RecordingFn()
    config = _config({"ai_os_docs": tmp_path})
    capability, store = _make_capability(config=config, ingest_fn=fake_ingest)

    response = capability.handle("/knowledge ingest ai_os_docs")

    assert "confirm" in response.lower()
    assert fake_ingest.calls == []
    pending, expired = store.consume()
    assert expired is False
    assert pending.action == "knowledge_ingest"
    assert pending.resource_key == "ai_os_docs"


def test_ingest_proposal_prompt_is_fixed_and_symbolic(tmp_path):
    config = _config({"ai_os_docs": tmp_path})
    capability, _ = _make_capability(config=config, ingest_fn=_RecordingFn())

    response = capability.handle("/knowledge ingest ai_os_docs")

    assert response == (
        "This will refresh locally indexed knowledge for 'ai_os_docs'.\n"
        "Reply /knowledge confirm within 2 minutes to proceed, or /knowledge cancel."
    )
    assert str(tmp_path) not in response


def test_ingest_unknown_source_cannot_create_confirmation():
    capability, store = _make_capability(config=_config({}), ingest_fn=_RecordingFn())

    response = capability.handle("/knowledge ingest does-not-exist")

    assert response == "Unknown knowledge source."
    pending, _ = store.consume()
    assert pending is None


def test_confirm_executes_the_pending_ingestion_exactly_once(tmp_path):
    result = IngestResult(
        source_key="ai_os_docs",
        documents_indexed=3,
        unchanged_documents=0,
        removed_documents=0,
        chunks_indexed=12,
        generation=1,
        elapsed_seconds=0.05,
    )
    fake_ingest = _RecordingFn(result=result)
    config = _config({"ai_os_docs": tmp_path})
    capability, store = _make_capability(config=config, ingest_fn=fake_ingest)

    capability.handle("/knowledge ingest ai_os_docs")
    response = capability.handle("/knowledge confirm")

    assert len(fake_ingest.calls) == 1
    assert fake_ingest.calls[0][0][0] == "ai_os_docs"
    assert "source: ai_os_docs" in response
    assert "documents indexed: 3" in response

    second = capability.handle("/knowledge confirm")
    assert len(fake_ingest.calls) == 1  # not called again
    assert "no pending action" in second.lower()


def test_confirm_accepts_no_arguments_and_no_replacement_source(tmp_path):
    config = _config({"ai_os_docs": tmp_path})
    fake_ingest = _RecordingFn(
        result=IngestResult("ai_os_docs", 0, 0, 0, 0, 1, 0.01)
    )
    capability, _ = _make_capability(config=config, ingest_fn=fake_ingest)

    capability.handle("/knowledge ingest ai_os_docs")
    # An altered follow-up with an extra token is a parse error, not a
    # way to redirect confirmation to a different source.
    altered_response = capability.handle("/knowledge confirm some_other_source")

    assert altered_response == "Invalid knowledge command. Use /knowledge help."
    assert fake_ingest.calls == []  # nothing executed by the malformed attempt

    confirm_response = capability.handle("/knowledge confirm")
    assert fake_ingest.calls[0][0][0] == "ai_os_docs"
    assert "source: ai_os_docs" in confirm_response


def test_confirm_with_nothing_pending():
    capability, _ = _make_capability()
    response = capability.handle("/knowledge confirm")
    assert "no pending action" in response.lower()


def test_confirm_with_expired_confirmation(tmp_path):
    config = _config({"ai_os_docs": tmp_path})
    store = ConfirmationStore(ttl_seconds=0.01)
    fake_ingest = _RecordingFn()
    capability, _ = _make_capability(config=config, confirmation_store=store, ingest_fn=fake_ingest)

    capability.handle("/knowledge ingest ai_os_docs")
    import time

    time.sleep(0.05)
    response = capability.handle("/knowledge confirm")

    assert "expired" in response.lower()
    assert fake_ingest.calls == []


def test_cancel_prevents_execution(tmp_path):
    config = _config({"ai_os_docs": tmp_path})
    fake_ingest = _RecordingFn()
    capability, _ = _make_capability(config=config, ingest_fn=fake_ingest)

    capability.handle("/knowledge ingest ai_os_docs")
    cancel_response = capability.handle("/knowledge cancel")
    confirm_response = capability.handle("/knowledge confirm")

    assert "cancelled" in cancel_response.lower()
    assert "no pending action" in confirm_response.lower()
    assert fake_ingest.calls == []


def test_cancel_with_nothing_pending():
    capability, _ = _make_capability()
    response = capability.handle("/knowledge cancel")
    assert "no pending action" in response.lower()


def test_invalid_pending_action_name_cannot_execute():
    store = ConfirmationStore()
    store.propose("some_other_action", "ai_os_docs")
    fake_ingest = _RecordingFn()
    capability, _ = _make_capability(confirmation_store=store, ingest_fn=fake_ingest)

    response = capability.handle("/knowledge confirm")

    assert fake_ingest.calls == []
    assert "no pending action" in response.lower()


def test_configuration_removal_before_confirmation_fails_closed(tmp_path):
    holder = _ConfigHolder(_config({"ai_os_docs": tmp_path}))
    fake_ingest = _RecordingFn()
    store = ConfirmationStore()
    capability = KnowledgeCommandsCapability(
        None, None, None,
        confirmation_store=store,
        config_loader=lambda: holder.current,
        ingest_fn=fake_ingest,
    )

    capability.handle("/knowledge ingest ai_os_docs")
    holder.current = _config({})  # source removed from the allowlist
    response = capability.handle("/knowledge confirm")

    assert fake_ingest.calls == []
    assert response == "Unknown knowledge source."


def test_failed_ingestion_returns_fixed_message(tmp_path):
    config = _config({"ai_os_docs": tmp_path})
    fake_ingest = _RecordingFn(error=DatabaseUnavailableError("knowledge database is unavailable"))
    capability, _ = _make_capability(config=config, ingest_fn=fake_ingest)

    capability.handle("/knowledge ingest ai_os_docs")
    response = capability.handle("/knowledge confirm")

    assert response == "Knowledge database is unavailable."


def test_pending_state_contains_no_query_path_or_message_body(tmp_path):
    config = _config({"ai_os_docs": tmp_path})
    store = ConfirmationStore()
    capability, _ = _make_capability(config=config, confirmation_store=store, ingest_fn=_RecordingFn())

    capability.handle("/knowledge ingest ai_os_docs")

    pending, _ = store.consume()
    assert pending.action == "knowledge_ingest"
    assert pending.resource_key == "ai_os_docs"
    # PendingAction is a frozen dataclass with exactly these two fields -
    # there is no field capable of carrying a path, query, or message.
    assert set(vars(pending).keys()) == {"action", "resource_key"}


# --- separate confirmation store ----------------------------------------


def test_default_knowledge_store_is_a_separate_instance_from_task_default_store():
    from kernel.tools.confirmation import default_store as task_default_store

    assert default_knowledge_confirmation_store is not task_default_store


def test_knowledge_confirmation_does_not_overwrite_task_pending_action(tmp_path):
    from kernel.tools.confirmation import default_store as task_default_store

    task_default_store.cancel()  # start clean
    task_default_store.propose("open_application", "notepad")

    config = _config({"ai_os_docs": tmp_path})
    knowledge_store = ConfirmationStore()
    capability, _ = _make_capability(config=config, confirmation_store=knowledge_store, ingest_fn=_RecordingFn())
    capability.handle("/knowledge ingest ai_os_docs")

    pending, _ = task_default_store.consume()
    assert pending is not None
    assert pending.action == "open_application"
    assert pending.resource_key == "notepad"


def test_task_pending_action_does_not_overwrite_knowledge_store(tmp_path):
    from kernel.tools.confirmation import default_store as task_default_store

    config = _config({"ai_os_docs": tmp_path})
    knowledge_store = ConfirmationStore()
    capability, _ = _make_capability(config=config, confirmation_store=knowledge_store, ingest_fn=_RecordingFn())
    capability.handle("/knowledge ingest ai_os_docs")

    task_default_store.cancel()
    task_default_store.propose("run_registered_script", "backup_wine_data")

    pending, _ = knowledge_store.consume()
    assert pending is not None
    assert pending.action == "knowledge_ingest"
    assert pending.resource_key == "ai_os_docs"

    task_default_store.cancel()


# --- audit ---------------------------------------------------------------


def test_ingest_audit_lifecycle(tmp_path):
    result = IngestResult("ai_os_docs", 1, 0, 0, 5, 1, 0.02)
    config = _config({"ai_os_docs": tmp_path})
    record, calls = _audit_recorder()
    capability, _ = _make_capability(
        config=config, ingest_fn=_RecordingFn(result=result), audit_record=record
    )

    capability.handle("/knowledge ingest ai_os_docs")
    capability.handle("/knowledge confirm")

    assert ("knowledge_ingest", "ai_os_docs", "proposed") in calls
    assert ("knowledge_ingest", "ai_os_docs", "confirmed") in calls
    assert ("knowledge_ingest", "ai_os_docs", "executed") in calls


def test_cancel_audit_uses_confirmation_action():
    record, calls = _audit_recorder()
    capability, _ = _make_capability(config=_config({"a": "x"}), ingest_fn=_RecordingFn(), audit_record=record)

    capability.handle("/knowledge ingest a")
    capability.handle("/knowledge cancel")

    # Matches capabilities/tasks/TasksCapability's own convention: cancel
    # audits under the generic "confirmation" action with resource_key=None,
    # not the specific pending action's own resource key.
    assert ("confirmation", None, "cancelled") in calls


def test_audit_write_failure_does_not_crash_or_change_response():
    def raising_record(action, resource_key, outcome):
        raise RuntimeError("disk full")

    capability, _ = _make_capability(status_fn=_RecordingFn(result=[]), audit_record=raising_record)

    response = capability.handle("/knowledge status")  # must not raise
    assert response == "No knowledge sources are configured."


def test_audit_never_contains_query_excerpt_or_path():
    fake_search = _RecordingFn(
        result=[
            KnowledgeSearchResult(
                source_key="ai_os_docs",
                relative_path="notes/a.md",
                chunk_ordinal=0,
                excerpt="a very specific secret excerpt token",
                rank=-1.5,
                chunk_id="abc123",
            )
        ]
    )
    record, calls = _audit_recorder()
    capability, _ = _make_capability(search_fn=fake_search, audit_record=record)

    capability.handle("/knowledge search -- a-very-specific-secret-query-token")

    for action, resource_key, outcome in calls:
        assert "a-very-specific-secret-query-token" not in str(resource_key)
        assert "a very specific secret excerpt token" not in str(resource_key)
        assert "notes/a.md" not in str(resource_key)


# --- privacy: logs -------------------------------------------------------


def test_query_absent_from_logs(caplog):
    fake_search = _RecordingFn(result=[])
    capability, _ = _make_capability(search_fn=fake_search)

    with caplog.at_level(logging.DEBUG):
        capability.handle("/knowledge search -- a-very-specific-secret-query-token")

    for record in caplog.records:
        assert "a-very-specific-secret-query-token" not in record.getMessage()


def test_excerpt_absent_from_logs(caplog):
    fake_search = _RecordingFn(
        result=[
            KnowledgeSearchResult(
                source_key="ai_os_docs",
                relative_path="a.md",
                chunk_ordinal=0,
                excerpt="a very specific secret excerpt token",
                rank=-1.0,
                chunk_id="abc",
            )
        ]
    )
    capability, _ = _make_capability(search_fn=fake_search)

    with caplog.at_level(logging.DEBUG):
        capability.handle("/knowledge search -- backup")

    for record in caplog.records:
        assert "a very specific secret excerpt token" not in record.getMessage()


def test_parse_error_never_contains_raw_prompt():
    capability, _ = _make_capability()
    secret_prompt = "/knowledge search a-very-specific-secret-query-token"  # missing delimiter

    response = capability.handle(secret_prompt)

    assert "a-very-specific-secret-query-token" not in response


# --- privacy: reply content ------------------------------------------------


def test_configured_path_absent_from_search_reply(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("Repository backup notes for AI-OS.", encoding="utf-8")
    config = _config({"ai_os_docs": docs})
    db_path = tmp_path / "knowledge_index.sqlite3"
    ingest_source("ai_os_docs", config=config, db_path=db_path)

    capability, _ = _make_capability(config=config, db_path=db_path)
    response = capability.handle("/knowledge search -- repository backup")

    assert str(docs) not in response
    assert str(db_path) not in response
    assert "a.md" in response


def test_no_bm25_rank_or_chunk_id_in_reply():
    fake_search = _RecordingFn(
        result=[
            KnowledgeSearchResult(
                source_key="ai_os_docs",
                relative_path="a.md",
                chunk_ordinal=0,
                excerpt="excerpt text",
                rank=-12.3456,
                chunk_id="deadbeefcafebabe",
            )
        ]
    )
    capability, _ = _make_capability(search_fn=fake_search)

    response = capability.handle("/knowledge search -- backup")

    assert "-12.3456" not in response
    assert "deadbeefcafebabe" not in response


def test_sql_and_fts_text_absent_from_search_reply():
    fake_search = _RecordingFn(result=[])
    capability, _ = _make_capability(search_fn=fake_search)

    response = capability.handle('/knowledge search -- "; DROP TABLE chunks; --')

    assert "DROP TABLE" not in response
    assert response == "No results found."


# --- reply formatting: help / status --------------------------------------


def test_status_no_sources_configured():
    capability, _ = _make_capability(status_fn=_RecordingFn(result=[]))
    response = capability.handle("/knowledge status")
    assert response == "No knowledge sources are configured."


def test_status_never_ingested_format():
    from kernel.knowledge_base.types import SourceStatus

    fake_status = _RecordingFn(
        result=[
            SourceStatus(
                source_key="ai_os_docs",
                ingested=False,
                generation=0,
                document_count=0,
                chunk_count=0,
                last_ingested_at=None,
            )
        ]
    )
    capability, _ = _make_capability(status_fn=fake_status)

    response = capability.handle("/knowledge status")
    assert response == "ai_os_docs: not yet ingested"


def test_status_ingested_format():
    from kernel.knowledge_base.types import SourceStatus

    fake_status = _RecordingFn(
        result=[
            SourceStatus(
                source_key="ai_os_docs",
                ingested=True,
                generation=1,
                document_count=3,
                chunk_count=111,
                last_ingested_at="2026-08-01T12:00:00+00:00",
            )
        ]
    )
    capability, _ = _make_capability(status_fn=fake_status)

    response = capability.handle("/knowledge status")
    assert response == (
        "ai_os_docs: 3 document(s), 111 chunk(s), generation 1, "
        "last ingested 2026-08-01T12:00:00+00:00"
    )


def test_status_ingested_empty_format():
    from kernel.knowledge_base.types import SourceStatus

    fake_status = _RecordingFn(
        result=[
            SourceStatus(
                source_key="empty_source",
                ingested=True,
                generation=1,
                document_count=0,
                chunk_count=0,
                last_ingested_at="2026-08-01T12:00:00+00:00",
            )
        ]
    )
    capability, _ = _make_capability(status_fn=fake_status)

    response = capability.handle("/knowledge status")
    assert "empty_source: 0 document(s), 0 chunk(s)" in response


# --- reply formatting: search -----------------------------------------


def _result(i, excerpt_len=20, path_len=10, source_key="ai_os_docs"):
    return KnowledgeSearchResult(
        source_key=source_key,
        relative_path="p" * path_len + ".md",
        chunk_ordinal=i,
        excerpt="e" * excerpt_len,
        rank=-1.0,
        chunk_id=f"chunk{i}",
    )


# A maximum-shape source key (64 chars, the longest _SOURCE_KEY_RE allows)
# so worst-case blocks reliably exceed MAX_INTERFACE_REPLY_CHARACTERS and
# exercise the omission path deterministically, rather than depending on
# a borderline size that may or may not overflow the budget.
_MAX_SOURCE_KEY = "s" + "o" * 63


def test_search_no_results():
    capability, _ = _make_capability(search_fn=_RecordingFn(result=[]))
    response = capability.handle("/knowledge search -- nonexistent")
    assert response == "No results found."


def test_search_result_defaults_to_five():
    fake_search = _RecordingFn(result=[])
    capability, _ = _make_capability(search_fn=fake_search)

    capability.handle("/knowledge search -- backup")

    assert fake_search.calls[0][1]["limit"] == 5


def test_search_excerpt_truncates_at_two_hundred_with_ellipsis():
    fake_search = _RecordingFn(result=[_result(1, excerpt_len=500)])
    capability, _ = _make_capability(search_fn=fake_search)

    response = capability.handle("/knowledge search -- backup")

    body_line = response.splitlines()[1].strip()
    assert len(body_line) == 200
    assert body_line.endswith("...")


def test_search_path_truncates_at_eighty_with_ellipsis():
    fake_search = _RecordingFn(result=[_result(1, path_len=500)])
    capability, _ = _make_capability(search_fn=fake_search)

    response = capability.handle("/knowledge search -- backup")

    header_line = response.splitlines()[0]
    # header format: "1. [ai_os_docs] <path> (chunk 1)"
    path_part = header_line.split("] ", 1)[1].rsplit(" (chunk", 1)[0]
    assert len(path_part) == 80
    assert path_part.endswith("...")


def test_search_reply_never_exceeds_budget():
    fake_search = _RecordingFn(result=[_result(i, excerpt_len=200, path_len=80) for i in range(10)])
    capability, _ = _make_capability(search_fn=fake_search)

    response = capability.handle("/knowledge search --limit 10 -- backup")

    assert len(response) <= 3_500


def test_omission_notice_appears_only_when_results_are_dropped():
    small_search = _RecordingFn(result=[_result(1, excerpt_len=20, path_len=10)])
    capability, _ = _make_capability(search_fn=small_search)
    response = capability.handle("/knowledge search -- backup")
    assert "omitted" not in response.lower()

    big_search = _RecordingFn(
        result=[
            _result(i, excerpt_len=200, path_len=80, source_key=_MAX_SOURCE_KEY) for i in range(10)
        ]
    )
    capability2, _ = _make_capability(search_fn=big_search)
    response2 = capability2.handle("/knowledge search --limit 10 -- backup")
    assert len(response2) <= 3_500
    assert "Additional results were omitted to keep the reply within the safe output limit." in response2


def test_results_are_omitted_only_at_complete_result_boundaries():
    big_search = _RecordingFn(
        result=[
            _result(i, excerpt_len=200, path_len=80, source_key=_MAX_SOURCE_KEY) for i in range(10)
        ]
    )
    capability, _ = _make_capability(search_fn=big_search)

    response = capability.handle("/knowledge search --limit 10 -- backup")

    # Every included ordinal line starts a full "N. [source] path (chunk N)"
    # block followed by its excerpt line - never a half-written block.
    lines = [line for line in response.splitlines() if line and line[0].isdigit()]
    for line in lines:
        assert "] " in line and "(chunk" in line


def test_unicode_excerpt_truncation_remains_valid_text():
    unicode_excerpt = "中文" * 150  # well over 200 chars
    fake_search = _RecordingFn(
        result=[
            KnowledgeSearchResult(
                source_key="ai_os_docs",
                relative_path="a.md",
                chunk_ordinal=0,
                excerpt=unicode_excerpt,
                rank=-1.0,
                chunk_id="abc",
            )
        ]
    )
    capability, _ = _make_capability(search_fn=fake_search)

    response = capability.handle("/knowledge search -- backup")

    # Must decode/round-trip cleanly and stay within the excerpt bound.
    assert isinstance(response, str)
    body_line = response.splitlines()[1].strip()
    assert len(body_line) <= 200


# --- reply formatting: ingest --------------------------------------------


def test_ingest_success_format(tmp_path):
    result = IngestResult(
        source_key="ai_os_docs",
        documents_indexed=3,
        unchanged_documents=1,
        removed_documents=2,
        chunks_indexed=111,
        generation=2,
        elapsed_seconds=0.038,
    )
    config = _config({"ai_os_docs": tmp_path})
    capability, _ = _make_capability(config=config, ingest_fn=_RecordingFn(result=result))

    capability.handle("/knowledge ingest ai_os_docs")
    response = capability.handle("/knowledge confirm")

    assert response == (
        "source: ai_os_docs\n"
        "documents indexed: 3\n"
        "unchanged documents: 1\n"
        "removed documents: 2\n"
        "chunks indexed: 111\n"
        "generation: 2\n"
        "duration: 0.038s"
    )


# --- no CLI import -------------------------------------------------------


def test_capability_module_imports_nothing_from_scripts_knowledge():
    import ast
    from pathlib import Path

    import capabilities.knowledge_commands.capability as capability_module
    import capabilities.knowledge_commands.command_parser as parser_module

    for module in (capability_module, parser_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)

        assert not any(name.startswith("scripts") for name in imported_names)
        assert "anthropic" not in imported_names
        assert "httpx" not in imported_names
        assert "requests" not in imported_names
        assert "socket" not in imported_names
        assert "subprocess" not in imported_names
