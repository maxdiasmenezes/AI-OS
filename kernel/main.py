"""
Kernel v0.0.1 entry point.

Usage:
    python -m kernel.main "your prompt here"

The application composition root: parses the CLI prompt, loads config,
constructs the capability loader, and hands the request to the
Orchestrator, which owns the request lifecycle (routing, capability
execution, model calls, memory, logging).
"""

import argparse

from capabilities.loader import CapabilityLoader
from kernel.config.config import load_config
from kernel.orchestrator import Orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-OS Kernel v0.0.1")
    parser.add_argument("prompt", help="Prompt to send to the model")
    args = parser.parse_args()

    config = load_config()
    loader = CapabilityLoader()
    orchestrator = Orchestrator(config, capability_loader=loader.load)
    response = orchestrator.handle(args.prompt)

    print(response.text)


if __name__ == "__main__":
    main()
