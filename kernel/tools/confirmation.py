"""
In-process pending-confirmation store for sensitive computer actions
(open_application, run_registered_script). A single, machine-wide slot is
enough: kernel/tools has no concept of separate users, and the interface
that reaches it today (WhatsApp) is itself single-user and processes one
message at a time.

State deliberately lives at module scope (see `default_store` below), not
as an attribute of any capability instance, because CapabilityLoader
constructs a brand new capability object on every single request (see
capabilities/loader.py) - an instance attribute would never survive
between the "propose" message and the later "confirm" message.
"""

import threading
import time
from dataclasses import dataclass

CONFIRMATION_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class PendingAction:
    action: str
    resource_key: str | None


class ConfirmationStore:
    """Holds at most one pending action, with a TTL, consumed exactly once."""

    def __init__(self, ttl_seconds: float = CONFIRMATION_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._pending: PendingAction | None = None
        self._created_at: float | None = None

    def propose(self, action: str, resource_key: str | None) -> None:
        """Register a new pending action, replacing any existing one -
        there is only ever one slot."""

        with self._lock:
            self._pending = PendingAction(action, resource_key)
            self._created_at = time.monotonic()

    def consume(self) -> tuple[PendingAction | None, bool]:
        """Atomically read and clear the pending action.

        Clearing happens unconditionally, before expiry is even checked,
        so a confirmation can never be replayed - not even if the caller
        goes on to fail while executing it. Returns
        (pending_action_or_None, was_expired).
        """

        with self._lock:
            pending, created_at = self._pending, self._created_at
            self._pending = None
            self._created_at = None

        if pending is None or created_at is None:
            return None, False
        if time.monotonic() - created_at > self._ttl_seconds:
            return None, True
        return pending, False

    def cancel(self) -> bool:
        """Clear any pending action. Returns whether one was actually
        pending. Safe to call when nothing is pending."""

        with self._lock:
            had_pending = self._pending is not None
            self._pending = None
            self._created_at = None
        return had_pending


# Process-wide singleton - see module docstring for why this can't live on
# a capability instance. Tests should construct their own ConfirmationStore
# instead of sharing this one.
default_store = ConfirmationStore()
