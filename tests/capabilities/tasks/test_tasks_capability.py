"""Tests for capabilities/tasks/capability.py: TasksCapability."""

import sys

from kernel.tools.config import ApplicationSpec, ToolsConfig, ToolsConfigError
from kernel.tools.confirmation import ConfirmationStore

from capabilities.tasks.capability import (
    HELP_TEXT,
    TasksCapability,
)


def _make_capability(tools_config=None, confirmation_store=None, ttl_seconds=120.0):
    if confirmation_store is None:
        confirmation_store = ConfirmationStore(ttl_seconds=ttl_seconds)
    loader = (lambda: tools_config) if tools_config is not None else (lambda: ToolsConfig({}, {}, {}))
    return TasksCapability(
        None, None, None, confirmation_store=confirmation_store, tools_config_loader=loader
    ), confirmation_store


def test_requires_computer_actions_is_true():
    capability, _ = _make_capability()

    assert capability.requires_computer_actions is True
    assert capability.id == "tasks"


def test_unrecognized_command_returns_a_generic_error():
    capability, _ = _make_capability()

    response = capability.handle("not a task command")

    assert "Unrecognized command" in response


def test_help_returns_the_fixed_help_text():
    capability, _ = _make_capability()

    assert capability.handle("/task help") == HELP_TEXT
    assert capability.handle("/task") == HELP_TEXT


def test_status_executes_immediately_without_confirmation(deterministic_system_status):
    # deterministic_system_status (tests/conftest.py) patches every real
    # machine/service reading - this never contacts a real Ollama or
    # ngrok, and never depends on either being running.
    capability, store = _make_capability()

    response = capability.handle("/task status")

    assert response == deterministic_system_status
    pending, _ = store.consume()
    assert pending is None  # status is not sensitive - nothing was proposed


def test_files_with_unregistered_key_is_rejected():
    capability, _ = _make_capability(tools_config=ToolsConfig({}, {}, {}))

    response = capability.handle("/task files documents")

    assert "not registered" in response


def test_files_with_registered_key_lists_contents(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    config = ToolsConfig(approved_directories={"documents": str(tmp_path)}, approved_applications={}, approved_scripts={})
    capability, _ = _make_capability(tools_config=config)

    response = capability.handle("/task files documents")

    assert "a.txt" in response


def test_sensitive_action_is_proposed_not_executed_immediately(tmp_path):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": ApplicationSpec(executable=str(tmp_path / "notepad.exe"), cwd=str(tmp_path))},
        approved_scripts={},
    )
    capability, store = _make_capability(tools_config=config)

    response = capability.handle("/task open notepad")

    assert "confirm" in response.lower()
    pending, expired = store.consume()
    assert expired is False
    assert pending.action == "open_application"
    assert pending.resource_key == "notepad"


def test_confirm_with_nothing_pending_says_so():
    capability, _ = _make_capability()

    response = capability.handle("/task confirm")

    assert "no pending action" in response.lower()


def test_confirm_with_expired_confirmation_says_expired(tmp_path):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": ApplicationSpec(executable=str(tmp_path / "notepad.exe"), cwd=str(tmp_path))},
        approved_scripts={},
    )
    capability, store = _make_capability(tools_config=config, ttl_seconds=0.01)

    capability.handle("/task open notepad")
    import time

    time.sleep(0.05)
    response = capability.handle("/task confirm")

    assert "expired" in response.lower()


def test_confirm_executes_the_pending_action_exactly_once(tmp_path):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={"sleeper": ApplicationSpec(executable=sys.executable, cwd=str(tmp_path))},
        approved_scripts={},
    )
    capability, store = _make_capability(tools_config=config)

    propose_response = capability.handle("/task open sleeper")
    assert "launched" not in propose_response.lower()

    confirm_response = capability.handle("/task confirm")
    assert "launched" in confirm_response.lower()

    # A second confirm has nothing left to consume - proves the
    # confirmation cannot be replayed.
    second_confirm_response = capability.handle("/task confirm")
    assert "no pending action" in second_confirm_response.lower()


def test_cancel_clears_a_pending_action(tmp_path):
    config = ToolsConfig(
        approved_directories={},
        approved_applications={"notepad": ApplicationSpec(executable=str(tmp_path / "notepad.exe"), cwd=str(tmp_path))},
        approved_scripts={},
    )
    capability, store = _make_capability(tools_config=config)

    capability.handle("/task open notepad")
    cancel_response = capability.handle("/task cancel")

    assert "cancelled" in cancel_response.lower()
    confirm_response = capability.handle("/task confirm")
    assert "no pending action" in confirm_response.lower()


def test_cancel_with_nothing_pending_says_so():
    capability, _ = _make_capability()

    response = capability.handle("/task cancel")

    assert "no pending action" in response.lower()


def test_invalid_tools_config_produces_a_safe_generic_error_for_resource_actions():
    def raising_loader():
        raise ToolsConfigError("malformed")

    store = ConfirmationStore()
    capability = TasksCapability(
        None, None, None, confirmation_store=store, tools_config_loader=raising_loader
    )

    response = capability.handle("/task files documents")

    assert response == "The task system is temporarily unavailable."
    assert "malformed" not in response


def test_status_is_unaffected_by_an_invalid_tools_config(deterministic_system_status):
    def raising_loader():
        raise ToolsConfigError("malformed")

    store = ConfirmationStore()
    capability = TasksCapability(
        None, None, None, confirmation_store=store, tools_config_loader=raising_loader
    )

    response = capability.handle("/task status")

    assert response == deterministic_system_status
