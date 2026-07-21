"""Tests for CapabilityRouter."""

from kernel.orchestrator.router import CapabilityRouter


# --- Existing literal "wine" rule ----------------------------------------


def test_wine_prompt_routes_to_wine():
    router = CapabilityRouter()
    assert router.route("What wine goes with steak?") == "wine"


def test_matching_is_case_insensitive():
    router = CapabilityRouter()
    assert router.route("WINE recommendations please") == "wine"
    assert router.route("Wine recommendations please") == "wine"


def test_wine_embedded_in_larger_word_does_not_route():
    router = CapabilityRouter()
    assert router.route("I'd like to visit a winery this weekend") is None


def test_unrelated_prompt_returns_none():
    router = CapabilityRouter()
    assert router.route("What's the weather tomorrow?") is None


# --- New natural wine-intent phrases (positive) --------------------------


def test_drink_with_food_routes_to_wine():
    router = CapabilityRouter()
    assert router.route("What should I drink with steak?") == "wine"


def test_bottle_selection_phrase_routes_to_wine():
    router = CapabilityRouter()
    assert router.route("Which bottle should I open tonight?") == "wine"


def test_pair_with_food_routes_to_wine():
    router = CapabilityRouter()
    assert router.route("Pair this with chicken stroganoff.") == "wine"


def test_suitable_for_food_routes_to_wine():
    router = CapabilityRouter()
    assert router.route("Do I have anything suitable for pizza?") == "wine"


def test_equivalent_pairing_phrase_with_another_food_cue_routes_to_wine():
    router = CapabilityRouter()
    assert router.route("Any recommendations to pair with grilled salmon tonight?") == "wine"


def test_new_phrases_are_case_insensitive():
    router = CapabilityRouter()
    assert router.route("WHAT SHOULD I DRINK WITH STEAK?") == "wine"
    assert router.route("which BOTTLE should I OPEN tonight?") == "wine"


# --- False-positive control (negative) -----------------------------------


def test_winery_mention_does_not_route():
    router = CapabilityRouter()
    assert router.route("Let's visit a winery.") is None


def test_bottle_opener_request_does_not_route():
    router = CapabilityRouter()
    assert router.route("I need a bottle opener.") is None


def test_drink_water_does_not_route():
    router = CapabilityRouter()
    assert router.route("Please drink some water.") is None


def test_pair_of_shoes_does_not_route():
    router = CapabilityRouter()
    assert router.route("I bought a pair of shoes.") is None


def test_open_medicine_bottle_does_not_route():
    router = CapabilityRouter()
    assert router.route("Open the medicine bottle.") is None


def test_goes_with_shirt_does_not_route():
    router = CapabilityRouter()
    assert router.route("What goes with this shirt?") is None


def test_suitable_for_business_meeting_does_not_route():
    router = CapabilityRouter()
    assert router.route("Is this suitable for a business meeting?") is None


def test_unrelated_ordinary_prompt_does_not_route():
    router = CapabilityRouter()
    assert router.route("Can you help me draft an email to my landlord?") is None
