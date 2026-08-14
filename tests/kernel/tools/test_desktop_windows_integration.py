"""Real Windows UIA integration tests for Milestone 45 P1
(kernel/tools/desktop_safety.py's resolve_target_status()/
resolve_control_status(), driven through the real
kernel/tools/handlers/desktop_target_status.py and desktop_control_status.py).
No mocking of pywinauto/UIA - these tests launch a real, dedicated,
test-owned Tkinter fixture (fixtures/desktop_fixture_app.py) and resolve it
through the real library, matching this codebase's established "prove
against the real pipeline, not an estimate" discipline (see M43/M44's own
real-Chromium/real-git-subprocess integration tests).

Windows-only: skipped entirely on any other platform so test collection
never fails elsewhere (see kernel/tools/desktop_safety.py's own PLATFORM
BOUNDARY docstring section).

PRODUCTION-IDENTITY-COMPLIANT LAUNCH: the fixture is launched via
`sys._base_executable` (never `sys.executable`, which inside this
project's uv-managed venv is a launcher/redirector - see
test_venv_launcher_and_base_interpreter_are_different_runtime_images
below for the actual empirical proof this regression protects against).
`approved_desktop_targets[*].process_executable` is configured to that
same `sys._base_executable` path throughout this file - production code
contains no Python-specific exception; the fix lives entirely in how
THESE TESTS choose to launch and configure their own fixture."""

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Milestone 45 P1 is Windows-only (UIA)"
)

if sys.platform == "win32":
    import win32gui
    from pywinauto import findwindows

    from kernel.tools.config import (
        ApplicationSpec,
        DesktopControlSpec,
        DesktopTargetSpec,
        ToolsConfig,
    )
    from kernel.tools.handlers import desktop_control_status, desktop_target_status
    from kernel.tools.types import ActionRequest

_FIXTURE_SCRIPT = Path(__file__).parent / "fixtures" / "desktop_fixture_app.py"
_TITLE = "AIOS-M45-Fixture-Window"
_LAUNCH_TIMEOUT_SECONDS = 10.0


class _FixtureProcess:
    """Launches and tracks exactly one fixture subprocess, always via
    sys._base_executable (see module docstring). Cleans up in __exit__
    even if a test fails partway through."""

    def __init__(self, status_path, extra_args=()):
        self._status_path = status_path
        self._extra_args = list(extra_args)
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys._base_executable, str(_FIXTURE_SCRIPT), str(self._status_path), *self._extra_args]
        )
        return self

    def __exit__(self, *exc_info):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
                self.proc.wait(timeout=5)


def _wait_for_window(title=_TITLE, timeout=_LAUNCH_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = findwindows.find_elements(title=title, backend="uia", visible_only=False)
        if matches:
            return matches[0]
        time.sleep(0.1)
    return None


def _wait_until_gone(title=_TITLE, timeout=_LAUNCH_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = findwindows.find_elements(title=title, backend="uia", visible_only=False)
        if not matches:
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(autouse=True)
def _ensure_no_leaked_fixture_windows():
    """Belt-and-suspenders: fail loudly (rather than let a leaked window
    silently corrupt a LATER test's cardinality expectations) if a
    previous test in this module left a fixture window running."""

    yield
    remaining = findwindows.find_elements(title=_TITLE, backend="uia", visible_only=False)
    assert remaining == [], "a fixture window leaked out of a previous test"


def _tools_config(targets=None, controls=None, applications=None):
    return ToolsConfig(
        approved_directories={},
        approved_applications=applications
        if applications is not None
        else {"fixture_app": ApplicationSpec(executable=sys._base_executable, cwd="C:/")},
        approved_scripts={},
        approved_desktop_targets=targets or {},
        approved_desktop_controls=controls or {},
    )


def _target_spec(process_executable=None, window_class_name="TkTopLevel", automation_id=None):
    return DesktopTargetSpec(
        application_key="fixture_app",
        process_executable=process_executable or sys._base_executable,
        window_class_name=window_class_name,
        window_automation_id=automation_id,
    )


# --- venv-launcher / runtime-image regression --------------------------------


def test_venv_launcher_and_base_interpreter_are_different_runtime_images():
    """The exact empirical finding Milestone 45's validation pass
    reproduced: sys.executable (inside this project's uv-managed venv) and
    sys._base_executable are two DIFFERENT files on disk - proving why
    approved_desktop_targets needed a SEPARATE process_executable field
    rather than reusing approved_applications' own launch path."""

    if sys.executable == sys._base_executable:
        pytest.skip("this interpreter is not running inside a venv launcher/redirector")

    from kernel.tools.desktop_safety import same_windows_executable

    assert same_windows_executable(sys.executable, sys._base_executable) is False


def test_fixture_launched_via_base_executable_reports_that_exact_runtime_image(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt") as fixture:
        element = _wait_for_window()
        assert element is not None

        runtime_exe = psutil.Process(element.process_id).exe()

    from kernel.tools.desktop_safety import same_windows_executable

    assert same_windows_executable(sys._base_executable, runtime_exe) is True


# --- desktop_target_status: cardinality ---------------------------------------


def test_target_absent_is_unavailable(tmp_path):
    config = _tools_config(targets={"fixture_window": _target_spec()})

    result = desktop_target_status.run(
        ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
    )

    assert result.success is True
    assert result.message == "Target 'fixture_window' is unavailable."


def test_exactly_one_fixture_is_available(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        config = _tools_config(targets={"fixture_window": _target_spec()})

        result = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )

    assert result.message == "Target 'fixture_window' is available."


def test_two_fixture_instances_are_ambiguous(tmp_path):
    with _FixtureProcess(tmp_path / "status_a.txt") as a, _FixtureProcess(
        tmp_path / "status_b.txt"
    ) as b:
        deadline = time.monotonic() + _LAUNCH_TIMEOUT_SECONDS
        matches = []
        while time.monotonic() < deadline:
            matches = findwindows.find_elements(title=_TITLE, backend="uia", visible_only=False)
            if len(matches) == 2:
                break
            time.sleep(0.1)
        assert len(matches) == 2, "both fixture instances must be visible before proceeding"

        config = _tools_config(targets={"fixture_window": _target_spec()})
        result = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )

    assert result.message == "Target 'fixture_window' is ambiguous."


def test_wrong_runtime_executable_is_unavailable(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        # A real, but WRONG, executable - never the one that actually
        # launched the live window.
        wrong_exe = str(Path(sys._base_executable).parent / "pythonw.exe")
        config = _tools_config(targets={"fixture_window": _target_spec(process_executable=wrong_exe)})

        result = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )

    assert result.message == "Target 'fixture_window' is unavailable."


def test_wrong_window_class_is_unavailable(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        config = _tools_config(
            targets={"fixture_window": _target_spec(window_class_name="NotTheRealClassName")}
        )

        result = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )

    assert result.message == "Target 'fixture_window' is unavailable."


def test_hidden_target_is_unavailable(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt", extra_args=["--hide"]):
        # No _wait_for_window() gate here - a hidden window may never
        # become enumerable at all (that is exactly what this test
        # proves), so give it a fixed settle time instead.
        time.sleep(2.0)
        config = _tools_config(targets={"fixture_window": _target_spec()})

        result = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )

    assert result.message == "Target 'fixture_window' is unavailable."


def test_minimized_target_is_unavailable(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt", extra_args=["--minimize"]):
        assert _wait_for_window() is not None
        config = _tools_config(targets={"fixture_window": _target_spec()})

        result = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )

    assert result.message == "Target 'fixture_window' is unavailable."


def test_one_visible_one_minimized_candidate_is_available_not_ambiguous(tmp_path):
    """Regression for the adversarial-review finding: two raw UIA
    candidates sharing the configured window class, only one of which is
    actually usable (not minimized), must resolve AVAILABLE - the
    minimized candidate must be filtered out (NON_QUALIFYING) before
    cardinality is computed, never counted toward ambiguity."""

    with _FixtureProcess(tmp_path / "status_visible.txt") as visible_fixture, _FixtureProcess(
        tmp_path / "status_minimized.txt", extra_args=["--minimize"]
    ) as minimized_fixture:
        deadline = time.monotonic() + _LAUNCH_TIMEOUT_SECONDS
        matches = []
        while time.monotonic() < deadline:
            matches = findwindows.find_elements(title=_TITLE, backend="uia", visible_only=False)
            if len(matches) == 2:
                break
            time.sleep(0.1)
        assert len(matches) == 2, "both fixture instances must exist before proceeding"

        config = _tools_config(targets={"fixture_window": _target_spec()})
        result = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )

    assert result.message == "Target 'fixture_window' is available."


def test_both_candidates_minimized_is_unavailable(tmp_path):
    """Companion regression: if EVERY raw candidate is non-qualifying
    (both minimized here), the result is UNAVAILABLE, not AMBIGUOUS and
    not AVAILABLE - zero qualified candidates, exactly like zero raw
    candidates."""

    with _FixtureProcess(
        tmp_path / "status_a.txt", extra_args=["--minimize"]
    ) as fixture_a, _FixtureProcess(
        tmp_path / "status_b.txt", extra_args=["--minimize"]
    ) as fixture_b:
        deadline = time.monotonic() + _LAUNCH_TIMEOUT_SECONDS
        matches = []
        while time.monotonic() < deadline:
            matches = findwindows.find_elements(title=_TITLE, backend="uia", visible_only=False)
            if len(matches) == 2:
                break
            time.sleep(0.1)
        assert len(matches) == 2

        config = _tools_config(targets={"fixture_window": _target_spec()})
        result = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )

    assert result.message == "Target 'fixture_window' is unavailable."


def test_target_disappears_between_calls_fresh_resolution_reports_unavailable(tmp_path):
    config = _tools_config(targets={"fixture_window": _target_spec()})

    with _FixtureProcess(tmp_path / "status.txt") as fixture:
        assert _wait_for_window() is not None
        first = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )
        assert first.message == "Target 'fixture_window' is available."

    assert _wait_until_gone()

    second = desktop_target_status.run(
        ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
    )
    assert second.message == "Target 'fixture_window' is unavailable."


def test_no_title_or_text_ever_appears_in_target_status_output(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        config = _tools_config(targets={"fixture_window": _target_spec()})

        result = desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )

    assert _TITLE not in result.message
    assert "Refresh" not in result.message
    assert "idle" not in result.message


# --- desktop_control_status ---------------------------------------------------


def _control_spec(automation_id="5001", control_type="Button", class_name=None):
    return DesktopControlSpec(
        target_key="fixture_window",
        control_automation_id=automation_id,
        control_type=control_type,
        control_class_name=class_name,
    )


def test_known_control_is_available(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        config = _tools_config(
            targets={"fixture_window": _target_spec()},
            controls={"fixture_refresh": _control_spec()},
        )

        result = desktop_control_status.run(
            ActionRequest(action="desktop_control_status", resource_key="fixture_refresh"), config
        )

    assert result.message == "Control 'fixture_refresh' is available."


def test_wrong_automation_id_is_unavailable(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        config = _tools_config(
            targets={"fixture_window": _target_spec()},
            controls={"fixture_refresh": _control_spec(automation_id="9999")},
        )

        result = desktop_control_status.run(
            ActionRequest(action="desktop_control_status", resource_key="fixture_refresh"), config
        )

    assert result.message == "Control 'fixture_refresh' is unavailable."


def test_same_class_and_type_sibling_never_satisfies_wrong_automation_id(tmp_path):
    """The core empirical finding this milestone's control-identity model
    rests on: the fixture's Quit button shares class_name="Button" and
    control_type="Button" with the Refresh button - configuring the
    REFRESH automation_id but landing on a resolver that ignored
    automation_id would silently resolve to either button. This proves it
    does not: requesting Refresh's automation_id never matches merely
    because SOME same-class/type sibling exists."""

    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        # A automation_id that belongs to NEITHER real button.
        config = _tools_config(
            targets={"fixture_window": _target_spec()},
            controls={"fixture_refresh": _control_spec(automation_id="not_a_real_id")},
        )

        result = desktop_control_status.run(
            ActionRequest(action="desktop_control_status", resource_key="fixture_refresh"), config
        )

    assert result.message == "Control 'fixture_refresh' is unavailable."


def test_control_status_ambiguous_when_underlying_target_is_ambiguous(tmp_path):
    with _FixtureProcess(tmp_path / "status_a.txt"), _FixtureProcess(tmp_path / "status_b.txt"):
        deadline = time.monotonic() + _LAUNCH_TIMEOUT_SECONDS
        matches = []
        while time.monotonic() < deadline:
            matches = findwindows.find_elements(title=_TITLE, backend="uia", visible_only=False)
            if len(matches) == 2:
                break
            time.sleep(0.1)
        assert len(matches) == 2

        config = _tools_config(
            targets={"fixture_window": _target_spec()},
            controls={"fixture_refresh": _control_spec()},
        )
        result = desktop_control_status.run(
            ActionRequest(action="desktop_control_status", resource_key="fixture_refresh"), config
        )

    # An ambiguous TARGET propagates unchanged to the control - the
    # control cannot be uniquely resolved for a documented reason
    # (ambiguity), which is more truthful than "unavailable" (see
    # kernel/tools/desktop_safety.py's resolve_control_status() docstring
    # - corrected after an adversarial review found the original
    # "ambiguous target -> unavailable control" mapping less accurate than
    # propagating the real status).
    assert result.message == "Control 'fixture_refresh' is ambiguous."


def test_control_status_disappears_with_target_fresh_resolution_reports_unavailable(tmp_path):
    config = _tools_config(
        targets={"fixture_window": _target_spec()},
        controls={"fixture_refresh": _control_spec()},
    )

    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        first = desktop_control_status.run(
            ActionRequest(action="desktop_control_status", resource_key="fixture_refresh"), config
        )
        assert first.message == "Control 'fixture_refresh' is available."

    assert _wait_until_gone()

    second = desktop_control_status.run(
        ActionRequest(action="desktop_control_status", resource_key="fixture_refresh"), config
    )
    assert second.message == "Control 'fixture_refresh' is unavailable."


def test_no_metadata_ever_appears_in_control_status_output(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        config = _tools_config(
            targets={"fixture_window": _target_spec()},
            controls={"fixture_refresh": _control_spec()},
        )

        result = desktop_control_status.run(
            ActionRequest(action="desktop_control_status", resource_key="fixture_refresh"), config
        )

    assert "5001" not in result.message
    assert "Button" not in result.message
    assert "Refresh" not in result.message


# --- no side effects -----------------------------------------------------------


def test_status_checks_never_change_the_foreground_window(tmp_path):
    with _FixtureProcess(tmp_path / "status.txt"):
        assert _wait_for_window() is not None
        foreground_before = win32gui.GetForegroundWindow()

        config = _tools_config(
            targets={"fixture_window": _target_spec()},
            controls={"fixture_refresh": _control_spec()},
        )
        desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )
        desktop_control_status.run(
            ActionRequest(action="desktop_control_status", resource_key="fixture_refresh"), config
        )

        foreground_after = win32gui.GetForegroundWindow()

    assert foreground_before == foreground_after


def test_status_checks_never_change_the_fixtures_own_observable_state(tmp_path):
    status_path = tmp_path / "status.txt"
    with _FixtureProcess(status_path):
        assert _wait_for_window() is not None
        before = status_path.read_text(encoding="utf-8")

        config = _tools_config(
            targets={"fixture_window": _target_spec()},
            controls={"fixture_refresh": _control_spec()},
        )
        desktop_target_status.run(
            ActionRequest(action="desktop_target_status", resource_key="fixture_window"), config
        )
        desktop_control_status.run(
            ActionRequest(action="desktop_control_status", resource_key="fixture_refresh"), config
        )

        after = status_path.read_text(encoding="utf-8")

    assert before == after == "idle"
