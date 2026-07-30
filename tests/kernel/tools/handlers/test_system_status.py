"""Tests for kernel/tools/handlers/system_status.py."""

from kernel.tools.handlers import system_status
from kernel.tools.types import ActionRequest


def test_run_reports_reachable_when_both_services_respond(monkeypatch):
    monkeypatch.setattr("kernel.tools.handlers.system_status._reachable", lambda url: True)

    result = system_status.run(ActionRequest(action="system_status"), tools_config=None)

    assert result.success is True
    assert result.outcome == "executed"
    assert "Ollama: reachable" in result.message
    assert "ngrok: reachable" in result.message


def test_run_reports_unreachable_when_services_do_not_respond(monkeypatch):
    monkeypatch.setattr("kernel.tools.handlers.system_status._reachable", lambda url: False)

    result = system_status.run(ActionRequest(action="system_status"), tools_config=None)

    assert "Ollama: unreachable" in result.message
    assert "ngrok: unreachable" in result.message


def test_run_never_needs_tools_config(monkeypatch):
    monkeypatch.setattr("kernel.tools.handlers.system_status._reachable", lambda url: False)

    # tools_config=None must not raise - system_status reads no
    # configuration at all.
    result = system_status.run(ActionRequest(action="system_status"), tools_config=None)

    assert result.success is True


def test_run_includes_all_expected_fields(monkeypatch):
    monkeypatch.setattr("kernel.tools.handlers.system_status._reachable", lambda url: True)

    result = system_status.run(ActionRequest(action="system_status"), tools_config=None)

    for label in ("CPU:", "Memory:", "Disk:", "Uptime:", "Ollama:", "ngrok:", "AI-OS: running"):
        assert label in result.message


def test_format_duration_examples():
    assert system_status._format_duration(59) == "0m"
    assert system_status._format_duration(3661) == "1h 1m"
    assert system_status._format_duration(90000) == "1d 1h 0m"
