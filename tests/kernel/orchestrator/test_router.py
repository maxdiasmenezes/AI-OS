"""Tests for CapabilityRouter."""

from kernel.orchestrator.router import CapabilityRouter


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
