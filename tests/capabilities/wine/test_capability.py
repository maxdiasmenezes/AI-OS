"""Tests for Wine Pairing v1 (WineCapability) and its model-backed fallback."""

import json
from pathlib import Path

import pytest

from capabilities.wine.capability import WineCapability
from kernel.knowledge import JSONKnowledgeStore
from kernel.memory import MemoryManager
from kernel.models.base import ModelProvider, ModelRequestOptions, ModelResponse

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

    def send_prompt(
        self, prompt: str, *, options: ModelRequestOptions | None = None
    ) -> ModelResponse:
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


class GetSpyKnowledgeStore(JSONKnowledgeStore):
    """A real JSONKnowledgeStore (tmp_path-backed) that records get()/list_records() calls."""

    def __init__(self, storage_dir):
        super().__init__(storage_dir)
        self.get_calls: list[tuple[str, str]] = []
        self.list_records_calls: list[str] = []

    def get(self, namespace: str, key: str):
        self.get_calls.append((namespace, key))
        return super().get(namespace, key)

    def list_records(self, namespace: str):
        self.list_records_calls.append(namespace)
        return super().list_records(namespace)


def _write_profile(knowledge_dir: Path, profile: dict) -> None:
    """Write a synthetic wine_profile.json record directly under tmp_path."""

    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "wine_profile.json").write_text(
        json.dumps({"profile": profile}), encoding="utf-8"
    )


@pytest.fixture
def fake_provider():
    return FakeModelProvider()


@pytest.fixture
def memory_manager(tmp_path):
    return RecallSpyMemoryManager({"storage_dir": str(tmp_path / "memory")})


@pytest.fixture
def knowledge_dir(tmp_path):
    return tmp_path / "knowledge"


@pytest.fixture
def knowledge_store(knowledge_dir):
    return GetSpyKnowledgeStore(knowledge_dir)


@pytest.fixture
def wine(fake_provider, memory_manager, knowledge_store):
    return WineCapability(fake_provider, memory_manager, knowledge_store)


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


def test_matched_category_prompt_does_not_call_knowledge_store(wine, knowledge_store):
    wine.handle("What wine goes with steak?")
    assert knowledge_store.get_calls == []
    assert knowledge_store.list_records_calls == []


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
    wine, fake_provider, memory_manager, knowledge_store, prompt
):
    wine.handle(prompt)
    assert fake_provider.received_prompts == []
    assert memory_manager.recall_calls == []
    assert knowledge_store.get_calls == []
    assert knowledge_store.list_records_calls == []


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


def test_deterministic_matches_do_not_call_recall_or_knowledge_even_with_seeded_data(
    wine, fake_provider, memory_manager, knowledge_store, knowledge_dir
):
    memory_manager.remember("conversation", "I like bold reds.", metadata={"role": "user"})
    _write_profile(knowledge_dir, {"notes": "Prefers Old World wines."})

    wine.handle("What wine goes with a steak?")

    assert memory_manager.recall_calls == []
    assert knowledge_store.get_calls == []
    assert knowledge_store.list_records_calls == []
    assert fake_provider.received_prompts == []


# --- Milestone 25: personal wine profile in the fallback prompt ----------


def test_unmatched_prompt_with_no_profile_has_no_profile_section(wine, fake_provider):
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine profile:" not in sent_prompt


def test_unmatched_prompt_with_empty_profile_has_no_profile_section(
    wine, fake_provider, knowledge_dir
):
    _write_profile(knowledge_dir, {})
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine profile:" not in sent_prompt


def test_unmatched_prompt_with_profile_renders_only_populated_fields_in_fixed_order(
    wine, fake_provider, knowledge_dir
):
    _write_profile(
        knowledge_dir,
        {
            "notes": "Prefers Old World wines.",
            "preferred_styles": ["dry Riesling", "Barolo", "Champagne"],
            "budget_range": "$20-40 per bottle",
        },
    )
    prompt = "What's a good Bordeaux vintage from 2015?"
    wine.handle(prompt)

    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine profile:" in sent_prompt
    assert "- Preferred styles: dry Riesling, Barolo, Champagne" in sent_prompt
    assert "- Usual budget: $20-40 per bottle" in sent_prompt
    assert "- Notes: Prefers Old World wines." in sent_prompt

    # Fields absent from the profile are omitted entirely.
    assert "Disliked styles" not in sent_prompt
    assert "Selection priorities" not in sent_prompt

    profile_index = sent_prompt.index("Personal wine profile:")
    styles_index = sent_prompt.index("Preferred styles")
    budget_index = sent_prompt.index("Usual budget")
    notes_index = sent_prompt.index("Notes:")
    request_index = sent_prompt.index("Current user request:")
    prompt_index = sent_prompt.index(prompt)

    assert (
        profile_index
        < styles_index
        < budget_index
        < notes_index
        < request_index
        < prompt_index
    )


def test_unmatched_prompt_profile_appears_before_conversation_and_request(
    wine, fake_provider, memory_manager, knowledge_dir
):
    _write_profile(knowledge_dir, {"notes": "Prefers Old World wines."})
    memory_manager.remember("conversation", "What's a good everyday red?", metadata={"role": "user"})
    memory_manager.remember("conversation", "Try a Cotes du Rhone.", metadata={"role": "assistant"})

    prompt = "What's a good Bordeaux vintage from 2015?"
    wine.handle(prompt)

    sent_prompt = fake_provider.received_prompts[0]
    profile_index = sent_prompt.index("Personal wine profile:")
    context_index = sent_prompt.index("Conversation context:")
    request_index = sent_prompt.index("Current user request:")
    prompt_index = sent_prompt.index(prompt)

    assert profile_index < context_index < request_index < prompt_index


def test_unmatched_prompt_profile_ignores_unknown_fields(wine, fake_provider, knowledge_dir):
    _write_profile(
        knowledge_dir,
        {"notes": "Prefers Old World wines.", "favorite_producer": "Antinori"},
    )
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Antinori" not in sent_prompt
    assert "favorite_producer" not in sent_prompt


@pytest.mark.parametrize(
    "profile",
    [
        {"preferred_styles": "dry Riesling"},
        {"preferred_styles": ["dry Riesling", ""]},
        {"preferred_styles": ["dry Riesling", 5]},
        {"disliked_styles": {"not": "a list"}},
        {"budget_range": 30},
        {"notes": ["not", "a", "string"]},
    ],
)
def test_unmatched_prompt_with_invalid_profile_field_raises_value_error(
    wine, fake_provider, knowledge_dir, profile
):
    _write_profile(knowledge_dir, profile)

    with pytest.raises(ValueError):
        wine.handle("What's a good Bordeaux vintage from 2015?")

    assert fake_provider.received_prompts == []


# --- Milestone 27: personal wine cellar inventory -------------------------


def _write_cellar(knowledge_dir: Path, records: dict) -> None:
    """Write a synthetic wine_cellar.json document directly under tmp_path."""

    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "wine_cellar.json").write_text(json.dumps(records), encoding="utf-8")


_SAMPLE_RECORD = {
    "producer": "Sample Estate",
    "wine_name": "Reserve Red",
    "color": "red",
    "quantity": 3,
    "vintage": 2021,
    "country": "Example Country",
    "region": "Example Region",
    "style": "medium-bodied red",
    "grapes": ["Sample Grape"],
    "estimated_price": 30,
    "price_currency": "USD",
    "vivino_rating": 3.8,
    "drinking_window": "2025-2029",
    "notes": "Synthetic test record",
    "special_occasion": False,
}


def test_deterministic_match_does_not_touch_seeded_cellar(
    wine, fake_provider, memory_manager, knowledge_store, knowledge_dir
):
    _write_cellar(knowledge_dir, {"sample-red-2021": _SAMPLE_RECORD})

    wine.handle("What wine goes with a steak?")

    assert knowledge_store.get_calls == []
    assert knowledge_store.list_records_calls == []
    assert fake_provider.received_prompts == []


def test_unmatched_prompt_with_no_cellar_namespace_has_no_cellar_section(wine, fake_provider):
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine cellar:" not in sent_prompt


def test_unmatched_prompt_with_empty_cellar_document_has_no_cellar_section(
    wine, fake_provider, knowledge_dir
):
    _write_cellar(knowledge_dir, {})
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine cellar:" not in sent_prompt


def test_unmatched_prompt_with_only_zero_quantity_records_has_no_cellar_section(
    wine, fake_provider, knowledge_dir
):
    zero_record = dict(_SAMPLE_RECORD, quantity=0)
    _write_cellar(knowledge_dir, {"sample-red-2021": zero_record})
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine cellar:" not in sent_prompt


def test_unmatched_prompt_with_cellar_renders_required_and_optional_fields(
    wine, fake_provider, knowledge_dir
):
    _write_cellar(knowledge_dir, {"sample-red-2021": _SAMPLE_RECORD})
    prompt = "What's a good Bordeaux vintage from 2015?"
    wine.handle(prompt)

    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine cellar:" in sent_prompt
    assert "Cellar ID: sample-red-2021" in sent_prompt
    assert "Producer: Sample Estate" in sent_prompt
    assert "Wine: Reserve Red" in sent_prompt
    assert "Vintage: 2021" in sent_prompt
    assert "Color: red" in sent_prompt
    assert "Style: medium-bodied red" in sent_prompt
    assert "Country: Example Country" in sent_prompt
    assert "Region: Example Region" in sent_prompt
    assert "Grapes: Sample Grape" in sent_prompt
    assert "Quantity: 3" in sent_prompt
    assert "Estimated price: 30 USD" in sent_prompt
    assert "Vivino rating: 3.8" in sent_prompt
    assert "Drinking window: 2025-2029" in sent_prompt
    assert "Notes: Synthetic test record" in sent_prompt
    # special_occasion is False, so it must not be rendered at all.
    assert "Special occasion" not in sent_prompt


def test_cellar_record_with_only_required_fields_renders_minimally(
    wine, fake_provider, knowledge_dir
):
    minimal_record = {
        "producer": "Minimal Producer",
        "wine_name": "Basic White",
        "color": "white",
        "quantity": 1,
    }
    _write_cellar(knowledge_dir, {"minimal-white": minimal_record})
    wine.handle("What's a good Bordeaux vintage from 2015?")

    sent_prompt = fake_provider.received_prompts[0]
    assert "Cellar ID: minimal-white" in sent_prompt
    assert "Producer: Minimal Producer" in sent_prompt
    assert "Wine: Basic White" in sent_prompt
    assert "Color: white" in sent_prompt
    assert "Quantity: 1" in sent_prompt
    assert "Vintage" not in sent_prompt
    assert "Style" not in sent_prompt


def test_cellar_special_occasion_true_is_rendered(wine, fake_provider, knowledge_dir):
    record = dict(_SAMPLE_RECORD, special_occasion=True)
    _write_cellar(knowledge_dir, {"sample-red-2021": record})
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Special occasion: yes" in sent_prompt


def test_cellar_zero_quantity_record_excluded_alongside_active_record(
    wine, fake_provider, knowledge_dir
):
    active = dict(_SAMPLE_RECORD)
    depleted = dict(_SAMPLE_RECORD, wine_name="Depleted Red", quantity=0)
    _write_cellar(
        knowledge_dir,
        {"sample-red-2021": active, "depleted-red-2019": depleted},
    )
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "sample-red-2021" in sent_prompt
    assert "depleted-red-2019" not in sent_prompt
    assert "Depleted Red" not in sent_prompt


def test_cellar_active_records_sorted_by_record_key(wine, fake_provider, knowledge_dir):
    record_b = dict(_SAMPLE_RECORD, wine_name="B Wine")
    record_a = dict(_SAMPLE_RECORD, wine_name="A Wine")
    _write_cellar(knowledge_dir, {"zzz-wine": record_b, "aaa-wine": record_a})
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]

    aaa_index = sent_prompt.index("aaa-wine")
    zzz_index = sent_prompt.index("zzz-wine")
    assert aaa_index < zzz_index


def test_cellar_grape_order_is_preserved(wine, fake_provider, knowledge_dir):
    record = dict(_SAMPLE_RECORD, grapes=["Merlot", "Cabernet Franc", "Petit Verdot"])
    _write_cellar(knowledge_dir, {"sample-red-2021": record})
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Grapes: Merlot, Cabernet Franc, Petit Verdot" in sent_prompt


def test_cellar_unknown_fields_are_ignored(wine, fake_provider, knowledge_dir):
    record = dict(_SAMPLE_RECORD, favorite_glassware="Riedel")
    _write_cellar(knowledge_dir, {"sample-red-2021": record})
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Riedel" not in sent_prompt
    assert "favorite_glassware" not in sent_prompt


def test_cellar_two_records_with_same_wine_name_remain_separate_via_record_key(
    wine, fake_provider, knowledge_dir
):
    record_1 = dict(_SAMPLE_RECORD)
    record_2 = dict(_SAMPLE_RECORD, quantity=1)
    _write_cellar(
        knowledge_dir,
        {"sample-red-2021-case-a": record_1, "sample-red-2021-case-b": record_2},
    )
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Cellar ID: sample-red-2021-case-a" in sent_prompt
    assert "Cellar ID: sample-red-2021-case-b" in sent_prompt


def test_profile_and_cellar_remain_separate_sections(wine, fake_provider, knowledge_dir):
    _write_profile(knowledge_dir, {"notes": "Prefers Old World wines."})
    _write_cellar(knowledge_dir, {"sample-red-2021": _SAMPLE_RECORD})
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]

    profile_index = sent_prompt.index("Personal wine profile:")
    cellar_index = sent_prompt.index("Personal wine cellar:")
    assert profile_index < cellar_index


def test_fallback_prompt_order_is_instructions_profile_cellar_conversation_request(
    wine, fake_provider, memory_manager, knowledge_dir
):
    _write_profile(knowledge_dir, {"notes": "Prefers Old World wines."})
    _write_cellar(knowledge_dir, {"sample-red-2021": _SAMPLE_RECORD})
    memory_manager.remember("conversation", "What's a good everyday red?", metadata={"role": "user"})
    memory_manager.remember("conversation", "Try a Cotes du Rhone.", metadata={"role": "assistant"})

    prompt = "What's a good Bordeaux vintage from 2015?"
    wine.handle(prompt)

    sent_prompt = fake_provider.received_prompts[0]
    instructions_index = sent_prompt.index(_FALLBACK_INSTRUCTIONS)
    profile_index = sent_prompt.index("Personal wine profile:")
    cellar_index = sent_prompt.index("Personal wine cellar:")
    context_index = sent_prompt.index("Conversation context:")
    request_index = sent_prompt.index("Current user request:")
    prompt_index = sent_prompt.index(prompt)

    assert (
        instructions_index
        < profile_index
        < cellar_index
        < context_index
        < request_index
        < prompt_index
    )


def test_fallback_prompt_current_request_appears_verbatim(wine, fake_provider):
    prompt = "What's a good Bordeaux vintage from 2015?"
    wine.handle(prompt)
    sent_prompt = fake_provider.received_prompts[0]
    assert f"Current user request:\n{prompt}" in sent_prompt


def test_fallback_instructions_include_everyday_versus_special_policy():
    assert "special_occasion: true" in _FALLBACK_INSTRUCTIONS
    assert "Never assume an occasion is special" in _FALLBACK_INSTRUCTIONS


def test_no_cellar_produces_no_invented_cellar_section(wine, fake_provider):
    wine.handle("What's a good Bordeaux vintage from 2015?")
    sent_prompt = fake_provider.received_prompts[0]
    assert "Cellar ID" not in sent_prompt


def test_cellar_over_limit_produces_honest_size_limit_section_without_individual_records(
    wine, fake_provider, knowledge_dir
):
    records = {
        f"wine-{i:03d}": dict(_SAMPLE_RECORD, wine_name=f"Wine {i}") for i in range(101)
    }
    _write_cellar(knowledge_dir, records)
    wine.handle("What's a good Bordeaux vintage from 2015?")

    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine cellar:" in sent_prompt
    assert "101" in sent_prompt
    assert "100" in sent_prompt
    assert "Cellar ID" not in sent_prompt
    assert len(fake_provider.received_prompts) == 1


def test_cellar_at_limit_includes_every_active_record(wine, fake_provider, knowledge_dir):
    records = {
        f"wine-{i:03d}": dict(_SAMPLE_RECORD, wine_name=f"Wine {i}") for i in range(100)
    }
    _write_cellar(knowledge_dir, records)
    wine.handle("What's a good Bordeaux vintage from 2015?")

    sent_prompt = fake_provider.received_prompts[0]
    assert sent_prompt.count("Cellar ID") == 100


# --- Milestone 27: cellar validation ---------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        {"wine_name": "Reserve Red", "color": "red", "quantity": 1},
        {"producer": "Sample Estate", "color": "red", "quantity": 1},
        {"producer": "Sample Estate", "wine_name": "Reserve Red", "quantity": 1},
        {"producer": "Sample Estate", "wine_name": "Reserve Red", "color": "red"},
        {"producer": "", "wine_name": "Reserve Red", "color": "red", "quantity": 1},
        {"producer": "Sample Estate", "wine_name": "", "color": "red", "quantity": 1},
        {"producer": "Sample Estate", "wine_name": "Reserve Red", "color": "", "quantity": 1},
        {
            "producer": "Sample Estate",
            "wine_name": "Reserve Red",
            "color": "red",
            "quantity": True,
        },
        {
            "producer": "Sample Estate",
            "wine_name": "Reserve Red",
            "color": "red",
            "quantity": -1,
        },
        {
            "producer": "Sample Estate",
            "wine_name": "Reserve Red",
            "color": "red",
            "quantity": 1.5,
        },
    ],
)
def test_cellar_missing_or_invalid_required_fields_raise_value_error(
    wine, fake_provider, knowledge_dir, record
):
    _write_cellar(knowledge_dir, {"sample-red-2021": record})

    with pytest.raises(ValueError):
        wine.handle("What's a good Bordeaux vintage from 2015?")

    assert fake_provider.received_prompts == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"country": ""},
        {"country": 5},
        {"region": ""},
        {"style": 5},
        {"drinking_window": ""},
        {"notes": 5},
        {"grapes": "Merlot"},
        {"grapes": [""]},
        {"grapes": [5]},
        {"vintage": 1799},
        {"vintage": 2101},
        {"vintage": True},
        {"vintage": "1999"},
        {"vintage": "nv"},
        {"estimated_price": -1},
        {"estimated_price": True},
        {"estimated_price": "30"},
        {"vivino_rating": -0.1},
        {"vivino_rating": 5.1},
        {"vivino_rating": True},
        {"special_occasion": "true"},
        {"special_occasion": 1},
    ],
)
def test_cellar_invalid_optional_fields_raise_value_error(
    wine, fake_provider, knowledge_dir, overrides
):
    record = dict(_SAMPLE_RECORD)
    record.update(overrides)
    _write_cellar(knowledge_dir, {"sample-red-2021": record})

    with pytest.raises(ValueError):
        wine.handle("What's a good Bordeaux vintage from 2015?")

    assert fake_provider.received_prompts == []


def test_cellar_estimated_price_without_currency_raises_value_error(
    wine, fake_provider, knowledge_dir
):
    record = dict(_SAMPLE_RECORD)
    del record["price_currency"]
    _write_cellar(knowledge_dir, {"sample-red-2021": record})

    with pytest.raises(ValueError):
        wine.handle("What's a good Bordeaux vintage from 2015?")

    assert fake_provider.received_prompts == []


def test_cellar_currency_without_estimated_price_raises_value_error(
    wine, fake_provider, knowledge_dir
):
    record = dict(_SAMPLE_RECORD)
    del record["estimated_price"]
    _write_cellar(knowledge_dir, {"sample-red-2021": record})

    with pytest.raises(ValueError):
        wine.handle("What's a good Bordeaux vintage from 2015?")

    assert fake_provider.received_prompts == []


def test_cellar_zero_quantity_record_is_still_validated(wine, fake_provider, knowledge_dir):
    invalid_zero_record = {
        "producer": "Sample Estate",
        "wine_name": "Reserve Red",
        "color": "red",
        "quantity": 0,
        "vintage": 9999,
    }
    _write_cellar(knowledge_dir, {"sample-red-2021": invalid_zero_record})

    with pytest.raises(ValueError):
        wine.handle("What's a good Bordeaux vintage from 2015?")

    assert fake_provider.received_prompts == []


def test_cellar_malformed_json_propagates_as_value_error(wine, fake_provider, knowledge_dir):
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "wine_cellar.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError):
        wine.handle("What's a good Bordeaux vintage from 2015?")

    assert fake_provider.received_prompts == []


def test_valid_cellar_fallback_calls_provider_exactly_once_and_returns_exact_response(
    wine, fake_provider, knowledge_dir
):
    _write_cellar(knowledge_dir, {"sample-red-2021": _SAMPLE_RECORD})
    response = wine.handle("What's a good Bordeaux vintage from 2015?")
    assert len(fake_provider.received_prompts) == 1
    assert response is fake_provider.response


# --- Milestone 29: deterministic cellar lookup -----------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "How many bottles of wine do I have in total?",
        "How many bottles of Reserve Red are in my cellar?",
        "Do I have Reserve Red in my cellar?",
        "Show me my wines from Sample Estate.",
        "What vintages of Reserve Red are in my cellar?",
    ],
)
def test_deterministic_cellar_query_returns_str_and_calls_list_records_exactly_once(
    wine, fake_provider, memory_manager, knowledge_store, knowledge_dir, prompt
):
    _write_cellar(knowledge_dir, {"sample-red-2021": _SAMPLE_RECORD})

    response = wine.handle(prompt)

    assert isinstance(response, str)
    assert knowledge_store.list_records_calls == ["wine_cellar"]
    assert knowledge_store.get_calls == []
    assert memory_manager.recall_calls == []
    assert fake_provider.received_prompts == []


def test_deterministic_total_query_reflects_seeded_cellar(wine, knowledge_dir):
    _write_cellar(knowledge_dir, {"sample-red-2021": _SAMPLE_RECORD})
    response = wine.handle("How many bottles are in my cellar?")
    assert "3 bottles" in response


def test_deterministic_cellar_query_with_no_cellar_namespace_is_still_deterministic(
    wine, fake_provider, knowledge_store
):
    response = wine.handle("How many bottles are in my cellar?")
    assert response == "The active cellar contains 0 bottles across 0 holdings."
    assert knowledge_store.list_records_calls == ["wine_cellar"]
    assert fake_provider.received_prompts == []


@pytest.mark.parametrize(
    "prompt",
    [
        "How many?",
        "Do I own this?",
        "Show me everything.",
        "What do I have?",
        "What vintages exist?",
        "What's a good Bordeaux vintage from 2015?",
    ],
)
def test_unsupported_factual_like_phrasing_falls_through_to_model_backed_fallback(
    wine, fake_provider, prompt
):
    response = wine.handle(prompt)
    assert response is fake_provider.response
    assert len(fake_provider.received_prompts) == 1


def test_deterministic_cellar_query_with_invalid_cellar_data_raises_before_provider_call(
    wine, fake_provider, knowledge_dir
):
    invalid_record = dict(_SAMPLE_RECORD, quantity=-1)
    _write_cellar(knowledge_dir, {"sample-red-2021": invalid_record})

    with pytest.raises(ValueError):
        wine.handle("How many bottles are in my cellar?")

    assert fake_provider.received_prompts == []


def test_pairing_categories_still_take_priority_over_cellar_query_detection(wine, fake_provider):
    # "What wine goes with a steak?" is a pairing prompt, not a cellar query -
    # it must still resolve to the deterministic pairing answer, not fall
    # through to cellar-query parsing or the model fallback.
    response = wine.handle("What wine goes with a steak?")
    assert "red meat" in response
    assert fake_provider.received_prompts == []
