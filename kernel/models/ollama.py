"""
Talks to a locally running Ollama server.

This is the only module that knows about Ollama's HTTP API - the rest of
the kernel only sees the ModelProvider interface.
"""

import json
import time
import urllib.request

from kernel.models.base import ModelProvider, ModelRequestOptions, ModelResponse

# Milestone 38: a fixed, code-level request timeout - not user-configurable,
# not a per-command option. Without this, a hung or unreachable Ollama
# server would hang urlopen() indefinitely, which made a bounded "model
# timed out" response impossible for /knowledge ask. Passed straight to
# urlopen(); on expiry or connection failure, urlopen() raises (e.g.
# socket.timeout / urllib.error.URLError) - this module does not catch or
# retry that, callers (e.g. capabilities/knowledge_commands/) are
# responsible for mapping such an exception to a fixed, privacy-safe reply.
OLLAMA_REQUEST_TIMEOUT_SECONDS = 120

# Milestone 39: the accepted range for ModelRequestOptions.temperature_override
# on this provider specifically - not a ModelRequestOptions-level rule, since
# only the provider knows what its own API actually accepts. Mirrors the
# conventional 0.0-2.0 range this provider's underlying model API accepts;
# the configured default temperature (settings["temperature"], used when no
# override is given) is trusted local configuration and is not re-validated
# against this range.
MIN_TEMPERATURE_OVERRIDE = 0.0
MAX_TEMPERATURE_OVERRIDE = 2.0


class OllamaProvider(ModelProvider):
    """Sends prompts to a local Ollama server."""

    def __init__(self, settings: dict):
        self.model = settings["model"]
        self.base_url = settings["base_url"]
        self.max_tokens = settings["max_tokens"]
        self.temperature = settings["temperature"]

    def send_prompt(
        self, prompt: str, *, options: ModelRequestOptions | None = None
    ) -> ModelResponse:
        """Send a single prompt to Ollama and return the response.

        With `options=None` (every call site written before Milestone 39),
        the outgoing payload is byte-identical to before this method
        gained the `options` parameter. `self.temperature`/`self.model`/
        `self.max_tokens` are never mutated by this call - a structured,
        temperature-overridden request has no effect on any later call on
        this same provider instance.
        """

        temperature = self.temperature
        if options is not None and options.temperature_override is not None:
            if not (
                MIN_TEMPERATURE_OVERRIDE
                <= options.temperature_override
                <= MAX_TEMPERATURE_OVERRIDE
            ):
                raise ValueError(
                    "OllamaProvider: temperature_override must be between "
                    f"{MIN_TEMPERATURE_OVERRIDE} and {MAX_TEMPERATURE_OVERRIDE}"
                )
            temperature = options.temperature_override

        body_dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": self.max_tokens,
            },
        }
        if options is not None and options.require_json:
            body_dict["format"] = options.json_schema if options.json_schema is not None else "json"

        payload = json.dumps(body_dict).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        start = time.monotonic()
        with urllib.request.urlopen(request, timeout=OLLAMA_REQUEST_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read())
        latency_seconds = time.monotonic() - start

        return ModelResponse(
            text=body["response"],
            model=body["model"],
            input_tokens=body.get("prompt_eval_count", 0),
            output_tokens=body.get("eval_count", 0),
            latency_seconds=latency_seconds,
        )
