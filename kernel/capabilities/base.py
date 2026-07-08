"""
Capability contract: the interface every capability implements.

Defines the shape every capability must satisfy, independent of how it is
discovered, loaded, or routed to. Nothing here does that discovery, loading,
or routing.
"""

from abc import ABC, abstractmethod


class Capability(ABC):
    """Common interface every capability must implement."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Unique identifier for this capability."""
        ...

    @abstractmethod
    def handle(self, prompt: str) -> str:
        """Handle a single prompt and return a response."""
        ...
