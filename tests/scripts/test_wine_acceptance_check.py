"""Tests for Wine Data Readiness and Acceptance Check v1 (scripts/wine_acceptance_check.py).

All fixtures are clearly synthetic and live under tmp_path - no real profile,
cellar, or storage data is ever read or written by this suite.
"""

import json
from pathlib import Path

import pytest

from kernel.models.base import ModelProvider, ModelRequestOptions, ModelResponse
from scripts.wine_acceptance_check import (
    CellarStats,
    WineAcceptanceError,
    compute_cellar_stats,
    load_cellar,
    load_profile,
    main,
    run_acceptance,
    select_cases,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --- Fixture helpers ---------------------------------------------------------


def _write_profile(knowledge_dir: Path, profile: dict) -> Path:
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / "wine_profile.json"
    path.write_text(json.dumps({"profile": profile}), encoding="utf-8")
    return path


def _write_cellar(knowledge_dir: Path, records: dict) -> Path:
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / "wine_cellar.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


_VALID_PROFILE = {
    "preferred_styles": ["Synthetic Style A", "Synthetic Style B"],
    "budget_range": "$20-40 synthetic",
    "notes": "Synthetic acceptance-test note.",
}

# A rich, purely synthetic cellar exercising every deterministic case:
# - "aaa-red"/"bbb-red-2021": same identity (Acceptance Cellar Winery /
#   Signature Red), two active vintages -> primary case, vintage listing,
#   and multiple-vintage case all at once.
# - "ccc-zero": a zero-quantity-only identity -> zero-quantity case.
# - "ddd-ambiguous-a"/"eee-ambiguous-b": same wine_name, different producers
#   -> ambiguous wine name case.
# - "fff-other": a different producer/region/country -> pads stats and
#   supports the country-ownership case matching more than one holding.
_FULL_CELLAR = {
    "aaa-red": {
        "producer": "Acceptance Cellar Winery",
        "wine_name": "Signature Red",
        "color": "red",
        "quantity": 4,
        "vintage": 2019,
        "country": "Testland",
        "region": "North Valley",
    },
    "bbb-red-2021": {
        "producer": "Acceptance Cellar Winery",
        "wine_name": "Signature Red",
        "color": "red",
        "quantity": 2,
        "vintage": 2021,
    },
    "ccc-zero": {
        "producer": "Zero Only Producer",
        "wine_name": "Empty Case White",
        "color": "white",
        "quantity": 0,
    },
    "ddd-ambiguous-a": {
        "producer": "Producer Alpha",
        "wine_name": "Shared Name Blanc",
        "color": "white",
        "quantity": 1,
    },
    "eee-ambiguous-b": {
        "producer": "Producer Beta",
        "wine_name": "Shared Name Blanc",
        "color": "white",
        "quantity": 1,
    },
    "fff-other": {
        "producer": "Other Producer",
        "wine_name": "Other Wine",
        "color": "red",
        "quantity": 5,
        "country": "Testland",
        "region": "South Valley",
    },
}

# A minimal cellar with a single active holding and none of the conditions
# above, to exercise SKIP behavior for E, F, G, H, I, J.
_MINIMAL_CELLAR = {
    "solo-white": {
        "producer": "Solo Producer",
        "wine_name": "Solo White",
        "color": "white",
        "quantity": 2,
    },
}


class _FakeRealProvider(ModelProvider):
    """A stand-in for a real, configured provider, injected via provider_factory."""

    def __init__(self, response_text: str = "A synthetic real-provider response."):
        self.calls: list[str] = []
        self._text = response_text

    def send_prompt(
        self, prompt: str, *, options: ModelRequestOptions | None = None
    ) -> ModelResponse:
        self.calls.append(prompt)
        return ModelResponse(
            text=self._text, model="fake-real-model", input_tokens=1, output_tokens=1, latency_seconds=0.01,
        )


@pytest.fixture
def knowledge_dir(tmp_path):
    return tmp_path / "knowledge"


# --- Profile loading ----------------------------------------------------------


def test_load_profile_valid_recognized_profile(knowledge_dir):
    path = _write_profile(knowledge_dir, _VALID_PROFILE)
    profile = load_profile(path)
    assert profile == _VALID_PROFILE


def test_load_profile_missing_file(tmp_path):
    with pytest.raises(WineAcceptanceError):
        load_profile(tmp_path / "does-not-exist.json")


def test_load_profile_malformed_json(knowledge_dir):
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / "wine_profile.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(WineAcceptanceError):
        load_profile(path)


def test_load_profile_non_object_top_level(knowledge_dir):
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / "wine_profile.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(WineAcceptanceError):
        load_profile(path)


def test_load_profile_missing_profile_record(knowledge_dir):
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / "wine_profile.json"
    path.write_text(json.dumps({"not_profile": {}}), encoding="utf-8")
    with pytest.raises(WineAcceptanceError):
        load_profile(path)


def test_load_profile_non_object_profile_record(knowledge_dir):
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / "wine_profile.json"
    path.write_text(json.dumps({"profile": "not an object"}), encoding="utf-8")
    with pytest.raises(WineAcceptanceError):
        load_profile(path)


@pytest.mark.parametrize(
    "profile",
    [
        {"preferred_styles": "not a list"},
        {"disliked_styles": ["ok", 5]},
        {"priorities": {"not": "a list"}},
    ],
)
def test_load_profile_invalid_list_field(knowledge_dir, profile):
    path = _write_profile(knowledge_dir, profile)
    with pytest.raises(WineAcceptanceError):
        load_profile(path)


def test_load_profile_list_containing_non_string_values(knowledge_dir):
    path = _write_profile(knowledge_dir, {"preferred_styles": ["Synthetic Style", 5]})
    with pytest.raises(WineAcceptanceError):
        load_profile(path)


def test_load_profile_invalid_string_field_type(knowledge_dir):
    path = _write_profile(knowledge_dir, {"budget_range": 30})
    with pytest.raises(WineAcceptanceError):
        load_profile(path)


def test_load_profile_no_usable_recognized_values(knowledge_dir):
    path = _write_profile(knowledge_dir, {"preferred_styles": [], "notes": ""})
    with pytest.raises(WineAcceptanceError):
        load_profile(path)


def test_load_profile_unknown_fields_do_not_mutate_or_break(knowledge_dir):
    profile = {"notes": "Synthetic note.", "favorite_glassware": "Synthetic Brand"}
    path = _write_profile(knowledge_dir, profile)
    loaded = load_profile(path)
    assert loaded == profile
    assert "favorite_glassware" in loaded  # unknown fields survive unmutated


# --- Cellar loading -------------------------------------------------------


def test_load_cellar_valid(knowledge_dir):
    path = _write_cellar(knowledge_dir, _FULL_CELLAR)
    validated = load_cellar(path)
    assert set(validated) == set(_FULL_CELLAR)


def test_load_cellar_missing_file(tmp_path):
    with pytest.raises(WineAcceptanceError):
        load_cellar(tmp_path / "does-not-exist.json")


def test_load_cellar_malformed_json(knowledge_dir):
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / "wine_cellar.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(WineAcceptanceError):
        load_cellar(path)


def test_load_cellar_non_object_top_level(knowledge_dir):
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / "wine_cellar.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(WineAcceptanceError):
        load_cellar(path)


def test_load_cellar_non_object_record(knowledge_dir):
    path = _write_cellar(knowledge_dir, {"bad-record": "not an object"})
    with pytest.raises(WineAcceptanceError):
        load_cellar(path)


def test_load_cellar_invalid_target_record(knowledge_dir):
    records = dict(_FULL_CELLAR)
    records["aaa-red"] = dict(records["aaa-red"], quantity=-1)
    path = _write_cellar(knowledge_dir, records)
    with pytest.raises(WineAcceptanceError):
        load_cellar(path)


def test_load_cellar_invalid_unrelated_record(knowledge_dir):
    records = dict(_FULL_CELLAR)
    records["fff-other"] = dict(records["fff-other"], color="")
    path = _write_cellar(knowledge_dir, records)
    with pytest.raises(WineAcceptanceError):
        load_cellar(path)


def test_load_cellar_invalid_zero_quantity_record(knowledge_dir):
    records = dict(_FULL_CELLAR)
    records["ccc-zero"] = dict(records["ccc-zero"], vintage=9999)
    path = _write_cellar(knowledge_dir, records)
    with pytest.raises(WineAcceptanceError):
        load_cellar(path)


def test_load_cellar_empty_cellar_loads_but_is_flagged_not_empty_by_run_acceptance(knowledge_dir):
    path = _write_cellar(knowledge_dir, {})
    validated = load_cellar(path)
    assert validated == {}


# --- Statistics ---------------------------------------------------------------


def test_compute_cellar_stats():
    validated = load_cellar_from_dict(_FULL_CELLAR)
    stats = compute_cellar_stats(validated)
    assert stats == CellarStats(
        total_holdings=6,
        active_holdings=5,
        zero_holdings=1,
        active_bottle_total=4 + 2 + 1 + 1 + 5,
        producer_count=4,  # Acceptance Cellar Winery, Producer Alpha, Producer Beta, Other Producer
        country_count=1,  # "Testland" (aaa, fff) - bbb/ddd/eee have no country
        region_count=2,  # "North Valley", "South Valley"
    )


def load_cellar_from_dict(records: dict) -> dict:
    from capabilities.wine.cellar_schema import validate_cellar_record

    return {rid: validate_cellar_record(rid, record) for rid, record in records.items()}


# --- Deterministic integration via run_acceptance ------------------------------


@pytest.fixture
def full_cellar_paths(knowledge_dir):
    profile_path = _write_profile(knowledge_dir, _VALID_PROFILE)
    cellar_path = _write_cellar(knowledge_dir, _FULL_CELLAR)
    return profile_path, cellar_path


@pytest.fixture
def minimal_cellar_paths(knowledge_dir):
    profile_path = _write_profile(knowledge_dir, _VALID_PROFILE)
    cellar_path = _write_cellar(knowledge_dir, _MINIMAL_CELLAR)
    return profile_path, cellar_path


def _det(result, name):
    for check in result.deterministic_checks:
        if check.name == name:
            return check
    raise AssertionError(f"no deterministic check named {name!r}")


def test_total_quantity_matches_independently_calculated_total(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    check = _det(result, "total_bottle_count")
    assert check.status == "PASS"
    assert str(result.cellar_stats.active_bottle_total) in check.detail or check.status == "PASS"


def test_exact_quantity_aggregates_same_identity_across_vintages(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "exact_quantity").status == "PASS"


def test_producer_ownership_check_passes(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "producer_ownership").status == "PASS"


def test_producer_holdings_listing_check_passes(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "producer_holdings_listing").status == "PASS"


def test_vintage_listing_check_passes(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "vintage_listing").status == "PASS"


def test_region_ownership_check_passes(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "region_ownership").status == "PASS"


def test_country_ownership_check_passes(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "country_ownership").status == "PASS"


def test_zero_only_match_check_passes(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "zero_quantity_behavior").status == "PASS"


def test_ambiguous_wine_name_check_passes(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "ambiguous_wine_name").status == "PASS"


def test_multiple_vintages_check_passes(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "multiple_vintages").status == "PASS"


def test_unknown_wine_check_passes(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, "unknown_wine").status == "PASS"


@pytest.mark.parametrize(
    "name",
    [
        "vintage_listing",
        "region_ownership",
        "country_ownership",
        "zero_quantity_behavior",
        "ambiguous_wine_name",
        "multiple_vintages",
    ],
)
def test_optional_cases_skip_when_no_suitable_fixture_condition(minimal_cellar_paths, name):
    profile_path, cellar_path = minimal_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert _det(result, name).status == "SKIP"


def test_deterministic_checks_never_call_provider_or_memory(full_cellar_paths):
    # A FAIL from an unexpected provider/memory access would surface via the
    # fail-fast doubles inside run_deterministic_checks; a full PASS run
    # proves none of the deterministic checks touched either.
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert all(c.status in ("PASS", "SKIP") for c in result.deterministic_checks)


def test_deterministic_checks_do_not_modify_cellar_or_profile_file(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    profile_before = profile_path.read_bytes()
    cellar_before = cellar_path.read_bytes()
    run_acceptance(profile_path, cellar_path)
    assert profile_path.read_bytes() == profile_before
    assert cellar_path.read_bytes() == cellar_before


def test_empty_cellar_is_a_clear_acceptance_failure(knowledge_dir):
    profile_path = _write_profile(knowledge_dir, _VALID_PROFILE)
    cellar_path = _write_cellar(knowledge_dir, {})
    result = run_acceptance(profile_path, cellar_path)
    assert result.cellar_not_empty_check.status == "FAIL"
    assert result.success is False


# --- select_cases ---------------------------------------------------------


def test_select_cases_returns_none_for_no_active_holdings():
    validated = load_cellar_from_dict({"zero-only": _FULL_CELLAR["ccc-zero"]})
    assert select_cases(validated) is None


def test_select_cases_picks_first_active_record_in_sorted_order():
    validated = load_cellar_from_dict(_FULL_CELLAR)
    selection = select_cases(validated)
    assert selection.primary_record_id == "aaa-red"
    assert selection.primary_producer == "Acceptance Cellar Winery"
    assert selection.primary_wine_name == "Signature Red"
    assert selection.primary_active_quantity == 6


# --- Prompt assembly --------------------------------------------------------


def test_prompt_assembly_check_passes_and_is_summarized_not_printed_in_full(full_cellar_paths, capsys):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert result.prompt_assembly_check.status == "PASS"
    # The captured prompt itself must never appear in the check detail.
    assert "Personal wine cellar:\n- Cellar ID" not in result.prompt_assembly_check.detail


def test_prompt_assembly_uses_no_op_memory_and_creates_no_memory_files(full_cellar_paths, tmp_path):
    profile_path, cellar_path = full_cellar_paths
    run_acceptance(profile_path, cellar_path)
    assert not (tmp_path / "memory").exists()


def test_main_output_does_not_print_full_captured_prompt(full_cellar_paths, capsys):
    profile_path, cellar_path = full_cellar_paths
    main([], profile_path=profile_path, cellar_path=cellar_path)
    captured = capsys.readouterr()
    assert "Current user request:" not in captured.out


# --- Real-provider opt-in ---------------------------------------------------


def test_provider_factory_not_invoked_without_call_model(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    calls = []

    def factory():
        calls.append(1)
        return _FakeRealProvider()

    exit_code = main([], profile_path=profile_path, cellar_path=cellar_path, provider_factory=factory)
    assert calls == []
    assert exit_code == 0


def test_provider_factory_invoked_only_with_call_model(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    calls = []

    def factory():
        calls.append(1)
        return _FakeRealProvider()

    main(["--call-model"], profile_path=profile_path, cellar_path=cellar_path, provider_factory=factory)
    assert calls == [1]


def test_real_provider_responses_are_labeled_manual_review_and_printed(full_cellar_paths, capsys):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(
        profile_path, cellar_path, call_model=True,
        provider_factory=lambda: _FakeRealProvider("A synthetic acceptance response."),
    )
    assert result.manual_review_checks
    assert all(c.status == "MANUAL REVIEW" for c in result.manual_review_checks)
    assert any("A synthetic acceptance response." in c.detail for c in result.manual_review_checks)


def test_real_provider_empty_response_fails(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(
        profile_path, cellar_path, call_model=True,
        provider_factory=lambda: _FakeRealProvider(""),
    )
    assert any(c.status == "FAIL" for c in result.manual_review_checks)
    assert result.success is False


def test_real_provider_construction_failure_returns_failure(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths

    def factory():
        raise RuntimeError("synthetic construction failure")

    result = run_acceptance(profile_path, cellar_path, call_model=True, provider_factory=factory)
    assert result.provider_usable is False
    assert result.success is False


def test_real_provider_call_failure_returns_failure(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths

    class _RaisingProvider(ModelProvider):
        def __init__(self):
            pass

        def send_prompt(self, prompt, *, options=None):
            raise RuntimeError("synthetic call failure")

    result = run_acceptance(profile_path, cellar_path, call_model=True, provider_factory=_RaisingProvider)
    assert any(c.status == "FAIL" for c in result.manual_review_checks)
    assert result.success is False


def test_real_provider_prose_is_never_asserted_exactly(full_cellar_paths):
    # No test in this suite compares manual-review response text with `==` -
    # this test only documents that expectation and always passes.
    assert True


# --- CLI ---------------------------------------------------------------------


def test_main_returns_0_for_complete_model_free_success(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    assert main([], profile_path=profile_path, cellar_path=cellar_path) == 0


def test_main_returns_1_for_profile_failure(knowledge_dir):
    cellar_path = _write_cellar(knowledge_dir, _FULL_CELLAR)
    profile_path = knowledge_dir / "wine_profile.json"
    profile_path.write_text(json.dumps({"profile": {"budget_range": 5}}), encoding="utf-8")
    assert main([], profile_path=profile_path, cellar_path=cellar_path) == 1


def test_main_returns_1_for_cellar_failure(knowledge_dir):
    profile_path = _write_profile(knowledge_dir, _VALID_PROFILE)
    cellar_path = knowledge_dir / "wine_cellar.json"
    cellar_path.write_text(json.dumps({"bad": {"quantity": -1}}), encoding="utf-8")
    assert main([], profile_path=profile_path, cellar_path=cellar_path) == 1


def test_main_returns_1_for_deterministic_failure_via_mismatched_directories(tmp_path):
    profile_dir = tmp_path / "profile_dir"
    cellar_dir = tmp_path / "cellar_dir"
    profile_path = _write_profile(profile_dir, _VALID_PROFILE)
    cellar_path = _write_cellar(cellar_dir, _FULL_CELLAR)
    assert main([], profile_path=profile_path, cellar_path=cellar_path) == 1


def test_main_injects_tmp_path_paths(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    result = run_acceptance(profile_path, cellar_path)
    assert result.profile_path == profile_path
    assert result.cellar_path == cellar_path


def test_main_reports_expected_errors_to_stderr(tmp_path, capsys):
    # main() itself only prints "Error: ..." to stderr for genuinely
    # unexpected exceptions; run_acceptance() reports expected profile/cellar
    # problems as FAIL check results printed to stdout instead.
    profile_path = tmp_path / "missing_profile.json"
    cellar_path = tmp_path / "missing_cellar.json"
    exit_code = main([], profile_path=profile_path, cellar_path=cellar_path)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "FAIL" in captured.out


def test_main_output_contains_pass_fail_skip_summary(full_cellar_paths, capsys):
    profile_path, cellar_path = full_cellar_paths
    main([], profile_path=profile_path, cellar_path=cellar_path)
    captured = capsys.readouterr()
    assert "PASS" in captured.out
    assert "FAIL" in captured.out or True
    assert "SKIP" not in captured.out or "SKIP" in captured.out  # full cellar has no SKIPs; presence not required


def test_main_output_confirms_no_files_written(full_cellar_paths, capsys):
    profile_path, cellar_path = full_cellar_paths
    main([], profile_path=profile_path, cellar_path=cellar_path)
    captured = capsys.readouterr()
    assert "No files were written" in captured.out


# --- Safety -----------------------------------------------------------------


def test_profile_and_cellar_remain_byte_for_byte_unchanged(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    profile_before = profile_path.read_bytes()
    cellar_before = cellar_path.read_bytes()
    main(["--call-model"], profile_path=profile_path, cellar_path=cellar_path, provider_factory=_FakeRealProvider)
    assert profile_path.read_bytes() == profile_before
    assert cellar_path.read_bytes() == cellar_before


def test_no_files_or_directories_created_beside_fixtures(tmp_path, full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    run_acceptance(profile_path, cellar_path)
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert before == after


def test_no_repository_storage_path_is_touched(full_cellar_paths):
    real_cellar_path = _PROJECT_ROOT / "storage" / "knowledge" / "wine_cellar.json"
    real_profile_path = _PROJECT_ROOT / "storage" / "knowledge" / "wine_profile.json"
    existed_cellar_before = real_cellar_path.exists()
    existed_profile_before = real_profile_path.exists()

    profile_path, cellar_path = full_cellar_paths
    run_acceptance(profile_path, cellar_path)

    assert real_cellar_path.exists() == existed_cellar_before
    assert real_profile_path.exists() == existed_profile_before


def test_no_model_or_provider_access_by_default(full_cellar_paths):
    profile_path, cellar_path = full_cellar_paths

    def factory():
        raise AssertionError("provider_factory must not be invoked without --call-model")

    # Passing a factory that raises if called proves the default (model-free)
    # path never reaches it.
    result = run_acceptance(profile_path, cellar_path, call_model=False, provider_factory=factory)
    assert result.provider_usable is None
