"""
Public interface of the model layer.

Callers outside this package must import from here, never from a specific
provider module, so the kernel never needs to know which provider is active.
"""

from kernel.models.base import ModelResponse
from kernel.models.factory import get_provider

__all__ = ["ModelResponse", "get_provider"]
