"""
Capability loader: given a capability id, returns an instance of that
capability's implementation.

This is the one place allowed to know about concrete capability classes.
Discovery of what capabilities exist stays in the kernel's
CapabilityRegistry; this module only maps a known id to its class and
instantiates it.
"""

from kernel.capabilities import CapabilityRegistry
from kernel.capabilities.base import Capability

from capabilities.wine.capability import WineCapability

_CAPABILITY_CLASSES: dict[str, type[Capability]] = {
    "wine": WineCapability,
}


class CapabilityLoader:
    """Instantiates capability implementations by id."""

    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self._registry = registry or CapabilityRegistry()

    def load(self, capability_id: str) -> Capability:
        """Return a new instance of the capability with the given id."""

        if self._registry.get_capability(capability_id) is None:
            raise ValueError(f"unknown capability: {capability_id}")

        capability_class = _CAPABILITY_CLASSES.get(capability_id)
        if capability_class is None:
            raise ValueError(f"no implementation registered for capability: {capability_id}")

        return capability_class()
