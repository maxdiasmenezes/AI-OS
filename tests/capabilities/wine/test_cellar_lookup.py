"""Tests for Deterministic Cellar Lookup v1 (capabilities/wine/cellar_lookup.py)."""

import pytest

from capabilities.wine.cellar_lookup import answer_cellar_query, parse_cellar_query

# --- Query parsing: total --------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "How many bottles of wine do I have in total?",
        "How many bottles are in my cellar?",
        "How many bottles do I have in my cellar?",
        "how many bottles of wine do i have in total?",
        "HOW MANY BOTTLES ARE IN MY CELLAR?",
        "How   many   bottles   are   in   my   cellar?",
        "How many bottles are in my cellar",
        "How many bottles are in my cellar.",
        "How many bottles are in my cellar!",
    ],
)
def test_total_forms_parse(prompt):
    assert parse_cellar_query(prompt) == ("total", None)


# --- Query parsing: quantity ------------------------------------------------


@pytest.mark.parametrize(
    "prompt, expected_target",
    [
        ("How many bottles of Reserve Red are in my cellar?", "Reserve Red"),
        ("How many bottles of Reserve Red do I have in my cellar?", "Reserve Red"),
        ("How many bottles of Reserve Red wine do I have?", "Reserve Red"),
        ("how many bottles of reserve red are in my cellar?", "reserve red"),
        ("How many bottles of Sample Estate Reserve Red are in my cellar?", "Sample Estate Reserve Red"),
    ],
)
def test_quantity_forms_parse(prompt, expected_target):
    assert parse_cellar_query(prompt) == ("quantity", expected_target)


def test_quantity_form_with_repeated_whitespace_parses():
    result = parse_cellar_query("How many bottles of  Reserve   Red  are in my cellar?")
    assert result == ("quantity", "Reserve Red")


# --- Query parsing: ownership -----------------------------------------------


@pytest.mark.parametrize(
    "prompt, expected_target",
    [
        ("Do I have Reserve Red in my cellar?", "Reserve Red"),
        ("Do I own any Reserve Red wine?", "Reserve Red"),
        ("Do I have any Reserve Red wine?", "Reserve Red"),
        ("Do I have any Reserve Red in my cellar?", "Reserve Red"),
        ("DO I HAVE ANY BURGUNDY WINE?", "BURGUNDY"),
    ],
)
def test_ownership_forms_parse(prompt, expected_target):
    assert parse_cellar_query(prompt) == ("ownership", expected_target)


# --- Query parsing: producer listing ----------------------------------------


@pytest.mark.parametrize(
    "prompt, expected_target",
    [
        ("Show me my wines from Sample Estate.", "Sample Estate"),
        ("What wines do I have from Sample Estate?", "Sample Estate"),
        ("What do I have from Sample Estate in my cellar?", "Sample Estate"),
        ("show me my WINES from sample estate.", "sample estate"),
    ],
)
def test_producer_listing_forms_parse(prompt, expected_target):
    assert parse_cellar_query(prompt) == ("producer", expected_target)


# --- Query parsing: vintage listing -----------------------------------------


@pytest.mark.parametrize(
    "prompt, expected_target",
    [
        ("What vintages of Reserve Red are in my cellar?", "Reserve Red"),
        ("What vintages of Reserve Red wine do I have?", "Reserve Red"),
        ("WHAT VINTAGES OF RESERVE RED ARE IN MY CELLAR?", "RESERVE RED"),
    ],
)
def test_vintage_listing_forms_parse(prompt, expected_target):
    assert parse_cellar_query(prompt) == ("vintage", expected_target)


# --- Query parsing: rejections -----------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "How many?",
        "Do I own this?",
        "Show me everything.",
        "What do I have?",
        "What vintages exist?",
        "How many bottles of  are in my cellar?",
        "Do I have  in my cellar?",
        "Show me my wines from .",
    ],
)
def test_unsupported_or_empty_target_phrasing_returns_none(prompt):
    assert parse_cellar_query(prompt) is None


@pytest.mark.parametrize(
    "prompt",
    [
        "What wine goes with chocolate cake?",
        "What wine goes with a steak?",
        "What's a good Bordeaux vintage from 2015?",
        "What's a good wine region to explore?",
        "Which bottle should I open tonight?",
    ],
)
def test_pairing_and_recommendation_prompts_return_none(prompt):
    assert parse_cellar_query(prompt) is None


# --- Normalization and matching ---------------------------------------------


def test_case_folding_matches_across_case():
    records = {"r1": _record(producer="Sample Estate", wine_name="Reserve Red", quantity=2)}
    response = answer_cellar_query("quantity", "reserve red", records)
    assert "2 bottles" in response


def test_whitespace_collapsing_in_stored_field_matches_normalized_target():
    records = {
        "r1": _record(producer="Sample  Estate", wine_name="Reserve   Red", quantity=2),
    }
    response = answer_cellar_query("quantity", "Sample Estate Reserve Red", records)
    assert "2 bottles" in response


def test_trailing_punctuation_removed_from_target_via_parsing():
    query = parse_cellar_query("Do I have Reserve Red in my cellar?")
    assert query == ("ownership", "Reserve Red")


def test_accents_are_not_stripped_and_do_not_match():
    records = {"r1": _record(producer="Château Example", wine_name="Cuvée", quantity=2)}
    response = answer_cellar_query("ownership", "Chateau Example", records)
    assert "I found no cellar record matching" in response


def test_internal_punctuation_is_preserved_and_must_match_exactly():
    records = {"r1": _record(producer="Sample Estate", wine_name="Reserve, Red", quantity=2)}
    no_match = answer_cellar_query("ownership", "Reserve Red", records)
    assert "I found no cellar record matching" in no_match
    match = answer_cellar_query("ownership", "Reserve, Red", records)
    assert "Yes" in match


def test_substrings_do_not_match():
    records = {"r1": _record(producer="Sample Estate", wine_name="Reserve Red", quantity=2)}
    response = answer_cellar_query("ownership", "Estate", records)
    assert "I found no cellar record matching" in response


# --- Totals ------------------------------------------------------------------


def _record(
    producer="Sample Estate",
    wine_name="Reserve Red",
    color="red",
    quantity=1,
    **overrides,
):
    record = {"producer": producer, "wine_name": wine_name, "color": color, "quantity": quantity}
    record.update(overrides)
    return record


def test_total_sums_active_quantities_and_counts_holdings():
    records = {
        "r1": _record(wine_name="A", quantity=3),
        "r2": _record(wine_name="B", quantity=2),
    }
    response = answer_cellar_query("total", None, records)
    assert "5 bottles" in response
    assert "2 holdings" in response


def test_total_excludes_zero_quantity_records():
    records = {
        "r1": _record(wine_name="A", quantity=3),
        "r2": _record(wine_name="B", quantity=0),
    }
    response = answer_cellar_query("total", None, records)
    assert "3 bottles" in response
    assert "1 holdings" in response


def test_total_with_missing_records_is_zero():
    response = answer_cellar_query("total", None, {})
    assert response == "The active cellar contains 0 bottles across 0 holdings."


# --- Quantity ------------------------------------------------------------


def test_quantity_exact_wine_name_match():
    records = {"r1": _record(wine_name="Reserve Red", quantity=4)}
    response = answer_cellar_query("quantity", "Reserve Red", records)
    assert "Sample Estate Reserve Red: 4 bottles" in response


def test_quantity_exact_producer_plus_wine_name_match():
    records = {"r1": _record(producer="Sample Estate", wine_name="Reserve Red", quantity=4)}
    response = answer_cellar_query("quantity", "Sample Estate Reserve Red", records)
    assert "4 bottles" in response


def test_quantity_aggregates_same_identity_across_multiple_vintages():
    records = {
        "r1": _record(wine_name="Reserve Red", quantity=2, vintage=2018),
        "r2": _record(wine_name="Reserve Red", quantity=3, vintage=2019),
    }
    response = answer_cellar_query("quantity", "Reserve Red", records)
    assert "5 bottles" in response
    assert "Vintages: 2018, 2019" in response


def test_quantity_aggregates_duplicate_holdings_of_same_identity():
    records = {
        "r1": _record(wine_name="Reserve Red", quantity=2),
        "r2": _record(wine_name="Reserve Red", quantity=3),
    }
    response = answer_cellar_query("quantity", "Reserve Red", records)
    assert "5 bottles" in response


def test_quantity_same_wine_name_under_different_producers_is_ambiguous():
    records = {
        "r1": _record(producer="Zeta Winery", wine_name="Reserve Red", quantity=2),
        "r2": _record(producer="Alpha Estate", wine_name="Reserve Red", quantity=3),
    }
    response = answer_cellar_query("quantity", "Reserve Red", records)
    assert "Multiple producers" in response
    alpha_index = response.index("Alpha Estate")
    zeta_index = response.index("Zeta Winery")
    assert alpha_index < zeta_index


def test_quantity_producer_plus_wine_name_is_not_ambiguous_even_with_other_producers():
    records = {
        "r1": _record(producer="Zeta Winery", wine_name="Reserve Red", quantity=2),
        "r2": _record(producer="Alpha Estate", wine_name="Reserve Red", quantity=3),
    }
    response = answer_cellar_query("quantity", "Alpha Estate Reserve Red", records)
    assert "3 bottles" in response
    assert "Multiple producers" not in response


def test_quantity_no_match():
    records = {"r1": _record(wine_name="Reserve Red", quantity=2)}
    response = answer_cellar_query("quantity", "Chardonnay", records)
    assert 'I found no cellar record matching "Chardonnay".' == response


def test_quantity_zero_quantity_only_match():
    records = {"r1": _record(wine_name="Reserve Red", quantity=0)}
    response = answer_cellar_query("quantity", "Reserve Red", records)
    assert "current active quantity is zero" in response


# --- Ownership -------------------------------------------------------------


def test_ownership_exact_wine_name():
    records = {"r1": _record(wine_name="Reserve Red", quantity=2)}
    response = answer_cellar_query("ownership", "Reserve Red", records)
    assert response.startswith("Yes,")
    assert "2 bottles" in response


def test_ownership_exact_producer():
    records = {"r1": _record(producer="Sample Estate", quantity=2)}
    response = answer_cellar_query("ownership", "Sample Estate", records)
    assert response.startswith("Yes,")
    assert "matched by producer" in response


def test_ownership_exact_producer_plus_wine_name():
    records = {"r1": _record(producer="Sample Estate", wine_name="Reserve Red", quantity=2)}
    response = answer_cellar_query("ownership", "Sample Estate Reserve Red", records)
    assert response.startswith("Yes,")


def test_ownership_exact_region():
    records = {"r1": _record(region="Example Region", quantity=2)}
    response = answer_cellar_query("ownership", "Example Region", records)
    assert response.startswith("Yes,")
    assert "matched by region" in response


def test_ownership_exact_country():
    records = {"r1": _record(country="Example Country", quantity=2)}
    response = answer_cellar_query("ownership", "Example Country", records)
    assert response.startswith("Yes,")
    assert "matched by country" in response


def test_ownership_multiple_region_holdings_produce_factual_aggregate():
    records = {
        "r1": _record(wine_name="A", region="Burgundy", quantity=2),
        "r2": _record(wine_name="B", producer="Other Estate", region="Burgundy", quantity=3),
    }
    response = answer_cellar_query("ownership", "Burgundy", records)
    assert "5 bottles" in response
    assert "2 holdings" in response


def test_ownership_no_match():
    records = {"r1": _record(wine_name="Reserve Red", quantity=2)}
    response = answer_cellar_query("ownership", "Chardonnay", records)
    assert 'I found no cellar record matching "Chardonnay".' == response


def test_ownership_zero_quantity_only_match():
    records = {"r1": _record(wine_name="Reserve Red", quantity=0)}
    response = answer_cellar_query("ownership", "Reserve Red", records)
    assert "current active quantity is zero" in response


# --- Vintage listing ---------------------------------------------------


def test_vintage_numeric_ascending():
    records = {
        "r1": _record(wine_name="Reserve Red", quantity=1, vintage=2020),
        "r2": _record(wine_name="Reserve Red", quantity=1, vintage=2018),
    }
    response = answer_cellar_query("vintage", "Reserve Red", records)
    assert "2018, 2020" in response


def test_vintage_nv_sorted_after_numeric():
    records = {
        "r1": _record(wine_name="Reserve Red", quantity=1, vintage="NV"),
        "r2": _record(wine_name="Reserve Red", quantity=1, vintage=2018),
    }
    response = answer_cellar_query("vintage", "Reserve Red", records)
    assert "2018, NV" in response


def test_vintage_duplicates_deduplicated():
    records = {
        "r1": _record(wine_name="Reserve Red", quantity=1, vintage=2018),
        "r2": _record(wine_name="Reserve Red", quantity=1, vintage=2018),
    }
    response = answer_cellar_query("vintage", "Reserve Red", records)
    assert response.count("2018") == 1


def test_vintage_records_without_vintage_omitted():
    records = {
        "r1": _record(wine_name="Reserve Red", quantity=1, vintage=2018),
        "r2": _record(wine_name="Reserve Red", quantity=1),
    }
    response = answer_cellar_query("vintage", "Reserve Red", records)
    assert "2018" in response
    assert response.count(",") == 0  # only one vintage present, no list separator


def test_vintage_same_name_different_producer_ambiguity():
    records = {
        "r1": _record(producer="Zeta Winery", wine_name="Reserve Red", quantity=1, vintage=2018),
        "r2": _record(producer="Alpha Estate", wine_name="Reserve Red", quantity=1, vintage=2019),
    }
    response = answer_cellar_query("vintage", "Reserve Red", records)
    assert "Multiple producers" in response


def test_vintage_no_match():
    records = {"r1": _record(wine_name="Reserve Red", quantity=1, vintage=2018)}
    response = answer_cellar_query("vintage", "Chardonnay", records)
    assert 'I found no cellar record matching "Chardonnay".' == response


def test_vintage_zero_quantity_only_match():
    records = {"r1": _record(wine_name="Reserve Red", quantity=0, vintage=2018)}
    response = answer_cellar_query("vintage", "Reserve Red", records)
    assert "current active quantity is zero" in response


# --- Producer listing -----------------------------------------------------


def test_producer_listing_active_records_only():
    records = {
        "r1": _record(wine_name="Active Wine", quantity=2),
        "r2": _record(wine_name="Depleted Wine", quantity=0),
    }
    response = answer_cellar_query("producer", "Sample Estate", records)
    assert "Active Wine" in response
    assert "Depleted Wine" not in response


def test_producer_listing_deterministic_order():
    records = {
        "r1": _record(wine_name="Zeta Wine", quantity=1),
        "r2": _record(wine_name="Alpha Wine", quantity=1),
    }
    response = answer_cellar_query("producer", "Sample Estate", records)
    alpha_index = response.index("Alpha Wine")
    zeta_index = response.index("Zeta Wine")
    assert alpha_index < zeta_index


def test_producer_listing_quantity_and_vintage_formatting():
    records = {"r1": _record(wine_name="Reserve Red", quantity=3, vintage=2019)}
    response = answer_cellar_query("producer", "Sample Estate", records)
    assert "Reserve Red (2019): 3 bottles" in response


def test_producer_listing_duplicate_holdings_remain_separate():
    records = {
        "r1": _record(wine_name="Reserve Red", quantity=2, vintage=2019),
        "r2": _record(wine_name="Reserve Red", quantity=1, vintage=2019),
    }
    response = answer_cellar_query("producer", "Sample Estate", records)
    assert "Cellar ID: r1" in response
    assert "Cellar ID: r2" in response


def test_producer_listing_no_match():
    records = {"r1": _record(producer="Other Estate", quantity=2)}
    response = answer_cellar_query("producer", "Sample Estate", records)
    assert 'I found no cellar record matching "Sample Estate".' == response


def test_producer_listing_zero_quantity_only_producer():
    records = {"r1": _record(producer="Sample Estate", quantity=0)}
    response = answer_cellar_query("producer", "Sample Estate", records)
    assert "current active quantity is zero" in response


# --- Validation --------------------------------------------------------


def test_every_record_is_validated_including_unrelated_and_zero_quantity():
    records = {
        "r1": _record(wine_name="Reserve Red", quantity=2),
        "r2": {"producer": "Bad Estate", "wine_name": "Broken", "color": "red", "quantity": -1},
    }
    with pytest.raises(ValueError):
        answer_cellar_query("total", None, records)


def test_one_invalid_record_rejects_the_complete_answer():
    records = {
        "r1": _record(wine_name="Reserve Red", quantity=2),
        "r2": {"producer": "Bad Estate", "color": "red", "quantity": 1},
    }
    with pytest.raises(ValueError):
        answer_cellar_query("quantity", "Reserve Red", records)


def test_malformed_record_error_identifies_record_and_field():
    records = {"bad-record": {"producer": "Sample Estate", "wine_name": "X", "color": "red", "quantity": -1}}
    with pytest.raises(ValueError) as exc_info:
        answer_cellar_query("total", None, records)
    assert "bad-record" in str(exc_info.value)
    assert "quantity" in str(exc_info.value)


def test_unknown_record_fields_are_harmless():
    records = {"r1": _record(wine_name="Reserve Red", quantity=2, favorite_glassware="Riedel")}
    response = answer_cellar_query("quantity", "Reserve Red", records)
    assert "2 bottles" in response
