"""Tests for kernel/task_planner/prompt.py: prompt/schema assembly only -
no wording assertions on the fixed instruction text (that text is
empirically validated as a whole; see docs/architecture.md)."""

from kernel.task_planner.prompt import build_prompt, build_schema
from kernel.task_planner.types import CatalogEntry

_CATALOG = (
    CatalogEntry(
        catalog_id="action_1",
        action_name="system_status",
        resource_key=None,
        sensitive=False,
        summary="Check the current system status.",
    ),
    CatalogEntry(
        catalog_id="action_2",
        action_name="repository_backup",
        resource_key="ai_os",
        sensitive=True,
        summary="Back up the registered 'ai_os' repository.",
    ),
)


def test_build_prompt_includes_request_text_and_every_catalog_summary():
    prompt = build_prompt("Check system status.", _CATALOG)
    assert "Check system status." in prompt
    assert "Check the current system status." in prompt
    assert "Back up the registered 'ai_os' repository." in prompt
    assert 'catalog_id="action_1"' in prompt
    assert 'catalog_id="action_2"' in prompt


def test_build_prompt_marks_sensitivity_per_entry():
    prompt = build_prompt("x", _CATALOG)
    assert "sensitive=no" in prompt
    assert "sensitive=yes" in prompt


def test_build_prompt_catalog_id_is_verbatim_and_opaque():
    prompt = build_prompt("x", _CATALOG)
    # The model is told to copy catalog_id verbatim - it never sees the
    # underlying action_name/resource_key pair as separate fields it could
    # reconstruct or alter (see kernel/task_planner/types.py:CatalogEntry).
    assert '"action_name"' not in prompt
    assert '"resource_key"' not in prompt


def test_build_schema_top_level_is_two_branch_oneof():
    schema = build_schema(_CATALOG)
    assert set(schema.keys()) == {"oneOf"}
    assert len(schema["oneOf"]) == 2
    consts = {branch["properties"]["result"]["const"] for branch in schema["oneOf"]}
    assert consts == {"plan", "cannot_plan"}


def test_build_schema_plan_branch_is_closed():
    schema = build_schema(_CATALOG)
    plan_branch = next(b for b in schema["oneOf"] if b["properties"]["result"]["const"] == "plan")
    assert plan_branch["additionalProperties"] is False
    assert set(plan_branch["required"]) == {"plan_version", "result", "objective", "steps"}


def test_build_schema_cannot_plan_branch_is_closed():
    schema = build_schema(_CATALOG)
    cannot_plan_branch = next(
        b for b in schema["oneOf"] if b["properties"]["result"]["const"] == "cannot_plan"
    )
    assert cannot_plan_branch["additionalProperties"] is False
    assert set(cannot_plan_branch["required"]) == {"plan_version", "result", "reason"}


def test_build_schema_action_step_catalog_id_enum_matches_catalog_exactly():
    schema = build_schema(_CATALOG)
    plan_branch = next(b for b in schema["oneOf"] if b["properties"]["result"]["const"] == "plan")
    step_schema = plan_branch["properties"]["steps"]["items"]
    action_branch = next(
        b for b in step_schema["oneOf"] if b["properties"]["step_kind"]["const"] == "action"
    )
    assert action_branch["properties"]["catalog_id"]["enum"] == ["action_1", "action_2"]
    assert action_branch["additionalProperties"] is False


def test_build_schema_respond_step_has_no_catalog_id_field():
    schema = build_schema(_CATALOG)
    plan_branch = next(b for b in schema["oneOf"] if b["properties"]["result"]["const"] == "plan")
    step_schema = plan_branch["properties"]["steps"]["items"]
    respond_branch = next(
        b for b in step_schema["oneOf"] if b["properties"]["step_kind"]["const"] == "respond"
    )
    assert "catalog_id" not in respond_branch["properties"]
    assert respond_branch["additionalProperties"] is False


def test_build_schema_step_count_is_bounded():
    schema = build_schema(_CATALOG)
    plan_branch = next(b for b in schema["oneOf"] if b["properties"]["result"]["const"] == "plan")
    assert plan_branch["properties"]["steps"]["minItems"] == 1
    assert plan_branch["properties"]["steps"]["maxItems"] == 8


def test_build_schema_with_empty_catalog_still_has_no_available_actions():
    schema = build_schema(())
    plan_branch = next(b for b in schema["oneOf"] if b["properties"]["result"]["const"] == "plan")
    step_schema = plan_branch["properties"]["steps"]["items"]
    action_branch = next(
        b for b in step_schema["oneOf"] if b["properties"]["step_kind"]["const"] == "action"
    )
    assert action_branch["properties"]["catalog_id"]["enum"] == []
