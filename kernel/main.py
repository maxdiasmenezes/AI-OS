"""
Kernel v0.0.1 entry point.

Usage:
    python -m kernel.main "your prompt here"

Steps: load config -> call the configured model provider -> log the
interaction -> print the response. No routing, no memory, no orchestration -
just those four steps.
"""

import argparse

from kernel.config.config import load_config
from kernel.models import get_provider
from kernel.logger import log_interaction


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-OS Kernel v0.0.1")
    parser.add_argument("prompt", help="Prompt to send to the model")
    args = parser.parse_args()

    config = load_config()
    provider = get_provider(config)
    response = provider.send_prompt(args.prompt)
    log_interaction(args.prompt, response, config.log_path)

    print(response.text)


if __name__ == "__main__":
    main()
