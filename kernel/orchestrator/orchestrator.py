"""
Orchestrator: owns the request lifecycle.

Given a config and a capability loader, wires up a model provider, a memory
manager, a read-only knowledge store, and a capability router once; given a
user prompt, routes it to a capability when the router matches one,
otherwise falls back to the model provider. Either way, persists the
exchange to memory and logs the interaction - unless the capability
returned an EphemeralResult (see kernel/capabilities/base.py), in which
case neither write happens for that one request; Milestone 38 introduced
this for `/knowledge search` and `/knowledge ask`, which must never leave
query/question text or model answers in memory or the interaction log.
No tools, no retries, no streaming - just routing plus the existing flow.
"""

import logging
from typing import Callable, Protocol

from kernel.capabilities.base import Capability, EphemeralResult
from kernel.config.config import Config
from kernel.knowledge import JSONKnowledgeStore, KnowledgeStore
from kernel.logger import log_interaction
from kernel.memory import MemoryEntry, MemoryManager
from kernel.models import ModelResponse, get_provider
from kernel.models.base import ModelProvider
from kernel.orchestrator.context import RequestContext
from kernel.orchestrator.router import CapabilityRouter
from kernel.prompts import build_prompt
from kernel.tools import audit

logger = logging.getLogger(__name__)

# Deterministic, fixed denial text - never mentions the capability id, the
# prompt, or any reason - returned when a matched capability requires
# computer-action trust the request's RequestContext doesn't grant. This
# response is synthetic: the model provider is never invoked to produce it,
# and the routed capability's handle() is never called either.
COMPUTER_ACTIONS_DENIED_TEXT = "This request is not authorized to perform computer actions."

# Stable, symbolic audit fields for a denied computer-action attempt - see
# kernel/tools/audit.py. The orchestrator stays domain-agnostic: it audits
# "computer_actions" generically, using whatever capability id the router
# matched (e.g. "tasks") as the resource, never anything specific to what
# that capability does.
_AUDIT_ACTION = "computer_actions"
_AUDIT_OUTCOME_REJECTED_UNAUTHORIZED = "rejected_unauthorized"


class SupportsMemory(Protocol):
    """Structural contract for the memory dependency Orchestrator uses.

    Lets a composition root inject a delegating adapter (e.g. a
    namespace-scoped wrapper) in place of a concrete MemoryManager, without
    requiring it to inherit from that class.
    """

    def remember(self, namespace: str, content: str, metadata: dict | None = None) -> None: ...

    def recall(self, namespace: str, limit: int | None = None) -> list[MemoryEntry]: ...


class Orchestrator:
    """Runs a single request end-to-end for a given config."""

    def __init__(
        self,
        config: Config,
        capability_loader: Callable[
            [str, ModelProvider, SupportsMemory, KnowledgeStore], Capability
        ],
        *,
        memory_manager: SupportsMemory | None = None,
    ) -> None:
        self._config = config
        self._provider = get_provider(config)
        self._memory = (
            memory_manager if memory_manager is not None else MemoryManager(config.memory_settings)
        )
        self._knowledge = JSONKnowledgeStore(config.knowledge_storage_dir)
        self._router = CapabilityRouter()
        self._capability_loader = capability_loader

    def handle(self, user_prompt: str, context: RequestContext | None = None) -> ModelResponse:
        """Run one request end-to-end and return the response.

        context defaults to a fully untrusted RequestContext() when omitted,
        so every existing caller - the CLI, and any test that doesn't pass
        one - stays denied for a capability that requires computer-action
        trust. See kernel/orchestrator/context.py.
        """

        if context is None:
            context = RequestContext()

        # Set when a capability result is an EphemeralResult (Milestone
        # 38) - the remember()/log_interaction() tail below is skipped for
        # that one request only. Every other path (denial, ordinary str/
        # ModelResponse capability results, model fallback) leaves this
        # False, so persistence stays byte-identical to before Milestone 38.
        skip_persistence = False

        capability_id = self._router.route(user_prompt)
        if capability_id is not None:
            capability = self._capability_loader(
                capability_id,
                self._provider,
                self._memory,
                self._knowledge,
            )
            if capability.requires_computer_actions and not context.allow_computer_actions:
                # Deny deterministically, before handle() is ever called -
                # this is already the final response, so it skips straight
                # to the shared remember/log tail below rather than the
                # model fallback branch.
                logger.info("computer_actions_denied capability=%s", capability_id)
                # Audit every rejected attempt, using only stable symbolic
                # values (never the prompt, an actor identifier, or any
                # other detail). A write failure here must never change or
                # prevent the deterministic denial response below - matches
                # audit.record()'s own never-raises guarantee, and stays
                # defensive against it regardless.
                try:
                    audit.record(
                        _AUDIT_ACTION, capability_id, _AUDIT_OUTCOME_REJECTED_UNAUTHORIZED
                    )
                except Exception:
                    logger.warning("computer_actions_denied_audit_failed")
                response = ModelResponse(
                    text=COMPUTER_ACTIONS_DENIED_TEXT,
                    model=f"capability:{capability_id}:denied",
                    input_tokens=0,
                    output_tokens=0,
                    latency_seconds=0.0,
                )
            else:
                capability_result = capability.handle(user_prompt)
                if isinstance(capability_result, EphemeralResult):
                    skip_persistence = True
                    response = ModelResponse(
                        text=str(capability_result),
                        model=f"capability:{capability_id}",
                        input_tokens=0,
                        output_tokens=0,
                        latency_seconds=0.0,
                    )
                elif isinstance(capability_result, ModelResponse):
                    response = capability_result
                elif isinstance(capability_result, str):
                    response = ModelResponse(
                        text=capability_result,
                        model=f"capability:{capability_id}",
                        input_tokens=0,
                        output_tokens=0,
                        latency_seconds=0.0,
                    )
                else:
                    raise TypeError(
                        f"capability {capability_id!r} returned unsupported result type "
                        f"{type(capability_result).__name__}; expected str or ModelResponse"
                    )
        else:
            augmented_prompt = build_prompt(user_prompt, self._memory)
            response = self._provider.send_prompt(augmented_prompt)

        if not skip_persistence:
            self._memory.remember("conversation", user_prompt, metadata={"role": "user"})
            self._memory.remember("conversation", response.text, metadata={"role": "assistant"})
            log_interaction(user_prompt, response, self._config.log_path)

        return response
