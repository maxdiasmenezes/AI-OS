"""Regression tests for the Milestone 45 P1 pre-staging correction pass:
proves complete-authority-before-cardinality, the QUALIFIED/NON_QUALIFYING/
INSPECTION_FAILED candidate model, CHECK_FAILED/AUTOMATION_UNAVAILABLE
semantics, target-ambiguity propagation into control status, and the
runtime manual-spec revalidation gap an adversarial review found and
reproduced. Uses mocked UIA/psutil/win32 candidates (never a real live
window) so the exact candidate-evaluation ORDER can be proven directly -
see tests/kernel/tools/test_desktop_windows_integration.py for the
live-fixture equivalents of the minimized-collision scenarios, which are
directly constructible with real processes and are proven there instead."""

import sys
from unittest.mock import MagicMock

import psutil
import pytest

from kernel.tools import desktop_safety
from kernel.tools.config import DesktopControlSpec, DesktopTargetSpec
from kernel.tools.desktop_safety import DesktopStatus

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="desktop_safety resolution is Windows-only"
)

# Two REAL, distinct files guaranteed to exist on any Windows machine -
# used so compare_windows_executable_identity() runs for REAL (no mocking
# of the file-identity layer itself needed for the "wrong process" cases),
# keeping these tests focused on candidate-evaluation ORDER, not on
# re-proving file identity (already covered in test_desktop_safety.py).
_EXE_CORRECT = sys.executable
_EXE_WRONG = r"C:\Windows\System32\notepad.exe"


class _FakeElement:
    def __init__(self, handle, process_id):
        self.handle = handle
        self.process_id = process_id


def _target_spec(process_executable=_EXE_CORRECT, class_name="TkTopLevel", automation_id=None):
    return DesktopTargetSpec(
        application_key="fixture_app",
        process_executable=process_executable,
        window_class_name=class_name,
        window_automation_id=automation_id,
    )


def _control_spec(target_key="fixture_window", automation_id="5001", control_type="Button", class_name=None):
    return DesktopControlSpec(
        target_key=target_key,
        control_automation_id=automation_id,
        control_type=control_type,
        control_class_name=class_name,
    )


def _patch_windows(monkeypatch, *, find_elements, is_iconic=None, process_factory=None):
    monkeypatch.setattr(desktop_safety, "WINDOWS", True)
    monkeypatch.setattr(desktop_safety, "findwindows", MagicMock(find_elements=find_elements))
    if is_iconic is not None:
        monkeypatch.setattr(desktop_safety, "win32gui", MagicMock(IsIconic=is_iconic))
    if process_factory is not None:
        monkeypatch.setattr(desktop_safety.psutil, "Process", process_factory)


# --- Section 14: wrong-process collision --------------------------------------


def test_wrong_process_candidate_does_not_create_ambiguity(monkeypatch):
    """Two raw candidates, same locator: one correct process, one wrong.
    Must be AVAILABLE (one qualified candidate), never AMBIGUOUS."""

    spec = _target_spec(process_executable=_EXE_CORRECT)
    correct = _FakeElement(handle=1, process_id=111)
    wrong = _FakeElement(handle=2, process_id=222)

    def process_factory(pid):
        m = MagicMock()
        m.exe.return_value = _EXE_CORRECT if pid == 111 else _EXE_WRONG
        return m

    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [correct, wrong],
        is_iconic=lambda h: False,
        process_factory=process_factory,
    )

    status, element = desktop_safety._resolve_target_internal(spec)

    assert status is DesktopStatus.AVAILABLE
    assert element is correct


def test_wrong_process_candidate_both_evaluated_before_cardinality(monkeypatch):
    """Proves BOTH candidates are actually evaluated against complete
    authority (not just the first one) before cardinality is decided -
    order-independence check with the wrong-process candidate listed
    FIRST."""

    spec = _target_spec(process_executable=_EXE_CORRECT)
    wrong = _FakeElement(handle=2, process_id=222)
    correct = _FakeElement(handle=1, process_id=111)

    def process_factory(pid):
        m = MagicMock()
        m.exe.return_value = _EXE_CORRECT if pid == 111 else _EXE_WRONG
        return m

    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [wrong, correct],  # wrong-process FIRST this time
        is_iconic=lambda h: False,
        process_factory=process_factory,
    )

    status, element = desktop_safety._resolve_target_internal(spec)

    assert status is DesktopStatus.AVAILABLE
    assert element is correct


# --- Section 15/16: minimized collision / multiple complete matches ----------


def test_minimized_candidate_does_not_create_ambiguity(monkeypatch):
    """Two raw candidates, BOTH the correct process - one visible, one
    minimized. Must be AVAILABLE, never AMBIGUOUS."""

    spec = _target_spec(process_executable=_EXE_CORRECT)
    visible = _FakeElement(handle=1, process_id=111)
    minimized = _FakeElement(handle=2, process_id=111)

    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [visible, minimized],
        is_iconic=lambda h: h == 2,
        process_factory=lambda pid: MagicMock(exe=lambda: _EXE_CORRECT),
    )

    status, element = desktop_safety._resolve_target_internal(spec)

    assert status is DesktopStatus.AVAILABLE
    assert element is visible


def test_both_candidates_minimized_is_unavailable(monkeypatch):
    spec = _target_spec(process_executable=_EXE_CORRECT)
    a = _FakeElement(handle=1, process_id=111)
    b = _FakeElement(handle=2, process_id=111)

    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [a, b],
        is_iconic=lambda h: True,
        process_factory=lambda pid: MagicMock(exe=lambda: _EXE_CORRECT),
    )

    status, element = desktop_safety._resolve_target_internal(spec)

    assert status is DesktopStatus.UNAVAILABLE
    assert element is None


def test_multiple_fully_qualified_candidates_is_ambiguous(monkeypatch):
    """Two raw candidates that BOTH satisfy complete authority (correct
    process, visible, not minimized) must be AMBIGUOUS - proves the fix
    did not accidentally broaden to 'take the first qualifying one'."""

    spec = _target_spec(process_executable=_EXE_CORRECT)
    a = _FakeElement(handle=1, process_id=111)
    b = _FakeElement(handle=2, process_id=222)

    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [a, b],
        is_iconic=lambda h: False,
        process_factory=lambda pid: MagicMock(exe=lambda: _EXE_CORRECT),
    )

    status, element = desktop_safety._resolve_target_internal(spec)

    assert status is DesktopStatus.AMBIGUOUS
    assert element is None


# --- Section 17: inspection failure during multi-candidate check -------------


def test_inspection_failure_among_candidates_is_check_failed_even_with_a_qualified_candidate(
    monkeypatch,
):
    """Candidate A fully qualifies; candidate B's process-executable
    inspection raises AccessDenied. Overall result must be CHECK_FAILED -
    never AVAILABLE/AMBIGUOUS/UNAVAILABLE - because exact cardinality
    cannot be proven while B remains unresolved (B might also have
    qualified)."""

    spec = _target_spec(process_executable=_EXE_CORRECT)
    qualifies = _FakeElement(handle=1, process_id=111)
    fails_inspection = _FakeElement(handle=2, process_id=222)

    def process_factory(pid):
        if pid == 111:
            return MagicMock(exe=lambda: _EXE_CORRECT)
        raise psutil.AccessDenied(pid=222)

    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [qualifies, fails_inspection],
        is_iconic=lambda h: False,
        process_factory=process_factory,
    )

    status, element = desktop_safety._resolve_target_internal(spec)

    assert status is DesktopStatus.CHECK_FAILED
    assert element is None


# --- Section 18: target failure regression matrix -----------------------------


def test_find_elements_raises_is_check_failed(monkeypatch):
    def raise_find_elements(**kwargs):
        raise RuntimeError("simulated pywinauto/COM crash")

    _patch_windows(monkeypatch, find_elements=raise_find_elements)

    status, element = desktop_safety._resolve_target_internal(_target_spec())

    assert status is DesktopStatus.CHECK_FAILED
    assert element is None


def test_psutil_access_denied_is_check_failed(monkeypatch):
    one = _FakeElement(handle=1, process_id=111)
    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [one],
        is_iconic=lambda h: False,
        process_factory=MagicMock(side_effect=psutil.AccessDenied(pid=111)),
    )

    status, element = desktop_safety._resolve_target_internal(_target_spec())

    assert status is DesktopStatus.CHECK_FAILED
    assert element is None


def test_psutil_no_such_process_during_inspection_is_non_qualifying(monkeypatch):
    """A candidate whose owning process cleanly exited (NoSuchProcess) is
    ordinary non-qualification, not an inspection failure - final status
    depends on the REMAINING candidates. With no other candidates, this
    means UNAVAILABLE (0 qualified), never CHECK_FAILED."""

    one = _FakeElement(handle=1, process_id=111)
    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [one],
        is_iconic=lambda h: False,
        process_factory=MagicMock(side_effect=psutil.NoSuchProcess(pid=111)),
    )

    status, element = desktop_safety._resolve_target_internal(_target_spec())

    assert status is DesktopStatus.UNAVAILABLE
    assert element is None


def test_no_such_process_candidate_alongside_a_qualified_one_is_available(monkeypatch):
    """A vanished candidate must not poison the result the way an
    INSPECTION_FAILED one does - with one qualifying and one vanished,
    the result is AVAILABLE, not CHECK_FAILED."""

    qualifies = _FakeElement(handle=1, process_id=111)
    vanished = _FakeElement(handle=2, process_id=222)

    def process_factory(pid):
        if pid == 111:
            return MagicMock(exe=lambda: _EXE_CORRECT)
        raise psutil.NoSuchProcess(pid=222)

    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [qualifies, vanished],
        is_iconic=lambda h: False,
        process_factory=process_factory,
    )

    status, element = desktop_safety._resolve_target_internal(_target_spec(process_executable=_EXE_CORRECT))

    assert status is DesktopStatus.AVAILABLE
    assert element is qualifies


def test_is_iconic_raises_is_check_failed(monkeypatch):
    one = _FakeElement(handle=1, process_id=111)
    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [one],
        is_iconic=MagicMock(side_effect=RuntimeError("simulated Win32 failure")),
    )

    status, element = desktop_safety._resolve_target_internal(_target_spec())

    assert status is DesktopStatus.CHECK_FAILED
    assert element is None


def test_file_identity_query_raises_is_check_failed(monkeypatch):
    one = _FakeElement(handle=1, process_id=111)
    _patch_windows(
        monkeypatch,
        find_elements=lambda **k: [one],
        is_iconic=lambda h: False,
        process_factory=lambda pid: MagicMock(exe=lambda: _EXE_CORRECT),
    )
    monkeypatch.setattr(
        desktop_safety,
        "win32file",
        MagicMock(CreateFile=MagicMock(side_effect=RuntimeError("simulated win32file failure"))),
    )

    status, element = desktop_safety._resolve_target_internal(_target_spec(process_executable=_EXE_CORRECT))

    assert status is DesktopStatus.CHECK_FAILED
    assert element is None


def test_non_windows_is_automation_unavailable(monkeypatch):
    monkeypatch.setattr(desktop_safety, "WINDOWS", False)

    status, element = desktop_safety._resolve_target_internal(_target_spec())

    assert status is DesktopStatus.AUTOMATION_UNAVAILABLE
    assert element is None


def test_no_scenario_ever_raises_out_of_resolve_target_status(monkeypatch):
    """No raw exception detail should ever propagate to a caller -
    resolve_target_status() itself must still never raise."""

    def raise_find_elements(**kwargs):
        raise RuntimeError("simulated pywinauto/COM crash with sensitive detail")

    _patch_windows(monkeypatch, find_elements=raise_find_elements)

    status = desktop_safety.resolve_target_status(_target_spec())
    assert status is DesktopStatus.CHECK_FAILED


# --- Section 19: control failure regression matrix ----------------------------


def test_control_status_propagates_ambiguous_target(monkeypatch):
    monkeypatch.setattr(
        desktop_safety, "_resolve_target_internal", lambda spec: (DesktopStatus.AMBIGUOUS, None)
    )
    status = desktop_safety.resolve_control_status(_target_spec(), _control_spec())
    assert status is DesktopStatus.AMBIGUOUS


def test_control_status_propagates_unavailable_target(monkeypatch):
    monkeypatch.setattr(
        desktop_safety, "_resolve_target_internal", lambda spec: (DesktopStatus.UNAVAILABLE, None)
    )
    status = desktop_safety.resolve_control_status(_target_spec(), _control_spec())
    assert status is DesktopStatus.UNAVAILABLE


def test_control_status_propagates_check_failed_target(monkeypatch):
    monkeypatch.setattr(
        desktop_safety, "_resolve_target_internal", lambda spec: (DesktopStatus.CHECK_FAILED, None)
    )
    status = desktop_safety.resolve_control_status(_target_spec(), _control_spec())
    assert status is DesktopStatus.CHECK_FAILED


def test_control_status_propagates_automation_unavailable_target(monkeypatch):
    monkeypatch.setattr(
        desktop_safety,
        "_resolve_target_internal",
        lambda spec: (DesktopStatus.AUTOMATION_UNAVAILABLE, None),
    )
    status = desktop_safety.resolve_control_status(_target_spec(), _control_spec())
    assert status is DesktopStatus.AUTOMATION_UNAVAILABLE


def test_control_find_elements_raises_is_check_failed(monkeypatch):
    target_element = _FakeElement(handle=1, process_id=111)
    monkeypatch.setattr(
        desktop_safety,
        "_resolve_target_internal",
        lambda spec: (DesktopStatus.AVAILABLE, target_element),
    )
    monkeypatch.setattr(desktop_safety, "WINDOWS", True)
    monkeypatch.setattr(
        desktop_safety,
        "findwindows",
        MagicMock(find_elements=MagicMock(side_effect=RuntimeError("simulated crash"))),
    )

    status = desktop_safety.resolve_control_status(_target_spec(), _control_spec())
    assert status is DesktopStatus.CHECK_FAILED


@pytest.mark.parametrize(
    "match_count, expected",
    [(0, DesktopStatus.UNAVAILABLE), (1, DesktopStatus.AVAILABLE), (2, DesktopStatus.AMBIGUOUS)],
)
def test_control_cardinality_zero_one_multiple(monkeypatch, match_count, expected):
    target_element = _FakeElement(handle=1, process_id=111)
    monkeypatch.setattr(
        desktop_safety,
        "_resolve_target_internal",
        lambda spec: (DesktopStatus.AVAILABLE, target_element),
    )
    monkeypatch.setattr(desktop_safety, "WINDOWS", True)
    fake_controls = [_FakeElement(handle=100 + i, process_id=111) for i in range(match_count)]
    monkeypatch.setattr(
        desktop_safety, "findwindows", MagicMock(find_elements=lambda **k: fake_controls)
    )

    status = desktop_safety.resolve_control_status(_target_spec(), _control_spec())
    assert status is expected


# --- Section 20: manual-config defense-in-depth (zero UIA calls) -------------


def _assert_zero_uia_calls(monkeypatch, resolve_call):
    spy = MagicMock(side_effect=AssertionError("find_elements must never be called for an invalid spec"))
    monkeypatch.setattr(desktop_safety, "WINDOWS", True)
    monkeypatch.setattr(desktop_safety, "findwindows", MagicMock(find_elements=spy))
    resolve_call()
    spy.assert_not_called()


def test_empty_window_class_name_never_reaches_find_elements(monkeypatch):
    bad_spec = _target_spec(class_name="")
    status = None

    def call():
        nonlocal status
        status = desktop_safety.resolve_target_status(bad_spec)

    _assert_zero_uia_calls(monkeypatch, call)
    assert status is DesktopStatus.CHECK_FAILED


def test_oversized_window_class_name_never_reaches_find_elements(monkeypatch):
    bad_spec = _target_spec(class_name="a" * (desktop_safety.MAX_WINDOW_CLASS_NAME_LENGTH + 1))
    status = None

    def call():
        nonlocal status
        status = desktop_safety.resolve_target_status(bad_spec)

    _assert_zero_uia_calls(monkeypatch, call)
    assert status is DesktopStatus.CHECK_FAILED


def test_relative_process_executable_never_reaches_find_elements(monkeypatch):
    bad_spec = _target_spec(process_executable="relative/notepad.exe")
    status = None

    def call():
        nonlocal status
        status = desktop_safety.resolve_target_status(bad_spec)

    _assert_zero_uia_calls(monkeypatch, call)
    assert status is DesktopStatus.CHECK_FAILED


def test_empty_control_automation_id_never_reaches_find_elements(monkeypatch):
    target_element = _FakeElement(handle=1, process_id=111)
    monkeypatch.setattr(
        desktop_safety,
        "_resolve_target_internal",
        lambda spec: (DesktopStatus.AVAILABLE, target_element),
    )
    bad_control = _control_spec(automation_id="")
    status = None

    def call():
        nonlocal status
        status = desktop_safety.resolve_control_status(_target_spec(), bad_control)

    _assert_zero_uia_calls(monkeypatch, call)
    assert status is DesktopStatus.CHECK_FAILED


def test_oversized_control_automation_id_never_reaches_find_elements(monkeypatch):
    target_element = _FakeElement(handle=1, process_id=111)
    monkeypatch.setattr(
        desktop_safety,
        "_resolve_target_internal",
        lambda spec: (DesktopStatus.AVAILABLE, target_element),
    )
    bad_control = _control_spec(automation_id="a" * (desktop_safety.MAX_AUTOMATION_ID_LENGTH + 1))
    status = None

    def call():
        nonlocal status
        status = desktop_safety.resolve_control_status(_target_spec(), bad_control)

    _assert_zero_uia_calls(monkeypatch, call)
    assert status is DesktopStatus.CHECK_FAILED


def test_unsupported_control_type_never_reaches_find_elements(monkeypatch):
    target_element = _FakeElement(handle=1, process_id=111)
    monkeypatch.setattr(
        desktop_safety,
        "_resolve_target_internal",
        lambda spec: (DesktopStatus.AVAILABLE, target_element),
    )
    bad_control = _control_spec(control_type="NotARealType")
    status = None

    def call():
        nonlocal status
        status = desktop_safety.resolve_control_status(_target_spec(), bad_control)

    _assert_zero_uia_calls(monkeypatch, call)
    assert status is DesktopStatus.CHECK_FAILED


def test_oversized_control_class_name_never_reaches_find_elements(monkeypatch):
    target_element = _FakeElement(handle=1, process_id=111)
    monkeypatch.setattr(
        desktop_safety,
        "_resolve_target_internal",
        lambda spec: (DesktopStatus.AVAILABLE, target_element),
    )
    bad_control = _control_spec(class_name="a" * (desktop_safety.MAX_CONTROL_CLASS_NAME_LENGTH + 1))
    status = None

    def call():
        nonlocal status
        status = desktop_safety.resolve_control_status(_target_spec(), bad_control)

    _assert_zero_uia_calls(monkeypatch, call)
    assert status is DesktopStatus.CHECK_FAILED


# --- Executable-identity failure semantics (section 11) -----------------------


def test_compare_identity_match(tmp_path):
    exe = tmp_path / "one.exe"
    exe.write_bytes(b"x")
    assert desktop_safety.compare_windows_executable_identity(
        str(exe), str(exe)
    ) is desktop_safety.IdentityComparison.MATCH


def test_compare_identity_no_match(tmp_path):
    a = tmp_path / "a.exe"
    b = tmp_path / "b.exe"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    assert desktop_safety.compare_windows_executable_identity(
        str(a), str(b)
    ) is desktop_safety.IdentityComparison.NO_MATCH


def test_compare_identity_check_failed_for_missing_file(tmp_path):
    exe = tmp_path / "exists.exe"
    exe.write_bytes(b"x")
    missing = tmp_path / "missing.exe"
    assert desktop_safety.compare_windows_executable_identity(
        str(exe), str(missing)
    ) is desktop_safety.IdentityComparison.CHECK_FAILED


def test_same_windows_executable_collapses_check_failed_to_false(tmp_path):
    exe = tmp_path / "exists.exe"
    exe.write_bytes(b"x")
    missing = tmp_path / "missing.exe"
    assert desktop_safety.same_windows_executable(str(exe), str(missing)) is False
