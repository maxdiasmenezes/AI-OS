"""Tests for kernel/tools/handlers/list_processes.py: a bounded, read-only
process snapshot (Milestone 43 P1; output-size-contract correction).
Every test injects a fake psutil.process_iter() - never enumerates or
touches real processes on the machine running these tests."""

from datetime import datetime, timezone

import psutil
import pytest

from kernel.employee_tasks import MAX_STEP_RESULT_JSON_CHARS
from kernel.task_execution.observation import build_action_observation, serialize_observation
from kernel.tools.handlers import list_processes
from kernel.tools.types import ActionRequest


class _FakeProcess:
    """A minimal stand-in for psutil.Process exposing only .pid, .name(),
    and .status() - deliberately has NO .cmdline()/.environ()/.cwd()/
    .open_files()/.connections() methods at all, so if list_processes.py
    ever called any of those, it would raise AttributeError (uncaught by
    the handler's narrow except clause) rather than silently succeeding -
    proving those are never requested."""

    def __init__(self, pid, name, status="running"):
        self.pid = pid
        self._name = name
        self._status = status

    def name(self):
        if isinstance(self._name, Exception):
            raise self._name
        return self._name

    def status(self):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


def _request(resource_key=None):
    return ActionRequest(action="list_processes", resource_key=resource_key)


def _patch(monkeypatch, processes):
    monkeypatch.setattr(list_processes.psutil, "process_iter", lambda: iter(processes))


def _serialize(result):
    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())
    return serialize_observation(observation)


def test_safe_fields_only_pid_name_status(monkeypatch):
    _patch(monkeypatch, [_FakeProcess(100, "explorer.exe"), _FakeProcess(4, "notepad.exe")])

    result = list_processes.run(_request(), None)

    assert result.success is True
    assert "explorer.exe" in result.message
    assert "notepad.exe" in result.message
    assert "(running)" in result.message


def test_deterministic_ordering_by_normalized_name_then_pid(monkeypatch):
    _patch(
        monkeypatch,
        [
            _FakeProcess(300, "Zeta.exe"),
            _FakeProcess(100, "alpha.exe"),
            _FakeProcess(50, "alpha.exe"),
            _FakeProcess(200, "beta.exe"),
        ],
    )

    result = list_processes.run(_request(), None)

    lines = result.message.splitlines()[1:]
    pids_in_order = [int(line.split(" ", 1)[0]) for line in lines]
    assert pids_in_order == [50, 100, 200, 300]  # alpha/50, alpha/100, beta/200, Zeta/300


def test_ordering_is_stable_across_repeated_calls(monkeypatch):
    processes = [_FakeProcess(300, "Zeta.exe"), _FakeProcess(100, "alpha.exe")]
    _patch(monkeypatch, processes)

    first = list_processes.run(_request(), None)
    second = list_processes.run(_request(), None)

    assert first.message == second.message


def test_maximum_count_ceiling_still_applies_for_short_names(monkeypatch):
    processes = [_FakeProcess(i, f"proc_{i:04d}.exe") for i in range(150)]
    _patch(monkeypatch, processes)

    result = list_processes.run(_request(), None)

    lines = result.message.splitlines()
    header, entries = lines[0], lines[1:]
    assert len(entries) == list_processes.MAX_PROCESSES
    assert "showing 100 of 150" in header


def test_disappearing_process_is_skipped_without_raising(monkeypatch):
    _patch(
        monkeypatch,
        [
            _FakeProcess(1, "survivor.exe"),
            _FakeProcess(2, psutil.NoSuchProcess(2)),
        ],
    )

    result = list_processes.run(_request(), None)

    assert result.success is True
    assert "survivor.exe" in result.message
    assert len(result.message.splitlines()) == 2  # header + one entry


def test_access_denied_process_is_skipped_without_raising(monkeypatch):
    _patch(
        monkeypatch,
        [
            _FakeProcess(1, "survivor.exe"),
            _FakeProcess(2, psutil.AccessDenied(2)),
        ],
    )

    result = list_processes.run(_request(), None)

    assert result.success is True
    assert "survivor.exe" in result.message
    assert len(result.message.splitlines()) == 2  # header + one entry


def test_zombie_process_is_skipped_without_raising(monkeypatch):
    _patch(
        monkeypatch,
        [
            _FakeProcess(1, "survivor.exe"),
            _FakeProcess(2, psutil.ZombieProcess(2)),
        ],
    )

    result = list_processes.run(_request(), None)

    assert result.success is True
    assert "survivor.exe" in result.message


def test_missing_or_empty_process_name_is_skipped(monkeypatch):
    _patch(
        monkeypatch,
        [
            _FakeProcess(1, "survivor.exe"),
            _FakeProcess(2, None),
            _FakeProcess(3, ""),
            _FakeProcess(4, "   "),
        ],
    )

    result = list_processes.run(_request(), None)

    assert result.success is True
    assert len(result.message.splitlines()) == 2  # header + survivor.exe only


def test_missing_status_falls_back_to_unknown_rather_than_dropping_the_process(monkeypatch):
    _patch(monkeypatch, [_FakeProcess(1, "survivor.exe", status="")])

    result = list_processes.run(_request(), None)

    assert result.success is True
    assert "(unknown)" in result.message


def test_empty_snapshot_reports_no_processes_found(monkeypatch):
    _patch(monkeypatch, [])

    result = list_processes.run(_request(), None)

    assert result.success is True
    assert "No processes found." in result.message


def test_never_calls_cmdline_environ_cwd_open_files_or_connections(monkeypatch):
    # _FakeProcess deliberately has none of these methods - if
    # list_processes.py ever called one, this test would fail with an
    # AttributeError bubbling out of run(), never a passing result.
    _patch(monkeypatch, [_FakeProcess(1, "proc.exe")])

    result = list_processes.run(_request(), None)

    assert result.success is True


# --- resource_key contract (Milestone 43 P1 correction) --------------------


def test_unexpected_resource_key_is_rejected_by_the_handler_itself(monkeypatch):
    _patch(monkeypatch, [_FakeProcess(1, "proc.exe")])

    result = list_processes.run(_request("some_key"), None)

    assert result.success is False
    assert result.outcome == "rejected"


def test_none_resource_key_is_accepted(monkeypatch):
    _patch(monkeypatch, [_FakeProcess(1, "proc.exe")])

    result = list_processes.run(_request(None), None)

    assert result.success is True


# --- name validation (Milestone 43 P1 correction) ---------------------------


def test_name_exceeding_the_length_bound_is_skipped_not_truncated(monkeypatch):
    too_long = "y" * (list_processes.MAX_PROCESS_NAME_CHARS + 1)
    _patch(monkeypatch, [_FakeProcess(1, too_long), _FakeProcess(2, "short.exe")])

    result = list_processes.run(_request(), None)

    assert too_long not in result.message
    assert too_long[:50] not in result.message  # never a truncated fragment either
    assert "short.exe" in result.message


def test_name_at_exactly_the_length_bound_is_shown(monkeypatch):
    exactly = "z" * list_processes.MAX_PROCESS_NAME_CHARS
    _patch(monkeypatch, [_FakeProcess(1, exactly)])

    result = list_processes.run(_request(), None)

    assert exactly in result.message


@pytest.mark.parametrize(
    "bad_name",
    [
        'evil"name.exe',
        "evil\\name.exe",
        "evil\x01name.exe",
        "evil\x7fname.exe",
        "evil\nname.exe",
    ],
)
def test_name_with_disallowed_character_is_skipped(monkeypatch, bad_name):
    _patch(monkeypatch, [_FakeProcess(1, bad_name), _FakeProcess(2, "normal.exe")])

    result = list_processes.run(_request(), None)

    assert bad_name not in result.message
    assert "normal.exe" in result.message
    assert len(result.message.splitlines()) == 2  # header + normal.exe only


# --- status validation (Milestone 43 P3 pre-push review correction,
#     Finding B) ------------------------------------------------------------


@pytest.mark.parametrize(
    "real_status",
    ["running", "sleeping", "disk-sleep", "stopped", "tracing-stop", "zombie", "dead", "idle"],
)
def test_realistic_psutil_status_values_are_shown_verbatim(monkeypatch, real_status):
    _patch(monkeypatch, [_FakeProcess(1, "proc.exe", status=real_status)])

    result = list_processes.run(_request(), None)

    assert result.success is True
    assert f"({real_status})" in result.message


def test_status_exceeding_the_length_bound_skips_the_whole_entry_not_just_the_status(monkeypatch):
    too_long_status = "s" * (list_processes.MAX_PROCESS_STATUS_CHARS + 1)
    _patch(
        monkeypatch,
        [_FakeProcess(1, "oversized_status.exe", status=too_long_status), _FakeProcess(2, "normal.exe")],
    )

    result = list_processes.run(_request(), None)

    assert "oversized_status.exe" not in result.message
    assert too_long_status not in result.message
    assert "normal.exe" in result.message
    assert len(result.message.splitlines()) == 2  # header + normal.exe only


def test_status_at_exactly_the_length_bound_is_shown(monkeypatch):
    exactly = "s" * list_processes.MAX_PROCESS_STATUS_CHARS
    _patch(monkeypatch, [_FakeProcess(1, "proc.exe", status=exactly)])

    result = list_processes.run(_request(), None)

    assert f"({exactly})" in result.message


@pytest.mark.parametrize(
    "bad_status",
    [
        'evil"status',
        "evil\\status",
        "evil\x01status",
        "evil\x7fstatus",
        "evil\nstatus",
    ],
)
def test_status_with_disallowed_character_skips_the_whole_entry(monkeypatch, bad_status):
    # A quote/backslash/control-character status is never realistic (psutil
    # only ever reports one of its own fixed STATUS_* constants) - proven
    # here as a structural guarantee anyway, exactly matching the name
    # policy above, rather than merely trusting psutil's current contract.
    _patch(
        monkeypatch,
        [_FakeProcess(1, "weird_status.exe", status=bad_status), _FakeProcess(2, "normal.exe")],
    )

    result = list_processes.run(_request(), None)

    assert "weird_status.exe" not in result.message
    assert bad_status not in result.message
    assert "normal.exe" in result.message
    assert len(result.message.splitlines()) == 2  # header + normal.exe only


def test_status_hardening_cannot_be_used_to_construct_an_oversized_successful_observation(
    monkeypatch,
):
    """The concrete concern Finding B raised: a pathological status value
    must never let a successful ActionResult overflow the real M42
    persistence bound. MAX_PROCESSES entries, each carrying a status at
    exactly the length bound, must still serialize within it."""

    processes = [
        _FakeProcess(i, f"proc_{i:04d}.exe", status="s" * list_processes.MAX_PROCESS_STATUS_CHARS)
        for i in range(list_processes.MAX_PROCESSES)
    ]
    _patch(monkeypatch, processes)

    result = list_processes.run(_request(), None)
    assert result.success is True

    serialized = _serialize(result)
    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS


# --- total output-size contract (Milestone 43 P1 correction) ---------------


def test_result_remains_bounded_even_with_many_processes(monkeypatch):
    processes = [_FakeProcess(i, f"proc_{i:05d}.exe") for i in range(5000)]
    _patch(monkeypatch, processes)

    result = list_processes.run(_request(), None)

    assert len(result.message) <= list_processes.MAX_RESULT_CHARS


def test_many_max_length_names_are_cut_off_deterministically_before_the_size_budget_is_exceeded(
    monkeypatch,
):
    # 100 processes, each at the per-name length ceiling - together they
    # would exceed MAX_RESULT_CHARS if all were shown, so the handler must
    # stop adding entries before the budget is exceeded, never emit a
    # message that itself exceeds MAX_RESULT_CHARS, and never silently
    # drop an entry from the MIDDLE of the deterministic order.
    processes = [
        _FakeProcess(i, f"{'x' * (list_processes.MAX_PROCESS_NAME_CHARS - 4)}_{i:03d}")
        for i in range(list_processes.MAX_PROCESSES)
    ]
    _patch(monkeypatch, processes)

    result = list_processes.run(_request(), None)

    assert len(result.message) <= list_processes.MAX_RESULT_CHARS
    lines = result.message.splitlines()
    header, entries = lines[0], lines[1:]
    assert len(entries) < list_processes.MAX_PROCESSES  # the size budget, not the count, stopped it
    assert f"showing {len(entries)} of {list_processes.MAX_PROCESSES}" in header

    # The shown entries are exactly the deterministic PREFIX of the sorted
    # order - never an arbitrary subset.
    shown_pids = [int(line.split(" ", 1)[0]) for line in entries]
    assert shown_pids == sorted(shown_pids)


def test_header_reserve_holds_even_for_an_extreme_total_count(monkeypatch):
    # A pathological fake total far beyond anything a real process table
    # could produce - proves the fixed header reserve is large enough
    # regardless of how many digits the "of TOTAL" note needs.
    processes = [_FakeProcess(i, "a") for i in range(2_000_000)]
    _patch(monkeypatch, processes)

    result = list_processes.run(_request(), None)

    assert len(result.message) <= list_processes.MAX_RESULT_CHARS
    assert "showing 100 of 2000000" in result.message.splitlines()[0]


# --- authoritative serialization proofs (Milestone 43 P1 correction) -------


def test_worst_case_disallowed_content_would_have_overflowed_the_observation_bound_before_this_fix():
    """Direct proof of the bug this correction fixes: 100 processes with
    long, JSON-escape-heavy names, run through the REAL StepObservation
    pipeline with none of this correction's name/size validation applied,
    exceed MAX_STEP_RESULT_JSON_CHARS. This is why list_processes.py must
    bound and validate its own output rather than relying on downstream
    serialization failure - see kernel/task_execution/service.py's
    _finalize_action_step(), which does NOT catch
    ObservationSerializationError the way the RESPOND path does."""

    from kernel.task_execution.observation import ObservationSerializationError
    from kernel.tools.types import ActionResult

    long_name = "a_very_long_windows_service_or_executable_name_example_" * 2
    lines = [f"{i} {long_name}_{i:04d}.exe (running)" for i in range(100)]
    message = "\n".join(["100 process(es)", *lines])
    result = ActionResult(True, message, "executed")
    observation = build_action_observation(1, result, datetime.now(timezone.utc).isoformat())

    with pytest.raises(ObservationSerializationError):
        serialize_observation(observation)


def test_worst_case_permitted_result_serializes_within_the_real_observation_bound(monkeypatch):
    """The authoritative proof required for this correction: the largest
    result list_processes.py can actually produce on success (MAX_PROCESSES
    entries at MAX_PROCESS_NAME_CHARS each, the exact scenario that
    previously overflowed) survives the REAL build_action_observation()/
    serialize_observation() pipeline within MAX_STEP_RESULT_JSON_CHARS -
    not an estimated overhead formula."""

    processes = [
        _FakeProcess(i, f"{'x' * (list_processes.MAX_PROCESS_NAME_CHARS - 4)}_{i:03d}")
        for i in range(list_processes.MAX_PROCESSES)
    ]
    _patch(monkeypatch, processes)

    result = list_processes.run(_request(), None)
    assert result.success is True

    serialized = _serialize(result)
    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS


def test_worst_case_short_realistic_names_at_max_processes_also_serializes_within_bound(monkeypatch):
    processes = [_FakeProcess(i, f"proc_{i:04d}.exe") for i in range(list_processes.MAX_PROCESSES)]
    _patch(monkeypatch, processes)

    result = list_processes.run(_request(), None)
    assert result.success is True
    assert len(result.message.splitlines()) - 1 == list_processes.MAX_PROCESSES

    serialized = _serialize(result)
    assert len(serialized) <= MAX_STEP_RESULT_JSON_CHARS
