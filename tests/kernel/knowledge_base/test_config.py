"""Tests for kernel/knowledge_base/config.py: load_knowledge_base_config()'s
fail-closed rules. Every test uses an explicit tmp_path override - never
the real kernel/config/knowledge_base.yaml."""

import pytest

from kernel.knowledge_base.config import (
    DEFAULT_KNOWLEDGE_BASE_YAML_PATH,
    KnowledgeConfigError,
    SourceSpec,
    is_valid_source_key,
    load_knowledge_base_config,
)


def _write(tmp_path, text):
    path = tmp_path / "knowledge_base.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _abs(tmp_path, name="docs"):
    p = tmp_path / name
    return str(p).replace("\\", "/")


def test_missing_file_yields_empty_deny_all_config(tmp_path):
    config = load_knowledge_base_config(tmp_path / "does_not_exist.yaml")

    assert config.approved_sources == {}


def test_empty_file_yields_empty_config(tmp_path):
    path = _write(tmp_path, "")

    config = load_knowledge_base_config(path)

    assert config.approved_sources == {}


def test_valid_config_loads_source_spec(tmp_path):
    docs = _abs(tmp_path)
    path = _write(tmp_path, f'approved_sources:\n  ai_os_docs:\n    path: "{docs}"\n    recursive: true\n')

    config = load_knowledge_base_config(path)

    assert config.approved_sources == {"ai_os_docs": SourceSpec(path=docs, recursive=True)}


def test_malformed_yaml_raises(tmp_path):
    path = _write(tmp_path, "approved_sources: [this is not: a mapping")

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_non_mapping_top_level_raises(tmp_path):
    path = _write(tmp_path, "- just\n- a\n- list\n")

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_duplicate_yaml_key_raises(tmp_path):
    docs = _abs(tmp_path)
    path = _write(
        tmp_path,
        f'approved_sources:\n  ai_os_docs:\n    path: "{docs}"\n    recursive: true\n'
        f'  ai_os_docs:\n    path: "{docs}"\n    recursive: false\n',
    )

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_case_colliding_key_raises(tmp_path):
    docs = _abs(tmp_path)
    path = _write(
        tmp_path,
        f'approved_sources:\n  ai_os_docs:\n    path: "{docs}"\n    recursive: true\n'
        f'  AI_OS_DOCS:\n    path: "{docs}"\n    recursive: true\n',
    )

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


@pytest.mark.parametrize(
    "bad_key",
    ["-leading-hyphen", "has space", "has/slash", "a" * 65, ""],
)
def test_unsafe_symbolic_key_raises(tmp_path, bad_key):
    docs = _abs(tmp_path)
    path = _write(tmp_path, f'approved_sources:\n  "{bad_key}":\n    path: "{docs}"\n    recursive: true\n')

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_mixed_case_key_is_casefolded_not_rejected(tmp_path):
    docs = _abs(tmp_path)
    path = _write(tmp_path, f'approved_sources:\n  Ai_Os_Docs:\n    path: "{docs}"\n    recursive: true\n')

    config = load_knowledge_base_config(path)

    assert "ai_os_docs" in config.approved_sources


def test_unknown_top_level_field_raises(tmp_path):
    path = _write(tmp_path, "not_a_real_section:\n  foo: bar\n")

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_unknown_entry_field_raises(tmp_path):
    docs = _abs(tmp_path)
    path = _write(
        tmp_path,
        f'approved_sources:\n  ai_os_docs:\n    path: "{docs}"\n    recursive: true\n    extra: 1\n',
    )

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_missing_required_field_raises(tmp_path):
    docs = _abs(tmp_path)
    path = _write(tmp_path, f'approved_sources:\n  ai_os_docs:\n    path: "{docs}"\n')

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_non_boolean_recursive_raises(tmp_path):
    docs = _abs(tmp_path)
    path = _write(tmp_path, f'approved_sources:\n  ai_os_docs:\n    path: "{docs}"\n    recursive: "yes"\n')

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_relative_source_path_raises(tmp_path):
    path = _write(tmp_path, 'approved_sources:\n  ai_os_docs:\n    path: "relative/path"\n    recursive: true\n')

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_non_mapping_source_entry_raises(tmp_path):
    path = _write(tmp_path, "approved_sources:\n  ai_os_docs: not-a-mapping\n")

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


def test_non_mapping_approved_sources_raises(tmp_path):
    path = _write(tmp_path, "approved_sources: not-a-mapping\n")

    with pytest.raises(KnowledgeConfigError):
        load_knowledge_base_config(path)


@pytest.mark.parametrize("key", ["ai_os_docs", "a", "a0", "a-b_c", "z" * 64])
def test_is_valid_source_key_accepts_conservative_keys(key):
    assert is_valid_source_key(key)


@pytest.mark.parametrize("key", ["Ai_Os", "-a", "_a", "a b", "a" * 65, "", 123])
def test_is_valid_source_key_rejects_unsafe_keys(key):
    assert not is_valid_source_key(key)


def test_default_path_points_at_gitignored_local_file():
    assert DEFAULT_KNOWLEDGE_BASE_YAML_PATH.name == "knowledge_base.yaml"
    assert DEFAULT_KNOWLEDGE_BASE_YAML_PATH.parent.name == "config"


def test_parsing_performs_no_filesystem_traversal(tmp_path):
    # The configured path does not need to exist for config parsing to
    # succeed - only traversal.py (invoked later, at ingestion time)
    # checks the filesystem.
    missing = _abs(tmp_path, "does-not-exist-yet")
    path = _write(tmp_path, f'approved_sources:\n  ai_os_docs:\n    path: "{missing}"\n    recursive: true\n')

    config = load_knowledge_base_config(path)

    assert config.approved_sources["ai_os_docs"].path == missing
