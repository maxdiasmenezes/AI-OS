"""
Capability router: decides which capability id should handle a prompt.

This only selects an id. It never discovers, loads, instantiates, or
executes a capability - that stays the job of CapabilityRegistry and
CapabilityLoader.
"""

import re

# Milestone 33: the strict "/task" command prefix routes to the "tasks"
# capability. This only detects the prefix - the full command grammar
# ("/task status", "/task open <key>", etc.) is parsed deterministically
# inside capabilities/tasks/command_parser.py, not here. Anchored to the
# start of the (stripped) prompt, since this is a command, not a phrase
# that can appear mid-sentence.
_TASK_COMMAND_PATTERN = re.compile(r"^/task(\s|$)", re.IGNORECASE)

# Milestone 37: the strict "/knowledge" command prefix routes to the
# "knowledge" capability, the same way "/task" routes to "tasks" above.
# Anchored and followed by whitespace-or-end, so "/knowledgeable" or
# "/knowledge" appearing mid-sentence never matches - only a leading
# command token does. The full grammar is parsed deterministically inside
# capabilities/knowledge_commands/command_parser.py, not here.
_KNOWLEDGE_COMMAND_PATTERN = re.compile(r"^/knowledge(?:\s|$)", re.IGNORECASE)

# Whole-word "wine" or "wines" mention - the original, broadest signal.
_WINE_WORD_PATTERN = re.compile(r"\bwines?\b", re.IGNORECASE)

# "my cellar" - a conservative, explicit cellar-ownership cue, distinct from
# the generic words the milestone forbids routing on alone (own, have,
# bottles, vintages, producer, region, country).
_MY_CELLAR_PATTERN = re.compile(r"\bmy\s+cellar\b", re.IGNORECASE)

# Small, explicit set of food/meal cues used to disambiguate otherwise
# generic selection/pairing language. Deliberately independent of
# WineCapability's own (larger, private) food categories.
_FOOD_CUE = r"(?:steak|chicken|pizza|salmon)"

# Standalone explicit wine-selection phrase: naming a "bottle" together
# with the act of choosing/opening it is specific enough on its own,
# without needing a food cue.
_BOTTLE_SELECTION_PATTERN = re.compile(
    r"\bbottle\s+should\s+i\s+open\b", re.IGNORECASE
)

# "drink"/"pair" combined with "with" and a food cue - neither word alone
# is a signal (see the false-positive words called out in the milestone),
# but the combination is.
_PAIRING_WITH_FOOD_PATTERN = re.compile(
    r"\b(?:drink|pair)\b(?:\s+\w+){0,3}\s+with\b(?:\s+\w+){0,3}\s+\b" + _FOOD_CUE + r"\b",
    re.IGNORECASE,
)

# "suitable for" combined with a food cue - "suitable for" by itself is
# too generic (e.g. "suitable for a business meeting").
_SUITABLE_FOR_FOOD_PATTERN = re.compile(
    r"\bsuitable\s+for\b(?:\s+\w+){0,3}\s+\b" + _FOOD_CUE + r"\b",
    re.IGNORECASE,
)

_WINE_INTENT_PATTERNS = (
    _WINE_WORD_PATTERN,
    _MY_CELLAR_PATTERN,
    _BOTTLE_SELECTION_PATTERN,
    _PAIRING_WITH_FOOD_PATTERN,
    _SUITABLE_FOR_FOOD_PATTERN,
)


class CapabilityRouter:
    """Maps prompt text to a capability id, or None if nothing matches."""

    def route(self, prompt: str) -> str | None:
        """Return the capability id that should handle prompt, or None."""

        if _TASK_COMMAND_PATTERN.match(prompt.strip()):
            return "tasks"
        if _KNOWLEDGE_COMMAND_PATTERN.match(prompt.strip()):
            return "knowledge"
        if any(pattern.search(prompt) for pattern in _WINE_INTENT_PATTERNS):
            return "wine"
        return None
