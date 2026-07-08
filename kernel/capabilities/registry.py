"""
Capability registry: discovers which capability directories exist under
capabilities/, without importing or executing anything inside them.
"""

from dataclasses import dataclass
from pathlib import Path

# Resolved relative to this file, not the current working directory, so
# discovery behaves the same no matter where the kernel is run from.
_REGISTRY_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _REGISTRY_DIR.parent.parent
_CAPABILITIES_DIR = _PROJECT_ROOT / "capabilities"


@dataclass(frozen=True)
class CapabilityInfo:
    """Identity of a discovered capability: its id and where it lives."""

    id: str
    path: Path


class CapabilityRegistry:
    """Discovers capability directories under capabilities/."""

    def __init__(self, capabilities_dir: Path | None = None) -> None:
        self._capabilities_dir = capabilities_dir or _CAPABILITIES_DIR
        self._capabilities = self._discover()

    def _discover(self) -> dict[str, CapabilityInfo]:
        capabilities = {}
        for entry in self._capabilities_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith((".", "__")):
                continue
            capabilities[entry.name] = CapabilityInfo(id=entry.name, path=entry)
        return capabilities

    def list_capabilities(self) -> list[CapabilityInfo]:
        """Return all discovered capabilities, sorted by id."""

        return sorted(self._capabilities.values(), key=lambda c: c.id)

    def get_capability(self, capability_id: str) -> CapabilityInfo | None:
        """Return the capability with the given id, or None if not found."""

        return self._capabilities.get(capability_id)
