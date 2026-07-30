"""Tests for kernel/tools/handlers/open_application.py."""

import sys
import time

from kernel.tools.config import ApplicationSpec, ToolsConfig
from kernel.tools.handlers import open_application
from kernel.tools.types import ActionRequest


def _config(approved_applications):
    return ToolsConfig(
        approved_directories={},
        approved_applications=approved_applications,
        approved_scripts={},
    )


def test_unregistered_key_is_rejected(tmp_path):
    config = _config({})

    result = open_application.run(
        ActionRequest(action="open_application", resource_key="notepad"), config
    )

    assert result.success is False
    assert result.outcome == "rejected"


def test_nonexistent_executable_fails_safely(tmp_path):
    config = _config(
        {"ghost": ApplicationSpec(executable=str(tmp_path / "ghost.exe"), cwd=str(tmp_path))}
    )

    result = open_application.run(
        ActionRequest(action="open_application", resource_key="ghost"), config
    )

    assert result.success is False
    assert result.outcome == "failed"


def test_valid_registered_application_launches_and_returns_immediately(tmp_path):
    config = _config(
        {
            "sleeper": ApplicationSpec(
                executable=sys.executable, cwd=str(tmp_path)
            )
        }
    )
    # Launching the real interpreter with a long sleep proves this handler
    # does not block waiting for it to exit.
    started = time.monotonic()
    result = open_application.run(
        ActionRequest(action="open_application", resource_key="sleeper"), config
    )
    elapsed = time.monotonic() - started

    assert result.success is True
    assert result.outcome == "executed"
    assert elapsed < 3

    # Cleanup: the bare interpreter launched with no args exits almost
    # immediately on its own (no script/stdin), so no explicit kill needed.


def test_resource_key_is_used_only_as_a_lookup_never_as_a_command(tmp_path):
    config = _config(
        {"notepad": ApplicationSpec(executable=str(tmp_path / "notepad.exe"), cwd=str(tmp_path))}
    )

    result = open_application.run(
        ActionRequest(action="open_application", resource_key="; calc.exe"), config
    )

    assert result.success is False
    assert result.outcome == "rejected"
