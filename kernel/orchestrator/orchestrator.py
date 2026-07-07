"""
Orchestrator: owns the request lifecycle.

Given a config, wires up a model provider and a memory manager once; given a
user prompt, builds the augmented prompt, calls the provider, persists the
exchange to memory, logs the interaction, and returns the model's response.
No routing, no tools, no retries, no streaming - just the existing flow.
"""

from kernel.config.config import Config
from kernel.logger import log_interaction
from kernel.memory import MemoryManager
from kernel.models import ModelResponse, get_provider
from kernel.prompts import build_prompt


class Orchestrator:
    """Runs a single request end-to-end for a given config."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._provider = get_provider(config)
        self._memory = MemoryManager(config.memory_settings)

    def handle(self, user_prompt: str) -> ModelResponse:
        """Run one request end-to-end and return the model's response."""

        augmented_prompt = build_prompt(user_prompt, self._memory)
        response = self._provider.send_prompt(augmented_prompt)
        self._memory.remember("conversation", user_prompt, metadata={"role": "user"})
        self._memory.remember("conversation", response.text, metadata={"role": "assistant"})
        log_interaction(user_prompt, response, self._config.log_path)

        return response
