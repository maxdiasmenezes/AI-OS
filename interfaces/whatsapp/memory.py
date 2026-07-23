"""
Namespace-scoped memory adapter for the WhatsApp interface.

Orchestrator's memory dependency is typed only against the small
structural SupportsMemory protocol (remember()/recall()), which lets a
composition root inject a delegating adapter instead of a concrete
MemoryManager. FixedNamespaceMemory is that adapter for WhatsApp: it wraps
a real memory manager and pins every call to one fixed namespace,
ignoring whatever namespace the caller (Orchestrator or a capability)
passes in. That keeps all memory written through this interface in a
single namespace, isolated from the CLI or any other interface sharing the
same underlying storage, without mutating or wrapping the memory manager
itself.
"""

from kernel.memory import MemoryEntry


class FixedNamespaceMemory:
    """Delegates remember()/recall() to another memory manager, pinned to one namespace."""

    def __init__(self, memory_manager, namespace: str = "whatsapp") -> None:
        self._memory = memory_manager
        self._namespace = namespace

    def remember(self, namespace: str, content: str, metadata: dict | None = None) -> None:
        self._memory.remember(self._namespace, content, metadata)

    def recall(self, namespace: str, limit: int | None = None) -> list[MemoryEntry]:
        return self._memory.recall(self._namespace, limit)
