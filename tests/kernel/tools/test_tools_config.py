"""Tests for kernel/tools/config.py: load_tools_config()'s fail-closed rules."""

import pytest

from kernel.tools.config import (
    ApplicationSpec,
    ScriptSpec,
    ToolsConfigError,
    load_tools_config,
)


def _write(tmp_path, text):
    path = tmp_path / "tools.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_yields_an_empty_config_rather_than_raising(tmp_path):
    config = load_tools_config(tmp_path / "does_not_exist.yaml")

    assert config.approved_directories == {}
    assert config.approved_applications == {}
    assert config.approved_scripts == {}


def test_empty_file_yields_an_empty_config(tmp_path):
    path = _write(tmp_path, "")

    config = load_tools_config(path)

    assert config.approved_directories == {}


def test_malformed_yaml_raises(tmp_path):
    path = _write(tmp_path, "list_files: [this is not: a mapping")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_non_mapping_top_level_raises(tmp_path):
    path = _write(tmp_path, "- just\n- a\n- list\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_unsupported_top_level_field_raises(tmp_path):
    path = _write(tmp_path, "not_a_real_section:\n  foo: bar\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_unsupported_field_within_list_files_raises(tmp_path):
    path = _write(tmp_path, "list_files:\n  unexpected_field: 1\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_relative_directory_path_raises(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n  approved_directories:\n    documents: relative/path\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_duplicate_yaml_key_raises(tmp_path):
    # Two literal, identically-spelled keys under the same mapping - PyYAML
    # would otherwise silently keep only the last one.
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/one\n"
        "    documents: C:/two\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_case_insensitive_duplicate_key_raises(tmp_path):
    # "Documents" and "documents" are distinct YAML keys but collide once
    # keys are casefolded for matching - must still be rejected.
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    Documents: C:/one\n"
        "    documents: C:/two\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_valid_list_files_section_parses_with_casefolded_keys(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    Documents: C:/Users/Example/Documents\n",
    )

    config = load_tools_config(path)

    assert config.approved_directories == {"documents": "C:/Users/Example/Documents"}


def test_open_application_missing_required_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "open_application:\n"
        "  approved_applications:\n"
        "    notepad:\n"
        "      executable: C:/Windows/System32/notepad.exe\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_open_application_unsupported_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "open_application:\n"
        "  approved_applications:\n"
        "    notepad:\n"
        "      executable: C:/Windows/System32/notepad.exe\n"
        "      cwd: C:/Windows/System32\n"
        "      extra_arg: --whatever\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_valid_open_application_section_parses(tmp_path):
    path = _write(
        tmp_path,
        "open_application:\n"
        "  approved_applications:\n"
        "    notepad:\n"
        "      executable: C:/Windows/System32/notepad.exe\n"
        "      cwd: C:/Windows/System32\n",
    )

    config = load_tools_config(path)

    assert config.approved_applications == {
        "notepad": ApplicationSpec(
            executable="C:/Windows/System32/notepad.exe", cwd="C:/Windows/System32"
        )
    }


def test_run_registered_script_relative_interpreter_raises(tmp_path):
    path = _write(
        tmp_path,
        "run_registered_script:\n"
        "  approved_scripts:\n"
        "    backup:\n"
        "      interpreter: python\n"
        "      script_path: C:/AI-OS/scripts/backup.py\n"
        "      cwd: C:/AI-OS\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_run_registered_script_non_positive_timeout_raises(tmp_path):
    path = _write(
        tmp_path,
        "run_registered_script:\n"
        "  approved_scripts:\n"
        "    backup:\n"
        "      interpreter: C:/python.exe\n"
        "      script_path: C:/AI-OS/scripts/backup.py\n"
        "      cwd: C:/AI-OS\n"
        "      timeout_seconds: 0\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_valid_run_registered_script_section_parses_with_default_timeout(tmp_path):
    path = _write(
        tmp_path,
        "run_registered_script:\n"
        "  approved_scripts:\n"
        "    backup:\n"
        "      interpreter: C:/python.exe\n"
        "      script_path: C:/AI-OS/scripts/backup.py\n"
        "      cwd: C:/AI-OS\n",
    )

    config = load_tools_config(path)

    assert config.approved_scripts == {
        "backup": ScriptSpec(
            interpreter="C:/python.exe",
            script_path="C:/AI-OS/scripts/backup.py",
            cwd="C:/AI-OS",
            timeout_seconds=30.0,
        )
    }


def test_full_valid_configuration_parses_all_three_sections(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "open_application:\n"
        "  approved_applications:\n"
        "    notepad:\n"
        "      executable: C:/Windows/System32/notepad.exe\n"
        "      cwd: C:/Windows/System32\n"
        "run_registered_script:\n"
        "  approved_scripts:\n"
        "    backup:\n"
        "      interpreter: C:/python.exe\n"
        "      script_path: C:/AI-OS/scripts/backup.py\n"
        "      cwd: C:/AI-OS\n"
        "      timeout_seconds: 60\n",
    )

    config = load_tools_config(path)

    assert config.approved_directories == {"documents": "C:/Users/Example/Documents"}
    assert "notepad" in config.approved_applications
    assert config.approved_scripts["backup"].timeout_seconds == 60.0
