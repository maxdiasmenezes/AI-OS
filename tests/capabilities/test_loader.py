"""Tests for CapabilityLoader."""

import json

import pytest

from capabilities.loader import CapabilityLoader
from capabilities.wine.capability import WineCapability
from kernel.capabilities import CapabilityRegistry
from kernel.knowledge import JSONKnowledgeStore
from kernel.memory import MemoryManager
from kernel.models.base import ModelProvider, ModelResponse


class FakeModelProvider(ModelProvider):
    """Minimal provider double; no external calls."""

    def __init__(self):
        pass

    def send_prompt(self, prompt: str) -> ModelResponse:
        raise AssertionError("provider should not be called in Milestone 21")


@pytest.fixture
def loader(tmp_path):
    (tmp_path / "wine").mkdir()
    (tmp_path / "travel").mkdir()
    registry = CapabilityRegistry(capabilities_dir=tmp_path)
    return CapabilityLoader(registry=registry)


@pytest.fixture
def fake_provider():
    return FakeModelProvider()


@pytest.fixture
def memory_manager(tmp_path):
    return MemoryManager({"storage_dir": str(tmp_path / "memory")})


@pytest.fixture
def knowledge_store(tmp_path):
    return JSONKnowledgeStore(tmp_path / "knowledge")


def test_load_wine_returns_wine_capability(loader, fake_provider, memory_manager, knowledge_store):
    assert isinstance(
        loader.load("wine", fake_provider, memory_manager, knowledge_store), WineCapability
    )


def test_load_wine_id_is_wine(loader, fake_provider, memory_manager, knowledge_store):
    assert loader.load("wine", fake_provider, memory_manager, knowledge_store).id == "wine"


def test_load_wine_returns_distinct_instances(loader, fake_provider, memory_manager, knowledge_store):
    assert loader.load("wine", fake_provider, memory_manager, knowledge_store) is not loader.load(
        "wine", fake_provider, memory_manager, knowledge_store
    )


def test_load_discovered_capability_without_implementation_raises(
    loader, fake_provider, memory_manager, knowledge_store
):
    with pytest.raises(ValueError):
        loader.load("travel", fake_provider, memory_manager, knowledge_store)


def test_load_unknown_capability_raises(loader, fake_provider, memory_manager, knowledge_store):
    with pytest.raises(ValueError):
        loader.load("nonexistent", fake_provider, memory_manager, knowledge_store)


def test_load_wine_passes_memory_manager_used_by_fallback(loader, memory_manager, knowledge_store):
    # Prove the loader wires the given MemoryManager into WineCapability by
    # observing its effect: a recalled entry surfaces in the fallback prompt.
    memory_manager.remember("conversation", "I love bold reds.", metadata={"role": "user"})
    provider = _RecordingProvider()

    wine = loader.load("wine", provider, memory_manager, knowledge_store)
    wine.handle("What's a good wine region to explore?")

    assert any("I love bold reds." in p for p in provider.received_prompts)


def test_load_wine_passes_provider_used_by_fallback(loader, memory_manager, knowledge_store):
    provider = _RecordingProvider()
    wine = loader.load("wine", provider, memory_manager, knowledge_store)

    response = wine.handle("What's a good wine region to explore?")

    assert response is provider.response
    assert len(provider.received_prompts) == 1


def test_load_wine_passes_knowledge_store_used_by_fallback(loader, memory_manager, tmp_path):
    # Prove the loader wires the given KnowledgeStore into WineCapability by
    # observing its effect: a profile record surfaces in the fallback prompt.
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "wine_profile.json").write_text(
        json.dumps({"profile": {"notes": "Prefers Old World wines."}}),
        encoding="utf-8",
    )
    knowledge_store = JSONKnowledgeStore(knowledge_dir)
    provider = _RecordingProvider()

    wine = loader.load("wine", provider, memory_manager, knowledge_store)
    wine.handle("What's a good wine region to explore?")

    assert any("Prefers Old World wines." in p for p in provider.received_prompts)


class _RecordingProvider(ModelProvider):
    """Records prompts and returns a fixed response, to prove wiring by behavior."""

    def __init__(self):
        self.received_prompts: list[str] = []
        self.response = ModelResponse(
            text="A Burgundy Pinot Noir is a great place to start.",
            model="fake-model",
            input_tokens=5,
            output_tokens=10,
            latency_seconds=0.1,
        )

    def send_prompt(self, prompt: str) -> ModelResponse:
        self.received_prompts.append(prompt)
        return self.response
