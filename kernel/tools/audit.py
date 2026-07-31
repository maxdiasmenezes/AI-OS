"""
Audit log for kernel/tools/ action executions - a JSONL file separate from
the kernel's own interaction log (storage/logs/interactions.jsonl), so the
safe task execution trail can be reviewed independently.

Only stable, symbolic fields are ever written: the action name, the
resource key (a config-defined symbolic name, e.g. "notepad" - never a
resolved filesystem path or executable path), and one of a small, fixed
set of outcome codes. Never a secret, an access token, a phone number,
message text, a traceback, or a resolved private filesystem path.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Stable, symbolic outcome codes used throughout kernel/tools/ and
# capabilities/tasks/.
OUTCOME_PROPOSED = "proposed"
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_EXECUTED = "executed"
OUTCOME_REJECTED = "rejected"
OUTCOME_EXPIRED = "expired"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_FAILED = "failed"

# kernel/tools/audit.py -> kernel/tools -> kernel -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_LOG_PATH = _PROJECT_ROOT / "storage" / "logs" / "task_actions.jsonl"


def record(action: str, resource_key: str | None, outcome: str, log_path: Path | None = None) -> None:
    """Append one audit record. Never raises - a logging failure must
    never break or mask the actual task-execution outcome, only be noted
    generically."""

    path = log_path or _DEFAULT_LOG_PATH
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "resource": resource_key,
        "outcome": outcome,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        logger.warning("audit_write_failed")
