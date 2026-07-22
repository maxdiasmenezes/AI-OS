"""
Safe Cellar Quantity Update v1: human-controlled quantity-only edit for one
existing holding in storage/knowledge/wine_cellar.json, selected by exact
Cellar ID.

A local maintenance script, not part of the runtime kernel. It never calls a
model and never uses KnowledgeStore as a write interface - KnowledgeStore
stays read-only end to end (see kernel/knowledge/README.md). It is never
reachable from WineCapability, the Orchestrator, the router, or a
conversation. The script writes the destination JSON file directly, only
when --write is explicitly supplied; the default is always a dry run.

Unlike scripts/import_wine_cellar.py (a full-replacement snapshot), this
script changes exactly one existing record's quantity field and leaves every
other field, including unrecognized ones, and every other record untouched.

Usage:
    uv run python -m scripts.update_wine_cellar_quantity <cellar_id> --set <N>
    uv run python -m scripts.update_wine_cellar_quantity <cellar_id> --decrement
    uv run python -m scripts.update_wine_cellar_quantity <cellar_id> --decrement <N>

Add --write to any command to apply the update after validation; without it,
every command is a dry run.
"""

import argparse
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from capabilities.wine.cellar_schema import validate_cellar_record

# scripts/update_wine_cellar_quantity.py -> scripts -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CELLAR_PATH = _PROJECT_ROOT / "storage" / "knowledge" / "wine_cellar.json"


class CellarQuantityError(ValueError):
    """Raised for load, structural, schema, lookup, or argument errors."""


def _load_cellar_document(cellar_path: Path) -> dict[str, object]:
    try:
        text = cellar_path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise CellarQuantityError(f"cellar file not found: {cellar_path}") from e
    except OSError as e:
        raise CellarQuantityError(f"{cellar_path}: {e}") from e

    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise CellarQuantityError(f"{cellar_path}: malformed JSON ({e})") from e

    if not isinstance(document, dict):
        raise CellarQuantityError(f"{cellar_path}: top-level JSON value must be an object")

    return document


def _validate_document(cellar_path: Path, document: dict[str, object]) -> dict[str, dict[str, object]]:
    """Validate every record in a cellar document; abort on the first invalid one."""

    validated: dict[str, dict[str, object]] = {}
    for record_id, record in document.items():
        if not isinstance(record, dict):
            raise CellarQuantityError(
                f"{cellar_path}: cellar record {record_id!r} must be a JSON object"
            )
        validated[record_id] = validate_cellar_record(record_id, record)
    return validated


def _validate_operation(set_quantity: int | None, decrement_by: int | None) -> None:
    """Enforce the --set/--decrement invariants independently of argparse."""

    if set_quantity is None and decrement_by is None:
        raise CellarQuantityError("exactly one of --set or --decrement is required")
    if set_quantity is not None and decrement_by is not None:
        raise CellarQuantityError("--set and --decrement cannot both be given")

    if set_quantity is not None:
        if isinstance(set_quantity, bool) or not isinstance(set_quantity, int) or set_quantity < 0:
            raise CellarQuantityError("--set value must be an integer greater than or equal to zero")
    else:
        if isinstance(decrement_by, bool) or not isinstance(decrement_by, int) or decrement_by <= 0:
            raise CellarQuantityError("--decrement value must be an integer greater than zero")


def _write_json_atomic(document: dict[str, object], cellar_path: Path) -> None:
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        dir=cellar_path.parent, prefix=f".{cellar_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp_path, cellar_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def update_cellar_quantity(
    cellar_path: str | Path,
    cellar_id: str,
    *,
    set_quantity: int | None = None,
    decrement_by: int | None = None,
    write: bool = False,
) -> dict[str, object]:
    """Validate and, optionally, apply a quantity-only update to one holding.

    Selects the target record by exact, case-sensitive equality against the
    top-level JSON key - no normalization, fallback, or fuzzy matching. The
    complete existing cellar is validated before anything is computed, and
    the complete resulting cellar is validated again before anything is
    written. The original parsed document is deep-copied and only the target
    record's `quantity` field is changed, so unknown fields and untouched
    records are preserved exactly. A proposed quantity equal to the current
    quantity is a no-op: the summary reports it, but no file is written even
    when write=True. Raises CellarQuantityError (a ValueError) for argument,
    load, structural, schema, or lookup problems; OSError may propagate from
    the underlying write.
    """

    _validate_operation(set_quantity, decrement_by)

    path = Path(cellar_path)
    document = _load_cellar_document(path)
    validated = _validate_document(path, document)

    if cellar_id not in document:
        raise CellarQuantityError(f"no cellar record found with Cellar ID {cellar_id!r} in {path}")

    current_fields = validated[cellar_id]
    current_quantity = current_fields["quantity"]

    if set_quantity is not None:
        proposed_quantity = set_quantity
        operation_description = f"set quantity to {set_quantity}"
    else:
        proposed_quantity = current_quantity - decrement_by
        operation_description = f"decrement quantity by {decrement_by}"
        if proposed_quantity < 0:
            raise CellarQuantityError(
                f"cannot decrement Cellar ID {cellar_id!r} by {decrement_by}: current "
                f"quantity is {current_quantity}, result would be below zero"
            )

    no_op = proposed_quantity == current_quantity

    updated_document = copy.deepcopy(document)
    updated_document[cellar_id]["quantity"] = proposed_quantity
    _validate_document(path, updated_document)

    written = False
    if write and not no_op:
        _write_json_atomic(updated_document, path)
        written = True

    return {
        "cellar_path": str(path),
        "cellar_id": cellar_id,
        "producer": current_fields["producer"],
        "wine_name": current_fields["wine_name"],
        "vintage": current_fields.get("vintage"),
        "current_quantity": current_quantity,
        "operation_description": operation_description,
        "proposed_quantity": proposed_quantity,
        "no_op": no_op,
        "written": written,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.update_wine_cellar_quantity",
        description=(
            "Change the quantity of one existing wine-cellar holding, "
            "selected by exact Cellar ID."
        ),
    )
    parser.add_argument("cellar_id", help="Exact, case-sensitive Cellar ID of the holding to update.")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--set", type=int, default=None, metavar="N", help="Set the quantity to N (N >= 0).")
    group.add_argument(
        "--decrement",
        nargs="?",
        const=1,
        type=int,
        default=None,
        metavar="N",
        help="Decrement the quantity by N (N > 0). Defaults to 1 when no value is given.",
    )

    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply the update. Without this flag, the command is a dry run.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    cellar_path: str | Path | None = None,
) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    path = Path(cellar_path) if cellar_path is not None else _DEFAULT_CELLAR_PATH

    try:
        result = update_cellar_quantity(
            path,
            args.cellar_id,
            set_quantity=args.set,
            decrement_by=args.decrement,
            write=args.write,
        )
    except (ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Cellar file: {result['cellar_path']}")
    print(f"Cellar ID: {result['cellar_id']}")
    print(f"Producer: {result['producer']}")
    print(f"Wine: {result['wine_name']}")
    if result["vintage"] is not None:
        print(f"Vintage: {result['vintage']}")
    print(f"Current quantity: {result['current_quantity']}")
    print(f"Requested operation: {result['operation_description']}")
    print(f"Proposed quantity: {result['proposed_quantity']}")
    print("Validation: all cellar records passed schema validation.")

    if result["no_op"]:
        print("No-op: quantity is already at the requested value. Cellar file left unchanged.")
    elif result["written"]:
        print("Write complete: the cellar file was atomically updated.")
    else:
        print("Dry run: no file was written. Re-run with --write to apply this update.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
