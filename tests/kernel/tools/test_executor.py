"""Tests for kernel/tools/executor.py: the single execution choke point."""

from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest, ActionResult
from kernel.tools.executor import SafeTaskExecutor


class _FakeRegistry:
    def __init__(self, handlers: dict, sensitive: frozenset = frozenset()):
        self._handlers = handlers
        self._sensitive = sensitive

    def is_known(self, action):
        return action in self._handlers

    def is_sensitive(self, action):
        return action in self._sensitive

    def handler_for(self, action):
        return self._handlers.get(action)


def _record_audit_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "kernel.tools.executor.audit.record",
        lambda action, resource_key, outcome: calls.append((action, resource_key, outcome)),
    )
    return calls


def test_unknown_action_is_rejected_and_audited(monkeypatch):
    calls = _record_audit_calls(monkeypatch)
    executor = SafeTaskExecutor(tools_config=None, registry=ActionRegistry())

    result = executor.execute(ActionRequest(action="delete_everything"))

    assert result.success is False
    assert result.outcome == "rejected"
    assert calls == [("delete_everything", None, "rejected")]


def test_successful_handler_result_is_returned_and_audited(monkeypatch):
    calls = _record_audit_calls(monkeypatch)
    handler = lambda request, config: ActionResult(True, "ok", "executed")
    registry = _FakeRegistry({"fake_action": handler})
    executor = SafeTaskExecutor(tools_config="config", registry=registry)

    result = executor.execute(ActionRequest(action="fake_action", resource_key="key"))

    assert result.success is True
    assert result.message == "ok"
    assert calls == [("fake_action", "key", "executed")]


def test_handler_exception_is_converted_to_a_safe_generic_failure(monkeypatch, caplog):
    def raising_handler(request, config):
        raise RuntimeError("leaked path: C:/secret/place")

    calls = _record_audit_calls(monkeypatch)
    registry = _FakeRegistry({"fake_action": raising_handler})
    executor = SafeTaskExecutor(tools_config="config", registry=registry)

    result = executor.execute(ActionRequest(action="fake_action", resource_key="key"))

    assert result.success is False
    assert result.outcome == "failed"
    assert "secret" not in result.message
    assert calls == [("fake_action", "key", "failed")]
    all_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "leaked path" not in all_log_text
    assert "secret" not in all_log_text


def test_handler_receives_the_tools_config_it_was_constructed_with(monkeypatch):
    _record_audit_calls(monkeypatch)
    received = {}

    def handler(request, config):
        received["config"] = config
        return ActionResult(True, "ok", "executed")

    registry = _FakeRegistry({"fake_action": handler})
    sentinel_config = object()
    executor = SafeTaskExecutor(tools_config=sentinel_config, registry=registry)

    executor.execute(ActionRequest(action="fake_action"))

    assert received["config"] is sentinel_config
