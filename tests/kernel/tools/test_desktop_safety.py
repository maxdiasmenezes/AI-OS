"""Tests for kernel/tools/desktop_safety.py that need NO live UIA/window
resolution - pure identity/comparison/classification logic, config-load-time
validation helpers, and Windows file-identity comparison against real
throwaway files. See tests/kernel/tools/test_desktop_windows_integration.py
for the real UIA-fixture-driven coverage (platform-gated)."""

import sys
from unittest.mock import MagicMock, patch

import psutil
import pytest

from kernel.tools import desktop_safety
from kernel.tools.config import DesktopControlSpec, DesktopTargetSpec

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows file-identity APIs are Windows-only"
)


# --- same_windows_executable() / Windows file identity -----------------------


def test_same_file_identity_accepted_for_the_same_path(tmp_path):
    exe = tmp_path / "one.exe"
    exe.write_bytes(b"placeholder")

    assert desktop_safety.same_windows_executable(str(exe), str(exe)) is True


def test_different_files_are_not_the_same_identity(tmp_path):
    a = tmp_path / "a.exe"
    b = tmp_path / "b.exe"
    a.write_bytes(b"placeholder-a")
    b.write_bytes(b"placeholder-b")

    assert desktop_safety.same_windows_executable(str(a), str(b)) is False


def test_case_insensitive_path_still_resolves_to_same_file_identity(tmp_path):
    exe = tmp_path / "MixedCase.exe"
    exe.write_bytes(b"placeholder")

    upper_variant = str(exe).upper()
    lower_variant = str(exe).lower()

    assert desktop_safety.same_windows_executable(upper_variant, lower_variant) is True


def test_a_launcher_and_the_real_target_it_starts_are_not_treated_as_equal(tmp_path):
    """The empirical M45 finding this regression protects: a venv
    launcher executable and the real base interpreter it execs into are
    two DIFFERENT files on disk - same_windows_executable() must never
    equate them merely because one happens to start the other. This test
    proves the general property with two synthetic files standing in for
    'launcher' and 'real target' - see
    test_desktop_windows_integration.py's
    test_venv_launcher_and_base_interpreter_are_different_runtime_images
    for the real, reproduced Python-specific case."""

    launcher = tmp_path / "launcher.exe"
    real_target = tmp_path / "real_target.exe"
    launcher.write_bytes(b"launcher placeholder")
    real_target.write_bytes(b"real target placeholder")

    assert desktop_safety.same_windows_executable(str(launcher), str(real_target)) is False


def test_nonexistent_path_never_matches_anything(tmp_path):
    exe = tmp_path / "exists.exe"
    exe.write_bytes(b"placeholder")
    missing = tmp_path / "does_not_exist.exe"

    assert desktop_safety.same_windows_executable(str(exe), str(missing)) is False
    assert desktop_safety.same_windows_executable(str(missing), str(exe)) is False


def test_both_paths_missing_never_matches(tmp_path):
    a = tmp_path / "missing_a.exe"
    b = tmp_path / "missing_b.exe"

    assert desktop_safety.same_windows_executable(str(a), str(b)) is False


# --- validate_process_executable_path() --------------------------------------


def test_validate_process_executable_path_accepts_real_absolute_file(tmp_path):
    exe = tmp_path / "real.exe"
    exe.write_bytes(b"placeholder")

    result = desktop_safety.validate_process_executable_path(str(exe), field_name="x")

    assert result == str(exe)


def test_validate_process_executable_path_rejects_relative_path(tmp_path):
    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_process_executable_path("relative/exe.exe", field_name="x")


def test_validate_process_executable_path_rejects_missing_file(tmp_path):
    missing = tmp_path / "missing.exe"

    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_process_executable_path(str(missing), field_name="x")


def test_validate_process_executable_path_rejects_directory(tmp_path):
    directory = tmp_path / "a_directory"
    directory.mkdir()

    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_process_executable_path(str(directory), field_name="x")


def test_validate_process_executable_path_rejects_oversized_path(tmp_path):
    oversized = "C:/" + ("a" * desktop_safety.MAX_EXECUTABLE_PATH_LENGTH) + "/x.exe"

    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_process_executable_path(oversized, field_name="x")


def test_validate_process_executable_path_rejects_nul_byte(tmp_path):
    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_process_executable_path("C:/a\x00b.exe", field_name="x")


# --- validate_window_class_name() ---------------------------------------------


def test_validate_window_class_name_accepts_reasonable_value():
    assert desktop_safety.validate_window_class_name("TkTopLevel", field_name="x") == "TkTopLevel"


def test_validate_window_class_name_rejects_empty():
    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_window_class_name("", field_name="x")


def test_validate_window_class_name_rejects_oversized():
    oversized = "a" * (desktop_safety.MAX_WINDOW_CLASS_NAME_LENGTH + 1)
    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_window_class_name(oversized, field_name="x")


# --- validate_automation_id() -------------------------------------------------


def test_validate_automation_id_accepts_reasonable_value():
    assert desktop_safety.validate_automation_id("5001", field_name="x") == "5001"


def test_validate_automation_id_rejects_empty_string():
    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_automation_id("", field_name="x")


def test_validate_automation_id_rejects_none():
    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_automation_id(None, field_name="x")


def test_validate_automation_id_rejects_oversized():
    oversized = "a" * (desktop_safety.MAX_AUTOMATION_ID_LENGTH + 1)
    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_automation_id(oversized, field_name="x")


# --- validate_control_type() --------------------------------------------------


@pytest.mark.parametrize("control_type", sorted(desktop_safety.SUPPORTED_CONTROL_TYPES))
def test_validate_control_type_accepts_every_supported_value(control_type):
    assert desktop_safety.validate_control_type(control_type, field_name="x") == control_type


def test_validate_control_type_rejects_unsupported_value():
    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_control_type("NotARealControlType", field_name="x")


def test_validate_control_type_rejects_none():
    with pytest.raises(desktop_safety.DesktopSafetyError):
        desktop_safety.validate_control_type(None, field_name="x")


def test_is_supported_control_type_helper():
    assert desktop_safety.is_supported_control_type("Button") is True
    assert desktop_safety.is_supported_control_type("NotReal") is False
    assert desktop_safety.is_supported_control_type(None) is False


# --- target_application_reference_valid() -------------------------------------


class _FakeTargetSpec:
    def __init__(self, application_key):
        self.application_key = application_key


class _FakeToolsConfig:
    def __init__(self, applications):
        self.approved_applications = applications


def test_target_application_reference_valid_true_when_present():
    spec = _FakeTargetSpec(application_key="notepad")
    cfg = _FakeToolsConfig(applications={"notepad": object()})

    assert desktop_safety.target_application_reference_valid(spec, cfg) is True


def test_target_application_reference_valid_false_when_removed():
    spec = _FakeTargetSpec(application_key="notepad")
    cfg = _FakeToolsConfig(applications={})

    assert desktop_safety.target_application_reference_valid(spec, cfg) is False


# --- visible-text is never part of the locator model --------------------------


def test_desktop_target_spec_has_no_title_or_text_field():
    from kernel.tools.config import DesktopTargetSpec

    field_names = set(DesktopTargetSpec.__dataclass_fields__)
    assert "title" not in field_names
    assert "window_title" not in field_names
    assert "name" not in field_names
    assert "text" not in field_names


def test_desktop_control_spec_has_no_name_or_text_field():
    from kernel.tools.config import DesktopControlSpec

    field_names = set(DesktopControlSpec.__dataclass_fields__)
    assert "name" not in field_names
    assert "text" not in field_names
    assert "title" not in field_names
