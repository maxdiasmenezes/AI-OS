"""Tests for Wine Pairing v1 (WineCapability) and its model-backed fallback."""

from pathlib import Path

import pytest

from capabilities.wine.capability import WineCapability
from kernel.memory import MemoryManager
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


class RecallSpyMemoryManager(MemoryManager):
    """A real MemoryManager (tmp_path-backed) that records recall() calls."""

    def __init__(self, settings: dict):
        super().__init__(settings)
        self.recall_calls: list[tuple[str, int | None]] = []

    def recall(self, namespace: str, limit: int | None = None):
        self.recall_calls.append((namespace, limit))
        return super().recall(namespace, limit)


@pytest.fixture
def fake_provider():
    return FakeModelProvider()


@pytest.fixture
def memory_manager(tmp_path):
    return RecallSpyMemoryManager({"storage_dir": str(tmp_path / "memory")})


@pytest.fixture
def wine(fake_provider, memory_manager):
    return WineCapability(fake_provider, memory_manager)


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
    sent_prompt = fake_provider.received_prompts[0]
    assert _FALLBACK_INSTRUCTIONS in sent_prompt
    assert prompt in sent_prompt
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


# --- Deterministic matches never call the provider or memory -------------


def test_matched_category_prompt_does_not_call_provider(wine, fake_provider):
    wine.handle("What wine goes with steak?")
    assert fake_provider.received_prompts == []


def test_matched_category_prompt_does_not_call_memory_recall(wine, memory_manager):
    wine.handle("What wine goes with steak?")
    assert memory_manager.recall_calls == []


@pytest.mark.parametrize(
    "prompt",
    [
        "What wine goes with chocolate cake?",
        "What wine goes with a spicy curry?",
        "What wine pairs with pizza?",
        "What wine goes with shrimp?",
        "What wine goes with salmon?",
        "What wine goes with pork chops?",
        "What wine goes with roast chicken?",
        "What wine goes with a steak?",
    ],
)
def test_all_deterministic_categories_return_immediately_without_side_effects(
    wine, fake_provider, memory_manager, prompt
):
    wine.handle(prompt)
    assert fake_provider.received_prompts == []
    assert memory_manager.recall_calls == []


# --- Milestone 22/23: unmatched prompts fall back to the injected provider -


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


# --- Milestone 23: memory-aware fallback ----------------------------------


def test_unmatched_prompt_with_empty_memory_has_no_conversation_context_section(
    wine, fake_provider
):
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Conversation context:" not in sent_prompt


def test_unmatched_prompt_with_seeded_history_includes_entries_in_order(
    wine, fake_provider, memory_manager
):
    memory_manager.remember("conversation", "What's a good everyday red?", metadata={"role": "user"})
    memory_manager.remember("conversation", "Try a Cotes du Rhone.", metadata={"role": "assistant"})

    prompt = "What's a good Bordeaux vintage from 2015?"
    wine.handle(prompt)

    sent_prompt = fake_provider.received_prompts[0]
    assert "Conversation context:" in sent_prompt

    context_index = sent_prompt.index("Conversation context:")
    user_turn_index = sent_prompt.index("What's a good everyday red?")
    assistant_turn_index = sent_prompt.index("Try a Cotes du Rhone.")
    request_index = sent_prompt.index("Current user request:")
    prompt_index = sent_prompt.index(prompt)

    assert context_index < user_turn_index < assistant_turn_index < request_index < prompt_index


def test_unmatched_prompt_recalls_last_ten_conversation_entries(wine, fake_provider, memory_manager):
    for i in range(12):
        memory_manager.remember("conversation", f"turn {i}", metadata={"role": "user"})

    wine.handle("What's a good Bordeaux vintage from 2015?")

    assert memory_manager.recall_calls == [("conversation", 10)]
    sent_prompt = fake_provider.received_prompts[0]
    assert "turn 0\n" not in sent_prompt
    assert "turn 1\n" not in sent_prompt
    assert "turn 11" in sent_prompt


def test_deterministic_matches_do_not_call_recall_even_with_seeded_history(
    wine, fake_provider, memory_manager
):
    memory_manager.remember("conversation", "I like bold reds.", metadata={"role": "user"})
    wine.handle("What wine goes with a steak?")
    assert memory_manager.recall_calls == []
    assert fake_provider.received_prompts == []
