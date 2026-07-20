"""
JSON-backed implementation of KnowledgeStore.

Each namespace maps to a single <storage_dir>/<namespace>.json file
containing one JSON object: record id -> record fields. This is the only
module that knows knowledge is stored this way - the rest of the kernel only
sees KnowledgeStore.
"""

import json
from pathlib import Path

from kernel.knowledge.base import KnowledgeStore

_INVALID_NAMESPACE_CHARS = ("/", "\\")


def _validate_namespace(namespace: str) -> None:
    if not namespace or namespace in (".", "..") or ".." in namespace:
        raise ValueError(f"Invalid namespace: {namespace!r}")

    if any(char in namespace for char in _INVALID_NAMESPACE_CHARS):
        raise ValueError(f"Invalid namespace: {namespace!r}")

    if Path(namespace).is_absolute():
        raise ValueError(f"Invalid namespace: {namespace!r}")


class JSONKnowledgeStore(KnowledgeStore):
    """Reads knowledge records from one keyed JSON document per namespace."""

    def __init__(self, storage_dir: str | Path):
        self._storage_dir = Path(storage_dir)

    def get(self, namespace: str, key: str) -> dict[str, object] | None:
        """Return a single record by key, or None if it does not exist."""

        records = self.list_records(namespace)
        return records.get(key)

    def list_records(self, namespace: str) -> dict[str, dict[str, object]]:
        """Return all records in a namespace, keyed by record id."""

        _validate_namespace(namespace)

        path = self._storage_dir / f"{namespace}.json"
        if not path.exists():
            return {}

        with open(path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"Malformed JSON in knowledge namespace {namespace!r}") from e

        if not isinstance(data, dict):
            raise ValueError(f"Knowledge namespace {namespace!r} must be a JSON object")

        for record_id, record in data.items():
            if not isinstance(record, dict):
                raise ValueError(
                    f"Record {record_id!r} in knowledge namespace {namespace!r} "
                    "must be a JSON object"
                )

        return data
