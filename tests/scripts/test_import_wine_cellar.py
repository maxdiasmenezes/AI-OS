"""Tests for Safe Cellar Import v1 (scripts/import_wine_cellar.py)."""

import json
import os
from pathlib import Path

import pytest

from capabilities.wine.cellar_schema import validate_cellar_record
from scripts.import_wine_cellar import (
    CellarImportError,
    import_cellar,
    main,
    parse_cellar_csv,
)

_FULL_HEADER = (
    "id,producer,wine_name,color,quantity,vintage,country,region,style,grapes,"
    "estimated_price,price_currency,vivino_rating,drinking_window,special_occasion,notes"
)
_MINIMAL_HEADER = "id,producer,wine_name,color,quantity"

_SAMPLE_ROW = (
    "sample-red-2021,Sample Estate,Reserve Red,red,3,2021,Example Country,"
    "Example Region,medium-bodied red,Sample Grape;Other Grape,30,USD,3.8,"
    "2025-2029,false,Synthetic test row"
)


def _write_csv(path: Path, content: str, *, encoding: str = "utf-8") -> Path:
    path.write_text(content, encoding=encoding, newline="")
    return path


# --- Parsing and successful conversion -------------------------------------


def test_full_valid_csv_converts_to_expected_structure(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")

    records = parse_cellar_csv(csv_path)

    assert records == {
        "sample-red-2021": {
            "producer": "Sample Estate",
            "wine_name": "Reserve Red",
            "color": "red",
            "quantity": 3,
            "vintage": 2021,
            "country": "Example Country",
            "region": "Example Region",
            "style": "medium-bodied red",
            "grapes": ["Sample Grape", "Other Grape"],
            "estimated_price": 30,
            "price_currency": "USD",
            "vivino_rating": 3.8,
            "drinking_window": "2025-2029",
            "special_occasion": False,
            "notes": "Synthetic test row",
        }
    }


def test_required_only_record_converts(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER + "\n" + "minimal-white,Minimal Producer,Basic White,white,1\n",
    )

    records = parse_cellar_csv(csv_path)

    assert records == {
        "minimal-white": {
            "producer": "Minimal Producer",
            "wine_name": "Basic White",
            "color": "white",
            "quantity": 1,
        }
    }


def test_optional_blank_cells_are_omitted(tmp_path):
    row = "sample-id,Sample Estate,Reserve Red,red,2,,,,,,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    record = records["sample-id"]
    assert set(record.keys()) == {"producer", "wine_name", "color", "quantity"}


def test_utf8_content_is_preserved(tmp_path):
    row = "chateau-id,Château Margaux,Côtes du Rhône,red,1,,,,,,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    assert records["chateau-id"]["producer"] == "Château Margaux"
    assert records["chateau-id"]["wine_name"] == "Côtes du Rhône"


def test_utf8_bom_input_works(tmp_path):
    content = _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n"
    csv_path = tmp_path / "cellar.csv"
    csv_path.write_bytes(content.encode("utf-8-sig"))

    records = parse_cellar_csv(csv_path)

    assert "sample-red-2021" in records


def test_surrounding_cell_whitespace_is_trimmed(tmp_path):
    row = "  sample-id  ,  Sample Estate  ,  Reserve Red  ,  red  ,  2  ,,,,,,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    assert "sample-id" in records
    assert records["sample-id"]["producer"] == "Sample Estate"


def test_internal_whitespace_is_preserved(tmp_path):
    row = "sample-id,Sample  Estate,Reserve Red,red,2,,,,,,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    assert records["sample-id"]["producer"] == "Sample  Estate"


def test_grapes_split_on_semicolons_preserve_order(tmp_path):
    row = "sample-id,Sample Estate,Reserve Red,red,2,,,,,Merlot;Cabernet Franc;Petit Verdot,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    assert records["sample-id"]["grapes"] == ["Merlot", "Cabernet Franc", "Petit Verdot"]


def test_quantity_becomes_int(tmp_path):
    row = "sample-id,Sample Estate,Reserve Red,red,7,,,,,,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    assert records["sample-id"]["quantity"] == 7
    assert isinstance(records["sample-id"]["quantity"], int)


@pytest.mark.parametrize(
    "vintage_cell, expected",
    [("2020", 2020), ("NV", "NV")],
)
def test_vintage_becomes_int_or_remains_nv(tmp_path, vintage_cell, expected):
    row = f"sample-id,Sample Estate,Reserve Red,red,2,{vintage_cell},,,,,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    assert records["sample-id"]["vintage"] == expected


def test_estimated_price_and_vivino_rating_become_numeric(tmp_path):
    row = "sample-id,Sample Estate,Reserve Red,red,2,,,,,,30,USD,3.8,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    record = records["sample-id"]
    assert record["estimated_price"] == 30
    assert isinstance(record["estimated_price"], int)
    assert record["vivino_rating"] == 3.8
    assert isinstance(record["vivino_rating"], float)


def test_estimated_price_decimal_becomes_float(tmp_path):
    row = "sample-id,Sample Estate,Reserve Red,red,2,,,,,,29.99,USD,4,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    record = records["sample-id"]
    assert record["estimated_price"] == 29.99
    assert isinstance(record["estimated_price"], float)
    assert record["vivino_rating"] == 4
    assert isinstance(record["vivino_rating"], int)


@pytest.mark.parametrize("cell, expected", [("true", True), ("false", False), ("True", True), ("FALSE", False)])
def test_special_occasion_parses_true_and_false_case_insensitively(tmp_path, cell, expected):
    row = f"sample-id,Sample Estate,Reserve Red,red,2,,,,,,,,,,{cell},"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    records = parse_cellar_csv(csv_path)

    assert records["sample-id"]["special_occasion"] is expected


def test_record_id_becomes_json_key_and_absent_from_record(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")

    records = parse_cellar_csv(csv_path)

    assert "sample-red-2021" in records
    assert "id" not in records["sample-red-2021"]


def test_output_record_keys_are_deterministic(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER
        + "\n"
        + "zzz-wine,Producer,Wine,red,1\n"
        + "aaa-wine,Producer,Wine,red,1\n",
    )

    records = parse_cellar_csv(csv_path)

    assert sorted(records.keys()) == ["aaa-wine", "zzz-wine"]


# --- Headers and row safety -------------------------------------------------


def test_missing_header_row_is_rejected(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", "")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


@pytest.mark.parametrize("missing", ["id", "producer", "wine_name", "color", "quantity"])
def test_missing_each_required_header_is_rejected(tmp_path, missing):
    headers = [h for h in _MINIMAL_HEADER.split(",") if h != missing]
    csv_path = _write_csv(
        tmp_path / "cellar.csv", ",".join(headers) + "\nvalue1,value2,value3,value4\n"
    )

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


def test_unknown_header_is_rejected(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER + ",favorite_glassware\nid1,Producer,Wine,red,1,Riedel\n",
    )

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


def test_duplicate_headers_are_rejected(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        "id,producer,wine_name,color,quantity,color\nid1,Producer,Wine,red,1,red\n",
    )

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


def test_blank_header_is_rejected(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        "id,producer,wine_name,color,quantity,\nid1,Producer,Wine,red,1,extra\n",
    )

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


def test_duplicate_ids_are_rejected(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER
        + "\n"
        + "dup-id,Producer A,Wine A,red,1\n"
        + "dup-id,Producer B,Wine B,white,2\n",
    )

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


def test_all_blank_rows_are_ignored(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER + "\nid1,Producer,Wine,red,1\n,,,,\n",
    )

    records = parse_cellar_csv(csv_path)

    assert list(records.keys()) == ["id1"]


def test_header_only_csv_is_rejected(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _MINIMAL_HEADER + "\n")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


# --- Invalid conversion and validation --------------------------------------


@pytest.mark.parametrize("quantity_cell", ["1.5", "true", "", "abc"])
def test_invalid_quantity_is_rejected(tmp_path, quantity_cell):
    row = f"sample-id,Sample Estate,Reserve Red,red,{quantity_cell},,,,,,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


def test_negative_quantity_is_rejected_via_schema(tmp_path):
    row = "sample-id,Sample Estate,Reserve Red,red,-1,,,,,,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


@pytest.mark.parametrize("vintage_cell", ["1999.5", "nv", "abc", "1799", "2101"])
def test_invalid_vintage_is_rejected(tmp_path, vintage_cell):
    row = f"sample-id,Sample Estate,Reserve Red,red,2,{vintage_cell},,,,,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


def test_invalid_grapes_trailing_semicolon_is_rejected(tmp_path):
    row = "sample-id,Sample Estate,Reserve Red,red,2,,,,,Merlot;,,,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


@pytest.mark.parametrize("price_cell", ["abc", "$30", "30,00"])
def test_invalid_estimated_price_is_rejected(tmp_path, price_cell):
    row = f"sample-id,Sample Estate,Reserve Red,red,2,,,,,,{price_cell},USD,,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


@pytest.mark.parametrize("rating_cell", ["abc", "5.1", "-0.1"])
def test_invalid_vivino_rating_is_rejected(tmp_path, rating_cell):
    row = f"sample-id,Sample Estate,Reserve Red,red,2,,,,,,,,{rating_cell},,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


@pytest.mark.parametrize("occasion_cell", ["yes", "no", "1", "0"])
def test_invalid_special_occasion_is_rejected(tmp_path, occasion_cell):
    row = f"sample-id,Sample Estate,Reserve Red,red,2,,,,,,,,,,{occasion_cell},"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


def test_schema_errors_propagate_with_row_and_field_context(tmp_path):
    row = "sample-id,Sample Estate,Reserve Red,red,2,,,,,,,,5.5,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    with pytest.raises(CellarImportError) as exc_info:
        parse_cellar_csv(csv_path)

    message = str(exc_info.value)
    assert "row 2" in message
    assert "vivino_rating" in message


def test_one_invalid_row_rejects_entire_multi_row_import(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER
        + "\n"
        + "good-id,Producer,Wine,red,1\n"
        + "bad-id,Producer,Wine,red,not-a-number\n"
        + "another-good-id,Producer,Wine,red,2\n",
    )

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)


# --- Write behavior ----------------------------------------------------------


def test_dry_run_never_creates_output_file_or_parent_directory(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")
    output_path = tmp_path / "out" / "wine_cellar.json"

    import_cellar(csv_path, output_path, write=False)

    assert not output_path.exists()
    assert not output_path.parent.exists()


def test_write_creates_directory_and_file_only_after_validation(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER + "\nbad-id,Producer,Wine,red,not-a-number\n",
    )
    output_path = tmp_path / "out" / "wine_cellar.json"

    with pytest.raises(CellarImportError):
        import_cellar(csv_path, output_path, write=True)

    assert not output_path.exists()
    assert not output_path.parent.exists()


def test_generated_json_has_expected_structure_and_trailing_newline(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")
    output_path = tmp_path / "out" / "wine_cellar.json"

    import_cellar(csv_path, output_path, write=True)

    text = output_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")

    data = json.loads(text)
    assert data == {
        "sample-red-2021": {
            "producer": "Sample Estate",
            "wine_name": "Reserve Red",
            "color": "red",
            "quantity": 3,
            "vintage": 2021,
            "country": "Example Country",
            "region": "Example Region",
            "style": "medium-bodied red",
            "grapes": ["Sample Grape", "Other Grape"],
            "estimated_price": 30,
            "price_currency": "USD",
            "vivino_rating": 3.8,
            "drinking_window": "2025-2029",
            "special_occasion": False,
            "notes": "Synthetic test row",
        }
    }


def test_write_fully_replaces_existing_cellar_rather_than_merging(tmp_path):
    output_path = tmp_path / "wine_cellar.json"
    output_path.write_text(
        json.dumps({"old-wine": {"producer": "Old", "wine_name": "Old Wine", "color": "red", "quantity": 5}}),
        encoding="utf-8",
    )

    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER + "\nnew-wine,New Producer,New Wine,white,1\n",
    )

    import_cellar(csv_path, output_path, write=True)

    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert "old-wine" not in data
    assert "new-wine" in data


def test_failed_import_leaves_existing_destination_unchanged(tmp_path):
    output_path = tmp_path / "wine_cellar.json"
    original_bytes = json.dumps({"existing": {"producer": "P", "wine_name": "W", "color": "red", "quantity": 1}}).encode(
        "utf-8"
    )
    output_path.write_bytes(original_bytes)

    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER + "\nbad-id,Producer,Wine,red,not-a-number\n",
    )

    with pytest.raises(CellarImportError):
        import_cellar(csv_path, output_path, write=True)

    assert output_path.read_bytes() == original_bytes


def test_no_temp_file_remains_after_successful_write(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")
    output_dir = tmp_path / "out"
    output_path = output_dir / "wine_cellar.json"

    import_cellar(csv_path, output_path, write=True)

    remaining = list(output_dir.iterdir())
    assert remaining == [output_path]


def test_simulated_replacement_failure_removes_temp_file_and_preserves_destination(tmp_path, monkeypatch):
    output_path = tmp_path / "wine_cellar.json"
    original_bytes = json.dumps({"existing": {"producer": "P", "wine_name": "W", "color": "red", "quantity": 1}}).encode(
        "utf-8"
    )
    output_path.write_bytes(original_bytes)

    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")

    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(OSError):
        import_cellar(csv_path, output_path, write=True)

    assert output_path.read_bytes() == original_bytes
    remaining = set(tmp_path.iterdir())
    assert remaining == {output_path, csv_path}


# --- CLI behavior -------------------------------------------------------------


def test_main_returns_0_for_successful_dry_run(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")
    output_path = tmp_path / "out" / "wine_cellar.json"

    result = main([str(csv_path)], output_path=output_path)

    assert result == 0
    assert not output_path.exists()


def test_dry_run_output_includes_counts_existence_and_no_write_notice(tmp_path, capsys):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER + "\nactive-id,Producer,Wine,red,1\nzero-id,Producer,Wine,red,0\n",
    )
    output_path = tmp_path / "wine_cellar.json"

    main([str(csv_path)], output_path=output_path)

    out = capsys.readouterr().out
    assert "Total holdings: 2" in out
    assert "Active holdings: 1" in out
    assert "Zero-quantity holdings: 1" in out
    assert "already exists: no" in out
    assert "no file was written" in out.lower()
    assert "--write" in out


def test_main_returns_0_for_successful_write(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")
    output_path = tmp_path / "out" / "wine_cellar.json"

    result = main([str(csv_path), "--write"], output_path=output_path)

    assert result == 0
    assert output_path.exists()


def test_write_output_says_created_or_replaced(tmp_path, capsys):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")
    output_path = tmp_path / "wine_cellar.json"

    main([str(csv_path), "--write"], output_path=output_path)
    created_output = capsys.readouterr().out
    assert "created" in created_output.lower()

    main([str(csv_path), "--write"], output_path=output_path)
    replaced_output = capsys.readouterr().out
    assert "replaced" in replaced_output.lower()


def test_main_returns_1_and_prints_stderr_for_expected_import_errors(tmp_path, capsys):
    csv_path = _write_csv(
        tmp_path / "cellar.csv",
        _MINIMAL_HEADER + "\nbad-id,Producer,Wine,red,not-a-number\n",
    )
    output_path = tmp_path / "wine_cellar.json"

    result = main([str(csv_path)], output_path=output_path)

    assert result == 1
    captured = capsys.readouterr()
    assert captured.err != ""
    assert captured.out == ""


def test_main_returns_1_for_nonexistent_csv_path(tmp_path, capsys):
    output_path = tmp_path / "wine_cellar.json"

    result = main([str(tmp_path / "does-not-exist.csv")], output_path=output_path)

    assert result == 1
    assert capsys.readouterr().err != ""


def test_cli_injects_tmp_path_output_and_never_touches_repository_storage(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")
    output_path = tmp_path / "isolated" / "wine_cellar.json"

    result = main([str(csv_path), "--write"], output_path=output_path)

    assert result == 0
    assert output_path.exists()

    repo_storage_file = Path(__file__).resolve().parents[2] / "storage" / "knowledge" / "wine_cellar.json"
    assert not repo_storage_file.exists()


# --- Shared-schema regression -------------------------------------------------


def test_importer_accepts_record_that_validate_cellar_record_accepts(tmp_path):
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + _SAMPLE_ROW + "\n")

    records = parse_cellar_csv(csv_path)
    record = records["sample-red-2021"]

    # validate_cellar_record must accept the importer's own converted output
    # unchanged - the importer never produces a record the shared validator
    # would then reject.
    assert validate_cellar_record("sample-red-2021", record) == record


def test_importer_refuses_record_that_shared_validator_refuses(tmp_path):
    # vivino_rating of 5.5 is out of the shared validator's 0-5 range.
    invalid_record = {
        "producer": "Sample Estate",
        "wine_name": "Reserve Red",
        "color": "red",
        "quantity": 1,
        "vivino_rating": 5.5,
    }
    with pytest.raises(ValueError):
        validate_cellar_record("sample-red-2021", invalid_record)

    row = "sample-red-2021,Sample Estate,Reserve Red,red,1,,,,,,,,5.5,,,"
    csv_path = _write_csv(tmp_path / "cellar.csv", _FULL_HEADER + "\n" + row + "\n")

    with pytest.raises(CellarImportError):
        parse_cellar_csv(csv_path)
