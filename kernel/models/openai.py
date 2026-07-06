"""
Placeholder for future OpenAI support.

Not implemented yet - AI-OS is built incrementally, provider by provider.
"""

from kernel.models.base import ModelProvider, ModelResponse


class OpenAIProvider(ModelProvider):
    """Not implemented yet."""

    def __init__(self, settings: dict):
        pass

    def send_prompt(self, prompt: str) -> ModelResponse:
        raise NotImplementedError("OpenAI provider is not implemented yet.")
