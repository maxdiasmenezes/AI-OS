"""
Public interface of the prompt-building layer.

Callers outside this package must import from here, never from builder.py
directly, matching the convention used by kernel/memory/__init__.py.
"""

from kernel.prompts.builder import build_prompt

__all__ = ["build_prompt"]
