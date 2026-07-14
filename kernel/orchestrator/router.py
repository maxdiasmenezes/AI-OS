"""
Capability router: decides which capability id should handle a prompt.

This only selects an id. It never discovers, loads, instantiates, or
executes a capability - that stays the job of CapabilityRegistry and
CapabilityLoader.
"""

import re

_WINE_PATTERN = re.compile(r"\bwine\b", re.IGNORECASE)


class CapabilityRouter:
    """Maps prompt text to a capability id, or None if nothing matches."""

    def route(self, prompt: str) -> str | None:
        """Return the capability id that should handle prompt, or None."""

        if _WINE_PATTERN.search(prompt):
            return "wine"
        return None
