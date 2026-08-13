"""Tests for kernel/tools/handlers/desktop_control_status.py. Monkeypatches
kernel.tools.desktop_safety.resolve_control_status (the one live-UIA
dependency) - see test_desktop_target_status.py's own module docstring for
why, and test_desktop_windows_integration.py for the real coverage."""

from datetime import datetime, timezone

import pytest

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_execution.observation import build_action_observation, serialize_observation
from kernel.tools.config import (
    MAX_SYMBOLIC_NAME_LENGTH,
    ApplicationSpec,
    DesktopControlSpec,
    DesktopTargetSpec,
    ToolsConfig,
)
from kernel.tools.desktop_safety import DesktopStatus
from kernel.tools.handlers import desktop_control_status
from kernel.tools.types import ActionRequest


def _target_spec(application_key="notepad"):
    return DesktopTargetSpec(
        application_key=application_key,
        process_executable="C:/Windows/System32/notepad.exe",
        window_class_name="Notepad",
        window_automation_id=None,
    )


def _control_spec(target_key="fixture_window"):
    return DesktopControlSpec(
        target_key=target_key,
        control_automation_id="5001",
        control_type="Button",
        control_class_name=None,
    )


def _config(controls, targets=None, applications=None):
    return ToolsConfig(
        approved_directories={},
        approved_applications=applications
        if applications is not None
        else {"notepad": ApplicationSpec(executable="C:/Windows/System32/notepad.exe", cwd="C:/")},
        approved_scripts={},
        approved_desktop_targets=targets if targets is not None else {"fixture_window": _target_spec()},
        approved_desktop_controls=controls,
    )


def _request(resource_key):
    return ActionRequest(action="desktop_control_status", resource_key=resource_key)


def test_unregistered_key_is_rejected():
    result = desktop_control_status.run(_request("fixture_refresh"), _config({}))

    assert result.success is False
    assert result.outcome == "rejected"
    assert result.message == "That desktop control is not registered."


def test_missing_resource_key_is_rejected():
    result = desktop_control_status.run(_request(None), _config({}))

    assert result.success is False
    assert result.outcome == "rejected"


def test_oversized_resource_key_is_rejected_before_lookup():
    long_key = "k" * (MAX_SYMBOLIC_NAME_LENGTH + 1)
    config = _config({long_key: _control_spec()})

    result = desktop_control_status.run(_request(long_key), config)

    assert result.success is False
    assert result.outcome == "rejected"


def test_removed_target_reference_fails_before_resolution(monkeypatch):
    calls = []
    monkeypatch.setattr(
        desktop_control_status,
        "resolve_control_status",
        lambda *a, **k: calls.append(1) or DesktopStatus.AVAILABLE,
    )
    config = _config({"fixture_refresh": _control_spec(target_key="fixture_window")}, targets={})

    result = desktop_control_status.run(_request("fixture_refresh"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert calls == []


def test_removed_application_reference_on_target_fails_before_resolution(monkeypatch):
    calls = []
    monkeypatch.setattr(
        desktop_control_status,
        "resolve_control_status",
        lambda *a, **k: calls.append(1) or DesktopStatus.AVAILABLE,
    )
    config = _config(
        {"fixture_refresh": _control_spec()},
        targets={"fixture_window": _target_spec(application_key="notepad")},
        applications={},
    )

    result = desktop_control_status.run(_request("fixture_refresh"), config)

    assert result.success is False
    assert result.outcome == "rejected"
    assert calls == []


def test_available_status_maps_to_fixed_message(monkeypatch):
    monkeypatch.setattr(
        desktop_control_status, "resolve_control_status", lambda *a, **k: DesktopStatus.AVAILABLE
    )
    config = _config({"fixture_refresh": _control_spec()})

    result = desktop_control_status.run(_request("fixture_refresh"), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert result.message == "Control 'fixture_refresh' is available."


def test_unavailable_status_maps_to_fixed_message(monkeypatch):
    monkeypatch.setattr(
        desktop_control_status, "resolve_control_status", lambda *a, **k: DesktopStatus.UNAVAILABLE
    )
    config = _config({"fixture_refresh": _control_spec()})

    result = desktop_control_status.run(_request("fixture_refresh"), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert result.message == "Control 'fixture_refresh' is unavailable."


def test_ambiguous_status_maps_to_fixed_message(monkeypatch):
    monkeypatch.setattr(
        desktop_control_status, "resolve_control_status", lambda *a, **k: DesktopStatus.AMBIGUOUS
    )
    config = _config({"fixture_refresh": _control_spec()})

    result = desktop_control_status.run(_request("fixture_refresh"), config)

    assert result.success is True
    assert result.outcome == "executed"
    assert result.message == "Control 'fixture_refresh' is ambiguous."


def test_check_failed_status_maps_to_failed_outcome(monkeypatch):
    monkeypatch.setattr(
        desktop_control_status, "resolve_control_status", lambda *a, **k: DesktopStatus.CHECK_FAILED
    )
    config = _config({"fixture_refresh": _control_spec()})

    result = desktop_control_status.run(_request("fixture_refresh"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert result.message == "That desktop control could not be checked."


def test_automation_unavailable_status_maps_to_failed_outcome(monkeypatch):
    monkeypatch.setattr(
        desktop_control_status,
        "resolve_control_status",
        lambda *a, **k: DesktopStatus.AUTOMATION_UNAVAILABLE,
    )
    config = _config({"fixture_refresh": _control_spec()})

    result = desktop_control_status.run(_request("fixture_refresh"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert result.message == "Desktop automation is unavailable."


def test_unexpected_exception_from_resolution_is_isolated(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("some raw pywinauto/COM detail that must never leak")

    monkeypatch.setattr(desktop_control_status, "resolve_control_status", _raise)
    config = _config({"fixture_refresh": _control_spec()})

    result = desktop_control_status.run(_request("fixture_refresh"), config)

    assert result.success is False
    assert result.outcome == "failed"
    assert "raw pywinauto" not in result.message
    assert result.message == "That desktop control could not be checked."


def test_no_metadata_ever_appears_in_output(monkeypatch):
    monkeypatch.setattr(
        desktop_control_status, "resolve_control_status", lambda *a, **k: DesktopStatus.AVAILABLE
    )
    config = _config({"fixture_refresh": _control_spec()})

    result = desktop_control_status.run(_request("fixture_refresh"), config)

    for forbidden in ("5001", "Button", "notepad.exe", "PID", "HWND", "AutomationId"):
        assert forbidden not in result.message


def test_largest_realistic_result_serializes_within_the_real_observation_bound(monkeypatch):
    monkeypatch.setattr(
        desktop_control_status, "resolve_control_status", lambda *a, **k: DesktopStatus.AMBIGUOUS
    )
    long_key = "a" * MAX_SYMBOLIC_NAME_LENGTH
    config = _config({long_key: _control_spec()})

    result = desktop_control_status.run(_request(long_key), config)
    assert result.success is True

    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())
    serialized = serialize_observation(observation)

    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS
