"""Tests for Safe Cellar Quantity Update v1 (scripts/update_wine_cellar_quantity.py)."""

import json
import os
from pathlib import Path

import pytest

from scripts.update_wine_cellar_quantity import (
    CellarQuantityError,
    main,
    update_cellar_quantity,
)

_TARGET_RECORD = {
    "producer": "Sample Estate",
    "wine_name": "Reserve Red",
    "color": "red",
    "quantity": 5,
    "vintage": 2021,
    "country": "Example Country",
    "region": "Example Region",
    "notes": "Synthetic test row",
    "custom_internal_field": "keep-me",
}

_UNRELATED_RECORD = {
    "producer": "Other Producer",
    "wine_name": "Other White",
    "color": "white",
    "quantity": 2,
    "custom_internal_field": "also-keep-me",
}

_ZERO_RECORD = {
    "producer": "Zero Producer",
    "wine_name": "Zero Wine",
    "color": "red",
    "quantity": 0,
}

_BASE_DOCUMENT = {
    "sample-red-2021": _TARGET_RECORD,
    "other-white-2022": _UNRELATED_RECORD,
    "zero-quantity-wine": _ZERO_RECORD,
}


def _write_cellar(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _base_cellar(tmp_path: Path) -> Path:
    return _write_cellar(tmp_path / "wine_cellar.json", _BASE_DOCUMENT)


# --- Successful operations --------------------------------------------------


def test_set_dry_run_calculates_correct_proposed_quantity(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=9)

    assert result["current_quantity"] == 5
    assert result["proposed_quantity"] == 9
    assert result["no_op"] is False
    assert result["written"] is False


def test_set_with_write_updates_target_quantity(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=9, write=True)

    assert result["written"] is True
    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert data["sample-red-2021"]["quantity"] == 9


def test_decrement_without_number_defaults_to_one(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = update_cellar_quantity(cellar_path, "sample-red-2021", decrement_by=1, write=True)

    assert result["proposed_quantity"] == 4
    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert data["sample-red-2021"]["quantity"] == 4


def test_decrement_with_explicit_positive_number_works(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = update_cellar_quantity(cellar_path, "sample-red-2021", decrement_by=3, write=True)

    assert result["proposed_quantity"] == 2
    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert data["sample-red-2021"]["quantity"] == 2


def test_decrementing_exactly_to_zero_works(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = update_cellar_quantity(cellar_path, "sample-red-2021", decrement_by=5, write=True)

    assert result["proposed_quantity"] == 0
    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert data["sample-red-2021"]["quantity"] == 0
    assert "sample-red-2021" in data


def test_required_and_optional_fields_remain_present_after_write(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    record = data["sample-red-2021"]
    for field in ("producer", "wine_name", "color", "quantity", "vintage", "country", "region", "notes"):
        assert field in record


def test_unknown_fields_remain_present_after_write(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert data["sample-red-2021"]["custom_internal_field"] == "keep-me"


def test_untouched_records_remain_semantically_unchanged(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert data["other-white-2022"] == _UNRELATED_RECORD
    assert data["zero-quantity-wine"] == _ZERO_RECORD


def test_only_selected_quantity_changes(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    expected_target = dict(_TARGET_RECORD)
    expected_target["quantity"] = 1
    assert data["sample-red-2021"] == expected_target


# --- Argument and value validation ------------------------------------------


def test_both_operations_rejected(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, decrement_by=1)


def test_neither_operation_rejected(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021")


def test_negative_set_is_rejected(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=-1)


def test_zero_decrement_is_rejected(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", decrement_by=0)


def test_negative_decrement_is_rejected(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", decrement_by=-2)


def test_non_integer_set_is_rejected(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1.5)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=True)


def test_non_integer_decrement_is_rejected(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", decrement_by=1.5)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", decrement_by=True)


def test_result_below_zero_is_rejected_and_never_clamped(tmp_path):
    cellar_path = _base_cellar(tmp_path)
    original_bytes = cellar_path.read_bytes()

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", decrement_by=99, write=True)

    assert cellar_path.read_bytes() == original_bytes


# --- Lookup behavior ---------------------------------------------------------


def test_exact_cellar_id_works(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1)

    assert result["cellar_id"] == "sample-red-2021"


def test_different_case_does_not_match(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "Sample-Red-2021", set_quantity=1)


def test_unknown_id_is_rejected_with_clear_message(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError) as exc_info:
        update_cellar_quantity(cellar_path, "does-not-exist", set_quantity=1)

    assert "does-not-exist" in str(exc_info.value)


def test_no_producer_or_wine_name_fallback_occurs(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "Reserve Red", set_quantity=1)

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "Sample Estate", set_quantity=1)


# --- Input validation ---------------------------------------------------------


def test_missing_cellar_file_is_rejected(tmp_path):
    cellar_path = tmp_path / "does-not-exist.json"

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1)


def test_malformed_json_is_rejected(tmp_path):
    cellar_path = tmp_path / "wine_cellar.json"
    cellar_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1)


def test_non_object_top_level_is_rejected(tmp_path):
    cellar_path = tmp_path / "wine_cellar.json"
    cellar_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1)


def test_non_object_record_is_rejected(tmp_path):
    cellar_path = tmp_path / "wine_cellar.json"
    cellar_path.write_text(json.dumps({"sample-red-2021": "not-an-object"}), encoding="utf-8")

    with pytest.raises(CellarQuantityError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1)


def test_invalid_target_record_blocks_operation(tmp_path):
    document = dict(_BASE_DOCUMENT)
    document["sample-red-2021"] = {**_TARGET_RECORD, "quantity": "not-a-number"}
    cellar_path = _write_cellar(tmp_path / "wine_cellar.json", document)

    with pytest.raises(ValueError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1)


def test_invalid_unrelated_record_blocks_operation(tmp_path):
    document = dict(_BASE_DOCUMENT)
    document["other-white-2022"] = {**_UNRELATED_RECORD, "producer": ""}
    cellar_path = _write_cellar(tmp_path / "wine_cellar.json", document)

    with pytest.raises(ValueError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1)


def test_invalid_zero_quantity_record_blocks_operation(tmp_path):
    document = dict(_BASE_DOCUMENT)
    document["zero-quantity-wine"] = {**_ZERO_RECORD, "quantity": -1}
    cellar_path = _write_cellar(tmp_path / "wine_cellar.json", document)

    with pytest.raises(ValueError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1)


def test_schema_errors_identify_record_and_field(tmp_path):
    document = dict(_BASE_DOCUMENT)
    document["other-white-2022"] = {**_UNRELATED_RECORD, "producer": ""}
    cellar_path = _write_cellar(tmp_path / "wine_cellar.json", document)

    with pytest.raises(ValueError) as exc_info:
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1)

    message = str(exc_info.value)
    assert "other-white-2022" in message
    assert "producer" in message


# --- Dry-run and no-op safety -------------------------------------------------


def test_dry_run_leaves_source_byte_for_byte_unchanged(tmp_path):
    cellar_path = _base_cellar(tmp_path)
    original_bytes = cellar_path.read_bytes()

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=False)

    assert cellar_path.read_bytes() == original_bytes


def test_dry_run_creates_no_temp_file(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=False)

    assert list(tmp_path.iterdir()) == [cellar_path]


def test_set_to_existing_quantity_is_successful_no_op(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=5)

    assert result["no_op"] is True
    assert result["proposed_quantity"] == 5
    assert result["written"] is False


def test_no_op_with_write_leaves_source_byte_for_byte_unchanged(tmp_path):
    cellar_path = _base_cellar(tmp_path)
    original_bytes = cellar_path.read_bytes()

    result = update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=5, write=True)

    assert result["no_op"] is True
    assert result["written"] is False
    assert cellar_path.read_bytes() == original_bytes


def test_no_op_creates_no_temp_file(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=5, write=True)

    assert list(tmp_path.iterdir()) == [cellar_path]


# --- Write behavior ------------------------------------------------------------


def test_write_uses_deterministic_json_with_sorted_keys(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    text = cellar_path.read_text(encoding="utf-8")
    data = json.loads(text)
    reserialized = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert text == reserialized


def test_output_has_exactly_one_trailing_newline(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    text = cellar_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_write_atomically_replaces_original(tmp_path, monkeypatch):
    cellar_path = _base_cellar(tmp_path)
    replace_args = []
    real_replace = os.replace

    def _spy_replace(src, dst, *args, **kwargs):
        # At the moment of replacement, the temp file must already hold the
        # full new content, and the destination must still be the original.
        replace_args.append((Path(src).read_text(encoding="utf-8"), dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _spy_replace)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    assert len(replace_args) == 1
    tmp_content, dst = replace_args[0]
    assert json.loads(tmp_content)["sample-red-2021"]["quantity"] == 1
    assert Path(dst) == cellar_path
    assert cellar_path.exists()
    assert list(tmp_path.iterdir()) == [cellar_path]


def test_no_merge_or_record_deletion_occurs(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert set(data.keys()) == set(_BASE_DOCUMENT.keys())


def test_no_temp_file_remains_after_successful_write(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    assert list(tmp_path.iterdir()) == [cellar_path]


def test_simulated_temp_file_write_failure_preserves_original(tmp_path, monkeypatch):
    cellar_path = _base_cellar(tmp_path)
    original_bytes = cellar_path.read_bytes()

    def _boom_fdopen(fd, *args, **kwargs):
        os.close(fd)
        raise OSError("simulated temp-file write failure")

    monkeypatch.setattr(os, "fdopen", _boom_fdopen)

    with pytest.raises(OSError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    assert cellar_path.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [cellar_path]


def test_simulated_replace_failure_preserves_original_and_removes_temp(tmp_path, monkeypatch):
    cellar_path = _base_cellar(tmp_path)
    original_bytes = cellar_path.read_bytes()

    def _boom_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom_replace)

    with pytest.raises(OSError):
        update_cellar_quantity(cellar_path, "sample-red-2021", set_quantity=1, write=True)

    assert cellar_path.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [cellar_path]


# --- CLI behavior ---------------------------------------------------------------


def test_main_returns_0_for_successful_dry_run(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = main(["sample-red-2021", "--set", "9"], cellar_path=cellar_path)

    assert result == 0
    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert data["sample-red-2021"]["quantity"] == 5


def test_main_returns_0_for_successful_write(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = main(["sample-red-2021", "--set", "9", "--write"], cellar_path=cellar_path)

    assert result == 0
    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert data["sample-red-2021"]["quantity"] == 9


def test_main_returns_0_for_no_op(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = main(["sample-red-2021", "--set", "5", "--write"], cellar_path=cellar_path)

    assert result == 0


def test_main_returns_1_and_writes_stderr_for_expected_errors(tmp_path, capsys):
    cellar_path = _base_cellar(tmp_path)

    result = main(["does-not-exist", "--set", "1"], cellar_path=cellar_path)

    assert result == 1
    captured = capsys.readouterr()
    assert captured.err != ""
    assert captured.out == ""


def test_dry_run_output_contains_quantities_and_no_write_notice(tmp_path, capsys):
    cellar_path = _base_cellar(tmp_path)

    main(["sample-red-2021", "--set", "9"], cellar_path=cellar_path)

    out = capsys.readouterr().out
    assert "Current quantity: 5" in out
    assert "Proposed quantity: 9" in out
    assert "no file was written" in out.lower()
    assert "--write" in out


def test_write_output_says_file_updated(tmp_path, capsys):
    cellar_path = _base_cellar(tmp_path)

    main(["sample-red-2021", "--set", "9", "--write"], cellar_path=cellar_path)

    out = capsys.readouterr().out
    assert "atomically updated" in out.lower()


def test_no_op_output_says_file_unchanged(tmp_path, capsys):
    cellar_path = _base_cellar(tmp_path)

    main(["sample-red-2021", "--set", "5", "--write"], cellar_path=cellar_path)

    out = capsys.readouterr().out
    assert "unchanged" in out.lower()


def test_main_accepts_injected_cellar_path_and_never_touches_repo_storage(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    result = main(["sample-red-2021", "--decrement"], cellar_path=cellar_path)

    assert result == 0
    repo_storage_file = Path(__file__).resolve().parents[2] / "storage" / "knowledge" / "wine_cellar.json"
    assert not repo_storage_file.exists()


def test_cli_both_operations_is_an_argparse_error(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(SystemExit):
        main(["sample-red-2021", "--set", "1", "--decrement", "1"], cellar_path=cellar_path)


def test_cli_neither_operation_is_an_argparse_error(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    with pytest.raises(SystemExit):
        main(["sample-red-2021"], cellar_path=cellar_path)


def test_cli_decrement_defaults_to_one_end_to_end(tmp_path):
    cellar_path = _base_cellar(tmp_path)

    main(["sample-red-2021", "--decrement", "--write"], cellar_path=cellar_path)

    data = json.loads(cellar_path.read_text(encoding="utf-8"))
    assert data["sample-red-2021"]["quantity"] == 4
