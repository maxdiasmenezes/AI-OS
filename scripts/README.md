# Scripts

Utility scripts for operating and maintaining the AI Operating System.

## Safe Cellar Import v1 (`import_wine_cellar.py`)

A human-controlled, local maintenance script that imports a CSV file of wine
holdings into `storage/knowledge/wine_cellar.json` — the same file
`WineCapability`'s model-backed fallback reads via `KnowledgeStore`
(`kernel/knowledge/README.md`).

**Safety model:**

- Runs entirely outside the runtime kernel — it is invoked directly by a
  human, never by the orchestrator, a capability, or a model.
- Never calls a model provider.
- Never goes through `KnowledgeStore`. `KnowledgeStore` remains strictly
  read-only end to end; this script writes the destination JSON file
  directly with plain file I/O.
- Defaults to a **dry run**: the CSV is fully validated and a summary is
  printed, but nothing is written. A file is only written when `--write` is
  passed explicitly — that flag *is* the human confirmation, so the script
  never prompts interactively.
- Validates the entire CSV before writing anything. Any error — a bad
  header, a bad row, a schema violation — rejects the complete import; no
  partial file is ever written, and an existing destination file is left
  byte-for-byte unchanged if the import fails.
- Writes atomically: the full JSON document is written to a temporary file
  in the destination directory, then moved into place with `os.replace()`.
  The temporary file is removed if anything goes wrong before the move
  completes.

## Commands

Dry run (default — validates and prints a summary, writes nothing):

```
uv run python -m scripts.import_wine_cellar <csv_path>
```

Write (validates, then replaces `storage/knowledge/wine_cellar.json`):

```
uv run python -m scripts.import_wine_cellar <csv_path> --write
```

## Full-replacement semantics

One CSV represents the **complete** desired cellar. A `--write` run replaces
the entire `wine_cellar.json` document — it does not merge with an existing
file, does not preserve records the CSV omits, and does not decrement
quantities. If a wine is missing from the CSV, it is gone from the cellar
after the import. This milestone does not implement backups or diffs; review
the dry-run summary before writing.

## CSV format

Comma-delimited, read via Python's standard `csv` module with
`encoding="utf-8-sig"` (so both plain UTF-8 and spreadsheet-exported UTF-8
files with a byte-order mark work) and `newline=""`.

Columns may appear in any order, but unknown column headers are rejected
outright rather than silently ignored — this protects against misspelled
field names and accidental data loss. Duplicate headers and blank headers
are also rejected, as is a CSV missing its header row entirely.

Recommended column order:

```
id,producer,wine_name,color,quantity,vintage,country,region,style,grapes,estimated_price,price_currency,vivino_rating,drinking_window,special_occasion,notes
```

Required headers: `id`, `producer`, `wine_name`, `color`, `quantity`. All
other headers above are optional and may be omitted entirely.

One synthetic example row (values are illustrative only, not real data):

```
sample-red-2021,Sample Estate,Reserve Red,red,3,2021,Example Country,Example Region,medium-bodied red,Sample Grape;Other Grape,30,USD,3.8,2025-2029,false,Synthetic example row
```

Field notes:

- `id` becomes the top-level JSON record key and is not copied into the
  record itself. It must be non-empty and unique within the CSV.
- Every cell has surrounding whitespace trimmed before conversion; internal
  whitespace is preserved.
- An optional column left blank for a given row means that field is absent
  from that record — it is not written as an empty string.
- `grapes` uses `;` as the in-cell separator (e.g. `Merlot;Cabernet Franc`);
  each item is trimmed, authored order is preserved, and an empty item (such
  as from a trailing `;`) is rejected.
- `special_occasion` accepts only `true` or `false`, case-insensitively —
  not `yes`/`no` or `1`/`0`.
- `quantity` must be a plain non-negative integer; `vintage` must be the
  exact string `NV` or an integer in the range the shared schema enforces.
- Every row is validated by the same
  `capabilities.wine.cellar_schema.validate_cellar_record()` that
  `WineCapability` uses, so the importer never accepts a record the runtime
  would reject, or vice versa.

## Output

`storage/knowledge/wine_cellar.json` is already excluded from Git by the
repository's existing `.gitignore` rule (`storage/knowledge/*.json`). No real
personal cellar data — and no CSV fixture containing it — is committed to
this repository.
