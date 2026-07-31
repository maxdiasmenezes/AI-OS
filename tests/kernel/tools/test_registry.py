"""Tests for kernel/tools/registry.py: the fixed action allowlist."""

from kernel.tools.registry import ActionRegistry


def test_exactly_the_four_milestone_actions_are_known():
    registry = ActionRegistry()

    for action in ("system_status", "list_files", "open_application", "run_registered_script"):
        assert registry.is_known(action)

    assert registry.is_known("delete_everything") is False
    assert registry.is_known("") is False


def test_open_application_and_run_registered_script_are_sensitive():
    registry = ActionRegistry()

    assert registry.is_sensitive("open_application") is True
    assert registry.is_sensitive("run_registered_script") is True


def test_system_status_and_list_files_are_not_sensitive():
    registry = ActionRegistry()

    assert registry.is_sensitive("system_status") is False
    assert registry.is_sensitive("list_files") is False


def test_unknown_action_is_not_sensitive_and_has_no_handler():
    registry = ActionRegistry()

    assert registry.is_sensitive("delete_everything") is False
    assert registry.handler_for("delete_everything") is None


def test_handler_for_returns_a_callable_for_each_known_action():
    registry = ActionRegistry()

    for action in ("system_status", "list_files", "open_application", "run_registered_script"):
        assert callable(registry.handler_for(action))
