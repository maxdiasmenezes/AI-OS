"""Tests for Wine Pairing v1 (WineCapability)."""

import pytest

from capabilities.wine.capability import WineCapability
from kernel.models.base import ModelProvider, ModelResponse


class FakeModelProvider(ModelProvider):
    """Records whether it was called; no external calls."""

    def __init__(self):
        self.received_prompts: list[str] = []

    def send_prompt(self, prompt: str) -> ModelResponse:
        self.received_prompts.append(prompt)
        return ModelResponse(
            text="unused",
            model="fake-model",
            input_tokens=0,
            output_tokens=0,
            latency_seconds=0.0,
        )


@pytest.fixture
def fake_provider():
    return FakeModelProvider()


@pytest.fixture
def wine(fake_provider):
    return WineCapability(fake_provider)


def _assert_out_of_scope(response: str) -> None:
    # Observable contract: names some supported categories, and says it
    # can't give a reliable pairing - without pinning the exact wording.
    assert "red meat" in response
    assert "chocolate/dessert" in response
    assert "can't give a reliable pairing" in response


def test_id_returns_wine(wine):
    assert wine.id == "wine"


@pytest.mark.parametrize(
    "prompt, expected_label, expected_style_fragment",
    [
        ("What wine goes with chocolate cake?", "chocolate/dessert", "Port"),
        ("What wine goes with a spicy curry?", "spicy food", "Riesling"),
        ("What wine pairs with pizza?", "tomato-based pasta/pizza", "Chianti"),
        ("What wine goes with shrimp?", "shellfish", "Chablis"),
        ("What wine goes with salmon?", "fish", "Sauvignon Blanc"),
        ("What wine goes with pork chops?", "pork", "Pinot Noir"),
        ("What wine goes with roast chicken?", "poultry", "Chardonnay"),
        ("What wine goes with a steak?", "red meat", "Cabernet Sauvignon"),
    ],
)
def test_supported_categories_return_expected_recommendation(
    wine, prompt, expected_label, expected_style_fragment
):
    response = wine.handle(prompt)
    assert expected_label in response
    assert expected_style_fragment in response


def test_matching_is_case_insensitive(wine):
    lower = wine.handle("what wine goes with steak?")
    upper = wine.handle("WHAT WINE GOES WITH STEAK?")
    mixed = wine.handle("What Wine Goes With STEAK?")
    assert lower == upper == mixed
    assert "red meat" in lower


@pytest.mark.parametrize(
    "prompt",
    [
        # "hamster" contains "ham" (a pork keyword) but isn't the word "ham".
        "I have a pet hamster, what wine should I serve at the party?",
        # "coding" contains "cod" (a fish keyword) but isn't the word "cod".
        "My favorite hobby is coding, any wine suggestions?",
    ],
)
def test_keywords_match_whole_words_only(wine, prompt):
    response = wine.handle(prompt)
    _assert_out_of_scope(response)


@pytest.mark.parametrize(
    "prompt, expected_label, expected_style_fragment",
    [
        ("I'm having spicy shrimp tonight, what wine?", "spicy food", "Riesling"),
        ("What wine goes with a pizza topped with shrimp?", "tomato-based pasta/pizza", "Chianti"),
    ],
)
def test_priority_prefers_preparation_over_protein(
    wine, prompt, expected_label, expected_style_fragment
):
    response = wine.handle(prompt)
    assert expected_label in response
    assert expected_style_fragment in response
    assert "shellfish" not in response


def test_unsupported_prompt_returns_out_of_scope_response(wine):
    response = wine.handle("What's a good Bordeaux vintage from 2015?")
    _assert_out_of_scope(response)


# --- Milestone 21: provider is injected but not yet used -----------------


def test_matched_category_prompt_does_not_call_provider(wine, fake_provider):
    wine.handle("What wine goes with steak?")
    assert fake_provider.received_prompts == []


def test_out_of_scope_prompt_does_not_call_provider(wine, fake_provider):
    wine.handle("What's a good Bordeaux vintage from 2015?")
    assert fake_provider.received_prompts == []
