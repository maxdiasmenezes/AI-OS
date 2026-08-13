"""Milestone 45 (Windows Desktop Worker) whole-milestone acceptance and
security-closure tests for kernel/tools/desktop_safety.py,
kernel/tools/handlers/desktop_target_status.py,
kernel/tools/handlers/desktop_control_status.py, kernel/tools/config.py,
kernel/tools/registry.py, and kernel/task_planner/catalog.py.

This module does NOT re-prove what tests/kernel/tools/test_desktop_safety.py,
test_desktop_config.py, test_desktop_resolver_corrections.py,
test_desktop_no_mutation_static_acceptance.py,
tests/kernel/tools/handlers/test_desktop_target_status.py,
test_desktop_control_status.py, test_desktop_windows_integration.py, and
tests/kernel/task_execution/test_milestone_45_p1_e2e.py already cover in
detail (per-candidate classification edge cases, live-fixture cardinality
proofs, the full failure-taxonomy matrix, the positive/negative API-surface
sweeps, the real M42 pipeline). It instead proves the small number of
WHOLE-MILESTONE properties no single implementation-level file was ever
positioned to prove on its own: the final registry shape, the exact-two-
desktop-action surface, the config authority shape, the launch-path-versus-
runtime-identity separation as one consolidated contract, the final status
taxonomy, the read-only/no-reconnaissance production surface, the M42/M46+
boundary, and a positive regression documenting that Milestone 45
deliberately closes WITHOUT a mutation phase (P2) - see
docs/architecture.md's Milestone 45 entry for the full reasoning: UI
Automation InvokePattern was empirically evaluated against the validation
fixture and found to change foreground state without reliably producing the
intended application effect, so native desktop mutation was rejected from
this milestone's accepted scope, not merely "not yet built." Any future
native desktop mutation capability requires its own new, explicit security
design."""

import ast
import inspect
import textwrap
from dataclasses import fields
from pathlib import Path

import pytest


def _code_identifiers(obj) -> set[str]:
    """AST-based (never raw substring) collection of every Name/Attribute
    identifier actually used in `obj`'s CODE - deliberately excludes
    docstrings/comments, which legitimately discuss rejected APIs and
    design history by name in prose (e.g. desktop_safety.py's own module
    docstring explains why `best_match`/`venv`/`approved_applications` are
    NOT used, which would otherwise false-positive a raw substring
    search - the exact pitfall test_desktop_no_mutation_static_acceptance.py
    already avoids the same way)."""

    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Import):
            identifiers.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            identifiers.add(node.module.split(".")[0])
    return identifiers

from kernel.task_execution.observation import StepObservation
from kernel.task_planner import PlanStep
from kernel.tools import desktop_safety
from kernel.tools.config import (
    DesktopControlSpec,
    DesktopTargetSpec,
    ToolsConfig,
    load_tools_config,
)
from kernel.tools.desktop_safety import DesktopStatus
from kernel.tools.handlers import desktop_control_status, desktop_target_status
from kernel.tools.registry import ActionRegistry, ResourceKeyRequirement

# ============================================================================
# 1. Final registry action set: exactly TWO desktop actions, non-sensitive
# ============================================================================


def test_registry_contains_exactly_fourteen_actions():
    registry = ActionRegistry()
    assert len(registry.descriptors()) == 14


def test_registry_contains_both_desktop_actions():
    registry = ActionRegistry()
    names = {d.name for d in registry.descriptors()}
    assert {"desktop_target_status", "desktop_control_status"} <= names


def test_desktop_actions_final_sensitivity_and_resource_key_contract():
    registry = ActionRegistry()
    by_name = {d.name: d for d in registry.descriptors()}

    for name in ("desktop_target_status", "desktop_control_status"):
        assert by_name[name].sensitive is False
        assert by_name[name].resource_key_requirement == ResourceKeyRequirement.REQUIRED


def test_final_sensitive_action_set_is_exactly_the_five_m43_write_actions():
    registry = ActionRegistry()
    sensitive_names = {d.name for d in registry.descriptors() if d.sensitive}

    assert sensitive_names == {
        "open_application",
        "run_registered_script",
        "repository_backup",
        "create_directory",
        "copy_file",
    }
    assert "desktop_target_status" not in sensitive_names
    assert "desktop_control_status" not in sensitive_names


def test_final_registry_action_set_is_exactly_fourteen_named_actions():
    """Positive closure regression: the complete registry, by name - fails
    loudly if a future change ever quietly adds or removes an action
    without this file being updated deliberately."""

    registry = ActionRegistry()
    action_names = {d.name for d in registry.descriptors()}

    assert action_names == {
        "system_status",
        "list_files",
        "open_application",
        "run_registered_script",
        "repo_health",
        "repository_backup",
        "file_metadata",
        "read_text_file",
        "list_processes",
        "create_directory",
        "copy_file",
        "browser_read_page",
        "desktop_target_status",
        "desktop_control_status",
    }


# ============================================================================
# 2. Exact desktop action surface: no reconnaissance/mutation capability
# ============================================================================


def test_exactly_two_desktop_actions_exist_no_reconnaissance_or_mutation_surface():
    registry = ActionRegistry()
    action_names = {d.name for d in registry.descriptors()}

    desktop_actions = {name for name in action_names if name.startswith("desktop_")}
    assert desktop_actions == {"desktop_target_status", "desktop_control_status"}

    forbidden_names = (
        "desktop_inspect_controls",
        "desktop_list_windows",
        "desktop_read_window",
        "desktop_window_metadata",
        "desktop_invoke_control",
        "desktop_click",
        "desktop_type_text",
        "desktop_hotkey",
        "desktop_set_control_value",
        "desktop_focus_window",
        "desktop_close_window",
        "desktop_screenshot",
        "desktop_capture_window",
        "desktop_capture_screen",
    )
    for forbidden in forbidden_names:
        assert forbidden not in action_names


def test_no_desktop_mutation_or_generic_reconnaissance_shaped_action_name():
    registry = ActionRegistry()
    action_names = {d.name for d in registry.descriptors()}

    forbidden_substrings = (
        "invoke",
        "click",
        "type",
        "hotkey",
        "set_value",
        "set_control",
        "focus",
        "close_window",
        "screenshot",
        "capture",
        "list_windows",
        "inspect",
        "read_window",
        "window_metadata",
        "clipboard",
        "mouse",
        "keyboard",
    )
    for name in action_names:
        if not name.startswith("desktop_"):
            continue
        for forbidden in forbidden_substrings:
            assert forbidden not in name, f"unexpected capability-shaped desktop action: {name}"


# ============================================================================
# 3. Config authority acceptance: target/control spec shape
# ============================================================================


def test_desktop_target_spec_has_exactly_the_final_authorized_fields():
    field_names = {f.name for f in fields(DesktopTargetSpec)}
    assert field_names == {
        "application_key",
        "process_executable",
        "window_class_name",
        "window_automation_id",
    }

    forbidden_substrings = (
        "title",
        "text",
        "pid",
        "hwnd",
        "coord",
        "keyboard",
        "mouse",
        "screenshot",
        "clipboard",
        "selector",
    )
    for field_name in field_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in field_name.lower()


def test_desktop_control_spec_has_exactly_the_final_authorized_fields():
    field_names = {f.name for f in fields(DesktopControlSpec)}
    assert field_names == {
        "target_key",
        "control_automation_id",
        "control_type",
        "control_class_name",
    }

    # "control_class_name" legitimately contains the substring "name" - the
    # exact-field-set assertion above already proves no separate Name/text
    # field exists, so this list only guards genuinely distinct, narrower
    # shapes it wouldn't otherwise catch.
    forbidden_substrings = (
        "title",
        "pid",
        "hwnd",
        "coord",
        "keyboard",
        "mouse",
        "screenshot",
        "clipboard",
        "value",
        "verb",
        "action",
    )
    for field_name in field_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in field_name.lower()


def test_tools_config_desktop_fields_are_exactly_two_flat_top_level_sections():
    field_names = {f.name for f in fields(ToolsConfig)}
    desktop_related = {name for name in field_names if "desktop" in name}
    assert desktop_related == {"approved_desktop_targets", "approved_desktop_controls"}


def test_tools_example_yaml_desktop_shape_matches_the_final_contract():
    example_path = Path(__file__).resolve().parents[3] / "kernel" / "config" / "tools.example.yaml"
    config = load_tools_config(example_path)

    assert config.approved_desktop_targets
    for spec in config.approved_desktop_targets.values():
        assert isinstance(spec, DesktopTargetSpec)

    assert config.approved_desktop_controls
    for spec in config.approved_desktop_controls.values():
        assert isinstance(spec, DesktopControlSpec)
        assert spec.control_automation_id


# ============================================================================
# 4. Launch path vs runtime process identity: the load-bearing separation
# ============================================================================


def test_application_key_and_process_executable_are_separate_required_fields():
    """Structural proof the launch-authority reference
    (application_key -> approved_applications) can never silently
    substitute for the independently-required runtime process-image
    identity (process_executable) - both fields exist, neither is derived
    from the other anywhere in the dataclass itself."""

    field_names = {f.name for f in fields(DesktopTargetSpec)}
    assert "application_key" in field_names
    assert "process_executable" in field_names
    assert field_names - {"application_key", "process_executable"} == {
        "window_class_name",
        "window_automation_id",
    }


def test_resolution_never_reads_approved_applications_executable_as_identity():
    """desktop_safety.py legitimately reads tools_config.approved_applications
    ONCE - target_application_reference_valid()'s `application_key in
    tools_config.approved_applications` membership check, proving the
    LAUNCH reference still exists (referential integrity, not identity).
    What must never happen is reading `.executable` off an
    ApplicationSpec pulled from that mapping - that would be the concrete
    mechanism by which a launch path could leak into runtime process
    identity, which is process_executable's job alone. AST-based (see
    _code_identifiers()) so the module docstring's own prose explaining
    this separation never false-positives the check."""

    identifiers = _code_identifiers(desktop_safety)
    assert "approved_applications" in identifiers  # the one legitimate membership check
    assert "executable" not in identifiers  # ApplicationSpec.executable never read here


def test_no_python_specific_launcher_exception_in_production_resolution():
    """The venv-launcher/base-interpreter distinction (proven empirically
    during M45's design validation) is handled entirely by test fixtures'
    own launch choice (sys._base_executable) - production resolution code
    must contain no special-cased Python/venv/interpreter handling: no
    `sys.executable`/`sys._base_executable` reference anywhere (AST-based,
    so the module docstring's own prose explaining this distinction never
    false-positives the check)."""

    tree = ast.parse(textwrap.dedent(inspect.getsource(desktop_safety)))
    sys_attribute_accesses = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    }
    assert "executable" not in sys_attribute_accesses
    assert "_base_executable" not in sys_attribute_accesses


# ============================================================================
# 5. Windows executable identity acceptance
# ============================================================================


def test_file_identity_uses_metadata_only_handle_access():
    source = inspect.getsource(desktop_safety._file_identity_strict)
    assert "CreateFile" in source
    assert "GetFileInformationByHandle" in source
    # Desired access is 0 (metadata query only) - never GENERIC_READ/WRITE.
    assert "GENERIC_READ" not in source
    assert "GENERIC_WRITE" not in source
    assert "ReadFile" not in source
    assert "WriteFile" not in source


def test_no_subprocess_content_hash_or_executable_launch_in_desktop_safety():
    """AST-based (see _code_identifiers()) - the module's own docstring
    prose mentions "Popen/CreateProcess" while explaining why
    process_executable is never itself launched, which a raw substring
    search would false-positive on."""

    identifiers = _code_identifiers(desktop_safety)
    for forbidden in ("subprocess", "Popen", "hashlib", "system"):
        assert forbidden not in identifiers


def test_identity_comparison_distinguishes_no_match_from_check_failed():
    assert {c.value for c in desktop_safety.IdentityComparison} == {
        "match",
        "no_match",
        "check_failed",
    }


def test_inspection_failure_is_not_collapsed_into_no_match(tmp_path):
    exe = tmp_path / "exists.exe"
    exe.write_bytes(b"x")
    missing = tmp_path / "missing.exe"

    result = desktop_safety.compare_windows_executable_identity(str(exe), str(missing))
    assert result is desktop_safety.IdentityComparison.CHECK_FAILED
    assert result is not desktop_safety.IdentityComparison.NO_MATCH


# ============================================================================
# 6. Complete-authority cardinality: structural + one live confirmation
# ============================================================================


def test_target_resolution_evaluates_every_raw_candidate_before_cardinality():
    """Structural proof of the corrected ordering (full behavioral coverage
    - wrong-process collision, minimized collision, multi-qualified
    ambiguity, inspection-failure poisoning - lives in
    test_desktop_resolver_corrections.py and is not re-proven here): the
    candidate loop in _resolve_target_internal() iterates ALL raw
    candidates (no early break/return inside the loop body) before
    cardinality is computed."""

    source = inspect.getsource(desktop_safety._resolve_target_internal)
    assert "for element in raw_candidates:" in source
    # No early exit inside the per-candidate loop - the loop body only
    # ever appends to qualified_elements or sets a flag, never returns.
    loop_body = source.split("for element in raw_candidates:", 1)[1].split("\n\n", 1)[0]
    assert "return" not in loop_body


def test_candidate_outcome_model_has_exactly_three_values():
    assert {o.value for o in desktop_safety._CandidateOutcome} == {
        "qualified",
        "non_qualifying",
        "inspection_failed",
    }


def test_wrong_process_and_minimized_do_not_create_ambiguity_live_confirmation(monkeypatch):
    """One direct, live confirmation (not a full re-proof) that the fixed
    algorithm is actually wired end to end through the public API."""

    from unittest.mock import MagicMock

    class _FakeElement:
        def __init__(self, handle, process_id):
            self.handle = handle
            self.process_id = process_id

    correct = _FakeElement(handle=1, process_id=111)
    wrong_process = _FakeElement(handle=2, process_id=222)

    exe_correct = __import__("sys").executable
    exe_wrong = r"C:\Windows\System32\notepad.exe"

    spec = DesktopTargetSpec(
        application_key="fixture_app",
        process_executable=exe_correct,
        window_class_name="TkTopLevel",
    )

    def process_factory(pid):
        m = MagicMock()
        m.exe.return_value = exe_correct if pid == 111 else exe_wrong
        return m

    monkeypatch.setattr(desktop_safety, "WINDOWS", True)
    monkeypatch.setattr(
        desktop_safety,
        "findwindows",
        MagicMock(find_elements=lambda **k: [correct, wrong_process]),
    )
    monkeypatch.setattr(desktop_safety, "win32gui", MagicMock(IsIconic=lambda h: False))
    monkeypatch.setattr(desktop_safety.psutil, "Process", process_factory)

    status = desktop_safety.resolve_target_status(spec)
    assert status is DesktopStatus.AVAILABLE


# ============================================================================
# 7. Final status taxonomy
# ============================================================================


def test_desktop_status_has_exactly_the_five_accepted_states():
    assert {s.value for s in DesktopStatus} == {
        "available",
        "unavailable",
        "ambiguous",
        "check_failed",
        "automation_unavailable",
    }


def test_non_windows_platform_is_automation_unavailable_not_ordinary_unavailable(monkeypatch):
    monkeypatch.setattr(desktop_safety, "WINDOWS", False)
    spec = DesktopTargetSpec(
        application_key="fixture_app",
        process_executable=r"C:\x.exe",
        window_class_name="TkTopLevel",
    )
    status = desktop_safety.resolve_target_status(spec)
    assert status is DesktopStatus.AUTOMATION_UNAVAILABLE


# ============================================================================
# 8. Target/control handler semantics
# ============================================================================


def _target_config(spec):
    from kernel.tools.config import ApplicationSpec

    return ToolsConfig(
        approved_directories={},
        approved_applications={"fixture_app": ApplicationSpec(executable=r"C:\x.exe", cwd="C:/")},
        approved_scripts={},
        approved_desktop_targets={"fixture_window": spec},
    )


@pytest.mark.parametrize(
    "status,expected_success,expected_outcome,expected_message",
    [
        (DesktopStatus.AVAILABLE, True, "executed", "Target 'fixture_window' is available."),
        (DesktopStatus.UNAVAILABLE, True, "executed", "Target 'fixture_window' is unavailable."),
        (DesktopStatus.AMBIGUOUS, True, "executed", "Target 'fixture_window' is ambiguous."),
        (
            DesktopStatus.CHECK_FAILED,
            False,
            "failed",
            "That desktop target could not be checked.",
        ),
        (
            DesktopStatus.AUTOMATION_UNAVAILABLE,
            False,
            "failed",
            "Desktop automation is unavailable.",
        ),
    ],
)
def test_target_handler_final_semantic_matrix(
    monkeypatch, status, expected_success, expected_outcome, expected_message
):
    from kernel.tools.types import ActionRequest

    spec = DesktopTargetSpec(
        application_key="fixture_app",
        process_executable=r"C:\x.exe",
        window_class_name="TkTopLevel",
    )
    monkeypatch.setattr(desktop_target_status, "resolve_target_status", lambda *a, **k: status)

    result = desktop_target_status.run(
        ActionRequest(action="desktop_target_status", resource_key="fixture_window"),
        _target_config(spec),
    )

    assert result.success is expected_success
    assert result.outcome == expected_outcome
    assert result.message == expected_message


def test_unregistered_target_is_rejected_not_failed():
    from kernel.tools.types import ActionRequest

    result = desktop_target_status.run(
        ActionRequest(action="desktop_target_status", resource_key="nope"),
        _target_config(
            DesktopTargetSpec(
                application_key="fixture_app",
                process_executable=r"C:\x.exe",
                window_class_name="TkTopLevel",
            )
        ),
    )
    assert result.success is False
    assert result.outcome == "rejected"


# ============================================================================
# 9. Control status propagation from target status
# ============================================================================


def _control_config(target_spec, control_spec):
    from kernel.tools.config import ApplicationSpec

    return ToolsConfig(
        approved_directories={},
        approved_applications={"fixture_app": ApplicationSpec(executable=r"C:\x.exe", cwd="C:/")},
        approved_scripts={},
        approved_desktop_targets={"fixture_window": target_spec},
        approved_desktop_controls={"fixture_refresh": control_spec},
    )


@pytest.mark.parametrize(
    "target_status",
    [
        DesktopStatus.UNAVAILABLE,
        DesktopStatus.AMBIGUOUS,
        DesktopStatus.CHECK_FAILED,
        DesktopStatus.AUTOMATION_UNAVAILABLE,
    ],
)
def test_control_status_propagates_non_available_target_status_unchanged(monkeypatch, target_status):
    monkeypatch.setattr(
        desktop_safety, "_resolve_target_internal", lambda spec: (target_status, None)
    )
    control_spec = DesktopControlSpec(
        target_key="fixture_window", control_automation_id="5001", control_type="Button"
    )
    target_spec = DesktopTargetSpec(
        application_key="fixture_app",
        process_executable=r"C:\x.exe",
        window_class_name="TkTopLevel",
    )

    status = desktop_safety.resolve_control_status(target_spec, control_spec)
    assert status is target_status


@pytest.mark.parametrize(
    "match_count,expected",
    [(0, DesktopStatus.UNAVAILABLE), (1, DesktopStatus.AVAILABLE), (2, DesktopStatus.AMBIGUOUS)],
)
def test_control_cardinality_within_an_available_target(monkeypatch, match_count, expected):
    from unittest.mock import MagicMock

    class _FakeElement:
        def __init__(self):
            self.handle = 1
            self.process_id = 111

    target_element = _FakeElement()
    monkeypatch.setattr(
        desktop_safety,
        "_resolve_target_internal",
        lambda spec: (DesktopStatus.AVAILABLE, target_element),
    )
    monkeypatch.setattr(desktop_safety, "WINDOWS", True)
    monkeypatch.setattr(
        desktop_safety,
        "findwindows",
        MagicMock(find_elements=lambda **k: [object()] * match_count),
    )

    control_spec = DesktopControlSpec(
        target_key="fixture_window", control_automation_id="5001", control_type="Button"
    )
    target_spec = DesktopTargetSpec(
        application_key="fixture_app",
        process_executable=r"C:\x.exe",
        window_class_name="TkTopLevel",
    )

    status = desktop_safety.resolve_control_status(target_spec, control_spec)
    assert status is expected


def test_control_search_is_scoped_to_the_resolved_target_never_global():
    source = inspect.getsource(desktop_safety.resolve_control_status)
    assert '"parent": target_element' in source
    assert '"top_level_only": False' in source


# ============================================================================
# 10. Runtime defense-in-depth: manually-built specs fail before UIA
# ============================================================================


def test_invalid_manual_target_spec_never_reaches_find_elements(monkeypatch):
    from unittest.mock import MagicMock

    spy = MagicMock(side_effect=AssertionError("find_elements must never be called"))
    monkeypatch.setattr(desktop_safety, "WINDOWS", True)
    monkeypatch.setattr(desktop_safety, "findwindows", MagicMock(find_elements=spy))

    bad_spec = DesktopTargetSpec(
        application_key="fixture_app", process_executable=r"C:\x.exe", window_class_name=""
    )
    status = desktop_safety.resolve_target_status(bad_spec)

    spy.assert_not_called()
    assert status is DesktopStatus.CHECK_FAILED


def test_invalid_manual_control_spec_never_reaches_find_elements(monkeypatch):
    from unittest.mock import MagicMock

    class _FakeElement:
        handle = 1
        process_id = 111

    monkeypatch.setattr(
        desktop_safety,
        "_resolve_target_internal",
        lambda spec: (DesktopStatus.AVAILABLE, _FakeElement()),
    )
    spy = MagicMock(side_effect=AssertionError("find_elements must never be called"))
    monkeypatch.setattr(desktop_safety, "WINDOWS", True)
    monkeypatch.setattr(desktop_safety, "findwindows", MagicMock(find_elements=spy))

    bad_control = DesktopControlSpec(
        target_key="fixture_window", control_automation_id="", control_type="Button"
    )
    target_spec = DesktopTargetSpec(
        application_key="fixture_app",
        process_executable=r"C:\x.exe",
        window_class_name="TkTopLevel",
    )
    status = desktop_safety.resolve_control_status(target_spec, bad_control)

    spy.assert_not_called()
    assert status is DesktopStatus.CHECK_FAILED


# ============================================================================
# 11. UIA exact-API boundary / scope
# ============================================================================


def test_no_fuzzy_or_magic_lookup_api_used_in_production():
    """AST-based (see _code_identifiers()) - the module's own docstring
    prose explicitly names best_match/ctrl_index/found_index as REJECTED
    APIs, which a raw substring search would false-positive on."""

    identifiers = _code_identifiers(desktop_safety)
    for forbidden in ("best_match", "ctrl_index", "found_index", "child_window", "Application", "Desktop"):
        assert forbidden not in identifiers


def test_target_enumeration_is_top_level_only():
    source = inspect.getsource(desktop_safety._find_exact_top_level_windows)
    assert '"top_level_only": True' in source
    assert '"visible_only": True' in source


# ============================================================================
# 12. No live content read
# ============================================================================


def test_no_name_or_text_property_read_anywhere_in_desktop_production_code():
    for module_source in (
        inspect.getsource(desktop_safety),
        inspect.getsource(desktop_target_status),
        inspect.getsource(desktop_control_status),
    ):
        for forbidden in (".name", "window_text", "texts(", "ValuePattern", ".title"):
            assert forbidden not in module_source


# ============================================================================
# 13. No mutation - references the dedicated static-acceptance suites
# ============================================================================


def test_no_mutation_static_acceptance_module_exists_and_is_collectible():
    """Full negative-denylist AND positive-allowlist coverage lives in
    test_desktop_no_mutation_static_acceptance.py and is not duplicated
    here - this is a structural tripwire proving that module still exists
    and still defines its two complementary boundary tests, so a future
    accidental deletion of that file would not silently remove M45's
    mutation guard."""

    import tests.kernel.tools.test_desktop_no_mutation_static_acceptance as guard

    assert hasattr(guard, "test_no_forbidden_input_or_mutation_identifiers")
    assert hasattr(guard, "test_only_the_approved_external_api_surface_is_used")


# ============================================================================
# 14. Fresh resolution / no durable UI object persistence
# ============================================================================


def test_plan_step_has_no_hwnd_pid_or_ui_object_field():
    field_names = {f.name for f in fields(PlanStep)}
    for forbidden in ("hwnd", "pid", "element", "wrapper", "control_ref"):
        assert forbidden not in field_names


def test_step_observation_has_no_hwnd_pid_or_ui_object_field():
    field_names = {f.name for f in fields(StepObservation)}
    for forbidden in ("hwnd", "pid", "element", "wrapper", "control_ref"):
        assert forbidden not in field_names


def test_resolve_functions_never_return_a_persisted_reference_type():
    """resolve_target_status()/resolve_control_status() (the only two
    functions anything outside desktop_safety.py ever calls) return
    DesktopStatus only - never an element/wrapper object."""

    import typing

    target_hints = typing.get_type_hints(desktop_safety.resolve_target_status)
    control_hints = typing.get_type_hints(desktop_safety.resolve_control_status)
    assert target_hints.get("return") is DesktopStatus
    assert control_hints.get("return") is DesktopStatus


# ============================================================================
# 15. Platform / session boundary
# ============================================================================


def test_no_elevation_or_uac_automation_in_production_code():
    source = inspect.getsource(desktop_safety)
    for forbidden in ("ShellExecute", "runas", "elevate", "UAC", "SecureDesktop"):
        assert forbidden not in source


def test_platform_check_is_structural_not_a_live_probe():
    source = inspect.getsource(desktop_safety)
    assert 'WINDOWS = sys.platform == "win32"' in source


# ============================================================================
# 16. Dependency acceptance
# ============================================================================


def test_pywinauto_is_the_only_new_direct_dependency():
    pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pywinauto>=0.6.9"' in pyproject
    for existing in ("anthropic", "playwright", "psutil", "python-dotenv", "pyyaml"):
        assert existing in pyproject


# ============================================================================
# 17. Audit privacy acceptance: unchanged schema
# ============================================================================


def test_audit_record_signature_remains_action_resource_key_outcome_only():
    from kernel.tools import audit

    signature = inspect.signature(audit.record)
    param_names = list(signature.parameters)
    assert param_names[:3] == ["action", "resource_key", "outcome"]


# ============================================================================
# 18. Prompt-injection / authority-flow boundary
# ============================================================================


def test_desktop_handlers_accept_only_action_request_and_tools_config():
    for handler_module in (desktop_target_status, desktop_control_status):
        signature = inspect.signature(handler_module.run)
        assert list(signature.parameters) == ["request", "tools_config"]


def test_desktop_handlers_never_construct_a_new_action_request():
    for handler_module in (desktop_target_status, desktop_control_status):
        source = inspect.getsource(handler_module)
        assert source.count("ActionRequest(") == 0


def test_plan_step_authority_remains_action_name_plus_resource_key_only():
    """No TaskPlan/schema change for M45 - PlanStep's authority-bearing
    fields are unchanged from every prior milestone."""

    field_names = {f.name for f in fields(PlanStep)}
    assert {"action_name", "resource_key"} <= field_names
    for forbidden in ("selector", "locator", "arguments", "keys", "text_input", "coordinates"):
        assert forbidden not in field_names


# ============================================================================
# 19. M42 / M46+ boundary acceptance
# ============================================================================


def test_task_execution_service_and_respond_have_no_desktop_specific_surface():
    from kernel.task_execution import respond, service

    assert "desktop" not in inspect.getsource(service).lower()
    assert "desktop" not in inspect.getsource(respond).lower()


def test_no_m46_plus_capability_named_in_the_registry():
    registry = ActionRegistry()
    action_names = {d.name for d in registry.descriptors()}

    forbidden_substrings = (
        "whatsapp_task",
        "reconcile",
        "recovery",
        "confirmation_reply",
        "employee_acceptance",
        "launch_approval",
    )
    for name in action_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in name.lower()


# ============================================================================
# 20. Positive regression: Milestone 45 closes WITHOUT a mutation phase
# ============================================================================


def test_milestone_45_closes_without_a_mutation_phase_by_design():
    """Documents Milestone 45's explicit closure decision: native desktop
    mutation was empirically evaluated and REJECTED, not merely unbuilt -
    UI Automation InvokePattern, tested against the M45 validation fixture,
    changed foreground state and did not reliably produce the intended
    application effect (see docs/architecture.md's Milestone 45 entry).
    This is a positive regression: it fails loudly if a future change ever
    quietly introduces desktop mutation capability without a fresh
    design/security review."""

    registry = ActionRegistry()
    action_names = {d.name for d in registry.descriptors()}

    assert action_names == {
        "system_status",
        "list_files",
        "open_application",
        "run_registered_script",
        "repo_health",
        "repository_backup",
        "file_metadata",
        "read_text_file",
        "list_processes",
        "create_directory",
        "copy_file",
        "browser_read_page",
        "desktop_target_status",
        "desktop_control_status",
    }
