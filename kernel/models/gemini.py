"""
Placeholder for future Gemini support.

Not implemented yet - AI-OS is built incrementally, provider by provider.
"""

from kernel.models.base import ModelProvider, ModelRequestOptions, ModelResponse


class GeminiProvider(ModelProvider):
    """Not implemented yet."""

    def __init__(self, settings: dict):
        pass

    def send_prompt(
        self, prompt: str, *, options: ModelRequestOptions | None = None
    ) -> ModelResponse:
        raise NotImplementedError("Gemini provider is not implemented yet.")
