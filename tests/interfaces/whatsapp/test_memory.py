"""Tests for FixedNamespaceMemory."""

from kernel.memory import MemoryEntry
from interfaces.whatsapp.memory import FixedNamespaceMemory


class RecordingMemory:
    """A minimal memory fake that records every remember()/recall() call it
    receives and stores entries in memory, keyed by namespace."""

    def __init__(self):
        self.remember_calls: list[tuple] = []
        self.recall_calls: list[tuple] = []
        self._entries: list[MemoryEntry] = []

    def remember(self, namespace, content, metadata=None):
        self.remember_calls.append((namespace, content, metadata))
        self._entries.append(MemoryEntry(namespace=namespace, content=content, metadata=metadata))

    def recall(self, namespace, limit=None):
        self.recall_calls.append((namespace, limit))
        entries = [e for e in self._entries if e.namespace == namespace]
        return entries if limit is None else entries[-limit:]


def test_remember_pins_writes_to_the_fixed_namespace_regardless_of_caller_namespace():
    real_memory = RecordingMemory()
    scoped_memory = FixedNamespaceMemory(real_memory)

    scoped_memory.remember("conversation", "hello", metadata={"role": "user"})
    scoped_memory.remember("wine_notes", "a bold Malbec", metadata={"role": "assistant"})

    assert real_memory.remember_calls == [
        ("whatsapp", "hello", {"role": "user"}),
        ("whatsapp", "a bold Malbec", {"role": "assistant"}),
    ]


def test_recall_reads_from_the_fixed_namespace_regardless_of_caller_namespace():
    real_memory = RecordingMemory()
    scoped_memory = FixedNamespaceMemory(real_memory)
    scoped_memory.remember("conversation", "earlier note")

    result = scoped_memory.recall("wine_notes", limit=5)

    assert real_memory.recall_calls == [("whatsapp", 5)]
    assert [e.content for e in result] == ["earlier note"]


def test_default_namespace_is_whatsapp():
    real_memory = RecordingMemory()
    scoped_memory = FixedNamespaceMemory(real_memory)

    scoped_memory.remember("conversation", "hi")

    assert real_memory.remember_calls[0][0] == "whatsapp"


def test_custom_namespace_can_be_supplied():
    real_memory = RecordingMemory()
    scoped_memory = FixedNamespaceMemory(real_memory, namespace="whatsapp-murilo")

    scoped_memory.remember("conversation", "hi")
    scoped_memory.recall("conversation")

    assert real_memory.remember_calls == [("whatsapp-murilo", "hi", None)]
    assert real_memory.recall_calls == [("whatsapp-murilo", None)]
