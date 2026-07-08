"""
Public interface of the capability registry.

Callers outside this package must import from here, never from registry.py
directly, so the kernel never needs to know how discovery works.
"""

from kernel.capabilities.registry import CapabilityInfo, CapabilityRegistry

__all__ = ["CapabilityInfo", "CapabilityRegistry"]
