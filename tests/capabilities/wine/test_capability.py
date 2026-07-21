"""Tests for Wine Pairing v1 (WineCapability) and its model-backed fallback."""

from pathlib import Path

import pytest

from capabilities.wine.capability import WineCapability
from kernel.models.base import ModelProvider, ModelResponse

# tests/capabilities/wine/test_capability.py -> tests/capabilities -> tests -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_FALLBACK_INSTRUCTIONS = (_PROJECT_ROOT / "prompts" / "wine" / "fallback.md").read_text(
    encoding="utf-8"
).strip()


class FakeModelProvider(ModelProvider):
    """Records prompts it receives and returns a fixed, distinguishable response."""

    def __init__(self):
        self.received_prompts: list[str] = []
        self.response = ModelResponse(
            text="A Loire Valley Sauvignon Blanc is a versatile, food-friendly choice.",
            model="fake-model",
            input_tokens=17,
            output_tokens=29,
            latency_seconds=0.42,
        )

    def send_prompt(self, prompt: str) -> ModelResponse:
        self.received_prompts.append(prompt)
        return self.response


@pytest.fixture
def fake_provider():
    return FakeModelProvider()


@pytest.fixture
def wine(fake_provider):
    return WineCapability(fake_provider)


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
def test_keywords_match_whole_words_only(wine, fake_provider, prompt):
    response = wine.handle(prompt)
    assert fake_provider.received_prompts == [f"{_FALLBACK_INSTRUCTIONS}\n\n{prompt}"]
    assert response is fake_provider.response


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


# --- Deterministic matches never call the provider -----------------------


def test_matched_category_prompt_does_not_call_provider(wine, fake_provider):
    wine.handle("What wine goes with steak?")
    assert fake_provider.received_prompts == []


# --- Milestone 22: unmatched prompts fall back to the injected provider --


def test_unmatched_prompt_calls_provider_exactly_once(wine, fake_provider):
    wine.handle("What's a good Bordeaux vintage from 2015?")
    assert len(fake_provider.received_prompts) == 1


def test_unmatched_prompt_returns_providers_model_response_unchanged(wine, fake_provider):
    response = wine.handle("What's a good Bordeaux vintage from 2015?")
    assert response is fake_provider.response


def test_fallback_prompt_contains_instruction_and_original_request(wine, fake_provider):
    prompt = "What's a good Bordeaux vintage from 2015?"
    wine.handle(prompt)

    sent_prompt = fake_provider.received_prompts[0]
    instruction_index = sent_prompt.index(_FALLBACK_INSTRUCTIONS)
    request_index = sent_prompt.index(prompt)

    assert instruction_index < request_index
