"""End-to-end acceptance tests for Milestone 44 P1 (browser_read_page)
driven through the REAL M42 pipeline: real TaskRepository (tmp_path
SQLite), real ActionRegistry/ToolsConfig, real SafeTaskExecutor, and the
real kernel.tools.handlers.browser_read_page.run() -> _execute_read()
chain against a real, local, deterministic Chromium instance - mirrors
tests/kernel/task_execution/test_milestone_43_p1_e2e.py's own discipline
exactly (real pipeline, only the one external system dependency faked).

Milestone 44 P1's production entry point (run() -> _build_authority())
always enforces HTTPS-only/private-network rejection, so it can never be
pointed at a plain-HTTP loopback fixture server directly - the one thing
this file fakes, for scenarios A and B only, is _build_authority() itself
(monkeypatched to return a PageAuthority pointing at a real local fixture
server), exactly matching test_milestone_43_p1_e2e.py's own precedent of
faking psutil.process_iter (list_processes' one external-system
dependency) while keeping every other layer real. Scenario C needs no such
fake at all - a stale/removed approved_pages resource must fail at
eligibility revalidation, before SafeTaskExecutor (and therefore before
browser_read_page.run()) is ever reached.

Proves browser_read_page needed ZERO changes to
kernel/task_execution/service.py, eligibility.py, or respond.py - it is
reachable purely because it is a registered action with a configured
resource, exactly like every action since Milestone 33."""

import http.server
import socketserver
import threading

import pytest

from kernel.employee_tasks import TaskRepository, TaskState, open_writer_connection
from kernel.models.base import ModelResponse
from kernel.task_execution.observation import deserialize_observation
from kernel.task_execution.service import run_task_until_blocked
from kernel.task_execution.types import ExecutionAdvanceStatus
from kernel.task_planner import PlanStep, StepKind, TaskPlan, serialize_plan
from kernel.tools import browser_safety
from kernel.tools.config import ApprovedPageSpec, ToolsConfig
from kernel.tools.executor import SafeTaskExecutor
from kernel.tools.handlers import browser_read_page
from kernel.tools.registry import ActionRegistry
from kernel.tools.types import ActionRequest, ActionResult


class _FakeExecutor:
    """Records every ActionRequest it receives - used only for scenario C
    (stale resource revalidation), where the point is proving the real
    handler is never reached at all."""

    def __init__(self):
        self.calls: list[ActionRequest] = []

    def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request)
        raise AssertionError("SafeTaskExecutor must never be called for a revalidation failure")


class _FakeModelProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def send_prompt(self, prompt, *, options=None):
        self.calls.append(prompt)
        return self._responses.pop(0)


def _action_step(position, action_name, resource_key, *, depends_on=()):
    return PlanStep(
        step_id=f"step_{position}",
        position=position,
        kind=StepKind.ACTION,
        action_name=action_name,
        resource_key=resource_key,
        catalog_id="action_1",
        description="read the page",
        expected_result="the page content is read",
        depends_on=depends_on,
        requires_confirmation=False,
    )


def _respond_step(position, *, depends_on=()):
    return PlanStep(
        step_id=f"step_{position}",
        position=position,
        kind=StepKind.RESPOND,
        action_name=None,
        resource_key=None,
        catalog_id=None,
        description="summarize",
        expected_result="a summary",
        depends_on=depends_on,
        requires_confirmation=False,
    )


def _config(approved_pages):
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_pages=approved_pages,
    )


def _ready_task(repo, steps):
    record = repo.create_task("read the page", "whatsapp")
    repo.transition_task(record.task_id, "created", "planning")
    plan = TaskPlan(
        plan_version=1,
        task_id=record.task_id,
        objective="read the page",
        steps=tuple(steps),
        created_at="2026-08-12T00:00:00+00:00",
    )
    return repo.persist_plan_and_ready(record.task_id, "planning", serialize_plan(plan))


@pytest.fixture
def repo(tmp_path):
    conn = open_writer_connection(tmp_path / "tasks.sqlite3")
    yield TaskRepository(conn)
    conn.close()


@pytest.fixture
def registry():
    return ActionRegistry()


@pytest.fixture(scope="module")
def fixture_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path == "/docs":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<!doctype html><html><head><title>Docs</title></head>"
                    b"<body><p>The quarterly report is on track.</p></body></html>"
                )
            else:
                self.send_response(404)
                self.end_headers()

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
def fake_authority(fixture_server, monkeypatch):
    """Points browser_read_page's production entry point at the real
    local fixture server for exactly one resource key, bypassing only the
    HTTPS-only/private-network validation _build_authority() would
    otherwise correctly enforce - see this module's own docstring."""

    def _build_authority(spec):
        return browser_safety.PageAuthority(
            document_url=f"{fixture_server}/docs",
            document_origin=browser_safety.url_origin(f"{fixture_server}/docs"),
            stylesheet_origins=(),
        )

    monkeypatch.setattr(browser_read_page, "_build_authority", _build_authority)


# --- A: browser_read_page action alone completes the task -------------------


def test_a_browser_read_page_action_completes_via_the_real_pipeline(
    repo, registry, fake_authority
):
    tools_config = _config({"example_docs": ApprovedPageSpec(url="https://example.com:443/docs")})
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "browser_read_page", "example_docs")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert model.calls == []

    step = repo.get_step_progress(task.task_id, 1)
    observation = deserialize_observation(step.result_json)
    assert observation.success is True
    assert observation.step_kind == StepKind.ACTION
    assert "example_docs" in observation.safe_summary
    assert "The quarterly report is on track." in observation.safe_summary


def test_a_browser_read_page_requires_no_confirmation(repo, registry, fake_authority):
    tools_config = _config({"example_docs": ApprovedPageSpec(url="https://example.com:443/docs")})
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "browser_read_page", "example_docs")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    # Never CONFIRMATION_REQUIRED/WAITING_FOR_CONFIRMATION - non-sensitive,
    # exactly like read_text_file/file_metadata.
    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED


# --- B: browser_read_page -> RESPOND depends on the durable observation -----


def test_b_browser_read_page_then_respond_synthesizes_from_durable_observation(
    repo, registry, fake_authority
):
    tools_config = _config({"example_docs": ApprovedPageSpec(url="https://example.com:443/docs")})
    executor = SafeTaskExecutor(tools_config, registry)
    model = _FakeModelProvider(
        responses=[ModelResponse("The report is on track.", "fake", 0, 0, 0.0)]
    )

    task = _ready_task(
        repo,
        [
            _action_step(1, "browser_read_page", "example_docs"),
            _respond_step(2, depends_on=(1,)),
        ],
    )

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_COMPLETED
    assert result.task.state == TaskState.COMPLETED
    assert len(model.calls) == 1
    # The model saw the durable, safe page-content observation as its
    # evidence - proving the RESPOND step used the real browser_read_page
    # result, not a stale/synthetic value. It is presented as untrusted
    # DATA (see kernel/task_execution/respond.py's own DATA-not-INSTRUCTION
    # prompt discipline) - never as an instruction, and this module's own
    # fixture page content contains no command-like text to begin with.
    assert "The quarterly report is on track." in model.calls[0]

    step_2 = repo.get_step_progress(task.task_id, 2)
    respond_observation = deserialize_observation(step_2.result_json)
    assert respond_observation.safe_summary == "The report is on track."


# --- C: stale/unknown approved_pages resource fails closed, never executes --


def test_c_stale_approved_pages_resource_fails_closed_without_ever_calling_the_executor(
    repo, registry
):
    # The plan was persisted referencing "example_docs", but the CURRENT
    # tools_config (as of execution time) no longer configures it - e.g.
    # removed from kernel/config/tools.yaml since planning. M42's existing,
    # unmodified eligibility revalidation must fail this closed before
    # SafeTaskExecutor (here, one that raises if ever called) is reached -
    # no browser is ever launched.
    tools_config = _config({})  # "example_docs" is not configured
    executor = _FakeExecutor()
    model = _FakeModelProvider(responses=[])

    task = _ready_task(repo, [_action_step(1, "browser_read_page", "example_docs")])

    result = run_task_until_blocked(task, repo, registry, tools_config, executor, model)

    assert result.status == ExecutionAdvanceStatus.TASK_FAILED
    assert result.detail == "action_no_longer_valid"
    assert result.task.state == TaskState.FAILED
    assert executor.calls == []
    assert repo.get_step_progress(task.task_id, 1) is None
