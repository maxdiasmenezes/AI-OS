"""Tests for kernel/knowledge_base/status.py: get_status()."""

import pytest

from kernel.knowledge_base.config import KnowledgeBaseConfig, SourceSpec
from kernel.knowledge_base.ingest import ingest_source
from kernel.knowledge_base.status import get_status
from kernel.knowledge_base.types import SourceStatus, UnknownSourceError


def _config(sources: dict) -> KnowledgeBaseConfig:
    return KnowledgeBaseConfig(
        approved_sources={key: SourceSpec(path=str(path), recursive=True) for key, path in sources.items()}
    )


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def two_source_setup(tmp_path):
    docs_a = tmp_path / "docs_a"
    docs_a.mkdir()
    _write(docs_a / "a.md", "Some content about repository backups.")

    docs_b = tmp_path / "docs_b"
    docs_b.mkdir()
    _write(docs_b / "b.md", "Some content about wine cellars.")
    _write(docs_b / "c.md", "More content about wine pairing.")

    config = _config({"source_a": docs_a, "source_b": docs_b})
    db_path = tmp_path / "knowledge_index.sqlite3"
    ingest_source("source_a", config=config, db_path=db_path)
    ingest_source("source_b", config=config, db_path=db_path)
    return config, db_path, docs_a, docs_b


# --- basic shape ------------------------------------------------------------


def test_all_approved_sources_returned_sorted(two_source_setup):
    config, db_path, _, _ = two_source_setup
    statuses = get_status(config=config, db_path=db_path)

    assert [s.source_key for s in statuses] == ["source_a", "source_b"]


def test_one_approved_source(two_source_setup):
    config, db_path, _, _ = two_source_setup
    statuses = get_status(["source_a"], config=config, db_path=db_path)

    assert len(statuses) == 1
    assert statuses[0].source_key == "source_a"


def test_multiple_approved_sources_explicit(two_source_setup):
    config, db_path, _, _ = two_source_setup
    statuses = get_status(["source_b", "source_a"], config=config, db_path=db_path)

    assert [s.source_key for s in statuses] == ["source_a", "source_b"]


def test_unknown_source_rejected(two_source_setup):
    config, db_path, _, _ = two_source_setup
    with pytest.raises(UnknownSourceError):
        get_status(["does-not-exist"], config=config, db_path=db_path)


def test_unknown_source_mixed_with_known_still_rejected(two_source_setup):
    config, db_path, _, _ = two_source_setup
    with pytest.raises(UnknownSourceError):
        get_status(["source_a", "does-not-exist"], config=config, db_path=db_path)


def test_source_keys_are_casefolded(two_source_setup):
    config, db_path, _, _ = two_source_setup
    statuses = get_status(["SOURCE_A"], config=config, db_path=db_path)

    assert statuses[0].source_key == "source_a"


# --- database missing / never ingested --------------------------------------


def test_database_missing_reports_not_ingested(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _config({"ai_os_docs": docs})
    db_path = tmp_path / "does_not_exist.sqlite3"

    statuses = get_status(config=config, db_path=db_path)

    assert len(statuses) == 1
    assert statuses[0] == SourceStatus(
        source_key="ai_os_docs",
        ingested=False,
        generation=0,
        document_count=0,
        chunk_count=0,
        last_ingested_at=None,
    )


def test_source_never_ingested_among_others(two_source_setup):
    config, db_path, _, _ = two_source_setup
    extended_config = KnowledgeBaseConfig(
        approved_sources={
            **config.approved_sources,
            "source_c": SourceSpec(path=str((db_path.parent / "docs_c")), recursive=True),
        }
    )
    (db_path.parent / "docs_c").mkdir()

    statuses = get_status(config=extended_config, db_path=db_path)

    by_key = {s.source_key: s for s in statuses}
    assert by_key["source_c"].ingested is False
    assert by_key["source_c"].generation == 0
    assert by_key["source_a"].ingested is True


# --- ingested sources ---------------------------------------------------


def test_source_ingested_with_documents_reports_counts_and_generation(two_source_setup):
    config, db_path, _, _ = two_source_setup
    statuses = get_status(["source_a"], config=config, db_path=db_path)

    status = statuses[0]
    assert status.ingested is True
    assert status.document_count == 1
    assert status.chunk_count >= 1
    assert status.generation == 1
    assert status.last_ingested_at is not None


def test_source_ingested_multiple_documents(two_source_setup):
    config, db_path, _, _ = two_source_setup
    statuses = get_status(["source_b"], config=config, db_path=db_path)

    assert statuses[0].document_count == 2


def test_successfully_ingested_empty_source(tmp_path):
    docs = tmp_path / "empty_docs"
    docs.mkdir()
    config = _config({"empty_source": docs})
    db_path = tmp_path / "knowledge_index.sqlite3"
    ingest_source("empty_source", config=config, db_path=db_path)

    statuses = get_status(["empty_source"], config=config, db_path=db_path)

    status = statuses[0]
    assert status.ingested is True
    assert status.generation >= 1
    assert status.document_count == 0
    assert status.chunk_count == 0
    assert status.last_ingested_at is not None


def test_last_ingested_timestamp_updates_on_reingest(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write(docs / "a.md", "content one")
    config = _config({"ai_os_docs": docs})
    db_path = tmp_path / "knowledge_index.sqlite3"
    ingest_source("ai_os_docs", config=config, db_path=db_path)
    first = get_status(["ai_os_docs"], config=config, db_path=db_path)[0]

    _write(docs / "a.md", "content one, now changed")
    ingest_source("ai_os_docs", config=config, db_path=db_path)
    second = get_status(["ai_os_docs"], config=config, db_path=db_path)[0]

    assert second.generation == first.generation + 1


# --- privacy: no absolute paths ----------------------------------------


def test_no_absolute_path_in_result(two_source_setup):
    config, db_path, docs_a, docs_b = two_source_setup
    statuses = get_status(config=config, db_path=db_path)

    for status in statuses:
        assert str(docs_a) not in repr(status)
        assert str(docs_b) not in repr(status)
        assert str(db_path) not in repr(status)


# --- read-only / single-query behavior ----------------------------------


def test_uses_read_only_connection(two_source_setup, monkeypatch):
    config, db_path, _, _ = two_source_setup
    import kernel.knowledge_base.status as status_module

    real_open = status_module.open_reader_connection
    query_only_values = []

    def spy_open(path):
        conn = real_open(path)
        query_only_values.append(conn.execute("PRAGMA query_only").fetchone()[0])
        return conn

    monkeypatch.setattr(status_module, "open_reader_connection", spy_open)
    get_status(config=config, db_path=db_path)

    assert query_only_values == [1]


def test_multiple_keys_use_a_single_parameterized_query_not_one_per_source(two_source_setup, monkeypatch):
    config, db_path, _, _ = two_source_setup
    import kernel.knowledge_base.status as status_module

    real_open = status_module.open_reader_connection
    executed_sql = []

    def spy_open(path):
        conn = real_open(path)
        conn.set_trace_callback(lambda sql: executed_sql.append(sql))
        return conn

    monkeypatch.setattr(status_module, "open_reader_connection", spy_open)
    get_status(["source_a", "source_b"], config=config, db_path=db_path)

    select_calls = [sql for sql in executed_sql if "SELECT" in sql.upper() and "sources" in sql]
    assert len(select_calls) == 1


# --- injection seams -----------------------------------------------------


def test_config_injection_is_used_instead_of_loading_real_config(tmp_path, monkeypatch):
    import kernel.knowledge_base.config as kb_config

    def raising_loader(*args, **kwargs):
        raise AssertionError("should not load the real config when config= is injected")

    monkeypatch.setattr(kb_config, "load_knowledge_base_config", raising_loader)

    docs = tmp_path / "docs"
    docs.mkdir()
    config = _config({"ai_os_docs": docs})
    db_path = tmp_path / "does_not_exist.sqlite3"

    statuses = get_status(config=config, db_path=db_path)
    assert statuses[0].source_key == "ai_os_docs"


def test_db_path_injection_points_to_the_given_database(two_source_setup, tmp_path):
    config, db_path, _, _ = two_source_setup
    other_db_path = tmp_path / "other.sqlite3"

    # A different (nonexistent) db_path reports not-ingested, proving
    # db_path is actually respected rather than resolved elsewhere.
    statuses = get_status(["source_a"], config=config, db_path=other_db_path)
    assert statuses[0].ingested is False

    statuses = get_status(["source_a"], config=config, db_path=db_path)
    assert statuses[0].ingested is True
