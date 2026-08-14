"""Tests for kernel/tools/config.py's Milestone 45 P1 additions:
approved_desktop_targets and approved_desktop_controls. Mirrors
test_tools_config.py's own style (_write() helper writing a real
tools.yaml-shaped file to tmp_path) - never the real, gitignored
kernel/config/tools.yaml.

process_executable must reference a real, existing regular file at
config-load time (kernel/tools/desktop_safety.py's
validate_process_executable_path()) - these tests create a real throwaway
file under tmp_path for that purpose rather than pointing at a real system
executable, so they need no assumption about what is installed on the
machine running them."""

from pathlib import Path

import pytest

from kernel.tools.config import (
    MAX_SYMBOLIC_NAME_LENGTH,
    DesktopControlSpec,
    DesktopTargetSpec,
    ToolsConfigError,
    load_tools_config,
)
from kernel.tools.desktop_safety import (
    MAX_AUTOMATION_ID_LENGTH,
    MAX_CONTROL_CLASS_NAME_LENGTH,
    MAX_EXECUTABLE_PATH_LENGTH,
    MAX_WINDOW_CLASS_NAME_LENGTH,
)


def _write(tmp_path, text):
    path = tmp_path / "tools.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_executable(tmp_path, name="fake.exe"):
    exe = tmp_path / name
    exe.write_bytes(b"not a real PE, just a placeholder regular file")
    return exe


_APPLICATION_SECTION = (
    "open_application:\n"
    "  approved_applications:\n"
    "    notepad:\n"
    "      executable: C:/Windows/System32/notepad.exe\n"
    "      cwd: C:/Windows/System32\n"
)


# --- omission --------------------------------------------------------------


def test_approved_desktop_targets_omitted_yields_empty_mapping(tmp_path):
    path = _write(tmp_path, _APPLICATION_SECTION)

    config = load_tools_config(path)

    assert config.approved_desktop_targets == {}


def test_approved_desktop_controls_omitted_yields_empty_mapping(tmp_path):
    path = _write(tmp_path, _APPLICATION_SECTION)

    config = load_tools_config(path)

    assert config.approved_desktop_controls == {}


# --- valid target/control ---------------------------------------------------


def test_valid_target_parses(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n",
    )

    config = load_tools_config(path)

    assert config.approved_desktop_targets == {
        "fixture_window": DesktopTargetSpec(
            application_key="notepad",
            process_executable=exe.as_posix(),
            window_class_name="TkTopLevel",
            window_automation_id=None,
        )
    }


def test_valid_target_with_optional_automation_id_parses(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "    window_automation_id: MainWindow\n",
    )

    config = load_tools_config(path)

    assert config.approved_desktop_targets["fixture_window"].window_automation_id == "MainWindow"


def test_valid_control_parses(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  fixture_refresh:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: "5001"\n'
        + "    control_type: Button\n",
    )

    config = load_tools_config(path)

    assert config.approved_desktop_controls == {
        "fixture_refresh": DesktopControlSpec(
            target_key="fixture_window",
            control_automation_id="5001",
            control_type="Button",
            control_class_name=None,
        )
    }


def test_valid_control_with_optional_class_name_parses(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  fixture_refresh:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: "5001"\n'
        + "    control_type: Button\n"
        + "    control_class_name: Button\n",
    )

    config = load_tools_config(path)

    assert config.approved_desktop_controls["fixture_refresh"].control_class_name == "Button"


# --- referential integrity --------------------------------------------------


def test_target_with_missing_application_reference_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        "approved_desktop_targets:\n"
        "  fixture_window:\n"
        "    application: does_not_exist\n"
        f"    process_executable: {exe.as_posix()}\n"
        "    window_class_name: TkTopLevel\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_control_with_missing_target_reference_raises(tmp_path):
    path = _write(
        tmp_path,
        "approved_desktop_controls:\n"
        "  fixture_refresh:\n"
        "    target: does_not_exist\n"
        '    control_automation_id: "5001"\n'
        "    control_type: Button\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


# --- key handling ------------------------------------------------------------


def test_casefold_duplicate_target_key_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  Fixture_Window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_casefold_duplicate_control_key_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  Fixture_Refresh:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: "5001"\n'
        + "    control_type: Button\n"
        + "  fixture_refresh:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: "5002"\n'
        + "    control_type: Button\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_target_key_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    oversized_key = "a" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + f"  {oversized_key}:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_control_key_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    oversized_key = "a" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + f"  {oversized_key}:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: "5001"\n'
        + "    control_type: Button\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


# --- process_executable ------------------------------------------------------


def test_relative_process_executable_raises(tmp_path):
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + "    process_executable: relative/path.exe\n"
        + "    window_class_name: TkTopLevel\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_nonexistent_process_executable_raises(tmp_path):
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {(tmp_path / 'does_not_exist.exe').as_posix()}\n"
        + "    window_class_name: TkTopLevel\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_directory_as_process_executable_raises(tmp_path):
    directory = tmp_path / "a_directory"
    directory.mkdir()
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {directory.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_process_executable_raises(tmp_path):
    # Constructed to fail the length bound regardless of whether the path
    # exists - length is checked before existence.
    oversized = "C:/" + ("a" * MAX_EXECUTABLE_PATH_LENGTH) + "/x.exe"
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {oversized}\n"
        + "    window_class_name: TkTopLevel\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


# --- window_class_name -------------------------------------------------------


def test_missing_window_class_name_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_window_class_name_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    oversized = "a" * (MAX_WINDOW_CLASS_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + f"    window_class_name: {oversized}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_window_automation_id_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    oversized = "a" * (MAX_AUTOMATION_ID_LENGTH + 1)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + f"    window_automation_id: {oversized}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


# --- control_automation_id ---------------------------------------------------


def test_missing_control_automation_id_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  fixture_refresh:\n"
        + "    target: fixture_window\n"
        + "    control_type: Button\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_empty_control_automation_id_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  fixture_refresh:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: ""\n'
        + "    control_type: Button\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_control_automation_id_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    oversized = "a" * (MAX_AUTOMATION_ID_LENGTH + 1)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  fixture_refresh:\n"
        + "    target: fixture_window\n"
        + f"    control_automation_id: {oversized}\n"
        + "    control_type: Button\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


# --- control_type -------------------------------------------------------------


def test_valid_control_type_accepted(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  fixture_entry:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: "note"\n'
        + "    control_type: Edit\n",
    )

    config = load_tools_config(path)

    assert config.approved_desktop_controls["fixture_entry"].control_type == "Edit"


def test_unsupported_control_type_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  fixture_refresh:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: "5001"\n'
        + "    control_type: NotARealUiaControlType\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_oversized_control_class_name_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    oversized = "a" * (MAX_CONTROL_CLASS_NAME_LENGTH + 1)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  fixture_refresh:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: "5001"\n'
        + "    control_type: Button\n"
        + f"    control_class_name: {oversized}\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


# --- unknown fields / malformed mappings -------------------------------------


def test_unknown_field_in_target_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "    window_title: not_allowed\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_unknown_field_in_control_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n"
        + "  fixture_refresh:\n"
        + "    target: fixture_window\n"
        + '    control_automation_id: "5001"\n'
        + "    control_type: Button\n"
        + "    control_name: Refresh\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_desktop_targets_not_a_mapping_raises(tmp_path):
    path = _write(tmp_path, "approved_desktop_targets: not_a_mapping\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_approved_desktop_controls_not_a_mapping_raises(tmp_path):
    path = _write(tmp_path, "approved_desktop_controls: not_a_mapping\n")

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_target_entry_not_a_mapping_raises(tmp_path):
    path = _write(
        tmp_path,
        _APPLICATION_SECTION + "approved_desktop_targets:\n  fixture_window: not_a_mapping\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


def test_control_entry_not_a_mapping_raises(tmp_path):
    exe = _fake_executable(tmp_path)
    path = _write(
        tmp_path,
        _APPLICATION_SECTION
        + "approved_desktop_targets:\n"
        + "  fixture_window:\n"
        + "    application: notepad\n"
        + f"    process_executable: {exe.as_posix()}\n"
        + "    window_class_name: TkTopLevel\n"
        + "approved_desktop_controls:\n  fixture_refresh: not_a_mapping\n",
    )

    with pytest.raises(ToolsConfigError):
        load_tools_config(path)


# --- tools.example.yaml -------------------------------------------------------


def test_tools_example_yaml_parses_desktop_sections():
    example_path = (
        Path(__file__).resolve().parents[3] / "kernel" / "config" / "tools.example.yaml"
    )
    config = load_tools_config(example_path)

    assert "example_window" in config.approved_desktop_targets
    target = config.approved_desktop_targets["example_window"]
    assert target.application_key == "notepad"
    assert target.window_class_name == "Notepad"
    assert target.window_automation_id is None

    assert "example_control" in config.approved_desktop_controls
    control = config.approved_desktop_controls["example_control"]
    assert control.target_key == "example_window"
    assert control.control_type == "Button"
