"""
Thread-safe, bounded FIFO cache of recently-seen WhatsApp message IDs.

WhatsApp's Cloud API delivers webhooks at-least-once, so the same message
can arrive more than once (retries, overlapping delivery). Reservation
into this cache happens synchronously in interfaces/whatsapp/server.py's
POST handling, before a message is ever queued for background processing -
so authorization and deduplication never depend on worker-thread timing.
Stores only message-ID strings - no sender IDs, text, timestamps, or other
payload fields - and nothing here is persisted across a process restart.
"""

import threading
from collections import deque

DEFAULT_CAPACITY = 256


class SeenMessageCache:
    """Remembers up to `max_size` message IDs, oldest evicted first."""

    def __init__(self, max_size: int = DEFAULT_CAPACITY) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._max_size = max_size
        self._order = deque()
        self._seen = set()
        self._lock = threading.Lock()

    def add_if_new(self, message_id: str) -> bool:
        """Atomically reserve `message_id`. Returns True if it was new."""

        with self._lock:
            if message_id in self._seen:
                return False
            self._order.append(message_id)
            self._seen.add(message_id)
            if len(self._order) > self._max_size:
                oldest = self._order.popleft()
                self._seen.discard(oldest)
            return True

    def discard(self, message_id: str) -> None:
        """Release a reservation - e.g. after failing to queue the message -
        so a later redelivery of the same ID is accepted rather than
        treated as a duplicate."""

        with self._lock:
            self._seen.discard(message_id)
            try:
                self._order.remove(message_id)
            except ValueError:
                pass
