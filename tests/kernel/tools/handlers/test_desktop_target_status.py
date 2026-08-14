"""Tests for kernel/tools/handlers/desktop_target_status.py. Monkeypatches
kernel.tools.desktop_safety.resolve_target_status (the one live-UIA
dependency) so these tests exercise the handler's own logic - key lookup,
referential-integrity re-check, message formatting, exception isolation -
without needing a real window. See
tests/kernel/tools/test_desktop_windows_integration.py for the real,
platform-gated UIA-fixture-driven coverage of resolve_target_status()
itself."""

from datetime import datetime, timezone

import pytest

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_execution.observation import build_action_observation, serialize_observation
from kernel.tools.config import ApplicationSpec, DesktopTargetSpec, MAX_SYMBOLIC_NAME_LENGTH, ToolsConfig
from kernel.tools.desktop_safety import DesktopStatus
from kernel.tools.handlers import desktop_target_status
from kernel.tools.types import ActionRequest


def _spec(application_key="notepad"):
    return DesktopTargetSpec(
        application_key=application_key,
        process_executable="C:/Windows/System32/notepad.exe",
        window_class_name="Notepad",
        window_automation_id=None,
    )


def _config(targets, applications=None):
    return ToolsConfig(
        approved_directories={},
        approved_applications=applications
        if applications is not None
        else {"notepad": ApplicationSpec(executable="C:/Windows/System32/notepad.exe", cwd="C:/")},
        approved_scripts={},
        approved_desktop_targets=targets,
    )


def _request(resource_key):
    return ActionRequest(action="desktop_target_status", resource_key=resource_key)


def test_unregistered_key_is_rejected():
    result = desktop_target_status.run(_request("fixture_window"), _config({}))

    assert result.success is False
    assert result.outcome == "rejected"
    assert result.message == "That desktop target is not registered."


def test_missing_resource_key_is_rejected():
    result = desktop_target_status.run(_request(None), _config({}))

    assert result.success is False
    assert result.outcome == "rejected"


def test_oversized_resource_key_is_rejected_before_lookup():
    long_key = "k" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    config = _config({long_key: _spec()})

    result = desktop_target_status.run(_request(long_key), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_removed_application_reference_fails_before_resolution(monkeypatch):
    calls = []
    monkeypatch.setattr(
        desktop_target_status,
        "resolve_target_status",
        lambda *a, **k: calls.append(1) or DesktopStatus.AVAILABLE,
    )
    config = _config({"fixture_window": _spec(application_key="notepad")}, applications={})

    result = desktop_target_status.run(_request("fixture_window"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert calls == []  # resolve_target_status must never be reached


def test_available_status_maps_to_fixed_message(monkeypatch):
    monkeypatch.setattr(
        desktop_target_status, "resolve_target_status", lambda *a, **k: DesktopStatus.AVAILABLE
    )
    config = _config({"fixture_window": _spec()})

    result = desktop_target_status.run(_request("fixture_window"), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert result.message == "Target 'fixture_window' is available."


def test_unavailable_status_maps_to_fixed_message(monkeypatch):
    monkeypatch.setattr(
        desktop_target_status, "resolve_target_status", lambda *a, **k: DesktopStatus.UNAVAILABLE
    )
    config = _config({"fixture_window": _spec()})

    result = desktop_target_status.run(_request("fixture_window"), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert result.message == "Target 'fixture_window' is unavailable."


def test_ambiguous_status_maps_to_fixed_message(monkeypatch):
    monkeypatch.setattr(
        desktop_target_status, "resolve_target_status", lambda *a, **k: DesktopStatus.AMBIGUOUS
    )
    config = _config({"fixture_window": _spec()})

    result = desktop_target_status.run(_request("fixture_window"), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert result.message == "Target 'fixture_window' is ambiguous."


def test_check_failed_status_maps_to_failed_outcome(monkeypatch):
    monkeypatch.setattr(
        desktop_target_status, "resolve_target_status", lambda *a, **k: DesktopStatus.CHECK_FAILED
    )
    config = _config({"fixture_window": _spec()})

    result = desktop_target_status.run(_request("fixture_window"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert result.message == "That desktop target could not be checked."


def test_automation_unavailable_status_maps_to_failed_outcome(monkeypatch):
    monkeypatch.setattr(
        desktop_target_status,
        "resolve_target_status",
        lambda *a, **k: DesktopStatus.AUTOMATION_UNAVAILABLE,
    )
    config = _config({"fixture_window": _spec()})

    result = desktop_target_status.run(_request("fixture_window"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert result.message == "Desktop automation is unavailable."


def test_ambiguous_message_never_includes_a_match_count(monkeypatch):
    monkeypatch.setattr(
        desktop_target_status, "resolve_target_status", lambda *a, **k: DesktopStatus.AMBIGUOUS
    )
    config = _config({"fixture_window": _spec()})

    result = desktop_target_status.run(_request("fixture_window"), config)

    assert any(char.isdigit() for char in result.message) is False


def test_unexpected_exception_from_resolution_is_isolated(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("some raw pywinauto/COM detail that must never leak")

    monkeypatch.setattr(desktop_target_status, "resolve_target_status", _raise)
    config = _config({"fixture_window": _spec()})

    result = desktop_target_status.run(_request("fixture_window"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert "raw pywinauto" not in result.message
    assert result.message == "That desktop target could not be checked."


def test_no_metadata_ever_appears_in_output(monkeypatch):
    monkeypatch.setattr(
        desktop_target_status, "resolve_target_status", lambda *a, **k: DesktopStatus.AVAILABLE
    )
    config = _config({"fixture_window": _spec()})

    result = desktop_target_status.run(_request("fixture_window"), config)

    for forbidden in (
        "notepad.exe",
        "Notepad",
        "C:/Windows",
        "PID",
        "HWND",
        "AutomationId",
        "ClassName",
    ):
        assert forbidden not in result.message


def test_largest_realistic_result_serializes_within_the_real_observation_bound(monkeypatch):
    """Proves the actual worst-case output (the longest allowed symbolic
    key, ambiguous status) survives the real
    build_action_observation()/serialize_observation() pipeline - matching
    the M43/M44 precedent of proving this directly rather than estimating
    it."""

    monkeypatch.setattr(
        desktop_target_status, "resolve_target_status", lambda *a, **k: DesktopStatus.AMBIGUOUS
    )
    long_key = "a" * MAX_SYMBOLIC_NAME_LENGTH
    config = _config({long_key: _spec()})

    result = desktop_target_status.run(_request(long_key), config)
    assert result.success is True

    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())
    serialized = serialize_observation(observation)

    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS
