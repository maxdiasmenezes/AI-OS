"""
Kernel v0.0.1 entry point.

Usage:
    python -m kernel.main "your prompt here"

Parses the CLI prompt, loads config, and hands the request to the
Orchestrator, which owns the request lifecycle (prompt building, model
call, memory, logging). No routing, no capabilities - just wiring.
"""

import argparse

from kernel.config.config import load_config
from kernel.orchestrator import Orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-OS Kernel v0.0.1")
    parser.add_argument("prompt", help="Prompt to send to the model")
    args = parser.parse_args()

    config = load_config()
    orchestrator = Orchestrator(config)
    response = orchestrator.handle(args.prompt)

    print(response.text)


if __name__ == "__main__":
    main()
