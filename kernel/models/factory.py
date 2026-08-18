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


def get_planner_provider(config) -> ModelProvider:
    """Instantiate the dedicated structured-planner provider selected in
    config.planner_provider/config.planner_provider_settings - the
    construction kernel/config/config.py's own Config docstring already
    names as the intended future implementation. Deliberately separate
    from get_provider(): the planner provider is configured independently
    (see kernel/task_execution/__init__.py's model-role-separation
    contract - RESPOND synthesis uses the general conversational provider
    and never this one) and must never be conflated with it."""

    try:
        provider_class = _PROVIDERS[config.planner_provider]
    except KeyError:
        raise ValueError(f"Unknown model provider: {config.planner_provider!r}")

    return provider_class(config.planner_provider_settings)
