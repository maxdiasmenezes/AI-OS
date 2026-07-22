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

## Safe Cellar Quantity Update v1 (`update_wine_cellar_quantity.py`)

A second human-controlled, local maintenance script. Where
`import_wine_cellar.py` replaces the **entire** cellar document from a CSV,
this script changes **only the `quantity` field of one existing holding**,
selected by its exact Cellar ID — nothing else about that record, and no
other record, is touched.

**Safety model:**

- Runs entirely outside the runtime kernel — invoked directly by a human,
  never by the orchestrator, `WineCapability`, or a model.
- Never calls a model provider.
- Never goes through `KnowledgeStore`, which remains strictly read-only; this
  script reads and writes `wine_cellar.json` directly with plain file I/O.
- Defaults to a **dry run**: the requested update is fully validated and a
  summary is printed, but nothing is written. A file is only written when
  `--write` is passed explicitly — that flag *is* the human confirmation;
  there is no interactive confirmation prompt.
- Selects the target holding by **exact, case-sensitive equality** against
  the top-level Cellar ID key only. No normalization, no producer/wine-name
  search, no substring or fuzzy matching, no model-generated suggestions. An
  unknown Cellar ID is rejected with a clear error naming the ID requested.
- Validates the **complete existing cellar** — every record, including
  unrelated and zero-quantity ones — before calculating anything, and
  validates the **complete resulting cellar** again after applying the
  proposed change, before writing anything. One invalid record anywhere
  aborts the whole operation; there is no partial update.
- Preserves the original document: only the selected record's `quantity`
  field is changed. Every other field on that record — including fields the
  current schema doesn't recognize — and every other record are carried
  through byte-for-byte equivalent (the parsed document is deep-copied, not
  reconstructed from validated output).
- Writes atomically, the same way `import_wine_cellar.py` does: a temporary
  file in the destination directory, flushed and closed, then moved into
  place with `os.replace()`; the temporary file is removed if anything goes
  wrong first.

## Commands

```
uv run python -m scripts.update_wine_cellar_quantity <cellar_id> --set <N>
uv run python -m scripts.update_wine_cellar_quantity <cellar_id> --decrement
uv run python -m scripts.update_wine_cellar_quantity <cellar_id> --decrement <N>
```

Add `--write` to any of the above to apply the update after validation.
Without `--write`, every command is a dry run. Examples:

```
uv run python -m scripts.update_wine_cellar_quantity sample-red-2021 --decrement
uv run python -m scripts.update_wine_cellar_quantity sample-red-2021 --decrement 2 --write
uv run python -m scripts.update_wine_cellar_quantity sample-red-2021 --set 5 --write
```

## Quantity behavior

- `--set N` sets the quantity to exactly `N` (`N` must be a non-negative
  integer).
- `--decrement` (no value) decrements the quantity by **1**.
- `--decrement N` decrements the quantity by the given `N`, which must be a
  positive integer — zero, negative, decimal, or boolean-like values are all
  rejected.
- Decrementing exactly to **zero is allowed**; the record is kept in the
  cellar with `quantity: 0`, never removed.
- A result **below zero is rejected outright** — it is never clamped to
  zero.
- Exactly one of `--set` or `--decrement` is required; both together, or
  neither, is rejected. These rules are enforced independently of argparse,
  so calling the underlying Python function directly cannot bypass them.
- If `--set` proposes the quantity the record already has, that's a
  successful **no-op**: the summary says so plainly, and the file is left
  byte-for-byte unchanged even when `--write` is passed.

This script only ever changes a `quantity` value on one existing record. It
does not add or remove holdings, does not edit any other field, and does not
decrement automatically — every run is a separate, explicit, human-invoked
command. It never runs on its own, and this milestone still implements no
backups or change history.

## Wine Data Readiness and Acceptance Check v1 (`wine_acceptance_check.py`)

A human-invoked, **read-only** utility that verifies a local wine profile
(`storage/knowledge/wine_profile.json`) and cellar
(`storage/knowledge/wine_cellar.json`) are structurally usable by
`WineCapability` — before you rely on them for real use. It does not create,
import, or edit either file; use `import_wine_cellar.py` and
`update_wine_cellar_quantity.py` (above) for that, and write
`wine_profile.json` by hand (see `storage/knowledge/README.md`).

**Safety model:**

- Runs entirely outside the runtime kernel — invoked directly by a human,
  never by the orchestrator or a capability.
- **Model-free by default.** No model provider is constructed or contacted
  unless `--call-model` is passed explicitly, and even then the real
  provider is constructed lazily, only inside that opt-in code path. A
  plain run works with no local model server running at all.
- **No persistent memory.** The script never constructs or uses the
  repository's real `MemoryManager` and never reads existing conversation
  history. It uses a tiny private no-op memory object for prompt-assembly
  checks (returns no recalled turns, records nothing, writes nothing) and a
  fail-fast memory object for deterministic checks that raises immediately
  if `WineCapability` ever tries to touch it — proving those code paths
  really are memory-free.
- **Writes nothing.** No profile, cellar, quantity, memory, log, report, or
  other repository file is ever created or modified. There is no `--write`
  flag; this script never has a destructive mode.
- Deterministic cellar-query checks run through the real
  `WineCapability.handle()` with a fail-fast provider *and* fail-fast
  memory, so any unexpected provider or memory access fails the check
  immediately instead of silently succeeding.
- Prompt-assembly checks run through the real `WineCapability.handle()`
  with a recording fake provider (captures the assembled fallback prompt,
  returns a fixed response) and the no-op memory — never the configured
  real provider.

## Commands

Model-free acceptance run (default — the only thing this script does unless
told otherwise):

```
uv run python -m scripts.wine_acceptance_check
```

Model-free run, plus a concise set of real-provider prompts for manual
review:

```
uv run python -m scripts.wine_acceptance_check --call-model
```

## Status meanings

- **PASS** — the check succeeded.
- **FAIL** — the check failed; a required, model-free check failing makes
  the whole run fail (exit code 1).
- **SKIP** — a condition-dependent check (e.g. a region, a zero-quantity
  holding, an ambiguous wine name, multiple vintages of one wine) had no
  matching real data to exercise it. SKIP does not fail the run; it is
  reported separately as an "optional checks skipped" count.
- **MANUAL REVIEW** — only appears with `--call-model`. The real model
  produced a non-empty response, printed for a human to judge; the script
  never asserts anything about a model's actual wording, pairing choices,
  or subjective quality, so a MANUAL REVIEW result is not proof the
  response was good — read it yourself before trusting it.

Model-backed responses under `--call-model` **always require human
review** — the script only mechanically confirms the call succeeded and the
response was non-empty, nothing about content or correctness.

## What a run reports

A model-free run prints: the resolved profile and cellar paths; profile
readiness; cellar statistics (total/active/zero-quantity holdings, active
bottle total, unique producers, represented countries/regions); each
deterministic acceptance check (A–K); prompt-assembly readiness;
confirmation that no real provider was called; confirmation that no files
were written; a final required-check pass count; and an optional-check skip
count. It never prints the complete cellar document or the complete captured
fallback prompt, since both may contain personal data — only derived
statistics and short pass/fail summaries.

**Do not paste real acceptance output into a public issue, chat, or file
committed to this repository** — a real run can legitimately print your real
producer names, wine names, regions, and profile preferences (though never
prices, ratings, or full record dumps).

## Default local paths

```
<repository-root>/storage/knowledge/wine_profile.json
<repository-root>/storage/knowledge/wine_cellar.json
```

Both are gitignored (`storage/knowledge/*.json`) and never committed. The
underlying `run_acceptance()`/`main()` functions accept both paths as
explicit overrides for testing; the two files must live in the same
directory and keep these exact filenames, since `WineCapability` reads both
through one `KnowledgeStore` instance pointed at that directory.

## `wine_profile.json` shape

The real file stays local and is never committed — only synthetic,
illustrative values are shown here:

```json
{
  "profile": {
    "preferred_styles": ["Synthetic Style A", "Synthetic Style B"],
    "disliked_styles": ["Synthetic Disliked Style"],
    "budget_range": "$20-40 per bottle (synthetic)",
    "priorities": ["Synthetic Priority"],
    "notes": "Synthetic free-text notes."
  }
}
```

Recognized fields: `preferred_styles`, `disliked_styles`, and `priorities`
(lists of strings), `budget_range` and `notes` (strings). Unknown fields are
ignored, not rejected. **Food dislikes belong in `notes`, not
`disliked_styles`** — `disliked_styles` is for wine styles (e.g. "oaky
Chardonnay"), not foods; a note like "avoid pairing with very spicy dishes"
belongs in `notes`.

## Real cellar CSV

The real cellar CSV that feeds `import_wine_cellar.py` stays **outside this
repository** entirely — only the resulting, gitignored
`storage/knowledge/wine_cellar.json` exists locally. See the CSV format
section above for the full column reference; a few conventions worth
repeating here since they affect acceptance results directly:

- `vintage`: the exact string `NV`, an integer year (1800–2100), or blank
  (omitted) for "not recorded" — never `0` or empty-string-as-a-value.
- Blank optional cells mean the field is absent, not empty-string.
- `grapes` is `;`-separated within one cell; order is preserved.
- `special_occasion` is `true`/`false` only.
- Prices need both `estimated_price` and `price_currency`, or neither.
- Quantities are non-negative integers; a wine you no longer physically hold
  should be `0`, not deleted — deterministic zero-quantity behavior depends
  on the record still existing.
- Duplicate vintages of the same producer+wine (different Cellar IDs) are
  expected and are exactly what the multiple-vintages acceptance case
  checks for.
- Duplicate wine names across different producers are expected and are
  exactly what the ambiguous-wine-name acceptance case checks for — the
  deterministic layer asks for the producer rather than guessing.
- All text is read and written as UTF-8; accented and non-Latin producer,
  region, and wine names are supported as-is.
- Cellar IDs (the CSV's `id` column) should be stable across re-imports —
  pick a scheme (e.g. `producer-slug-wine-slug-vintage`) and keep using it,
  since `update_wine_cellar_quantity.py` selects records by exact Cellar ID.

## Recommended real-data onboarding sequence

1. Write your real cellar CSV (outside this repository) and your real
   `storage/knowledge/wine_profile.json` by hand, using the shape above.
2. Dry-run the importer and review its summary:
   `uv run python -m scripts.import_wine_cellar <csv_path>`
3. Confirm the destination is git-ignored and the tree is otherwise clean:
   `git check-ignore -v storage/knowledge/wine_cellar.json` and
   `git status --porcelain -- storage/`.
4. Write for real: `uv run python -m scripts.import_wine_cellar <csv_path> --write`
5. Run this acceptance check:
   `uv run python -m scripts.wine_acceptance_check`
6. Optionally, review real model-backed responses:
   `uv run python -m scripts.wine_acceptance_check --call-model`
7. For a quantity change: dry-run first
   (`uv run python -m scripts.update_wine_cellar_quantity <id> --decrement`),
   apply explicitly (`... --write`), then re-run the acceptance check to
   verify the cellar is still structurally sound, and spot-check the
   specific holding's new quantity in the printed statistics or a targeted
   query.
