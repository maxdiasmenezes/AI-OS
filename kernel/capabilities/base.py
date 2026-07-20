"""
Capability contract: the interface every capability implements.

Defines the shape every capability must satisfy, independent of how it is
discovered, loaded, or routed to. Nothing here does that discovery, loading,
or routing.
"""

from abc import ABC, abstractmethod

from kernel.models.base import ModelResponse


class Capability(ABC):
    """Common interface every capability must implement."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Unique identifier for this capability."""
        ...

    @abstractmethod
    def handle(self, prompt: str) -> str | ModelResponse:
        """Handle a single prompt and return a response.

        A plain str means a deterministic response with no model call. A
        ModelResponse means the capability called a model itself and the
        result should carry that model's real metadata.
        """
        ...
