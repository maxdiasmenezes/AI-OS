"""Tests for CapabilityLoader."""

import pytest

from capabilities.loader import CapabilityLoader
from capabilities.wine.capability import WineCapability
from kernel.capabilities import CapabilityRegistry


@pytest.fixture
def loader(tmp_path):
    (tmp_path / "wine").mkdir()
    (tmp_path / "travel").mkdir()
    registry = CapabilityRegistry(capabilities_dir=tmp_path)
    return CapabilityLoader(registry=registry)


def test_load_wine_returns_wine_capability(loader):
    assert isinstance(loader.load("wine"), WineCapability)


def test_load_wine_id_is_wine(loader):
    assert loader.load("wine").id == "wine"


def test_load_wine_returns_distinct_instances(loader):
    assert loader.load("wine") is not loader.load("wine")


def test_load_discovered_capability_without_implementation_raises(loader):
    with pytest.raises(ValueError):
        loader.load("travel")


def test_load_unknown_capability_raises(loader):
    with pytest.raises(ValueError):
        loader.load("nonexistent")
