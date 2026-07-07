"""
Assembles the final prompt sent to a model provider.

This is the only place that combines a user prompt with recalled memory -
it reuses MemoryManager.recall() as-is and does not alter how memory is
written.
"""

from kernel.memory import MemoryManager

_NAMESPACE = "conversation"
_LIMIT = 10


def build_prompt(user_prompt: str, memory: MemoryManager) -> str:
    """Prepend the last 10 conversation memories to a user prompt."""

    entries = memory.recall(_NAMESPACE, limit=_LIMIT)
    if not entries:
        return user_prompt

    transcript = "\n".join(f"{e.metadata['role']}: {e.content}" for e in entries)
    return f"Previous conversation:\n{transcript}\n\n{user_prompt}"
