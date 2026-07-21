"""
Shared wine-cellar record schema.

Domain-level validation for a single cellar record - one wine holding, keyed
by its own record id. Used by both WineCapability's model-backed fallback
(read path) and scripts/import_wine_cellar.py (the human-invoked write
path), so the two never drift on what a valid cellar record is.

Prompt formatting, cellar size limiting, cellar sorting, and fallback
behavior are not schema concerns and stay private to WineCapability.
"""

CELLAR_REQUIRED_FIELDS = ("producer", "wine_name", "color", "quantity")
CELLAR_OPTIONAL_FIELDS = (
    "vintage",
    "country",
    "region",
    "style",
    "grapes",
    "estimated_price",
    "price_currency",
    "vivino_rating",
    "drinking_window",
    "notes",
    "special_occasion",
)
CELLAR_RECORD_FIELDS = CELLAR_REQUIRED_FIELDS + CELLAR_OPTIONAL_FIELDS

_CELLAR_REQUIRED_STRING_FIELDS = ("producer", "wine_name", "color")
_CELLAR_OPTIONAL_STRING_FIELDS = ("country", "region", "style", "drinking_window", "notes")

_CELLAR_VINTAGE_MIN = 1800
_CELLAR_VINTAGE_MAX = 2100
_CELLAR_VIVINO_RATING_MIN = 0
_CELLAR_VIVINO_RATING_MAX = 5


def _is_number(value: object) -> bool:
    """True for int or float, excluding bool (a bool is technically an int)."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_cellar_record(record_id: str, record: dict[str, object]) -> dict[str, object]:
    """Validate one cellar record and return only its recognized fields.

    Required fields (producer, wine_name, color, quantity) are validated for
    every record, including zero-quantity ones, since callers decide
    inclusion after validation. Unknown fields are ignored. An invalid
    recognized field raises ValueError naming the record and the field,
    never coerced into another type.
    """

    def _fail(field: str, detail: str) -> None:
        raise ValueError(f"wine cellar record {record_id!r} field {field!r} {detail}")

    fields: dict[str, object] = {}

    for field in _CELLAR_REQUIRED_STRING_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or value == "":
            _fail(field, "must be a non-empty string")
        fields[field] = value

    quantity = record.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        _fail("quantity", "must be an integer greater than or equal to zero")
    fields["quantity"] = quantity

    if "vintage" in record:
        vintage = record["vintage"]
        is_valid_year = (
            isinstance(vintage, int)
            and not isinstance(vintage, bool)
            and _CELLAR_VINTAGE_MIN <= vintage <= _CELLAR_VINTAGE_MAX
        )
        if not is_valid_year and vintage != "NV":
            _fail(
                "vintage",
                f'must be an integer from {_CELLAR_VINTAGE_MIN} through '
                f'{_CELLAR_VINTAGE_MAX}, or the exact string "NV"',
            )
        fields["vintage"] = vintage

    for field in _CELLAR_OPTIONAL_STRING_FIELDS:
        if field not in record:
            continue
        value = record[field]
        if not isinstance(value, str) or value == "":
            _fail(field, "must be a non-empty string")
        fields[field] = value

    if "grapes" in record:
        grapes = record["grapes"]
        if not isinstance(grapes, list) or not all(
            isinstance(item, str) and item != "" for item in grapes
        ):
            _fail("grapes", "must be a list of non-empty strings")
        fields["grapes"] = grapes

    has_price = "estimated_price" in record
    has_currency = "price_currency" in record
    if has_price != has_currency:
        raise ValueError(
            f"wine cellar record {record_id!r} fields 'estimated_price' and "
            "'price_currency' must either both be supplied or both be absent"
        )

    if has_price:
        price = record["estimated_price"]
        if not _is_number(price) or price < 0:
            _fail("estimated_price", "must be a non-negative number")
        fields["estimated_price"] = price

        currency = record["price_currency"]
        if not isinstance(currency, str) or currency == "":
            _fail("price_currency", "must be a non-empty string")
        fields["price_currency"] = currency

    if "vivino_rating" in record:
        rating = record["vivino_rating"]
        if not _is_number(rating) or not (
            _CELLAR_VIVINO_RATING_MIN <= rating <= _CELLAR_VIVINO_RATING_MAX
        ):
            _fail(
                "vivino_rating",
                f"must be a number from {_CELLAR_VIVINO_RATING_MIN} through "
                f"{_CELLAR_VIVINO_RATING_MAX}",
            )
        fields["vivino_rating"] = rating

    if "special_occasion" in record:
        special_occasion = record["special_occasion"]
        if not isinstance(special_occasion, bool):
            _fail("special_occasion", "must be a boolean")
        fields["special_occasion"] = special_occasion

    return fields
