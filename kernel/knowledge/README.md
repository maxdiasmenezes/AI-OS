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

`WineCapability` (`capabilities/wine/capability.py`) is the first consumer:
it reads an optional personal wine-preferences profile at namespace
`"wine_profile"`, key `"profile"`, via `knowledge_store.get("wine_profile",
"profile")` only — never `list_records()`, and never outside its
model-backed fallback path (the eight deterministic pairing categories never
touch the knowledge store at all). The store itself stays exactly as
read-only as before; schema validation of the profile record's fields lives
entirely in `capabilities/wine/capability.py`, not here. Real profile data
would live at `storage/knowledge/wine_profile.json` (already excluded from
Git by `.gitignore`); no such file is committed, and nothing in this
repository writes one. Cellar inventory, bottle-level data, search,
embeddings, vector retrieval, web access, and any write API remain
unimplemented.
