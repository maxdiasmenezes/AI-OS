"""
Strict command grammar for the /task interface (Milestone 33). Every
supported form is listed explicitly below - there is no natural-language
matching, no fuzzy matching, and no partial-token matching. Anything that
doesn't exactly match one of these forms is a parse error, not a guess.

Supported forms ("/task" and the verb are matched case-insensitively; a
resource key is casefolded so it matches kernel/config/tools.yaml's
casefolded keys - see kernel/tools/config.py):

    /task status
    /task files <registered-directory-key>
    /task open <registered-application-key>
    /task run <registered-script-key>
    /task confirm
    /task cancel
    /task help

Any extra token, missing token, or unrecognized verb is a ParseError -
never guessed at or partially honored.
"""

from dataclasses import dataclass

TASK_PREFIX = "/task"

_NO_ARG_VERBS = frozenset({"status", "confirm", "cancel", "help"})

# verb -> the kernel/tools action name it maps to.
_ONE_ARG_VERBS = {
    "files": "list_files",
    "open": "open_application",
    "run": "run_registered_script",
}


@dataclass(frozen=True)
class ParsedCommand:
    verb: str  # "status" | "confirm" | "cancel" | "help" | "files" | "open" | "run"
    action: str | None  # kernel/tools action name for files/open/run, else None
    resource_key: str | None  # casefolded resource key for files/open/run, else None


@dataclass(frozen=True)
class ParseError:
    reason: str  # stable, symbolic reason code - never free text


def parse_task_command(prompt: str) -> ParsedCommand | ParseError:
    tokens = prompt.strip().split()
    if not tokens or tokens[0].casefold() != TASK_PREFIX:
        return ParseError("not_a_task_command")

    rest = tokens[1:]
    if not rest:
        return ParsedCommand("help", None, None)

    verb = rest[0].casefold()
    args = rest[1:]

    if verb in _NO_ARG_VERBS:
        if args:
            return ParseError("wrong_argument_count")
        return ParsedCommand(verb, None, None)

    if verb in _ONE_ARG_VERBS:
        if len(args) != 1:
            return ParseError("wrong_argument_count")
        return ParsedCommand(verb, _ONE_ARG_VERBS[verb], args[0].casefold())

    return ParseError("unknown_verb")
