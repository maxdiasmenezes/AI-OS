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

    # Concrete, non-abstract: defaults to False so every existing and
    # future capability stays reachable exactly as before unless it
    # explicitly opts in. A capability that sets this True (e.g.
    # capabilities/tasks/TasksCapability) is refused by Orchestrator.handle()
    # - its handle() is never even called - unless the request's
    # RequestContext explicitly grants allow_computer_actions. See
    # kernel/orchestrator/context.py and kernel/orchestrator/orchestrator.py.
    requires_computer_actions: bool = False

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
