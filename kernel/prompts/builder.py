"""
Assembles the final prompt sent to a model provider.

This is the only place that combines a system prompt, recalled memory, and a
user prompt - it reuses MemoryManager.recall() as-is and does not alter how
memory is written.
"""

from pathlib import Path

from kernel.memory import MemoryManager

_NAMESPACE = "conversation"
_LIMIT = 10

# kernel/prompts/builder.py -> kernel/prompts -> kernel -> project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SYSTEM_PROMPT_PATH = _PROJECT_ROOT / "prompts" / "system.md"
_SYSTEM_PROMPT = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


def build_prompt(user_prompt: str, memory: MemoryManager) -> str:
    """Combine the system prompt, recalled memory, and the user prompt."""

    entries = memory.recall(_NAMESPACE, limit=_LIMIT)

    parts = [_SYSTEM_PROMPT]
    if entries:
        transcript = "\n".join(f"{e.metadata['role']}: {e.content}" for e in entries)
        parts.append(f"Previous conversation:\n{transcript}")
    parts.append(user_prompt)

    return "\n\n".join(parts)
