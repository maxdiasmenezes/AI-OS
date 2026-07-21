"""Tests for CapabilityLoader."""

import pytest

from capabilities.loader import CapabilityLoader
from capabilities.wine.capability import WineCapability
from kernel.capabilities import CapabilityRegistry
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


def test_load_wine_returns_wine_capability(loader, fake_provider, memory_manager):
    assert isinstance(loader.load("wine", fake_provider, memory_manager), WineCapability)


def test_load_wine_id_is_wine(loader, fake_provider, memory_manager):
    assert loader.load("wine", fake_provider, memory_manager).id == "wine"


def test_load_wine_returns_distinct_instances(loader, fake_provider, memory_manager):
    assert loader.load("wine", fake_provider, memory_manager) is not loader.load(
        "wine", fake_provider, memory_manager
    )


def test_load_discovered_capability_without_implementation_raises(loader, fake_provider, memory_manager):
    with pytest.raises(ValueError):
        loader.load("travel", fake_provider, memory_manager)


def test_load_unknown_capability_raises(loader, fake_provider, memory_manager):
    with pytest.raises(ValueError):
        loader.load("nonexistent", fake_provider, memory_manager)


def test_load_wine_passes_memory_manager_used_by_fallback(loader, memory_manager):
    # Prove the loader wires the given MemoryManager into WineCapability by
    # observing its effect: a recalled entry surfaces in the fallback prompt.
    memory_manager.remember("conversation", "I love bold reds.", metadata={"role": "user"})
    provider = _RecordingProvider()

    wine = loader.load("wine", provider, memory_manager)
    wine.handle("What's a good wine region to explore?")

    assert any("I love bold reds." in p for p in provider.received_prompts)


def test_load_wine_passes_provider_used_by_fallback(loader, memory_manager):
    provider = _RecordingProvider()
    wine = loader.load("wine", provider, memory_manager)

    response = wine.handle("What's a good wine region to explore?")

    assert response is provider.response
    assert len(provider.received_prompts) == 1


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
