"""
Contract every model provider must satisfy.

Nothing outside a provider's own file should need to know how that provider
works internally - only that it accepts a prompt and returns a ModelResponse.
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass


class ModelResponse:
    """Result of a single call to a model, independent of provider."""

    def __init__(self, text: str, model: str, input_tokens: int,
                 output_tokens: int, latency_seconds: float):
        self.text = text
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_seconds = latency_seconds


@dataclass(frozen=True)
class ModelRequestOptions:
    """Optional, per-request overrides for a single send_prompt() call
    (Milestone 39). Every field defaults to the provider's ordinary,
    unmodified behavior - omitting `options` entirely (the default `None`
    on send_prompt) or passing `ModelRequestOptions()` must produce a
    byte-identical request to every call site that predates this type.

    require_json / json_schema together select the provider's structured-
    output mode for this one request only - never provider-global state
    (see kernel/models/ollama.py). temperature_override, when given,
    replaces the provider's configured temperature for this one request
    only; the provider's own `self.temperature` is never mutated.

    Validated at construction time (__post_init__), so an invalid
    combination fails before a caller can ever pass it to send_prompt -
    no HTTP request is possible from an object that failed to construct.
    Provider-specific numeric ranges (e.g. Ollama's accepted temperature
    range) are validated by the provider itself, not here, since only the
    provider knows its own range.
    """

    require_json: bool = False
    json_schema: dict[str, object] | None = None
    temperature_override: float | None = None

    def __post_init__(self):
        if not self.require_json and self.json_schema is not None:
            raise ValueError(
                "ModelRequestOptions: json_schema requires require_json=True"
            )
        if self.temperature_override is not None:
            if isinstance(self.temperature_override, bool) or not isinstance(
                self.temperature_override, (int, float)
            ):
                raise ValueError(
                    "ModelRequestOptions: temperature_override must be a real number"
                )
            if not math.isfinite(self.temperature_override):
                raise ValueError(
                    "ModelRequestOptions: temperature_override must be finite"
                )


class ModelProvider(ABC):
    """Common interface every model provider must implement."""

    @abstractmethod
    def __init__(self, settings: dict):
        ...

    @abstractmethod
    def send_prompt(
        self, prompt: str, *, options: ModelRequestOptions | None = None
    ) -> ModelResponse:
        """Send a single prompt to the model and return the response.

        `options` is keyword-only and defaults to None, which preserves
        exact prior behavior - every call site written before Milestone 39
        keeps working unmodified. Options are evaluated fresh for each
        call; nothing about a request-scoped option is retained on the
        provider instance afterward.
        """
        ...
