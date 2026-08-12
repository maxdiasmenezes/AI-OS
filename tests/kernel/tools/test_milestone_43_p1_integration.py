"""Integration tests for Milestone 43 P1's three new actions
(file_metadata, read_text_file, list_processes) through the REAL
ActionRegistry + ToolsConfig + SafeTaskExecutor - never a fake registry or
handler. Process enumeration is still faked (never touches real processes
on the machine running these tests) - see test_list_processes.py's own
_FakeProcess for the same seam, duplicated minimally here rather than
importing test helpers across files."""

from kernel.task_execution.eligibility import revalidate_action
from kernel.tools.config import FileSpec, ToolsConfig
from kernel.tools.executor import SafeTaskExecutor
from kernel.tools.handlers import list_processes as list_processes_handler
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest


class _FakeProcess:
    def __init__(self, pid, name, status="running"):
        self.pid = pid
        self._name = name
        self._status = status

    def name(self):
        return self._name

    def status(self):
        return self._status


def _config(approved_files):
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_files=approved_files,
    )


def test_file_metadata_action_request_routes_to_the_real_handler(tmp_path):
    target = tmp_path / "resume.pdf"
    target.write_bytes(b"pdf bytes")
    config = _config({"resume_pdf": FileSpec(path=str(target))})
    executor = SafeTaskExecutor(config, ActionRegistry())

    result = executor.execute(ActionRequest(action="file_metadata", resource_key="resume_pdf"))

    assert result.success is True
    assert result.outcome == "executed"
    assert "resume_pdf" in result.message


def test_read_text_file_action_request_routes_to_the_real_handler(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_bytes(b"hello there")
    config = _config({"notes_txt": FileSpec(path=str(target))})
    executor = SafeTaskExecutor(config, ActionRegistry())

    result = executor.execute(ActionRequest(action="read_text_file", resource_key="notes_txt"))

    assert result.success is True
    assert "hello there" in result.message


def test_list_processes_action_request_routes_to_the_real_handler(monkeypatch):
    monkeypatch.setattr(
        list_processes_handler.psutil,
        "process_iter",
        lambda: iter([_FakeProcess(1, "explorer.exe")]),
    )
    config = _config({})
    executor = SafeTaskExecutor(config, ActionRegistry())

    result = executor.execute(ActionRequest(action="list_processes", resource_key=None))

    assert result.success is True
    assert "explorer.exe" in result.message


def test_unknown_file_resource_is_rejected_through_the_real_executor():
    config = _config({})
    executor = SafeTaskExecutor(config, ActionRegistry())

    result = executor.execute(ActionRequest(action="file_metadata", resource_key="not_registered"))

    assert result.success is False
    assert result.outcome == "rejected"


def test_file_action_without_resource_key_is_rejected_through_the_real_executor():
    config = _config({})
    executor = SafeTaskExecutor(config, ActionRegistry())

    for action in ("file_metadata", "read_text_file"):
        result = executor.execute(ActionRequest(action=action, resource_key=None))
        assert result.success is False
        assert result.outcome == "rejected"


def test_list_processes_with_a_resource_key_is_rejected_through_the_real_executor(monkeypatch):
    """Correction: kernel/tools's own SafeTaskExecutor/handler layer does
    NOT centrally enforce ActionRegistry's ResourceKeyRequirement for
    every action (system_status, for instance, still simply ignores an
    extraneous resource_key rather than rejecting it - inspected and
    confirmed unchanged, and deliberately not touched here, since a
    generic SafeTaskExecutor-level enforcement is not an established
    architectural pattern anywhere else in this codebase). Rather than
    broadening SafeTaskExecutor, list_processes.py itself now fails closed
    when given a resource_key it must never have (see
    kernel/tools/handlers/list_processes.py's run()) - proven here through
    the REAL SafeTaskExecutor + ActionRegistry, not just a direct handler
    call."""

    monkeypatch.setattr(
        list_processes_handler.psutil,
        "process_iter",
        lambda: iter([_FakeProcess(1, "explorer.exe")]),
    )
    config = _config({})
    executor = SafeTaskExecutor(config, ActionRegistry())

    result = executor.execute(ActionRequest(action="list_processes", resource_key="some_key"))

    assert result.success is False
    assert result.outcome == "rejected"


def test_list_processes_with_a_resource_key_is_also_rejected_by_execution_time_revalidation():
    """Defense in depth, unchanged by this correction: even independently
    of list_processes.py's own new check, kernel.task_execution.eligibility.
    revalidate_action() - the execution-time revalidation every M42
    TaskPlan step must always pass before SafeTaskExecutor is ever called -
    still separately rejects a list_processes step carrying a resource_key
    it must never have, and accepts one that correctly omits it."""

    config = _config({})
    registry = ActionRegistry()

    assert revalidate_action("list_processes", "some_key", registry, config) is False
    assert revalidate_action("list_processes", None, registry, config) is True
