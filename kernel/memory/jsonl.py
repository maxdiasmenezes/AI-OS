"""
Persists memory entries as JSON Lines files, one per namespace.

This is the only module that knows memory is stored as JSONL on disk - the
rest of the kernel only sees MemoryManager.
"""

import json
from pathlib import Path

from kernel.memory.base import MemoryEntry

# Paths are resolved relative to this file, not the current working
# directory, matching kernel/config/config.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class JSONLBackend:
    """Reads and writes memory entries as JSONL files under a directory."""

    def __init__(self, settings: dict):
        self.dir = _PROJECT_ROOT / settings["storage_dir"]

    def write(self, entry: MemoryEntry) -> None:
        """Append a single memory entry to its namespace's JSONL file."""

        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{entry.namespace}.jsonl"

        record = {
            "timestamp": entry.timestamp,
            "namespace": entry.namespace,
            "content": entry.content,
            "metadata": entry.metadata,
        }

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def read(self, namespace: str, limit: int | None = None) -> list[MemoryEntry]:
        """Return entries for a namespace, oldest to newest."""

        path = self.dir / f"{namespace}.jsonl"
        if not path.exists():
            return []

        entries = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                entries.append(MemoryEntry(
                    namespace=record["namespace"],
                    content=record["content"],
                    metadata=record["metadata"],
                    timestamp=record["timestamp"],
                ))

        if limit is not None:
            entries = entries[-limit:]

        return entries
