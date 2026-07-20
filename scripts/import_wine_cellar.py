"""
Safe Cellar Import v1: human-controlled CSV -> storage/knowledge/wine_cellar.json.

A local maintenance script, not part of the runtime kernel. It never calls a
model and never uses KnowledgeStore as a write interface - KnowledgeStore
stays read-only end to end (see kernel/knowledge/README.md). The script
writes the destination JSON file directly, only when --write is explicitly
supplied; the default is always a dry run. One CSV represents the complete
desired cellar: a successful write fully replaces the existing document,
atomically, or the import fails and nothing is written at all.

Usage:
    uv run python -m scripts.import_wine_cellar <csv_path>
    uv run python -m scripts.import_wine_cellar <csv_path> --write
"""

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from capabilities.wine.cellar_schema import CELLAR_RECORD_FIELDS, validate_cellar_record

# scripts/import_wine_cellar.py -> scripts -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT_PATH = _PROJECT_ROOT / "storage" / "knowledge" / "wine_cellar.json"

_ID_HEADER = "id"
_REQUIRED_CSV_HEADERS = ("id", "producer", "wine_name", "color", "quantity")
_KNOWN_HEADERS = (_ID_HEADER,) + CELLAR_RECORD_FIELDS

_REQUIRED_STRING_FIELDS = ("producer", "wine_name", "color")
_OPTIONAL_STRING_FIELDS = ("country", "region", "style", "drinking_window", "notes")

_GRAPES_SEPARATOR = ";"
_NV_VINTAGE = "NV"

_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+\.\d+")


class CellarImportError(ValueError):
    """Raised for CSV header, row-conversion, or schema errors during import."""


def _parse_int(value: str, row_num: int, field: str) -> int:
    if not _INT_RE.fullmatch(value):
        raise CellarImportError(f"row {row_num}: field {field!r} must be an integer, got {value!r}")
    return int(value)


def _parse_number(value: str, row_num: int, field: str) -> int | float:
    if _INT_RE.fullmatch(value):
        return int(value)
    if _FLOAT_RE.fullmatch(value):
        return float(value)
    raise CellarImportError(f"row {row_num}: field {field!r} must be a number, got {value!r}")


def _parse_bool(value: str, row_num: int, field: str) -> bool:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise CellarImportError(
        f'row {row_num}: field {field!r} must be "true" or "false", got {value!r}'
    )


def _validate_headers(raw_header_row: list[str]) -> list[str]:
    headers = [name.strip() for name in raw_header_row]

    seen: set[str] = set()
    for name in headers:
        if name == "":
            raise CellarImportError("CSV header row contains a blank header name")
        if name in seen:
            raise CellarImportError(f"CSV header row contains duplicate header {name!r}")
        seen.add(name)
        if name not in _KNOWN_HEADERS:
            raise CellarImportError(f"CSV header row contains unknown header {name!r}")

    missing = [header for header in _REQUIRED_CSV_HEADERS if header not in seen]
    if missing:
        raise CellarImportError(
            f"CSV header row is missing required header(s): {', '.join(missing)}"
        )

    return headers


def _row_to_record(cells: dict[str, str], headers: list[str], row_num: int) -> dict[str, object]:
    record: dict[str, object] = {}

    for field in _REQUIRED_STRING_FIELDS:
        value = cells[field]
        if value == "":
            raise CellarImportError(f"row {row_num}: field {field!r} must not be empty")
        record[field] = value

    record["quantity"] = _parse_int(cells["quantity"], row_num, "quantity")

    if "vintage" in headers:
        vintage_cell = cells["vintage"]
        if vintage_cell != "":
            record["vintage"] = (
                _NV_VINTAGE if vintage_cell == _NV_VINTAGE else _parse_int(vintage_cell, row_num, "vintage")
            )

    for field in _OPTIONAL_STRING_FIELDS:
        if field not in headers:
            continue
        value = cells[field]
        if value != "":
            record[field] = value

    if "grapes" in headers:
        grapes_cell = cells["grapes"]
        if grapes_cell != "":
            items = [item.strip() for item in grapes_cell.split(_GRAPES_SEPARATOR)]
            if any(item == "" for item in items):
                raise CellarImportError(f"row {row_num}: field 'grapes' must not contain empty items")
            record["grapes"] = items

    if "estimated_price" in headers:
        price_cell = cells["estimated_price"]
        if price_cell != "":
            record["estimated_price"] = _parse_number(price_cell, row_num, "estimated_price")

    if "price_currency" in headers:
        currency_cell = cells["price_currency"]
        if currency_cell != "":
            record["price_currency"] = currency_cell

    if "vivino_rating" in headers:
        rating_cell = cells["vivino_rating"]
        if rating_cell != "":
            record["vivino_rating"] = _parse_number(rating_cell, row_num, "vivino_rating")

    if "special_occasion" in headers:
        occasion_cell = cells["special_occasion"]
        if occasion_cell != "":
            record["special_occasion"] = _parse_bool(occasion_cell, row_num, "special_occasion")

    return record


def parse_cellar_csv(csv_path: str | Path) -> dict[str, dict[str, object]]:
    """Parse and validate a cellar CSV into a full replacement cellar document.

    Raises CellarImportError (a ValueError) for any header, row-conversion,
    or schema problem, naming the row and field where applicable. Never
    returns a partial result - either the whole CSV is valid, or nothing is
    returned at all.
    """

    csv_path = Path(csv_path)
    records: dict[str, dict[str, object]] = {}

    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                raw_header_row = next(reader)
            except StopIteration:
                raise CellarImportError(f"{csv_path}: CSV file has no header row")

            headers = _validate_headers(raw_header_row)
            seen_ids: set[str] = set()

            for row_num, row in enumerate(reader, start=2):
                if all(cell.strip() == "" for cell in row):
                    continue

                if len(row) != len(headers):
                    raise CellarImportError(
                        f"row {row_num}: expected {len(headers)} columns, got {len(row)}"
                    )

                cells = {header: value.strip() for header, value in zip(headers, row)}

                record_id = cells[_ID_HEADER]
                if record_id == "":
                    raise CellarImportError(f"row {row_num}: field 'id' must not be empty")
                if record_id in seen_ids:
                    raise CellarImportError(f"row {row_num}: duplicate id {record_id!r}")
                seen_ids.add(record_id)

                record = _row_to_record(cells, headers, row_num)

                try:
                    records[record_id] = validate_cellar_record(record_id, record)
                except ValueError as e:
                    raise CellarImportError(f"row {row_num}: {e}") from e
    except (OSError, csv.Error) as e:
        raise CellarImportError(f"{csv_path}: {e}") from e

    if not records:
        raise CellarImportError(f"{csv_path}: CSV contains no data rows")

    return records


def _write_json_atomic(records: dict[str, dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp_path, output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def import_cellar(
    csv_path: str | Path,
    output_path: str | Path,
    *,
    write: bool = False,
) -> dict[str, dict[str, object]]:
    """Validate a cellar CSV and, if write=True, replace the destination JSON.

    The complete CSV is validated before anything is written. On write, the
    destination's parent directory is created only after validation
    succeeds, and the destination is replaced atomically - a failure at any
    point leaves an existing destination file untouched.
    """

    records = parse_cellar_csv(csv_path)

    if write:
        _write_json_atomic(records, Path(output_path))

    return records


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.import_wine_cellar",
        description="Import a CSV wine cellar into storage/knowledge/wine_cellar.json.",
    )
    parser.add_argument("csv_path", help="Path to the source CSV file.")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the destination JSON file. Without this flag, the import is a dry run.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    output_path: str | Path | None = None,
) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    destination = Path(output_path) if output_path is not None else _DEFAULT_OUTPUT_PATH
    existed_before = destination.exists()

    try:
        records = import_cellar(args.csv_path, destination, write=args.write)
    except (ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    total = len(records)
    active = sum(1 for record in records.values() if record["quantity"] > 0)
    zero = total - active

    print(f"Source CSV: {args.csv_path}")
    print(f"Destination: {destination}")
    print(f"Total holdings: {total}")
    print(f"Active holdings: {active}")
    print(f"Zero-quantity holdings: {zero}")

    if args.write:
        print("Destination replaced." if existed_before else "Destination created.")
    else:
        print(f"Destination already exists: {'yes' if existed_before else 'no'}")
        print("Dry run - no file was written. Re-run with --write to write the file.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
