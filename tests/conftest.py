"""
Repository-wide test fixtures.

Autouse, session-independent: redirects kernel/tools/'s audit log default
path to a per-test tmp_path, so no test that exercises the real
SafeTaskExecutor / TasksCapability path (which calls kernel.tools.audit.record()
with no explicit log_path) ever writes to this repository's real
storage/logs/task_actions.jsonl - matching the existing convention that no
test suite here touches real repository storage.
"""

from types import SimpleNamespace

import pytest

from kernel.tools import audit
from kernel.tools.handlers import system_status


@pytest.fixture(autouse=True)
def _redirect_task_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_DEFAULT_LOG_PATH", tmp_path / "task_actions.jsonl")


@pytest.fixture
def deterministic_system_status(monkeypatch):
    """Patches every real machine/service reading kernel/tools/handlers/
    system_status.py makes - psutil CPU/memory/uptime, disk usage, and the
    Ollama/ngrok reachability checks - so any test exercising the real
    system_status handler (directly, through TasksCapability, or through
    the full orchestrator) never touches a real local service and never
    depends on whether Ollama or ngrok happen to be running on this
    machine. Returns the exact resulting message, for equality assertions.
    """

    monkeypatch.setattr(system_status.psutil, "cpu_percent", lambda interval=None: 12.0)
    monkeypatch.setattr(
        system_status.psutil, "virtual_memory", lambda: SimpleNamespace(percent=34.0)
    )
    monkeypatch.setattr(
        system_status.shutil, "disk_usage", lambda path: SimpleNamespace(used=50, total=100)
    )
    monkeypatch.setattr(system_status.psutil, "boot_time", lambda: 0.0)
    monkeypatch.setattr(system_status.time, "time", lambda: 3661.0)
    monkeypatch.setattr(system_status, "_reachable", lambda url: True)

    return "\n".join(
        [
            "CPU: 12%",
            "Memory: 34% used",
            "Disk: 50% used",
            "Uptime: 1h 1m",
            "Ollama: reachable",
            "ngrok: reachable",
            "AI-OS: running",
        ]
    )
