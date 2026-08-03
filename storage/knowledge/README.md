# Knowledge

Persisted knowledge base data used across capabilities.

## Wine profile and cellar

Two files are read by `WineCapability` through the read-only
`KnowledgeStore` (`kernel/knowledge/`), and are never created or modified by
the runtime kernel itself:

- `wine_profile.json` — one JSON object with a top-level `"profile"` record
  holding a personal wine-preferences profile. Recognized fields:
  `preferred_styles`, `disliked_styles`, `priorities` (lists of strings),
  `budget_range`, `notes` (strings). See `scripts/README.md` for the full
  shape (with synthetic placeholder values) and field conventions.
- `wine_cellar.json` — one JSON object mapping a stable Cellar ID to a
  cellar record (producer, wine name, quantity, and other fields defined by
  `capabilities/wine/cellar_schema.py`). Written only by the human-invoked
  `scripts/import_wine_cellar.py` and `scripts/update_wine_cellar_quantity.py`
  maintenance scripts — never by the kernel, `WineCapability`, or a model.

Both files stay **local and gitignored** (`storage/knowledge/*.json`); no
real profile or cellar data is committed to this repository.
`scripts/wine_acceptance_check.py` reads both, but — like everything else in
this directory — never writes to either. Normal runtime `KnowledgeStore`
access remains strictly read-only end to end.

## Local knowledge-base index (Milestone 36)

`knowledge_index.sqlite3` — a local SQLite database (plus its WAL/
shared-memory/journal sidecar files while a write is in progress) built
and queried by `kernel/knowledge_base/`. Its location is not a separate
setting: it is always `<knowledge.storage_dir>/knowledge_index.sqlite3`,
i.e. this same directory, derived from the existing
`knowledge.storage_dir` setting in `kernel/config/config.yaml`. It is
**local and gitignored** (`storage/knowledge/*.sqlite3*`) — never
committed, and never created automatically; this directory must already
exist before `kernel/knowledge_base/db.py` will place the database file
here.

The database holds document and chunk metadata plus a SQLite FTS5
lexical-search index for whatever local `.md`/`.txt` sources are approved
in the separate, also-gitignored `kernel/config/knowledge_base.yaml`. It
never stores absolute filesystem paths — only symbolic source keys and
source-relative paths — and is built/queried entirely through
`scripts/knowledge.py` (`status`/`ingest`/`search`), never automatically.
See `kernel/knowledge_base/README.md` for the full design: this is
lexical (keyword, BM25-ranked) search only, with no embeddings, no
semantic/vector search, and no orchestrator/RAG integration yet.
