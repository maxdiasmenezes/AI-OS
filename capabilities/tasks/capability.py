"""
TasksCapability: Milestone 33's safe computer task execution capability
(repo_health added in Milestone 34; repository_backup added in
Milestone 35).

Every command is handled deterministically - no model call is ever made
here, matching the pattern WineCapability's Deterministic Cellar Lookup v1
established (capabilities/wine/cellar_lookup.py). This capability never
reads memory or the knowledge store either - system_status/list_files/
open_application/run_registered_script/repo_health are all stateless,
one-shot actions.

requires_computer_actions = True means Orchestrator refuses to call
handle() at all unless the request's RequestContext explicitly grants
allow_computer_actions - see kernel/orchestrator/context.py and
kernel/orchestrator/orchestrator.py. This capability trusts that gate
completely: it performs no authorization of its own and knows nothing
about WhatsApp, phone numbers, or any other interface. Do not duplicate
that check here.
"""

from kernel.capabilities.base import Capability
from kernel.tools import (
    ActionRegistry,
    ActionRequest,
    SafeTaskExecutor,
    ToolsConfigError,
    default_store,
    load_tools_config,
)
from kernel.tools import audit
from kernel.tools.config import EMPTY_TOOLS_CONFIG

from capabilities.tasks.command_parser import ParseError, parse_task_command

HELP_TEXT = (
    "Available commands:\n"
    "/task status\n"
    "/task files <directory>\n"
    "/task open <application>\n"
    "/task run <script>\n"
    "/task repo <repository>\n"
    "/task backup <repository>\n"
    "/task confirm\n"
    "/task cancel\n"
    "/task help"
)

_PARSE_ERROR_TEXT = "Unrecognized command. Send /task help for the list of supported commands."
_CONFIG_ERROR_TEXT = "The task system is temporarily unavailable."
_NOTHING_PENDING_TEXT = "There is no pending action to confirm."
_CONFIRMATION_EXPIRED_TEXT = "That confirmation has expired. Please send the command again."
_CANCELLED_TEXT = "Pending action cancelled."
_NOTHING_TO_CANCEL_TEXT = "There is no pending action to cancel."


def _confirmation_prompt(action: str, resource_key: str | None) -> str:
    target = f" '{resource_key}'" if resource_key else ""
    return (
        f"This will run {action}{target}. "
        "Reply /task confirm within 2 minutes to proceed, or /task cancel."
    )


class TasksCapability(Capability):
    requires_computer_actions = True

    def __init__(
        self,
        model_provider,
        memory_manager,
        knowledge_store,
        *,
        confirmation_store=None,
        tools_config_loader=None,
    ) -> None:
        # model_provider/memory_manager/knowledge_store are accepted only
        # for constructor-signature parity with every other capability -
        # CapabilityLoader calls all of them the same way (see
        # capabilities/loader.py). This capability never calls a model and
        # never reads memory or the knowledge store - see module docstring.
        self._registry = ActionRegistry()
        self._confirmations = confirmation_store if confirmation_store is not None else default_store
        self._load_tools_config = tools_config_loader or load_tools_config

    @property
    def id(self) -> str:
        return "tasks"

    def handle(self, prompt: str) -> str:
        parsed = parse_task_command(prompt)
        if isinstance(parsed, ParseError):
            return _PARSE_ERROR_TEXT

        if parsed.verb == "help":
            return HELP_TEXT

        if parsed.verb == "cancel":
            cancelled = self._confirmations.cancel()
            audit.record("confirmation", None, "cancelled" if cancelled else "rejected")
            return _CANCELLED_TEXT if cancelled else _NOTHING_TO_CANCEL_TEXT

        if parsed.verb == "confirm":
            pending, expired = self._confirmations.consume()
            if pending is None:
                audit.record("confirmation", None, "expired" if expired else "rejected")
                return _CONFIRMATION_EXPIRED_TEXT if expired else _NOTHING_PENDING_TEXT
            audit.record(pending.action, pending.resource_key, "confirmed")
            return self._execute(pending.action, pending.resource_key)

        # status / files / open / run
        action = parsed.action or "system_status"
        resource_key = parsed.resource_key

        if self._registry.is_sensitive(action):
            self._confirmations.propose(action, resource_key)
            audit.record(action, resource_key, "proposed")
            return _confirmation_prompt(action, resource_key)

        return self._execute(action, resource_key)

    def _execute(self, action: str, resource_key: str | None) -> str:
        if action == "system_status":
            # Needs no local configuration - stays available even if
            # kernel/config/tools.yaml is missing or invalid.
            tools_config = EMPTY_TOOLS_CONFIG
        else:
            try:
                tools_config = self._load_tools_config()
            except ToolsConfigError:
                audit.record(action, resource_key, "failed")
                return _CONFIG_ERROR_TEXT

        executor = SafeTaskExecutor(tools_config, self._registry)
        result = executor.execute(ActionRequest(action=action, resource_key=resource_key))
        return result.message
