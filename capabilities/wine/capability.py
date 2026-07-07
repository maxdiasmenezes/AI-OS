"""
Wine capability: minimal concrete implementation of the Capability contract.
"""

from kernel.capabilities.base import Capability


class WineCapability(Capability):
    """Placeholder AI employee for wine: proves the Capability contract works."""

    @property
    def id(self) -> str:
        return "wine"

    def handle(self, prompt: str) -> str:
        return f"wine capability received: {prompt}"
