# Knowledge

Shared knowledge base infrastructure used by capabilities to store and query domain knowledge.

A minimal, read-only `KnowledgeStore` contract now exists (`base.py`), with
one implementation: `JSONKnowledgeStore` (`json_store.py`). It reads one
keyed JSON document per namespace (`<storage_dir>/<namespace>.json`, one
JSON object mapping record id to record fields) from a storage directory
that is explicitly injected by the caller — the store does not load
configuration itself. A missing namespace is treated as empty rather than an
error; malformed knowledge data (invalid JSON, a non-object top-level value,
or a non-object record) raises `ValueError` instead of being silently
treated as empty.

No capability consumes the knowledge store yet. `WineCapability` integration
and a wine-specific schema remain planned. There is no write API, search,
embeddings, vector retrieval, web access, or autonomous writes.
