"""
Public interface of the memory layer.

Callers outside this package must import from here, never from base.py or
jsonl.py directly, so the kernel never needs to know how memory is stored.
"""

from kernel.memory.base import MemoryEntry
from kernel.memory.manager import MemoryManager

__all__ = ["MemoryEntry", "MemoryManager"]
