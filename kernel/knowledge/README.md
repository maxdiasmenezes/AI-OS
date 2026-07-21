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

`WineCapability` (`capabilities/wine/capability.py`) is the first consumer,
using both methods the contract exposes, and only on its model-backed
fallback path (the eight deterministic pairing categories never touch the
knowledge store at all):

- `knowledge_store.get("wine_profile", "profile")` — an optional personal
  wine-preferences profile, a single record.
- `knowledge_store.list_records("wine_cellar")` — an optional personal
  cellar inventory, one record per wine holding (not per physical bottle),
  keyed by the record's own ID rather than a duplicate `id` field inside it.

The store itself stays exactly as read-only as before; schema validation of
the profile record's fields lives in `capabilities/wine/capability.py`, and
cellar record field validation lives in
`capabilities/wine/cellar_schema.py` (`validate_cellar_record()`) — neither
lives here. For the cellar namespace, `WineCapability` validates every
record returned by `list_records()` (including zero-quantity ones) before
deciding which are active, excludes zero-quantity holdings, sorts the rest
by record key, and caps a single fallback prompt at 100 active records —
refusing to send a silently truncated partial inventory above that cap.
None of that filtering, sorting, or limiting happens here in the knowledge
layer; this module only ever returns exactly what is on disk. Real profile
and cellar data would live at `storage/knowledge/wine_profile.json` and
`storage/knowledge/wine_cellar.json` respectively (already excluded from Git
by `.gitignore`); no such files are committed.

`KnowledgeStore` itself still exposes no write API, and `JSONKnowledgeStore`
is never used to write either file — that contract is unchanged.
`storage/knowledge/wine_cellar.json` does have exactly one writer today:
`scripts/import_wine_cellar.py` (see `scripts/README.md`), a local,
human-invoked maintenance script that lives entirely outside the runtime
kernel. It is not a capability, is never reached through the orchestrator,
and does not call `KnowledgeStore` — it validates a CSV using the same
`validate_cellar_record()` that `WineCapability` reads through, then writes
the JSON file directly and atomically, replacing the whole document, only
when a human runs it with an explicit `--write` flag. This does not expand
what `KnowledgeStore` itself can do; the kernel's read-only contract is
unchanged. Deterministic bottle-count lookup and wine-name matching over the
cellar, bottle-level purchase/ratings history beyond what a cellar record
already carries, search, embeddings, vector retrieval, and web access all
remain unimplemented.
