"""
Public interface of the knowledge layer.

Callers outside this package must import from here, never from base.py or
json_store.py directly, so the kernel never needs to know how knowledge is
stored.
"""

from kernel.knowledge.base import KnowledgeStore
from kernel.knowledge.json_store import JSONKnowledgeStore

__all__ = ["KnowledgeStore", "JSONKnowledgeStore"]
