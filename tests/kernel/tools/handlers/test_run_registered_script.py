"""Tests for kernel/tools/handlers/run_registered_script.py."""

import sys

from kernel.tools.config import ScriptSpec, ToolsConfig
from kernel.tools.handlers import run_registered_script
from kernel.tools.types import ActionRequest


def _config(approved_scripts):
    return ToolsConfig(approved_directories={}, approved_applications={}, approved_scripts=approved_scripts)


def _script_spec(tmp_path, code, timeout_seconds=10.0):
    script_path = tmp_path / "script.py"
    script_path.write_text(code, encoding="utf-8")
    return ScriptSpec(
        interpreter=sys.executable,
        script_path=str(script_path),
        cwd=str(tmp_path),
        timeout_seconds=timeout_seconds,
    )


def test_unregistered_key_is_rejected(tmp_path):
    config = _config({})

    result = run_registered_script.run(
        ActionRequest(action="run_registered_script", resource_key="backup"), config
    )

    assert result.success is False
    assert result.outcome == "rejected"


def test_successful_script_reports_executed(tmp_path):
    config = _config({"backup": _script_spec(tmp_path, "print('done')")})

    result = run_registered_script.run(
        ActionRequest(action="run_registered_script", resource_key="backup"), config
    )

    assert result.success is True
    assert result.outcome == "executed"


def test_failing_script_reports_failed(tmp_path):
    config = _config(
        {"backup": _script_spec(tmp_path, "import sys; sys.exit(1)")}
    )

    result = run_registered_script.run(
        ActionRequest(action="run_registered_script", resource_key="backup"), config
    )

    assert result.success is False
    assert result.outcome == "failed"


def test_script_exceeding_timeout_reports_timed_out(tmp_path):
    config = _config(
        {
            "slow": _script_spec(
                tmp_path, "import time; time.sleep(30)", timeout_seconds=0.5
            )
        }
    )

    result = run_registered_script.run(
        ActionRequest(action="run_registered_script", resource_key="slow"), config
    )

    assert result.success is False
    assert result.outcome == "timed_out"


def test_resource_key_is_used_only_as_a_lookup_never_as_an_argument(tmp_path):
    config = _config({"backup": _script_spec(tmp_path, "print('done')")})

    result = run_registered_script.run(
        ActionRequest(action="run_registered_script", resource_key="backup; rm -rf /"), config
    )

    assert result.success is False
    assert result.outcome == "rejected"
