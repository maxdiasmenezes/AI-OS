"""Tests for scripts/knowledge.py: CLI grammar, exit codes, and privacy.

Every test monkeypatches the default config/database path constants to
tmp_path locations - the real kernel/config/knowledge_base.yaml and real
storage/knowledge/ are never read or written by this suite.
"""

import logging

import pytest

import kernel.knowledge_base.config as kb_config
import kernel.knowledge_base.db as kb_db
from scripts import knowledge as cli


def _write_knowledge_base_yaml(tmp_path, docs_path):
    path = tmp_path / "knowledge_base.yaml"
    posix_path = str(docs_path).replace("\\", "/")
    path.write_text(
        f'approved_sources:\n  ai_os_docs:\n    path: "{posix_path}"\n    recursive: true\n',
        encoding="utf-8",
    )
    return path


def _write_config_yaml(tmp_path, storage_dir):
    path = tmp_path / "config.yaml"
    posix_path = str(storage_dir).replace("\\", "/")
    path.write_text(f'knowledge:\n  storage_dir: "{posix_path}"\n', encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _redirect_default_paths(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("Repository backup notes for AI-OS.", encoding="utf-8")

    kb_yaml = _write_knowledge_base_yaml(tmp_path, docs)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config_yaml = _write_config_yaml(tmp_path, storage_dir)

    monkeypatch.setattr(kb_config, "DEFAULT_KNOWLEDGE_BASE_YAML_PATH", kb_yaml)
    monkeypatch.setattr(kb_db, "_DEFAULT_CONFIG_YAML_PATH", config_yaml)

    return {"docs": docs, "storage_dir": storage_dir}


# --- grammar -----------------------------------------------------------


def test_unknown_command_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["bogus-command"])
    assert exc_info.value.code == cli.EXIT_USAGE_ERROR


def test_ingest_wrong_argument_count_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["ingest"])
    assert exc_info.value.code == cli.EXIT_USAGE_ERROR

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["ingest", "one", "two"])
    assert exc_info.value.code == cli.EXIT_USAGE_ERROR


def test_search_missing_query_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["search"])
    assert exc_info.value.code == cli.EXIT_USAGE_ERROR


def test_no_path_option_exists_for_ingest():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["ingest", "ai_os_docs", "--path", "C:/somewhere"])
    assert exc_info.value.code == cli.EXIT_USAGE_ERROR


def test_no_sql_or_fts_option_exists_for_search():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["search", "hello", "--sql", "DROP TABLE chunks"])
    assert exc_info.value.code == cli.EXIT_USAGE_ERROR


def test_invalid_limit_type_exits_with_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["search", "hello", "--limit", "not-a-number"])
    assert exc_info.value.code == cli.EXIT_USAGE_ERROR


# --- successful operations -----------------------------------------------


def test_successful_status_before_ingest(capsys):
    code = cli.main(["status"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_SUCCESS
    assert "not yet ingested" in out


def test_successful_ingest(capsys):
    code = cli.main(["ingest", "ai_os_docs"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_SUCCESS
    assert "documents indexed: 1" in out
    assert "generation: 1" in out


def test_successful_status_after_ingest(capsys):
    cli.main(["ingest", "ai_os_docs"])
    capsys.readouterr()

    code = cli.main(["status", "--source", "ai_os_docs"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_SUCCESS
    assert "1 document(s)" in out


def test_successful_search(capsys):
    cli.main(["ingest", "ai_os_docs"])
    capsys.readouterr()

    code = cli.main(["search", "repository backup"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_SUCCESS
    assert "a.md" in out


def test_search_with_source_filter_and_limit(capsys):
    cli.main(["ingest", "ai_os_docs"])
    capsys.readouterr()

    code = cli.main(["search", "repository backup", "--source", "ai_os_docs", "--limit", "5"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_SUCCESS
    assert "a.md" in out


def test_search_no_results_is_still_success(capsys):
    cli.main(["ingest", "ai_os_docs"])
    capsys.readouterr()

    code = cli.main(["search", "nonexistenttermxyz"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_SUCCESS
    assert "No results found." in out


# --- recognized failures --------------------------------------------------


def test_unknown_source_ingest_exits_operation_failed(capsys):
    code = cli.main(["ingest", "does-not-exist"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OPERATION_FAILED
    assert out.strip() == "Unknown knowledge source."


def test_unknown_source_status_exits_operation_failed(capsys):
    code = cli.main(["status", "--source", "does-not-exist"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OPERATION_FAILED
    assert out.strip() == "Unknown knowledge source."


def test_invalid_limit_value_exits_operation_failed(capsys):
    cli.main(["ingest", "ai_os_docs"])
    capsys.readouterr()

    code = cli.main(["search", "repository", "--limit", "0"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OPERATION_FAILED
    assert out.strip() == "Search query is invalid."


def test_unknown_source_filter_exits_operation_failed(capsys):
    cli.main(["ingest", "ai_os_docs"])
    capsys.readouterr()

    code = cli.main(["search", "repository", "--source", "nope"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OPERATION_FAILED
    assert out.strip() == "Unknown knowledge source in filter."


def test_no_traceback_for_expected_failure(capsys):
    code = cli.main(["ingest", "does-not-exist"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OPERATION_FAILED
    assert "Traceback" not in out
    assert "Error" not in out  # no raw exception class name either


# --- privacy ---------------------------------------------------------------


def test_no_absolute_path_in_status_output(capsys, _redirect_default_paths):
    cli.main(["ingest", "ai_os_docs"])
    capsys.readouterr()

    cli.main(["status"])
    out = capsys.readouterr().out
    assert str(_redirect_default_paths["docs"]) not in out
    assert str(_redirect_default_paths["storage_dir"]) not in out


def test_no_absolute_path_in_search_output(capsys, _redirect_default_paths):
    cli.main(["ingest", "ai_os_docs"])
    capsys.readouterr()

    cli.main(["search", "repository"])
    out = capsys.readouterr().out
    assert str(_redirect_default_paths["docs"]) not in out


def test_query_text_absent_from_logs(caplog):
    cli.main(["ingest", "ai_os_docs"])

    with caplog.at_level(logging.DEBUG):
        cli.main(["search", "a-very-specific-secret-query-token"])

    for record in caplog.records:
        assert "a-very-specific-secret-query-token" not in record.getMessage()


def test_excerpt_content_absent_from_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        cli.main(["ingest", "ai_os_docs"])
        cli.main(["search", "repository"])

    for record in caplog.records:
        assert "Repository backup notes for AI-OS" not in record.getMessage()


def test_help_does_not_reveal_local_configuration(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "ai_os_docs" not in out
    assert ".yaml" not in out


# --- no model or network invocation -----------------------------------------


def test_cli_module_imports_nothing_model_or_network_related():
    import ast
    from pathlib import Path

    source = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module.split(".")[0])

    assert "anthropic" not in imported_names
    assert "httpx" not in imported_names
    assert "requests" not in imported_names
    assert "socket" not in imported_names


# --- local configuration remains untouched ----------------------------------


def test_real_local_configuration_untouched(_redirect_default_paths):
    from pathlib import Path

    real_config_path = Path(__file__).resolve().parents[2] / "kernel" / "config" / "knowledge_base.yaml"
    existed_before = real_config_path.exists()
    mtime_before = real_config_path.stat().st_mtime_ns if existed_before else None

    cli.main(["ingest", "ai_os_docs"])
    cli.main(["status"])
    cli.main(["search", "repository"])

    # The autouse fixture redirected the default config/db path constants
    # for this whole module, so none of the calls above could have
    # touched the real gitignored local file.
    assert real_config_path.exists() == existed_before
    if existed_before:
        assert real_config_path.stat().st_mtime_ns == mtime_before
