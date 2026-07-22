"""
Orchestrator: owns the request lifecycle.

Given a config and a capability loader, wires up a model provider, a memory
manager, a read-only knowledge store, and a capability router once; given a
user prompt, routes it to a capability when the router matches one,
otherwise falls back to the model provider. Either way, persists the
exchange to memory, logs the interaction, and returns the response. No
tools, no retries, no streaming - just routing plus the existing flow.
"""

from typing import Callable, Protocol

from kernel.capabilities.base import Capability
from kernel.config.config import Config
from kernel.knowledge import JSONKnowledgeStore, KnowledgeStore
from kernel.logger import log_interaction
from kernel.memory import MemoryEntry, MemoryManager
from kernel.models import ModelResponse, get_provider
from kernel.models.base import ModelProvider
from kernel.orchestrator.router import CapabilityRouter
from kernel.prompts import build_prompt


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

    def handle(self, user_prompt: str) -> ModelResponse:
        """Run one request end-to-end and return the response."""

        capability_id = self._router.route(user_prompt)
        if capability_id is not None:
            capability = self._capability_loader(
                capability_id,
                self._provider,
                self._memory,
                self._knowledge,
            )
            capability_result = capability.handle(user_prompt)
            if isinstance(capability_result, ModelResponse):
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

        self._memory.remember("conversation", user_prompt, metadata={"role": "user"})
        self._memory.remember("conversation", response.text, metadata={"role": "assistant"})
        log_interaction(user_prompt, response, self._config.log_path)

        return response
