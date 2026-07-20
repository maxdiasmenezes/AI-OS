"""
Contract every knowledge store must satisfy.

Read-only by design: a knowledge store answers lookups against domain
knowledge that was placed there by some other process. Nothing here writes,
updates, deletes, searches, or ranks - only get and list_records.
"""

from abc import ABC, abstractmethod


class KnowledgeStore(ABC):
    """Common interface every knowledge store must implement."""

    @abstractmethod
    def get(self, namespace: str, key: str) -> dict[str, object] | None:
        """Return a single record by key, or None if it does not exist."""
        ...

    @abstractmethod
    def list_records(self, namespace: str) -> dict[str, dict[str, object]]:
        """Return all records in a namespace, keyed by record id."""
        ...
