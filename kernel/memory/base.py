"""
Shared record type for the memory subsystem.

MemoryEntry is intentionally dumb - it does not know about capabilities,
namespaces semantics, or storage format. It is the common currency that
manager.py and jsonl.py pass between each other.
"""

from datetime import datetime, timezone


class MemoryEntry:
    """A single piece of remembered content, scoped to a namespace."""

    def __init__(self, namespace: str, content: str, metadata: dict | None = None,
                 timestamp: str | None = None):
        self.namespace = namespace
        self.content = content
        self.metadata = metadata or {}
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
