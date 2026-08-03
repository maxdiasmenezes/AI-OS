"""Tests for kernel/tools/config.py: load_tools_config()'s fail-closed rules."""

import json

import pytest

from kernel.tools.config import (
    ApplicationSpec,
    RepoBackupSpec,
    RepoSpec,
    ScriptSpec,
    ToolsConfigError,
    is_valid_backup_key,
    is_valid_git_branch_name,
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
    assert config.approved_repositories == {}


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


def test_repo_health_missing_required_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      main_branch: main\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_repo_health_unsupported_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "      extra_field: nope\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_repo_health_relative_path_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: relative/path\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


@pytest.mark.parametrize(
    "bad_branch",
    [
        "",
        "has space",
        "weird\tchar",
        "semi;colon",
        "-main",
        "/main",
        "main/",
        ".main",
        "feature/.hidden",
        "main.",
        "feature.lock",
        "feature/test.lock",
        "main..old",
        "feature//test",
        "main@{1}",
        "@",
        "has space",
        "has\tcontrol",
    ],
)
def test_repo_health_invalid_main_branch_raises(tmp_path, bad_branch):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        f"      path: C:/AI-OS\n"
        f"      main_branch: {bad_branch!r}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


@pytest.mark.parametrize(
    "valid_branch",
    ["main", "trunk", "release/2026-08", "feature/repo_health", "version_2.1"],
)
def test_repo_health_valid_main_branch_names_parse(tmp_path, valid_branch):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        f"      main_branch: {valid_branch!r}\n",
    )

    config = load_tools_config(path)

    assert config.approved_repositories["ai_os"].main_branch == valid_branch


@pytest.mark.parametrize(
    "invalid_name",
    [
        "",
        None,
        123,
        "-main",
        "/main",
        "main/",
        ".main",
        "feature/.hidden",
        "main.",
        "feature.lock",
        "feature/test.lock",
        "main..old",
        "feature//test",
        "main@{1}",
        "@",
        "has space",
        "has\tcontrol",
        "back\\slash",
    ],
)
def test_is_valid_git_branch_name_rejects(invalid_name):
    assert is_valid_git_branch_name(invalid_name) is False


@pytest.mark.parametrize(
    "valid_name",
    ["main", "trunk", "release/2026-08", "feature/repo_health", "version_2.1"],
)
def test_is_valid_git_branch_name_accepts(valid_name):
    assert is_valid_git_branch_name(valid_name) is True


def test_repo_health_duplicate_key_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    Ai_Os:\n"
        "      path: C:/one\n"
        "    ai_os:\n"
        "      path: C:/two\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_valid_repo_health_section_parses_with_default_main_branch(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    Ai_Os:\n"
        "      path: C:/AI-OS\n",
    )

    config = load_tools_config(path)

    assert config.approved_repositories == {
        "ai_os": RepoSpec(path="C:/AI-OS", main_branch="main")
    }


def test_valid_repo_health_section_parses_with_explicit_main_branch(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "      main_branch: trunk\n",
    )

    config = load_tools_config(path)

    assert config.approved_repositories["ai_os"].main_branch == "trunk"


def test_full_valid_configuration_parses_all_four_sections(tmp_path):
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
        "      timeout_seconds: 60\n"
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n",
    )

    config = load_tools_config(path)

    assert config.approved_directories == {"documents": "C:/Users/Example/Documents"}
    assert "notepad" in config.approved_applications
    assert config.approved_scripts["backup"].timeout_seconds == 60.0
    assert config.approved_repositories["ai_os"] == RepoSpec(path="C:/AI-OS", main_branch="main")


# --- repository_backup (Milestone 35) ---


def test_valid_repository_backup_section_parses(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        "    ai_os:\n"
        "      destination_directory: D:/AI-OS-Backups\n",
    )

    config = load_tools_config(path)

    assert config.approved_backups == {
        "ai_os": RepoBackupSpec(destination_directory="D:/AI-OS-Backups")
    }


def test_repository_backup_key_casefolds_like_repo_health(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    Ai_Os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        "    AI_OS:\n"
        "      destination_directory: D:/AI-OS-Backups\n",
    )

    config = load_tools_config(path)

    assert "ai_os" in config.approved_backups


def test_repository_backup_unsupported_top_level_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  not_a_real_field: 1\n"
        "  approved_backups:\n"
        "    ai_os:\n"
        "      destination_directory: D:/AI-OS-Backups\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_repository_backup_unsupported_entry_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        "    ai_os:\n"
        "      destination_directory: D:/AI-OS-Backups\n"
        "      extra_field: nope\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_repository_backup_missing_required_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        "    ai_os: {}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_repository_backup_relative_destination_path_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        "    ai_os:\n"
        "      destination_directory: relative/backups\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_repository_backup_duplicate_key_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        "    ai_os:\n"
        "      destination_directory: D:/one\n"
        "    Ai_Os:\n"
        "      destination_directory: D:/two\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_repository_backup_key_without_matching_repo_health_entry_raises(tmp_path):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        "    other_repo:\n"
        "      destination_directory: D:/AI-OS-Backups\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_repository_backup_section_without_any_repo_health_section_raises(tmp_path):
    path = _write(
        tmp_path,
        "repository_backup:\n"
        "  approved_backups:\n"
        "    ai_os:\n"
        "      destination_directory: D:/AI-OS-Backups\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "ai/os",
        "ai\\os",
        "..",
        "ai:os",
        "ai os",
        ".ai_os",
        "-ai_os",
        "_ai_os",
        "ai_os!",
        "ai\tos",
        "a" * 65,
    ],
)
def test_repository_backup_filename_unsafe_key_raises(tmp_path, unsafe_key):
    # The key is emitted as a JSON-quoted string, which is also valid
    # YAML double-quoted scalar syntax with the same escaping rules -
    # unlike Python's repr(), this survives backslashes, tabs, and other
    # special characters round-tripping through YAML correctly.
    quoted_key = json.dumps(unsafe_key)
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        f"    {quoted_key}:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        f"    {quoted_key}:\n"
        "      destination_directory: D:/AI-OS-Backups\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


@pytest.mark.parametrize(
    "safe_key",
    ["ai_os", "ai-os", "ai_os2", "a", "0ai", "a" * 64],
)
def test_repository_backup_filename_safe_key_parses(tmp_path, safe_key):
    path = _write(
        tmp_path,
        "repo_health:\n"
        "  approved_repositories:\n"
        f"    {safe_key}:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        f"    {safe_key}:\n"
        "      destination_directory: D:/AI-OS-Backups\n",
    )

    config = load_tools_config(path)

    assert safe_key in config.approved_backups


@pytest.mark.parametrize(
    "invalid_key",
    ["", "ai/os", "ai\\os", "..", "ai:os", "ai os", ".ai_os", "-ai_os", "_ai_os", "AI_OS", None, 123, "a" * 65],
)
def test_is_valid_backup_key_rejects(invalid_key):
    assert is_valid_backup_key(invalid_key) is False


@pytest.mark.parametrize(
    "valid_key",
    ["ai_os", "ai-os", "ai_os2", "a", "0ai", "a" * 64],
)
def test_is_valid_backup_key_accepts(valid_key):
    assert is_valid_backup_key(valid_key) is True


def test_full_valid_configuration_parses_all_five_sections(tmp_path):
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
        "      timeout_seconds: 60\n"
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        "    ai_os:\n"
        "      destination_directory: D:/AI-OS-Backups\n",
    )

    config = load_tools_config(path)

    assert config.approved_directories == {"documents": "C:/Users/Example/Documents"}
    assert "notepad" in config.approved_applications
    assert config.approved_scripts["backup"].timeout_seconds == 60.0
    assert config.approved_repositories["ai_os"] == RepoSpec(path="C:/AI-OS", main_branch="main")
    assert config.approved_backups["ai_os"] == RepoBackupSpec(destination_directory="D:/AI-OS-Backups")
