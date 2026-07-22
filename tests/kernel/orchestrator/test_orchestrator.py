"""Tests for Orchestrator.handle()."""

import json
from pathlib import Path
from unittest.mock import ANY, Mock

import pytest

from capabilities.loader import CapabilityLoader
from capabilities.wine.capability import WineCapability
from kernel.capabilities.base import Capability
from kernel.config.config import Config
from kernel.knowledge import JSONKnowledgeStore, KnowledgeStore
from kernel.memory import MemoryEntry, MemoryManager
from kernel.models.base import ModelProvider, ModelResponse
from kernel.orchestrator import Orchestrator

# tests/kernel/orchestrator/test_orchestrator.py -> tests/kernel -> tests -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SYSTEM_PROMPT = (_PROJECT_ROOT / "prompts" / "system.md").read_text(encoding="utf-8").strip()


class FakeModelProvider(ModelProvider):
    """Records every prompt it receives and returns a fixed response."""

    def __init__(self, response: ModelResponse):
        self._response = response
        self.received_prompts: list[str] = []

    def send_prompt(self, prompt: str) -> ModelResponse:
        self.received_prompts.append(prompt)
        return self._response


class FakeCapability(Capability):
    """Records the prompt it receives and returns a fixed response."""

    def __init__(self, capability_id: str, response_text: str):
        self._id = capability_id
        self._response_text = response_text
        self.received_prompts: list[str] = []

    @property
    def id(self) -> str:
        return self._id

    def handle(self, prompt: str) -> str:
        self.received_prompts.append(prompt)
        return self._response_text


class FakeModelBackedCapability(Capability):
    """A capability that calls a model itself and returns its ModelResponse."""

    def __init__(self, capability_id: str, response: ModelResponse):
        self._id = capability_id
        self._response = response
        self.received_prompts: list[str] = []

    @property
    def id(self) -> str:
        return self._id

    def handle(self, prompt: str) -> ModelResponse:
        self.received_prompts.append(prompt)
        return self._response


class FakeBadCapability(Capability):
    """A capability that returns a result type the orchestrator must reject."""

    def __init__(self, capability_id: str):
        self._id = capability_id

    @property
    def id(self) -> str:
        return self._id

    def handle(self, prompt: str):
        return 12345


class RecordingMemory:
    """A minimal, structurally compatible memory fake that records every
    remember()/recall() call it receives, without inheriting from
    MemoryManager (standing in for a future FixedNamespaceMemory adapter)."""

    def __init__(self):
        self.remember_calls: list[tuple] = []
        self.recall_calls: list[tuple] = []
        self._entries: list[MemoryEntry] = []

    def remember(self, namespace, content, metadata=None):
        self.remember_calls.append((namespace, content, metadata))
        self._entries.append(MemoryEntry(namespace=namespace, content=content, metadata=metadata))

    def recall(self, namespace, limit=None):
        self.recall_calls.append((namespace, limit))
        entries = [e for e in self._entries if e.namespace == namespace]
        return entries if limit is None else entries[-limit:]


class MemoryUsingCapability(Capability):
    """A capability that reads from the memory dependency it was given,
    proving a capability can use the exact object the loader received."""

    def __init__(self, capability_id: str, response_text: str, memory):
        self._id = capability_id
        self._response_text = response_text
        self._memory = memory
        self.recalled_at_handle_time: list = []

    @property
    def id(self) -> str:
        return self._id

    def handle(self, prompt: str) -> str:
        self.recalled_at_handle_time = self._memory.recall("conversation")
        return self._response_text


def _make_config(tmp_path: Path) -> Config:
    return Config(
        provider="fake",
        provider_settings={},
        log_path=tmp_path / "logs" / "interactions.jsonl",
        memory_settings={"storage_dir": str(tmp_path / "memory")},
        knowledge_storage_dir=tmp_path / "knowledge",
    )


def _make_orchestrator(monkeypatch, config, fake_provider, capability_loader):
    # Patch the symbol the orchestrator module imported, not kernel.models.get_provider.
    monkeypatch.setattr(
        "kernel.orchestrator.orchestrator.get_provider",
        lambda cfg: fake_provider,
    )
    return Orchestrator(config, capability_loader=capability_loader)


def _make_orchestrator_with_memory(monkeypatch, config, fake_provider, capability_loader, memory_manager):
    monkeypatch.setattr(
        "kernel.orchestrator.orchestrator.get_provider",
        lambda cfg: fake_provider,
    )
    return Orchestrator(
        config,
        capability_loader=capability_loader,
        memory_manager=memory_manager,
    )


def _fake_response(text: str = "fallback response") -> ModelResponse:
    return ModelResponse(
        text=text,
        model="fake-model",
        input_tokens=11,
        output_tokens=22,
        latency_seconds=0.5,
    )


def _read_last_log_record(log_path: Path) -> dict:
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


# --- Routed branch -----------------------------------------------------


def test_wine_prompt_routes_to_capability_and_skips_model(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_capability = FakeCapability("wine", "a bold Malbec would work well")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What wine goes with steak?")

    loader.assert_called_once_with("wine", fake_provider, ANY, ANY)
    assert fake_capability.received_prompts == ["What wine goes with steak?"]
    assert response.text == "a bold Malbec would work well"
    assert response.model == "capability:wine"
    assert fake_provider.received_prompts == []


def test_natural_wine_intent_prompt_without_word_wine_routes_to_capability(monkeypatch, tmp_path):
    # "What should I drink with steak?" contains no literal "wine" but is a
    # natural wine-selection request, so it must still follow the routed
    # branch (CapabilityRouter's new deterministic natural-phrase rules).
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_capability = FakeCapability("wine", "a robust Cabernet Sauvignon would work well")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What should I drink with steak?")

    loader.assert_called_once_with("wine", fake_provider, ANY, ANY)
    assert fake_capability.received_prompts == ["What should I drink with steak?"]
    assert response.text == "a robust Cabernet Sauvignon would work well"
    assert response.model == "capability:wine"
    assert fake_provider.received_prompts == []


def test_orchestrator_passes_its_own_provider_and_memory_manager_to_loader(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_capability = FakeCapability("wine", "a bold Malbec would work well")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    orchestrator.handle("What wine goes with steak?")

    loader.assert_called_once()
    call_id, call_provider, call_memory, call_knowledge = loader.call_args.args
    assert call_id == "wine"
    assert call_provider is fake_provider
    assert isinstance(call_memory, MemoryManager)
    assert isinstance(call_knowledge, KnowledgeStore)

    # Prove it's the same MemoryManager instance the orchestrator itself
    # persists to (not just any instance), by recalling through the
    # reference the loader received.
    entries = call_memory.recall("conversation")
    assert [e.content for e in entries] == [
        "What wine goes with steak?",
        "a bold Malbec would work well",
    ]


def test_orchestrator_passes_its_own_knowledge_store_to_loader(monkeypatch, tmp_path):
    # Prove it's the same KnowledgeStore instance the orchestrator itself
    # was configured with (not just any instance), by seeding a synthetic
    # profile under the configured storage dir and reading it back through
    # the reference the loader received.
    config = _make_config(tmp_path)
    knowledge_dir = config.knowledge_storage_dir
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "wine_profile.json").write_text(
        json.dumps({"profile": {"notes": "Prefers Old World wines."}}),
        encoding="utf-8",
    )

    fake_provider = FakeModelProvider(_fake_response())
    fake_capability = FakeCapability("wine", "a bold Malbec would work well")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    orchestrator.handle("What wine goes with steak?")

    _, _, _, call_knowledge = loader.call_args.args
    assert call_knowledge.get("wine_profile", "profile") == {
        "notes": "Prefers Old World wines."
    }


# --- Routed branch: model-backed capability results ----------------------


def test_capability_returning_model_response_preserves_its_metadata(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    capability_response = ModelResponse(
        text="A Loire Valley Sauvignon Blanc is a versatile, food-friendly choice.",
        model="real-provider-model",
        input_tokens=17,
        output_tokens=29,
        latency_seconds=0.42,
    )
    fake_capability = FakeModelBackedCapability("wine", capability_response)
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What's a good wine region to explore?")

    assert response is capability_response
    assert response.model == "real-provider-model"
    assert response.input_tokens == 17
    assert response.output_tokens == 29
    assert response.latency_seconds == 0.42
    assert fake_provider.received_prompts == []

    record = _read_last_log_record(config.log_path)
    assert record["response"] == capability_response.text
    assert record["model"] == "real-provider-model"
    assert record["input_tokens"] == 17
    assert record["output_tokens"] == 29
    assert record["latency_seconds"] == 0.42


def test_capability_returning_unsupported_type_raises_type_error(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    loader = Mock(return_value=FakeBadCapability("wine"))

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)

    with pytest.raises(TypeError):
        orchestrator.handle("What's a good wine region to explore?")


# --- Routed branch: real WineCapability integration -----------------------


def test_orchestrator_with_real_wine_capability_preserves_fallback_metadata(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fallback_response = ModelResponse(
        text="A Loire Valley Sauvignon Blanc is a versatile, food-friendly choice.",
        model="fake-model",
        input_tokens=17,
        output_tokens=29,
        latency_seconds=0.42,
    )
    fake_provider = FakeModelProvider(fallback_response)
    wine_memory = MemoryManager(config.memory_settings)
    wine_knowledge = JSONKnowledgeStore(config.knowledge_storage_dir)
    loader = Mock(return_value=WineCapability(fake_provider, wine_memory, wine_knowledge))

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    # Contains the whole word "wine" (routes to WineCapability) but avoids
    # all eight deterministic food categories, so it hits the fallback path.
    response = orchestrator.handle("What's a good wine region to explore?")

    assert response is fallback_response
    assert response.text == fallback_response.text
    assert response.model == "fake-model"
    assert response.input_tokens == 17
    assert response.output_tokens == 29
    assert response.latency_seconds == 0.42

    record = _read_last_log_record(config.log_path)
    assert record["response"] == fallback_response.text
    assert record["model"] == "fake-model"
    assert record["input_tokens"] == 17
    assert record["output_tokens"] == 29
    assert record["latency_seconds"] == 0.42


def test_end_to_end_routed_wine_fallback_sees_conversation_history_under_tmp_path(
    monkeypatch, tmp_path
):
    config = _make_config(tmp_path)
    fallback_response = ModelResponse(
        text="A Loire Valley Sauvignon Blanc is a versatile, food-friendly choice.",
        model="fake-model",
        input_tokens=17,
        output_tokens=29,
        latency_seconds=0.42,
    )
    fake_provider = FakeModelProvider(fallback_response)

    # Seed conversation memory under tmp_path before running the request,
    # using the same storage settings the orchestrator's MemoryManager reads.
    seed_memory = MemoryManager(config.memory_settings)
    seed_memory.remember("conversation", "What's a good everyday red?", metadata={"role": "user"})
    seed_memory.remember("conversation", "Try a Cotes du Rhone.", metadata={"role": "assistant"})

    real_loader = CapabilityLoader()
    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, real_loader.load)
    # Contains the whole word "wine" but avoids all eight deterministic food
    # categories, so it hits WineCapability's model-backed fallback.
    response = orchestrator.handle("What's a good wine region to explore?")

    assert response is fallback_response
    assert len(fake_provider.received_prompts) == 1
    sent_prompt = fake_provider.received_prompts[0]
    assert "Try a Cotes du Rhone." in sent_prompt
    assert "What's a good wine region to explore?" in sent_prompt

    record = _read_last_log_record(config.log_path)
    assert record["model"] == "fake-model"
    assert record["input_tokens"] == 17
    assert record["output_tokens"] == 29
    assert record["latency_seconds"] == 0.42


def test_end_to_end_routed_wine_fallback_sees_synthetic_profile_under_tmp_path(
    monkeypatch, tmp_path
):
    config = _make_config(tmp_path)
    fallback_response = ModelResponse(
        text="A Loire Valley Sauvignon Blanc is a versatile, food-friendly choice.",
        model="fake-model",
        input_tokens=17,
        output_tokens=29,
        latency_seconds=0.42,
    )
    fake_provider = FakeModelProvider(fallback_response)

    # Seed a synthetic wine profile under tmp_path before running the
    # request, using the same storage dir the orchestrator's KnowledgeStore
    # reads from.
    config.knowledge_storage_dir.mkdir(parents=True)
    (config.knowledge_storage_dir / "wine_profile.json").write_text(
        json.dumps({"profile": {"preferred_styles": ["dry Riesling", "Barolo"]}}),
        encoding="utf-8",
    )

    real_loader = CapabilityLoader()
    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, real_loader.load)
    response = orchestrator.handle("What's a good wine region to explore?")

    assert response is fallback_response
    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine profile:" in sent_prompt
    assert "Preferred styles: dry Riesling, Barolo" in sent_prompt


def test_end_to_end_routed_wine_fallback_sees_synthetic_cellar_under_tmp_path(
    monkeypatch, tmp_path
):
    # Milestone 27: prove the cellar inventory reaches WineCapability's
    # fallback prompt through the real CapabilityLoader and the
    # orchestrator's own KnowledgeStore instance, all under tmp_path.
    config = _make_config(tmp_path)
    fallback_response = ModelResponse(
        text="A Loire Valley Sauvignon Blanc is a versatile, food-friendly choice.",
        model="fake-model",
        input_tokens=17,
        output_tokens=29,
        latency_seconds=0.42,
    )
    fake_provider = FakeModelProvider(fallback_response)

    config.knowledge_storage_dir.mkdir(parents=True)
    (config.knowledge_storage_dir / "wine_cellar.json").write_text(
        json.dumps(
            {
                "sample-red-2021": {
                    "producer": "Sample Estate",
                    "wine_name": "Reserve Red",
                    "color": "red",
                    "quantity": 3,
                    "vintage": 2021,
                }
            }
        ),
        encoding="utf-8",
    )

    real_loader = CapabilityLoader()
    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, real_loader.load)
    # Contains the whole word "wine" but avoids all eight deterministic food
    # categories, so it hits WineCapability's model-backed fallback.
    response = orchestrator.handle("What's a good wine region to explore?")

    assert response is fallback_response
    assert len(fake_provider.received_prompts) == 1
    sent_prompt = fake_provider.received_prompts[0]
    assert "Personal wine cellar:" in sent_prompt
    assert "Cellar ID: sample-red-2021" in sent_prompt
    assert "Producer: Sample Estate" in sent_prompt
    assert "Quantity: 3" in sent_prompt

    record = _read_last_log_record(config.log_path)
    assert record["model"] == "fake-model"
    assert record["input_tokens"] == 17
    assert record["output_tokens"] == 29
    assert record["latency_seconds"] == 0.42

    # Nothing should have been written under the repository's real storage/.
    assert not (_PROJECT_ROOT / "storage" / "knowledge" / "wine_cellar.json").exists()


# --- Milestone 29: deterministic cellar lookup, real WineCapability -------


def test_end_to_end_deterministic_cellar_total_query_skips_provider(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    # The fake provider's response must never be used on this path - if it
    # were, the assertions on model/token/latency below would fail.
    fake_provider = FakeModelProvider(_fake_response("should not be used"))

    config.knowledge_storage_dir.mkdir(parents=True)
    (config.knowledge_storage_dir / "wine_cellar.json").write_text(
        json.dumps(
            {
                "sample-red-2021": {
                    "producer": "Sample Estate",
                    "wine_name": "Reserve Red",
                    "color": "red",
                    "quantity": 3,
                    "vintage": 2021,
                }
            }
        ),
        encoding="utf-8",
    )

    real_loader = CapabilityLoader()
    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, real_loader.load)
    response = orchestrator.handle("How many bottles of wine do I have in total?")

    assert fake_provider.received_prompts == []
    assert "3 bottles" in response.text
    assert response.model == "capability:wine"
    assert response.input_tokens == 0
    assert response.output_tokens == 0
    assert response.latency_seconds == 0.0

    record = _read_last_log_record(config.log_path)
    assert record["prompt"] == "How many bottles of wine do I have in total?"
    assert record["response"] == response.text
    assert record["model"] == "capability:wine"
    assert record["input_tokens"] == 0
    assert record["output_tokens"] == 0
    assert record["latency_seconds"] == 0.0

    memory = MemoryManager(config.memory_settings)
    entries = memory.recall("conversation")
    assert len(entries) == 2
    assert entries[0].content == "How many bottles of wine do I have in total?"
    assert entries[0].metadata["role"] == "user"
    assert entries[1].content == response.text
    assert entries[1].metadata["role"] == "assistant"

    # Nothing should have been written under the repository's real storage/.
    assert not (_PROJECT_ROOT / "storage" / "knowledge" / "wine_cellar.json").exists()


# --- Fallback branch -----------------------------------------------------


def test_unrelated_prompt_uses_model_provider(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fixed_response = _fake_response("the weather tomorrow is sunny")
    fake_provider = FakeModelProvider(fixed_response)
    loader = Mock()

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What's the weather tomorrow?")

    loader.assert_not_called()
    assert len(fake_provider.received_prompts) == 1
    assert response is fixed_response


def test_fallback_prompt_contains_system_memory_and_user_prompt_in_order(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    loader = Mock()

    # Seed conversation memory using the same storage settings the
    # orchestrator's own MemoryManager will read from.
    seed_memory = MemoryManager(config.memory_settings)
    seed_memory.remember("conversation", "What's a good everyday red?", metadata={"role": "user"})
    seed_memory.remember("conversation", "Try a Cotes du Rhone.", metadata={"role": "assistant"})

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    orchestrator.handle("What's the weather tomorrow?")

    loader.assert_not_called()
    assert len(fake_provider.received_prompts) == 1
    sent_prompt = fake_provider.received_prompts[0]

    system_index = sent_prompt.index(_SYSTEM_PROMPT)
    memory_index = sent_prompt.index("Try a Cotes du Rhone.")
    user_index = sent_prompt.index("What's the weather tomorrow?")

    assert system_index < memory_index < user_index


# --- Shared: memory persistence -----------------------------------------


def test_routed_branch_persists_user_prompt_and_response_to_memory(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_capability = FakeCapability("wine", "a crisp Sauvignon Blanc")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What wine goes with fish?")

    memory = MemoryManager(config.memory_settings)
    entries = memory.recall("conversation")

    assert len(entries) == 2
    assert entries[0].content == "What wine goes with fish?"
    assert entries[0].metadata["role"] == "user"
    assert entries[1].content == response.text
    assert entries[1].metadata["role"] == "assistant"


def test_fallback_branch_persists_user_prompt_and_response_to_memory(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fixed_response = _fake_response("it's sunny tomorrow")
    fake_provider = FakeModelProvider(fixed_response)
    loader = Mock()

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    orchestrator.handle("What's the weather tomorrow?")

    memory = MemoryManager(config.memory_settings)
    entries = memory.recall("conversation")

    assert len(entries) == 2
    assert entries[0].content == "What's the weather tomorrow?"
    assert entries[0].metadata["role"] == "user"
    assert entries[1].content == "it's sunny tomorrow"
    assert entries[1].metadata["role"] == "assistant"


# --- Shared: interaction logging -----------------------------------------


def test_routed_branch_logs_interaction(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_capability = FakeCapability("wine", "a light Pinot Noir")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What wine goes with salmon?")

    record = _read_last_log_record(config.log_path)
    assert record["prompt"] == "What wine goes with salmon?"
    assert record["response"] == response.text
    assert record["model"] == "capability:wine"
    assert record["input_tokens"] == 0
    assert record["output_tokens"] == 0
    assert record["latency_seconds"] == 0.0


def test_fallback_branch_logs_interaction(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fixed_response = _fake_response("it's sunny tomorrow")
    fake_provider = FakeModelProvider(fixed_response)
    loader = Mock()

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    orchestrator.handle("What's the weather tomorrow?")

    record = _read_last_log_record(config.log_path)
    assert record["prompt"] == "What's the weather tomorrow?"
    assert record["response"] == "it's sunny tomorrow"
    assert record["model"] == "fake-model"
    assert record["input_tokens"] == 11
    assert record["output_tokens"] == 22
    assert record["latency_seconds"] == 0.5


# --- Milestone 32A: memory injection seam ---------------------------------


def test_default_construction_constructs_exactly_one_memory_manager_from_config(
    monkeypatch, tmp_path
):
    # A. Default construction still builds a real MemoryManager from
    # config.memory_settings, and builds exactly one.
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    loader = Mock(return_value=FakeCapability("wine", "a bold Malbec would work well"))
    monkeypatch.setattr(
        "kernel.orchestrator.orchestrator.get_provider", lambda cfg: fake_provider
    )

    constructed_settings = []
    original_init = MemoryManager.__init__

    def spy_init(self, settings):
        constructed_settings.append(settings)
        original_init(self, settings)

    monkeypatch.setattr(MemoryManager, "__init__", spy_init)

    Orchestrator(config, capability_loader=loader)

    assert constructed_settings == [config.memory_settings]


def test_positional_construction_without_memory_manager_still_works(monkeypatch, tmp_path):
    # E. The existing two-positional-argument construction remains valid.
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    loader = Mock()
    monkeypatch.setattr(
        "kernel.orchestrator.orchestrator.get_provider", lambda cfg: fake_provider
    )

    orchestrator = Orchestrator(config, loader)

    assert isinstance(orchestrator, Orchestrator)


def test_memory_manager_parameter_is_keyword_only(monkeypatch, tmp_path):
    # E. The injected memory dependency cannot be passed positionally.
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    loader = Mock()
    fake_memory = RecordingMemory()
    monkeypatch.setattr(
        "kernel.orchestrator.orchestrator.get_provider", lambda cfg: fake_provider
    )

    with pytest.raises(TypeError):
        Orchestrator(config, loader, fake_memory)


def test_injecting_memory_manager_skips_constructing_a_new_one(monkeypatch, tmp_path):
    # B. Supplying a memory dependency means no second MemoryManager is
    # constructed.
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_memory = RecordingMemory()
    loader = Mock(return_value=FakeCapability("wine", "a bold Malbec would work well"))
    monkeypatch.setattr(
        "kernel.orchestrator.orchestrator.get_provider", lambda cfg: fake_provider
    )

    constructed_settings = []
    original_init = MemoryManager.__init__

    def spy_init(self, settings):
        constructed_settings.append(settings)
        original_init(self, settings)

    monkeypatch.setattr(MemoryManager, "__init__", spy_init)

    Orchestrator(config, capability_loader=loader, memory_manager=fake_memory)

    assert constructed_settings == []


def test_capability_loader_receives_the_exact_injected_memory_object(monkeypatch, tmp_path):
    # B & D. capability_loader receives the exact injected object, not a
    # newly constructed or wrapped one.
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_memory = RecordingMemory()
    captured = {}

    def loader(capability_id, provider, memory, knowledge):
        captured["memory"] = memory
        return FakeCapability(capability_id, "a bold Malbec would work well")

    orchestrator = _make_orchestrator_with_memory(
        monkeypatch, config, fake_provider, loader, fake_memory
    )
    orchestrator.handle("What wine goes with steak?")

    assert captured["memory"] is fake_memory


def test_fallback_path_uses_injected_memory_for_recall_and_remember(monkeypatch, tmp_path):
    # C. Orchestrator's own recall/remember operations reach the injected
    # memory object, exercised via the model-fallback path.
    config = _make_config(tmp_path)
    fixed_response = _fake_response("it's sunny tomorrow")
    fake_provider = FakeModelProvider(fixed_response)
    fake_memory = RecordingMemory()
    loader = Mock()

    orchestrator = _make_orchestrator_with_memory(
        monkeypatch, config, fake_provider, loader, fake_memory
    )
    response = orchestrator.handle("What's the weather tomorrow?")

    loader.assert_not_called()
    assert response is fixed_response
    assert fake_memory.recall_calls == [("conversation", 10)]
    assert fake_memory.remember_calls == [
        ("conversation", "What's the weather tomorrow?", {"role": "user"}),
        ("conversation", "it's sunny tomorrow", {"role": "assistant"}),
    ]


def test_routed_path_remember_calls_reach_the_injected_memory_object(monkeypatch, tmp_path):
    # C. The remember() calls Orchestrator makes after a routed capability
    # response also reach the injected memory object.
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_memory = RecordingMemory()
    fake_capability = FakeCapability("wine", "a bold Malbec would work well")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator_with_memory(
        monkeypatch, config, fake_provider, loader, fake_memory
    )
    orchestrator.handle("What wine goes with steak?")

    assert fake_memory.remember_calls == [
        ("conversation", "What wine goes with steak?", {"role": "user"}),
        ("conversation", "a bold Malbec would work well", {"role": "assistant"}),
    ]


def test_capability_can_recall_through_the_exact_injected_memory_object(monkeypatch, tmp_path):
    # D. A capability that uses the memory dependency it was given reaches
    # the exact same injected object, seeing data seeded on it beforehand.
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_memory = RecordingMemory()
    fake_memory.remember("conversation", "earlier note", metadata={"role": "assistant"})

    captured_capability = {}

    def loader(capability_id, provider, memory, knowledge):
        capability = MemoryUsingCapability(
            capability_id, "a bold Malbec would work well", memory
        )
        captured_capability["capability"] = capability
        return capability

    orchestrator = _make_orchestrator_with_memory(
        monkeypatch, config, fake_provider, loader, fake_memory
    )
    orchestrator.handle("What wine goes with steak?")

    capability = captured_capability["capability"]
    assert [e.content for e in capability.recalled_at_handle_time] == ["earlier note"]
