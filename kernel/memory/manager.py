"""
Public entry point to the memory subsystem.

MemoryManager owns the JSONL backend directly. There is only one backend
today, so there is nothing to select between - if a second backend (e.g.
SQLite) is added later, a factory belongs here, not before.
"""

from kernel.memory.base import MemoryEntry
from kernel.memory.jsonl import JSONLBackend


class MemoryManager:
    """Remembers and recalls content on behalf of the rest of the kernel."""

    def __init__(self, settings: dict):
        self._backend = JSONLBackend(settings)

    def remember(self, namespace: str, content: str, metadata: dict | None = None) -> None:
        """Persist a piece of content under a namespace."""

        entry = MemoryEntry(namespace=namespace, content=content, metadata=metadata)
        self._backend.write(entry)

    def recall(self, namespace: str, limit: int | None = None) -> list[MemoryEntry]:
        """Return entries for a namespace, oldest to newest."""

        return self._backend.read(namespace, limit)
