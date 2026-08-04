"""
Talks to a locally running Ollama server.

This is the only module that knows about Ollama's HTTP API - the rest of
the kernel only sees the ModelProvider interface.
"""

import json
import time
import urllib.request

from kernel.models.base import ModelProvider, ModelResponse

# Milestone 38: a fixed, code-level request timeout - not user-configurable,
# not a per-command option. Without this, a hung or unreachable Ollama
# server would hang urlopen() indefinitely, which made a bounded "model
# timed out" response impossible for /knowledge ask. Passed straight to
# urlopen(); on expiry or connection failure, urlopen() raises (e.g.
# socket.timeout / urllib.error.URLError) - this module does not catch or
# retry that, callers (e.g. capabilities/knowledge_commands/) are
# responsible for mapping such an exception to a fixed, privacy-safe reply.
OLLAMA_REQUEST_TIMEOUT_SECONDS = 120


class OllamaProvider(ModelProvider):
    """Sends prompts to a local Ollama server."""

    def __init__(self, settings: dict):
        self.model = settings["model"]
        self.base_url = settings["base_url"]
        self.max_tokens = settings["max_tokens"]
        self.temperature = settings["temperature"]

    def send_prompt(self, prompt: str) -> ModelResponse:
        """Send a single prompt to Ollama and return the response."""

        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }).encode("utf-8")

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
