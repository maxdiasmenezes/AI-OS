"""Tests for CapabilityLoader."""

import pytest

from capabilities.loader import CapabilityLoader
from capabilities.wine.capability import WineCapability
from kernel.capabilities import CapabilityRegistry
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


def test_load_wine_returns_wine_capability(loader, fake_provider):
    assert isinstance(loader.load("wine", fake_provider), WineCapability)


def test_load_wine_id_is_wine(loader, fake_provider):
    assert loader.load("wine", fake_provider).id == "wine"


def test_load_wine_returns_distinct_instances(loader, fake_provider):
    assert loader.load("wine", fake_provider) is not loader.load("wine", fake_provider)


def test_load_discovered_capability_without_implementation_raises(loader, fake_provider):
    with pytest.raises(ValueError):
        loader.load("travel", fake_provider)


def test_load_unknown_capability_raises(loader, fake_provider):
    with pytest.raises(ValueError):
        loader.load("nonexistent", fake_provider)
