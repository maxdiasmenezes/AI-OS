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
