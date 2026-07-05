"""
Contract every model provider must satisfy.

Nothing outside a provider's own file should need to know how that provider
works internally - only that it accepts a prompt and returns a ModelResponse.
"""

from abc import ABC, abstractmethod


class ModelResponse:
    """Result of a single call to a model, independent of provider."""

    def __init__(self, text: str, model: str, input_tokens: int,
                 output_tokens: int, latency_seconds: float):
        self.text = text
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_seconds = latency_seconds


class ModelProvider(ABC):
    """Common interface every model provider must implement."""

    @abstractmethod
    def __init__(self, settings: dict):
        ...

    @abstractmethod
    def send_prompt(self, prompt: str) -> ModelResponse:
        """Send a single prompt to the model and return the response."""
        ...
