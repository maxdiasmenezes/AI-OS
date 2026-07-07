"""
Kernel v0.0.1 entry point.

Usage:
    python -m kernel.main "your prompt here"

Steps: load config -> build a prompt augmented with recent conversation
memory -> call the configured model provider -> store the prompt and
response in memory -> log the interaction -> print the response.
No routing, no orchestration - just those steps.
"""

import argparse

from kernel.config.config import load_config
from kernel.memory import MemoryManager
from kernel.models import get_provider
from kernel.logger import log_interaction
from kernel.prompts import build_prompt


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-OS Kernel v0.0.1")
    parser.add_argument("prompt", help="Prompt to send to the model")
    args = parser.parse_args()

    config = load_config()
    provider = get_provider(config)
    memory = MemoryManager(config.memory_settings)
    augmented_prompt = build_prompt(args.prompt, memory)
    response = provider.send_prompt(augmented_prompt)
    memory.remember("conversation", args.prompt, metadata={"role": "user"})
    memory.remember("conversation", response.text, metadata={"role": "assistant"})
    log_interaction(args.prompt, response, config.log_path)

    print(response.text)


if __name__ == "__main__":
    main()
