"""
Capability contract: the interface every capability implements.

Defines the shape every capability must satisfy, independent of how it is
discovered, loaded, or routed to. Nothing here does that discovery, loading,
or routing.
"""

from abc import ABC, abstractmethod

from kernel.models.base import ModelResponse


class EphemeralResult(str):
    """A capability response whose exchange must never be persisted.

    Orchestrator.handle() unconditionally writes every prompt/response
    pair to memory and the interaction log after a normal `str` or
    ModelResponse capability result (see kernel/orchestrator/orchestrator.py).
    A capability that returns EphemeralResult instead signals that this
    specific exchange - not the whole capability - must skip both writes
    (Milestone 38: `/knowledge ask` sends private question text and local
    excerpts to a model provider and must never persist either; `/knowledge
    search` sends query text that has the same requirement).

    Deliberately a str subclass, not a separate wrapper type: every place
    that already does `capability.handle(prompt) == "some text"` (existing
    capability tests, MessageHandler._extract_response_text's isinstance(str)
    branch) keeps working unmodified, because an EphemeralResult *is* a
    str. Only Orchestrator.handle() needs to know the difference, via an
    isinstance(result, EphemeralResult) check performed before the plain
    isinstance(result, str) branch.
    """


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
