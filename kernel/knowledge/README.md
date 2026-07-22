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
using both methods the contract exposes. The eight deterministic pairing
categories never touch the knowledge store at all. Two other paths do:

- `knowledge_store.get("wine_profile", "profile")` — an optional personal
  wine-preferences profile, a single record, read only on the model-backed
  fallback path.
- `knowledge_store.list_records("wine_cellar")` — an optional personal
  cellar inventory, one record per wine holding (not per physical bottle),
  keyed by the record's own ID rather than a duplicate `id` field inside it.
  Read on the model-backed fallback path (to build cellar context for the
  model), and also, independently, by Deterministic Cellar Lookup v1
  (`capabilities/wine/cellar_lookup.py`) for a small set of factual cellar
  questions it answers without any model call — total bottle count, exact
  quantity, exact ownership, producer holdings, and vintage listing. That
  path detects a supported query from the prompt text alone before ever
  touching the store, and, once detected, calls `list_records()` only —
  never `get()` — so a deterministic cellar answer still costs exactly one
  knowledge-store read and zero model calls. See
  `capabilities/wine/README.md` for the supported phrasings and matching
  rules.

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
`storage/knowledge/wine_cellar.json` now has two writers, and both are local,
human-invoked maintenance scripts that live entirely outside the runtime
kernel, documented in `scripts/README.md`:

- `scripts/import_wine_cellar.py` validates a CSV using the same
  `validate_cellar_record()` that `WineCapability` reads through, then writes
  the JSON file directly and atomically, replacing the whole document, only
  when a human runs it with an explicit `--write` flag.
- `scripts/update_wine_cellar_quantity.py` (Safe Cellar Quantity Update v1)
  changes only the `quantity` field of one existing holding, selected by its
  exact Cellar ID. It re-validates every record in the cellar — with the
  same shared `validate_cellar_record()` — both before and after computing
  the proposed quantity, deep-copies the original parsed document so
  unrecognized fields and untouched records survive unchanged, and, like the
  importer, only writes atomically when a human passes an explicit `--write`
  flag; every run defaults to a dry run.

Neither script is a capability, neither is ever reached through the
orchestrator, and neither calls `KnowledgeStore` to write — both write the
JSON file directly with plain file I/O. This does not expand what
`KnowledgeStore` itself can do, and it does not create an autonomous write
path: both scripts are one-shot, explicitly human-invoked commands, never
triggered automatically, on a schedule, or as a side effect of handling a
prompt. The kernel's read-only contract is unchanged. Deterministic
bottle-count lookup and wine-name matching over the cellar also exist
(`capabilities/wine/cellar_lookup.py`), entirely as read-only, exact-match
logic layered on top of `list_records()` — they add no write path, no fuzzy
or semantic matching, and no ranking. Adding or removing holdings, editing
any field other than quantity, automatic decrementing, bottle-level
purchase/ratings history beyond what a cellar record already carries,
backups, change history, search, embeddings, vector retrieval, and web
access all remain unimplemented.
