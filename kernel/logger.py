"""
Logs each kernel interaction as one JSON line under storage/logs/.

JSONL keeps every record self-contained and append-only, so no log
parsing framework is needed to read or write it.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from kernel.models import ModelResponse


def log_interaction(prompt: str, response: ModelResponse, log_path: Path) -> None:
    """Append a single interaction record to the log file."""

    log_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "response": response.text,
        "model": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_seconds": response.latency_seconds,
    }

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
