"""Tests for kernel/tools/config.py: load_tools_config()'s fail-closed rules."""

import json
from pathlib import Path

import pytest

from kernel.tools.browser_safety import MAX_STYLESHEET_ORIGINS
from kernel.tools.config import (
    MAX_SYMBOLIC_NAME_LENGTH,
    ApplicationSpec,
    ApprovedPageSpec,
    DirectoryCreationSpec,
    FileCopySpec,
    FileSpec,
    RepoBackupSpec,
    RepoSpec,
    ScriptSpec,
    ToolsConfigError,
    is_valid_backup_key,
    is_valid_child_name,
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


# --- approved_files (Milestone 43 P1) ---------------------------------------


def test_missing_approved_files_section_yields_an_empty_dict(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n  approved_directories:\n    documents: C:/Users/Example/Documents\n",
    )

    config = load_tools_config(path)

    assert config.approved_files == {}


def test_valid_approved_files_entry_parses(tmp_path):
    path = _write(
        tmp_path,
        "approved_files:\n"
        "  resume_pdf:\n"
        "    path: C:/Users/Example/Documents/resume.pdf\n",
    )

    config = load_tools_config(path)

    assert config.approved_files == {
        "resume_pdf": FileSpec(path="C:/Users/Example/Documents/resume.pdf")
    }


def test_multiple_approved_files_keys_parse(tmp_path):
    path = _write(
        tmp_path,
        "approved_files:\n"
        "  resume_pdf:\n"
        "    path: C:/Users/Example/Documents/resume.pdf\n"
        "  notes_txt:\n"
        "    path: C:/Users/Example/Documents/notes.txt\n",
    )

    config = load_tools_config(path)

    assert set(config.approved_files) == {"resume_pdf", "notes_txt"}
    assert config.approved_files["notes_txt"] == FileSpec(
        path="C:/Users/Example/Documents/notes.txt"
    )


def test_approved_files_duplicate_yaml_key_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_files:\n"
        "  resume_pdf:\n"
        "    path: C:/one/resume.pdf\n"
        "  resume_pdf:\n"
        "    path: C:/two/resume.pdf\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_files_case_insensitive_duplicate_key_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_files:\n"
        "  Resume_Pdf:\n"
        "    path: C:/one/resume.pdf\n"
        "  resume_pdf:\n"
        "    path: C:/two/resume.pdf\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_files_relative_path_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_files:\n  resume_pdf:\n    path: relative/resume.pdf\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_files_missing_required_field_raises(tmp_path):
    path = _write(tmp_path, "approved_files:\n  resume_pdf: {}\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_files_unsupported_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_files:\n"
        "  resume_pdf:\n"
        "    path: C:/Users/Example/Documents/resume.pdf\n"
        "    extra_field: nope\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_files_non_mapping_entry_raises(tmp_path):
    path = _write(tmp_path, "approved_files:\n  resume_pdf: C:/Users/Example/resume.pdf\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_files_non_mapping_section_raises(tmp_path):
    path = _write(tmp_path, "approved_files:\n  - not\n  - a\n  - mapping\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_files_path_with_nul_raises(tmp_path):
    # A raw NUL byte cannot round-trip through plain YAML scalar syntax in
    # the same way as tab/backslash - written via a double-quoted YAML
    # escape instead, which PyYAML decodes to an actual NUL character.
    path = _write(
        tmp_path,
        'approved_files:\n  resume_pdf:\n    path: "C:/Users/Example/resume\\0.pdf"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_full_valid_configuration_parses_all_six_sections(tmp_path):
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
        "      destination_directory: D:/AI-OS-Backups\n"
        "approved_files:\n"
        "  resume_pdf:\n"
        "    path: C:/Users/Example/Documents/resume.pdf\n",
    )

    config = load_tools_config(path)

    assert config.approved_directories == {"documents": "C:/Users/Example/Documents"}
    assert "notepad" in config.approved_applications
    assert config.approved_scripts["backup"].timeout_seconds == 60.0
    assert config.approved_repositories["ai_os"] == RepoSpec(path="C:/AI-OS", main_branch="main")
    assert config.approved_backups["ai_os"] == RepoBackupSpec(destination_directory="D:/AI-OS-Backups")
    assert config.approved_files["resume_pdf"] == FileSpec(
        path="C:/Users/Example/Documents/resume.pdf"
    )


# --- the real, committed kernel/config/tools.example.yaml (never the
# --- machine-local, gitignored tools.yaml) ----------------------------------

# tests/kernel/tools/test_tools_config.py -> tests/kernel/tools -> tests/kernel
# -> tests -> project root.
_EXAMPLE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "kernel" / "config" / "tools.example.yaml"
)


def test_committed_example_configuration_loads_without_error():
    config = load_tools_config(_EXAMPLE_CONFIG_PATH)

    assert config.approved_directories
    assert config.approved_applications
    assert config.approved_scripts
    assert config.approved_repositories
    assert config.approved_backups
    assert config.approved_files
    assert config.approved_directory_creations
    assert config.approved_copies


def test_committed_example_configuration_p2_sections_are_genuine_top_level_sections():
    """Regression: create_directory/copy_file must be their own top-level
    YAML sections - mirrors
    test_committed_example_configuration_approved_files_is_a_genuine_top_level_section
    above for the same reason (a nesting mistake would silently make a
    section unreachable rather than loading it at all)."""

    config = load_tools_config(_EXAMPLE_CONFIG_PATH)

    assert config.approved_directory_creations == {
        "project_exports": DirectoryCreationSpec(
            parent_directory_key="documents", directory_name="exports"
        )
    }
    assert config.approved_copies == {
        "resume_backup": FileCopySpec(
            source_file_key="resume_pdf",
            destination_directory_key="downloads",
            destination_name="resume_backup.pdf",
        )
    }


def test_committed_example_configuration_approved_files_is_a_genuine_top_level_section():
    """Regression: approved_files must be its own top-level YAML section,
    not accidentally nested under repository_backup (or any other
    section) - a nesting mistake would silently make it unreachable
    (rejected as an unrecognized field of whatever it was nested under, or
    simply never parsed) rather than loading as approved_files at all."""

    config = load_tools_config(_EXAMPLE_CONFIG_PATH)

    assert config.approved_files == {
        "resume_pdf": FileSpec(path="C:/Users/REPLACE_ME/Documents/resume.pdf"),
        "notes_txt": FileSpec(path="C:/Users/REPLACE_ME/Documents/notes.txt"),
    }


# --- is_valid_child_name() (Milestone 43 P2) --------------------------------


@pytest.mark.parametrize(
    "invalid_name",
    [
        None,
        123,
        "",
        ".",
        "..",
        "a\x00b",
        "a/b",
        "a\\b",
        "/etc/passwd",
        "C:/Windows",
        "C:\\Windows",
        "C:",
        "\\\\server\\share",
        "name:stream",
        " leading",
        "trailing ",
        " both ",
        "trailing.",
        "CON",
        "con",
        "CON.txt",
        "con.TXT",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "com9",
        "LPT1",
        "lpt9.log",
    ],
)
def test_is_valid_child_name_rejects(invalid_name):
    assert is_valid_child_name(invalid_name) is False


@pytest.mark.parametrize(
    "valid_name",
    [
        "exports",
        "monthly_report.pdf",
        "Report Archive",
        "a",
        "CONTACT",  # contains "CON" but is not equal to it
        "NULLABLE",  # contains "NUL" but is not equal to it
        "report.tar.gz",
        "résumé.pdf",
    ],
)
def test_is_valid_child_name_accepts(valid_name):
    assert is_valid_child_name(valid_name) is True


# --- create_directory (Milestone 43 P2) -------------------------------------


def test_valid_create_directory_section_parses(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: documents\n"
        "      directory_name: exports\n",
    )

    config = load_tools_config(path)

    assert config.approved_directory_creations == {
        "project_exports": DirectoryCreationSpec(
            parent_directory_key="documents", directory_name="exports"
        )
    }


def test_multiple_create_directory_operations_parse(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "    downloads: C:/Users/Example/Downloads\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: documents\n"
        "      directory_name: exports\n"
        "    download_archive:\n"
        "      parent_directory: downloads\n"
        "      directory_name: archive\n",
    )

    config = load_tools_config(path)

    assert set(config.approved_directory_creations) == {"project_exports", "download_archive"}


def test_missing_create_directory_section_yields_an_empty_dict(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n  approved_directories:\n    documents: C:/Users/Example/Documents\n",
    )

    config = load_tools_config(path)

    assert config.approved_directory_creations == {}


def test_create_directory_unsupported_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: documents\n"
        "      directory_name: exports\n"
        "      extra_field: nope\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_create_directory_missing_required_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: documents\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_create_directory_non_mapping_section_raises(tmp_path):
    path = _write(tmp_path, "create_directory:\n  - not\n  - a\n  - mapping\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_create_directory_duplicate_key_raises(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    Project_Exports:\n"
        "      parent_directory: documents\n"
        "      directory_name: one\n"
        "    project_exports:\n"
        "      parent_directory: documents\n"
        "      directory_name: two\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_create_directory_unknown_parent_directory_reference_raises(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: not_registered\n"
        "      directory_name: exports\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_create_directory_reference_without_any_list_files_section_raises(tmp_path):
    path = _write(
        tmp_path,
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: documents\n"
        "      directory_name: exports\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_create_directory_parent_directory_reference_is_case_insensitive(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    Documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: DOCUMENTS\n"
        "      directory_name: exports\n",
    )

    config = load_tools_config(path)

    assert config.approved_directory_creations["project_exports"].parent_directory_key == "documents"


@pytest.mark.parametrize("unsafe_name", ["..", "a/b", "a\\b", "CON", "trailing."])
def test_create_directory_unsafe_directory_name_raises_at_config_load(tmp_path, unsafe_name):
    quoted = json.dumps(unsafe_name)
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: documents\n"
        f"      directory_name: {quoted}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


# --- copy_file (Milestone 43 P2) --------------------------------------------


def test_valid_copy_file_section_parses(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    archive: D:/Archive\n"
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: archive\n"
        "      destination_name: monthly_report.pdf\n",
    )

    config = load_tools_config(path)

    assert config.approved_copies == {
        "monthly_report_archive": FileCopySpec(
            source_file_key="monthly_report",
            destination_directory_key="archive",
            destination_name="monthly_report.pdf",
        )
    }


def test_missing_copy_file_section_yields_an_empty_dict(tmp_path):
    path = _write(tmp_path, "list_files:\n  approved_directories:\n    archive: D:/Archive\n")

    config = load_tools_config(path)

    assert config.approved_copies == {}


def test_copy_file_unsupported_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    archive: D:/Archive\n"
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: archive\n"
        "      destination_name: monthly_report.pdf\n"
        "      extra_field: nope\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_copy_file_missing_required_field_raises(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    archive: D:/Archive\n"
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: archive\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_copy_file_non_mapping_section_raises(tmp_path):
    path = _write(tmp_path, "copy_file:\n  - not\n  - a\n  - mapping\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_copy_file_duplicate_key_raises(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    archive: D:/Archive\n"
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    Monthly_Report_Archive:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: archive\n"
        "      destination_name: one.pdf\n"
        "    monthly_report_archive:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: archive\n"
        "      destination_name: two.pdf\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_copy_file_unknown_source_file_reference_raises(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    archive: D:/Archive\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: not_registered\n"
        "      destination_directory: archive\n"
        "      destination_name: monthly_report.pdf\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_copy_file_unknown_destination_directory_reference_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: not_registered\n"
        "      destination_name: monthly_report.pdf\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_copy_file_references_are_case_insensitive(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    Archive: D:/Archive\n"
        "approved_files:\n"
        "  Monthly_Report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: MONTHLY_REPORT\n"
        "      destination_directory: ARCHIVE\n"
        "      destination_name: monthly_report.pdf\n",
    )

    config = load_tools_config(path)

    spec = config.approved_copies["monthly_report_archive"]
    assert spec.source_file_key == "monthly_report"
    assert spec.destination_directory_key == "archive"


@pytest.mark.parametrize("unsafe_name", ["..", "a/b", "a\\b", "CON", "trailing."])
def test_copy_file_unsafe_destination_name_raises_at_config_load(tmp_path, unsafe_name):
    quoted = json.dumps(unsafe_name)
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    archive: D:/Archive\n"
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: archive\n"
        f"      destination_name: {quoted}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


# --- MAX_SYMBOLIC_NAME_LENGTH (Milestone 43 P2 correction) ------------------
# A successful create_directory/copy_file ActionResult.message echoes the
# composite operation key and every reference field verbatim - all must be
# bounded so a successful result can never exceed the M42 persistence
# bound, discovered only AFTER the real side effect already happened.


def test_oversized_create_directory_operation_key_raises_at_config_load(tmp_path):
    long_key = "k" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        f"    {long_key}:\n"
        "      parent_directory: documents\n"
        "      directory_name: exports\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_create_directory_operation_key_at_exactly_the_length_limit_parses(tmp_path):
    exact_key = "k" * MAX_SYMBOLIC_NAME_LENGTH
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        f"    {exact_key}:\n"
        "      parent_directory: documents\n"
        "      directory_name: exports\n",
    )

    config = load_tools_config(path)

    assert exact_key in config.approved_directory_creations


def test_oversized_approved_files_key_raises_at_config_load(tmp_path):
    """Milestone 43 P3 pre-push review correction: a successful
    file_metadata/read_text_file ActionResult.message echoes the
    approved_files key verbatim (see kernel/tools/handlers/file_metadata.py,
    read_text_file.py), so it must be bounded exactly like
    create_directory/copy_file's own composite keys - previously this was
    the one M43 symbolic identifier MAX_SYMBOLIC_NAME_LENGTH did not cover,
    letting a real (not merely hand-built) tools.yaml load successfully and
    then overflow the M42 persistence bound at execution time."""

    long_key = "k" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        "approved_files:\n"
        f"  {long_key}:\n"
        "    path: C:/Users/Example/notes.txt\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_files_key_at_exactly_the_length_limit_parses(tmp_path):
    exact_key = "k" * MAX_SYMBOLIC_NAME_LENGTH
    path = _write(
        tmp_path,
        "approved_files:\n"
        f"  {exact_key}:\n"
        "    path: C:/Users/Example/notes.txt\n",
    )

    config = load_tools_config(path)

    assert exact_key in config.approved_files


def test_oversized_create_directory_parent_reference_raises_at_config_load(tmp_path):
    long_parent_key = "p" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        f"    {long_parent_key}: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        f"      parent_directory: {long_parent_key}\n"
        "      directory_name: exports\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_directory_name_raises_at_config_load(tmp_path):
    long_name = "e" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: documents\n"
        f"      directory_name: {long_name}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_copy_file_operation_key_raises_at_config_load(tmp_path):
    long_key = "k" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    archive: D:/Archive\n"
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        f"    {long_key}:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: archive\n"
        "      destination_name: monthly_report.pdf\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_copy_file_source_reference_raises_at_config_load(tmp_path):
    long_source_key = "s" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    archive: D:/Archive\n"
        "approved_files:\n"
        f"  {long_source_key}:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        f"      source_file: {long_source_key}\n"
        "      destination_directory: archive\n"
        "      destination_name: monthly_report.pdf\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_copy_file_destination_directory_reference_raises_at_config_load(tmp_path):
    long_dest_key = "d" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        f"    {long_dest_key}: D:/Archive\n"
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: monthly_report\n"
        f"      destination_directory: {long_dest_key}\n"
        "      destination_name: monthly_report.pdf\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_destination_name_raises_at_config_load(tmp_path):
    long_name = "n" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    archive: D:/Archive\n"
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: archive\n"
        f"      destination_name: {long_name}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_is_valid_child_name_rejects_a_name_over_the_length_limit():
    assert is_valid_child_name("a" * (MAX_SYMBOLIC_NAME_LENGTH + 1)) is False


def test_is_valid_child_name_accepts_a_name_at_exactly_the_length_limit():
    assert is_valid_child_name("a" * MAX_SYMBOLIC_NAME_LENGTH) is True


def test_full_valid_configuration_parses_all_eight_sections(tmp_path):
    path = _write(
        tmp_path,
        "list_files:\n"
        "  approved_directories:\n"
        "    documents: C:/Users/Example/Documents\n"
        "    archive: D:/Archive\n"
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
        "repo_health:\n"
        "  approved_repositories:\n"
        "    ai_os:\n"
        "      path: C:/AI-OS\n"
        "repository_backup:\n"
        "  approved_backups:\n"
        "    ai_os:\n"
        "      destination_directory: D:/AI-OS-Backups\n"
        "approved_files:\n"
        "  monthly_report:\n"
        "    path: C:/Users/Example/Documents/report.pdf\n"
        "create_directory:\n"
        "  approved_directory_creations:\n"
        "    project_exports:\n"
        "      parent_directory: documents\n"
        "      directory_name: exports\n"
        "copy_file:\n"
        "  approved_copies:\n"
        "    monthly_report_archive:\n"
        "      source_file: monthly_report\n"
        "      destination_directory: archive\n"
        "      destination_name: monthly_report.pdf\n",
    )

    config = load_tools_config(path)

    assert config.approved_directory_creations == {
        "project_exports": DirectoryCreationSpec(
            parent_directory_key="documents", directory_name="exports"
        )
    }
    assert config.approved_copies == {
        "monthly_report_archive": FileCopySpec(
            source_file_key="monthly_report",
            destination_directory_key="archive",
            destination_name="monthly_report.pdf",
        )
    }


# --- Milestone 44 P1: approved_pages ------------------------------------------


def test_approved_pages_omitted_yields_empty_mapping(tmp_path):
    path = _write(tmp_path, "list_files:\n  approved_directories:\n    documents: C:/one\n")

    config = load_tools_config(path)

    assert config.approved_pages == {}


def test_valid_canonical_https_page_parses(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n',
    )

    config = load_tools_config(path)

    assert config.approved_pages == {
        "example_docs": ApprovedPageSpec(
            url="https://example.com:443/docs", allowed_stylesheet_origins=()
        )
    }


def test_allowed_stylesheet_origins_omitted_defaults_to_empty(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n  example_docs:\n    url: \"https://example.com/docs\"\n",
    )

    config = load_tools_config(path)

    assert config.approved_pages["example_docs"].allowed_stylesheet_origins == ()


def test_single_valid_stylesheet_origin(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    allowed_stylesheet_origins:\n"
        '      - "https://static.example.com"\n',
    )

    config = load_tools_config(path)

    assert config.approved_pages["example_docs"].allowed_stylesheet_origins == (
        "https://static.example.com:443",
    )


def test_multiple_stylesheet_origins(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    allowed_stylesheet_origins:\n"
        '      - "https://static.example.com"\n'
        '      - "https://cdn.example.com"\n',
    )

    config = load_tools_config(path)

    assert config.approved_pages["example_docs"].allowed_stylesheet_origins == (
        "https://static.example.com:443",
        "https://cdn.example.com:443",
    )


def test_duplicate_normalized_stylesheet_origin_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    allowed_stylesheet_origins:\n"
        '      - "https://static.example.com"\n'
        '      - "https://STATIC.example.com:443"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_too_many_stylesheet_origins_raises(tmp_path):
    origins = "\n".join(f'      - "https://s{i}.example.com"' for i in range(MAX_STYLESHEET_ORIGINS + 1))
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        f"    allowed_stylesheet_origins:\n{origins}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_max_stylesheet_origins_is_accepted(tmp_path):
    origins = "\n".join(f'      - "https://s{i}.example.com"' for i in range(MAX_STYLESHEET_ORIGINS))
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        f"    allowed_stylesheet_origins:\n{origins}\n",
    )

    config = load_tools_config(path)

    assert len(config.approved_pages["example_docs"].allowed_stylesheet_origins) == MAX_STYLESHEET_ORIGINS


def test_wildcard_stylesheet_origin_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    allowed_stylesheet_origins:\n"
        '      - "https://*.example.com"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_http_page_url_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n  bad:\n    url: \"http://example.com/docs\"\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_http_stylesheet_origin_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    allowed_stylesheet_origins:\n"
        '      - "http://static.example.com"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_userinfo_in_page_url_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n  bad:\n    url: \"https://user:pass@example.com/docs\"\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_userinfo_in_stylesheet_origin_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    allowed_stylesheet_origins:\n"
        '      - "https://user:pass@static.example.com"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "127.255.255.254",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
    ],
)
def test_ipv4_and_localhost_page_urls_rejected(tmp_path, host):
    path = _write(tmp_path, f'approved_pages:\n  bad:\n    url: "https://{host}/docs"\n')

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


@pytest.mark.parametrize("host", ["[::1]", "[fe80::1]"])
def test_ipv6_loopback_and_link_local_page_urls_rejected(tmp_path, host):
    path = _write(tmp_path, f'approved_pages:\n  bad:\n    url: "https://{host}/docs"\n')

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_non_https_scheme_variants_rejected(tmp_path):
    for scheme_url in (
        "file:///etc/passwd",
        "data:text/html,hi",
        "javascript:alert(1)",
        "blob:https://example.com/x",
        "about:blank",
    ):
        path = _write(tmp_path, f'approved_pages:\n  bad:\n    url: "{scheme_url}"\n')
        with pytest.raises(ToolsConfigError):
            load_tools_config(path)


def test_malformed_url_raises(tmp_path):
    path = _write(tmp_path, 'approved_pages:\n  bad:\n    url: "not a url"\n')

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_origin_with_path_raises(tmp_path):
    # allowed_stylesheet_origins entries must be an origin only - no path.
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    allowed_stylesheet_origins:\n"
        '      - "https://static.example.com/some/path"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_origin_with_query_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    allowed_stylesheet_origins:\n"
        '      - "https://static.example.com?x=1"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_default_port_normalization_in_stored_url(tmp_path):
    path = _write(
        tmp_path,
        'approved_pages:\n  a:\n    url: "https://example.com:443/docs"\n',
    )
    path2 = _write(
        tmp_path,
        'approved_pages:\n  a:\n    url: "https://example.com/docs"\n',
    )

    config1 = load_tools_config(path)
    config2 = load_tools_config(path2)

    assert config1.approved_pages["a"].url == config2.approved_pages["a"].url


def test_explicit_non_default_port_preserved(tmp_path):
    path = _write(tmp_path, 'approved_pages:\n  a:\n    url: "https://example.com:8443/docs"\n')

    config = load_tools_config(path)

    assert ":8443" in config.approved_pages["a"].url


def test_hostname_case_normalized(tmp_path):
    path = _write(tmp_path, 'approved_pages:\n  a:\n    url: "https://EXAMPLE.com/docs"\n')

    config = load_tools_config(path)

    assert config.approved_pages["a"].url.startswith("https://example.com")


def test_trailing_dot_hostname_normalized(tmp_path):
    path = _write(tmp_path, 'approved_pages:\n  a:\n    url: "https://example.com./docs"\n')

    config = load_tools_config(path)

    assert "example.com." not in config.approved_pages["a"].url
    assert config.approved_pages["a"].url.startswith("https://example.com:443")


def test_idn_unicode_hostname_rejected(tmp_path):
    path = _write(tmp_path, 'approved_pages:\n  a:\n    url: "https://mÃ¼nchen.example/docs"\n')

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_idn_punycode_hostname_accepted(tmp_path):
    path = _write(
        tmp_path, 'approved_pages:\n  a:\n    url: "https://xn--mnchen-3ya.example/docs"\n'
    )

    config = load_tools_config(path)

    assert "xn--mnchen-3ya.example" in config.approved_pages["a"].url


def test_oversized_symbolic_page_key_raises(tmp_path):
    long_key = "k" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(tmp_path, f'approved_pages:\n  {long_key}:\n    url: "https://example.com/docs"\n')

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_max_length_symbolic_page_key_accepted(tmp_path):
    max_key = "k" * MAX_SYMBOLIC_NAME_LENGTH
    path = _write(tmp_path, f'approved_pages:\n  {max_key}:\n    url: "https://example.com/docs"\n')

    config = load_tools_config(path)

    assert max_key in config.approved_pages


def test_oversized_page_url_raises(tmp_path):
    from kernel.tools.browser_safety import MAX_URL_LENGTH

    huge = "https://example.com/" + ("a" * MAX_URL_LENGTH)
    path = _write(tmp_path, f'approved_pages:\n  a:\n    url: "{huge}"\n')

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_stylesheet_origin_raises(tmp_path):
    from kernel.tools.browser_safety import MAX_STYLESHEET_ORIGIN_LENGTH

    huge = "https://" + ("a" * MAX_STYLESHEET_ORIGIN_LENGTH) + ".example.com"
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    allowed_stylesheet_origins:\n"
        f'      - "{huge}"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_case_insensitive_duplicate_page_key_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  Example_Docs:\n"
        '    url: "https://example.com/docs"\n'
        "  example_docs:\n"
        '    url: "https://other.example.com/docs"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_unknown_field_in_approved_pages_entry_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        "    unexpected_field: 1\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_missing_required_url_field_raises(tmp_path):
    path = _write(tmp_path, "approved_pages:\n  example_docs:\n    allowed_stylesheet_origins: []\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_allowed_stylesheet_origins_not_a_list_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_pages:\n"
        "  example_docs:\n"
        '    url: "https://example.com/docs"\n'
        '    allowed_stylesheet_origins: "https://static.example.com"\n',
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_tools_example_yaml_parses():
    example_path = (
        Path(__file__).resolve().parents[3] / "kernel" / "config" / "tools.example.yaml"
    )
    config = load_tools_config(example_path)

    assert "example_docs" in config.approved_pages
    assert config.approved_pages["example_docs"].url.startswith("https://")
    assert len(config.approved_pages["example_docs"].allowed_stylesheet_origins) == 1


# --- Milestone 44 P1 adversarial-review correction: path ambiguity -----------


def test_dot_segment_page_url_raises_through_full_config_load(tmp_path):
    path = _write(tmp_path, 'approved_pages:\n  bad:\n    url: "https://example.com/a/../docs"\n')

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_percent_encoded_dot_segment_page_url_raises_through_full_config_load(tmp_path):
    path = _write(
        tmp_path, 'approved_pages:\n  bad:\n    url: "https://example.com/a/%2e%2e/docs"\n'
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_backslash_page_url_raises_through_full_config_load(tmp_path):
    path = _write(tmp_path, 'approved_pages:\n  bad:\n    url: "https://example.com/a\\docs"\n')

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_default_port_page_url_still_loads_normally_through_full_config_load(tmp_path):
    # Regression guard: the path-ambiguity rejection above must not
    # accidentally reject an ordinary, unambiguous configured URL.
    path = _write(tmp_path, 'approved_pages:\n  ok:\n    url: "https://example.com/docs"\n')

    config = load_tools_config(path)

    assert config.approved_pages["ok"].url == "https://example.com:443/docs"
