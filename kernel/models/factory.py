"""
Resolves which model provider to use based on config.

This is the only module that imports more than one provider - everything
else reaches providers through get_provider(), so the rest of the kernel
never needs to know which provider is active.
"""

from kernel.models.anthropic import AnthropicProvider
from kernel.models.base import ModelProvider
from kernel.models.gemini import GeminiProvider
from kernel.models.ollama import OllamaProvider
from kernel.models.openai import OpenAIProvider

_PROVIDERS = {
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


def get_provider(config) -> ModelProvider:
    """Instantiate the model provider selected in config."""

    try:
        provider_class = _PROVIDERS[config.provider]
    except KeyError:
        raise ValueError(f"Unknown model provider: {config.provider!r}")

    return provider_class(config.provider_settings)
