"""
RequestContext: the trusted-request mechanism that gates which callers are
allowed to reach capabilities that perform computer actions.

Reaching Orchestrator.handle() at all is not authorization to perform a
computer action - a capability that declares
Capability.requires_computer_actions=True also needs an explicit, trusted
RequestContext, or Orchestrator denies it deterministically before the
capability's handle() is ever called (see kernel/orchestrator/orchestrator.py).

The default denies. The CLI (kernel/main.py) and any other caller that
doesn't pass a context stay denied. Only interfaces/whatsapp/handler.py
constructs an authorizing context, and only after its own exact-sender
authorization already succeeded (that check happens earlier, synchronously,
in interfaces/whatsapp/server.py, before a message is ever queued) - this
module knows nothing about WhatsApp, phone numbers, or any other
interface-specific authorization, and must not be given any.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    """Per-request trust context passed into Orchestrator.handle().

    allow_computer_actions must be explicitly set True by a caller that has
    already performed its own authorization - it is never inferred from
    which interface or code path is calling. `actor` is a short,
    non-sensitive label (e.g. "whatsapp") for audit purposes only; it must
    never carry a phone number, sender ID, or other personal identifier.
    """

    allow_computer_actions: bool = False
    actor: str = "unknown"
