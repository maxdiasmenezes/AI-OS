"""Tests for kernel/task_planner/grounding.py: the deterministic,
post-parse capability-grounding validation boundary. Pure module - no I/O,
no model call, no execution."""

import pytest

from kernel.task_planner.catalog import build_catalog
from kernel.task_planner.grounding import validate_capability_grounding
from kernel.task_planner.types import (
    CatalogEntry,
    ParsedPlan,
    PlannerErrorCode,
    PlannerFailure,
    PlanStep,
    StepKind,
)
from kernel.tools.config import ApplicationSpec, RepoSpec, ScriptSpec, ToolsConfig
from kernel.tools.registry import ActionRegistry

_SCRIPT_ENTRY = CatalogEntry(
    catalog_id="action_6",
    action_name="run_registered_script",
    resource_key="whatsapp_test",
    sensitive=True,
    summary="Run the registered 'whatsapp_test' script.",
    requires_capability_grounding=True,
)
_APP_ENTRY = CatalogEntry(
    catalog_id="action_5",
    action_name="open_application",
    resource_key="notepad",
    sensitive=True,
    summary="Open the registered 'notepad' application.",
    requires_capability_grounding=True,
)
_REPO_HEALTH_ENTRY = CatalogEntry(
    catalog_id="action_7",
    action_name="repo_health",
    resource_key="ai_os",
    sensitive=False,
    summary="Check the health of the registered 'ai_os' repository.",
    requires_capability_grounding=False,
)
_BACKUP_ENTRY = CatalogEntry(
    catalog_id="action_8",
    action_name="repository_backup",
    resource_key="ai_os",
    sensitive=True,
    summary="Back up the registered 'ai_os' repository.",
    requires_capability_grounding=False,
)
_CATALOG = (_SCRIPT_ENTRY, _APP_ENTRY, _REPO_HEALTH_ENTRY, _BACKUP_ENTRY)


def _action_step(entry: CatalogEntry, position: int = 1) -> PlanStep:
    return PlanStep(
        step_id=f"step_{position}",
        position=position,
        kind=StepKind.ACTION,
        action_name=entry.action_name,
        resource_key=entry.resource_key,
        catalog_id=entry.catalog_id,
        description="x",
        expected_result="y",
        depends_on=(),
        requires_confirmation=entry.sensitive,
    )


def _plan(*steps: PlanStep) -> ParsedPlan:
    return ParsedPlan(plan_version=1, objective="x", steps=tuple(steps))


# --- the named regression: generic "run the tests" must not authorize ------
# --- the specifically-scoped whatsapp_test capability -----------------------


def test_generic_run_the_tests_does_not_ground_whatsapp_test():
    plan = _plan(_action_step(_SCRIPT_ENTRY))
    result = validate_capability_grounding(
        plan, "Check the AI-OS repository, run the tests, and summarize the result.", _CATALOG
    )
    assert isinstance(result, PlannerFailure)
    assert result.error is PlannerErrorCode.UNGROUNDED_CAPABILITY


@pytest.mark.parametrize(
    "phrasing",
    [
        "run the tests",
        "run tests",
        "please run the test",
        "run all the tests for me",
    ],
)
def test_various_generic_test_phrasings_all_fail_grounding(phrasing):
    plan = _plan(_action_step(_SCRIPT_ENTRY))
    result = validate_capability_grounding(plan, phrasing, _CATALOG)
    assert isinstance(result, PlannerFailure)
    assert result.error is PlannerErrorCode.UNGROUNDED_CAPABILITY


# --- legitimate explicit requests remain eligible ---------------------------


@pytest.mark.parametrize(
    "phrasing",
    [
        "Run the whatsapp_test script.",
        "run the WhatsApp test",
        "Run the configured WhatsApp test script.",
        "RUN THE WHATSAPP TEST",
        "Please run whatsapp test for me.",
    ],
)
def test_explicit_whatsapp_test_requests_pass_grounding(phrasing):
    plan = _plan(_action_step(_SCRIPT_ENTRY))
    result = validate_capability_grounding(plan, phrasing, _CATALOG)
    assert result is None


# --- open_application shares the same named-capability contract ------------


def test_generic_open_the_application_does_not_ground_notepad():
    plan = _plan(_action_step(_APP_ENTRY))
    result = validate_capability_grounding(plan, "Open the application for me.", _CATALOG)
    assert isinstance(result, PlannerFailure)
    assert result.error is PlannerErrorCode.UNGROUNDED_CAPABILITY


def test_explicit_open_notepad_passes_grounding():
    plan = _plan(_action_step(_APP_ENTRY))
    result = validate_capability_grounding(plan, "Open notepad.", _CATALOG)
    assert result is None


# --- broad resource/location actions are never grounded ---------------------
# --- (matches the already-validated "the repository" singular-option case) --


def test_repo_health_never_requires_grounding_even_when_unnamed():
    plan = _plan(_action_step(_REPO_HEALTH_ENTRY))
    result = validate_capability_grounding(
        plan, "Check repository status, then back it up.", _CATALOG
    )
    assert result is None


def test_repository_backup_never_requires_grounding_even_when_unnamed():
    plan = _plan(_action_step(_BACKUP_ENTRY))
    result = validate_capability_grounding(plan, "Back up the repository.", _CATALOG)
    assert result is None


# --- structural edge cases --------------------------------------------------


def test_respond_step_is_never_checked():
    respond_step = PlanStep(
        step_id="step_1",
        position=1,
        kind=StepKind.RESPOND,
        action_name=None,
        resource_key=None,
        catalog_id=None,
        description="x",
        expected_result="y",
        depends_on=(),
        requires_confirmation=False,
    )
    result = validate_capability_grounding(_plan(respond_step), "anything at all", _CATALOG)
    assert result is None


def test_multi_step_plan_surfaces_the_first_ungrounded_step():
    plan = _plan(
        _action_step(_REPO_HEALTH_ENTRY, position=1),
        _action_step(_SCRIPT_ENTRY, position=2),
    )
    result = validate_capability_grounding(plan, "Check repository status and run the tests.", _CATALOG)
    assert isinstance(result, PlannerFailure)
    assert "step 2" in result.detail


def test_multi_step_plan_with_every_named_capability_grounded_passes():
    plan = _plan(
        _action_step(_APP_ENTRY, position=1),
        _action_step(_SCRIPT_ENTRY, position=2),
    )
    result = validate_capability_grounding(
        plan, "Open notepad, then run the whatsapp_test script.", _CATALOG
    )
    assert result is None


# --- end-to-end: the seven scenarios (real build_catalog() + real ----------
# --- validate_capability_grounding(), not manually constructed fixtures) ---


def _repo_config(*repo_keys: str) -> ToolsConfig:
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={},
        approved_repositories={key: RepoSpec(path=f"/r/{key}", main_branch="main") for key in repo_keys},
        approved_backups={},
    )


def _app_config(*app_keys: str) -> ToolsConfig:
    return ToolsConfig(
        approved_directories={},
        approved_applications={
            key: ApplicationSpec(executable=f"/e/{key}", cwd="/e") for key in app_keys
        },
        approved_scripts={},
        approved_repositories={},
        approved_backups={},
    )


def _script_config(*script_keys: str) -> ToolsConfig:
    return ToolsConfig(
        approved_directories={},
        approved_applications={},
        approved_scripts={
            key: ScriptSpec(interpreter="/i/python", script_path=f"/s/{key}.py", cwd="/s", timeout_seconds=30)
            for key in script_keys
        },
        approved_repositories={},
        approved_backups={},
    )


def _plan_selecting(catalog: tuple[CatalogEntry, ...], action_name: str, resource_key: str) -> ParsedPlan:
    entry = next(e for e in catalog if e.action_name == action_name and e.resource_key == resource_key)
    return _plan(_action_step(entry))


def test_scenario_one_repository_check_the_repository_passes():
    catalog = build_catalog(ActionRegistry(), _repo_config("ai_os"))
    plan = _plan_selecting(catalog, "repo_health", "ai_os")
    assert validate_capability_grounding(plan, "Check the repository.", catalog) is None


def test_scenario_three_repositories_check_the_repository_fails():
    catalog = build_catalog(ActionRegistry(), _repo_config("ai_os", "navexis", "personal_finance"))
    plan = _plan_selecting(catalog, "repo_health", "navexis")
    result = validate_capability_grounding(plan, "Check the repository.", catalog)
    assert isinstance(result, PlannerFailure)
    assert result.error is PlannerErrorCode.UNGROUNDED_CAPABILITY


def test_scenario_three_repositories_check_the_ai_os_repository_passes():
    catalog = build_catalog(ActionRegistry(), _repo_config("ai_os", "navexis", "personal_finance"))
    plan = _plan_selecting(catalog, "repo_health", "ai_os")
    result = validate_capability_grounding(plan, "Check the AI-OS repository.", catalog)
    assert result is None


def test_scenario_one_script_run_the_tests_fails():
    catalog = build_catalog(ActionRegistry(), _script_config("whatsapp_test"))
    plan = _plan_selecting(catalog, "run_registered_script", "whatsapp_test")
    result = validate_capability_grounding(plan, "Run the tests.", catalog)
    assert isinstance(result, PlannerFailure)
    assert result.error is PlannerErrorCode.UNGROUNDED_CAPABILITY


def test_scenario_one_script_run_the_whatsapp_test_passes():
    catalog = build_catalog(ActionRegistry(), _script_config("whatsapp_test"))
    plan = _plan_selecting(catalog, "run_registered_script", "whatsapp_test")
    result = validate_capability_grounding(plan, "Run the WhatsApp test.", catalog)
    assert result is None


def test_scenario_two_applications_open_the_application_fails():
    catalog = build_catalog(ActionRegistry(), _app_config("notepad", "calculator"))
    plan = _plan_selecting(catalog, "open_application", "notepad")
    result = validate_capability_grounding(plan, "Open the application.", catalog)
    assert isinstance(result, PlannerFailure)
    assert result.error is PlannerErrorCode.UNGROUNDED_CAPABILITY


def test_scenario_two_applications_open_notepad_passes():
    catalog = build_catalog(ActionRegistry(), _app_config("notepad", "calculator"))
    plan = _plan_selecting(catalog, "open_application", "notepad")
    result = validate_capability_grounding(plan, "Open Notepad.", catalog)
    assert result is None


def test_scenario_two_applications_wrong_selection_also_fails():
    # If the model had instead selected the OTHER app for a "Notepad"
    # request, grounding must still reject it - it does not merely check
    # "was ANY app named," it checks "was THIS selected entry named."
    catalog = build_catalog(ActionRegistry(), _app_config("notepad", "calculator"))
    plan = _plan_selecting(catalog, "open_application", "calculator")
    result = validate_capability_grounding(plan, "Open Notepad.", catalog)
    assert isinstance(result, PlannerFailure)
    assert result.error is PlannerErrorCode.UNGROUNDED_CAPABILITY
