# Knowledge Base

A local-only, deterministic knowledge-base service (Milestone 36):
ingests approved local `.md`/`.txt` sources by symbolic key, stores
document and chunk metadata plus a SQLite FTS5 lexical-search index, and
answers plain-text search queries. Everything here runs entirely on this
machine - no model call, no embedding service, no vector database, and no
network access, in either ingestion or search.

This is **lexical (keyword) search only**, backed by SQLite's FTS5 module
and BM25 ranking. There is no semantic or vector search, no embeddings,
and no model-generated summaries or metadata anywhere in this package.

Distinct from `kernel/knowledge/` (the read-only `KnowledgeStore`
`get()`/`list_records()` contract used by `WineCapability`) - search and
ranking don't fit that contract, so this is a separate, sibling kernel
package rather than a new `KnowledgeStore` implementation. Also distinct
from `capabilities/knowledge/`, an unrelated, still-unimplemented
capability stub. Nothing in this package is wired into the orchestrator,
a capability, WhatsApp, memory, or a model - the only caller is
`scripts/knowledge.py`, a human-invoked, offline CLI (see that script and
`scripts/README.md`). A future orchestrator/RAG integration that reads
from this index is explicitly not part of this milestone.

## Configuration

Approved knowledge sources live in `kernel/config/knowledge_base.yaml` -
gitignored, machine-local, loaded and validated by
`kernel/knowledge_base/config.py`. `kernel/config/knowledge_base.example.yaml`
is the committed placeholder; copy it and fill in real absolute paths
locally. A missing `knowledge_base.yaml` is not an error - it is treated
as zero approved sources (deny-all). A present-but-invalid file (bad
YAML, a duplicate or case-colliding key, an unknown or missing field, a
relative path, a non-boolean `recursive`, or an unsafe symbolic key)
always raises `KnowledgeConfigError`; callers must treat that as deny.
Symbolic source keys must match `^[a-z0-9][a-z0-9_-]{0,63}$` and are
casefolded consistently. Parsing performs no filesystem traversal.

The SQLite index's location is **not** configured here at all: it is
derived exclusively from the existing `knowledge.storage_dir` setting in
`kernel/config/config.yaml` (the same directory `JSONKnowledgeStore`
already uses), with a fixed filename, `knowledge_index.sqlite3` - this
milestone never introduces a second, duplicate storage-location setting.
That directory must already exist and be an actual directory (never a
symlink/junction/reparse point/special file); it is never created
automatically.

## Supported content

`.md` and `.txt` only, matched by suffix case-insensitively. Encoding
must be strict UTF-8 or UTF-8 with a BOM; anything else (invalid UTF-8, a
NUL byte, a legacy codepage) fails closed. PDFs, Office documents,
archives, images, OCR, and any other format are out of scope and are
never attempted.

## Source authorization and traversal

A caller may only supply a symbolic source key already present in
`knowledge_base.yaml` - never a path, glob, extension list, or recursion
depth. The configured root's *original* (unresolved) identity is checked
with `lstat` before it is ever resolved - a symlink, junction, reparse
point, socket, FIFO, device, or other special file at the root is
rejected outright. Every candidate discovered underneath a directory
source is independently resolved and confirmed to remain inside the
canonical root; traversal never follows a symlink, and any symlink,
junction, or reparse point encountered anywhere in the tree (not just
unsupported ones) fails the whole source. Hidden-file policy: any path
component starting with `.` is skipped, and on Windows any entry with the
hidden or system attribute is skipped - both silently, not as a failure.
An unsupported extension is likewise silently ignored, never rejected.

Traversal order is deterministic (entries sorted by name at each
directory level, depth-first), so repeated ingestion of unchanged content
produces byte-identical results.

Reading a candidate's content is race-resistant: identity is re-checked
immediately before opening (not reusing a stale `lstat` from traversal),
opened with the strictest available no-follow flags, re-confirmed via
`fstat` against the pre-open identity, size-limited before reading, and
re-confirmed again after reading completes. Any mismatch anywhere in that
sequence - the file was replaced, mutated, or grew mid-read - fails the
whole source ingestion; it never causes a partial read.

Fixed safety limits (code constants, never configurable):

| Limit | Value |
|---|---|
| Max files per source | 5,000 |
| Max individual file size | 5 MiB |
| Max total source bytes | 500 MiB |
| Max recursion depth | 16 |
| Max document characters | 2,000,000 |
| Max chunks per document | 4,000 |
| Max total chunks per ingestion | 200,000 |

Exceeding any of these fails the whole source ingestion and rolls back -
never a silent truncation.

**Any invalid file (bad UTF-8, a NUL byte, an unsafe identity, or any
other validation failure) fails the entire source's ingestion**, rolling
back every change made so far for that source and leaving the previous
generation fully searchable. An existing source that traverses to zero
supported files is a valid, empty ingestion, and atomically removes any
previously indexed documents for that source.

## Chunking and identifiers

Text is normalized (BOM stripped, CRLF/CR normalized to LF, invalid UTF-8
and NUL bytes rejected) and split into paragraphs on blank-line runs,
discarding only empty/whitespace-only paragraphs - no summarization,
rewriting, translation, or inferred metadata. Paragraphs are greedily
packed into chunks of at most 1,200 characters with 200 characters of
overlap seeded from the end of the previous chunk (backed off to a word
boundary); a paragraph longer than 1,200 characters on its own is
hard-split the same way. Chunk order and offsets (`char_start`/
`char_end` into the normalized text) are fully deterministic.

Every identifier is a SHA-256 digest of trusted, explicit inputs - never
Python's process-randomized `hash()`:

- `content_hash = sha256(normalized_text)` - a real content hash,
  independent of source key or path.
- `document_key = sha256(source_key + "\n" + relative_path_key)` - stable
  document identity.
- `chunk_id = sha256(document_key + "\n" + content_hash + "\n" + ordinal + "\n" + sha256(chunk_text))`.

`relative_path_key` is a case-normalized form of `relative_path` (via
`os.path.normcase` on Windows, exact on case-sensitive platforms), used
only for internal diffing/identity - it is never returned to a caller;
`relative_path` (original casing, POSIX form) is what's stored for
display and returned by search.

## Database

SQLite (stdlib `sqlite3`), schema version 1: `schema_meta`, `sources`,
`documents` (with `source_key`, a proper `ON DELETE CASCADE` foreign key,
and both `relative_path` and `relative_path_key`), `chunks` (`ON DELETE
CASCADE` from `documents`), and an external-content FTS5 virtual table
`chunks_fts` kept in sync via insert/delete triggers (chunk rows are
write-once/delete-only, so no update trigger is needed). Indexes exist on
`documents.source_key` and `chunks.document_id`.

FTS5 availability is verified at runtime by creating, populating,
querying, and dropping a *temporary* schema object
(`temp.__ai_os_fts5_probe`) - never the persistent schema, and never
relying solely on `sqlite_compileoption_used()`. If FTS5 is unavailable,
`FTS5UnavailableError` is raised; there is no fallback to `LIKE` or
another dependency.

Ingestion runs inside one `BEGIN IMMEDIATE` transaction per source:
existing documents are diffed by `(source_key, relative_path_key,
content_hash)` - unchanged documents are left completely untouched (same
row, same chunk IDs), changed documents are deleted (cascading to their
chunks and FTS rows) and reinserted, new documents are inserted, and
files no longer present are deleted once every candidate has succeeded.
Any failure rolls the whole transaction back, leaving the prior
generation exactly as it was; a crash or abrupt failure can never leave a
mixture of old and new state. Only one ingestion may hold the write lock
on a database at a time (`BEGIN IMMEDIATE` itself enforces this - a
second concurrent ingestion attempt surfaces as the fixed "database
locked" condition, mapped from SQLite's `SQLITE_BUSY`). Connections:
writers set `journal_mode=WAL` (only ever set during writable
initialization, never on a search connection) and `synchronous=NORMAL`;
both writer and reader connections set `foreign_keys=ON` and a 5-second
busy timeout; reader connections additionally set `query_only=ON` and
never touch `journal_mode`. WAL's snapshot isolation means a concurrent
search always sees a fully consistent prior or new committed generation,
never a partial one.

## Search

`search(query, source_keys=None, limit=5)` is read-only end to end and
never invokes a model or the network. The query is validated (must be a
non-blank string, whitespace-stripped, at most 200 characters, no
embedded C0 control character or DEL), Unicode-normalized (NFC), and
mechanically tokenized into alphanumeric terms only (`re.findall(r"[^\W_]+", ...)`).
Each extracted term becomes an individually double-quoted FTS5 string
literal, joined with `AND` (deduplicated case-insensitively, capped at 20
terms) - this means quotes, wildcards, `NEAR`, `OR`, `NOT`, and column
filters supplied by a caller can never behave as FTS5 operators; they can
only ever match as plain literal text. The resulting MATCH expression is
still passed as a bound SQL parameter, never concatenated into SQL.
Source filters accept only currently-approved symbolic keys - an unknown
key is rejected, never silently dropped. Ranking uses SQLite's built-in
BM25 (`bm25(chunks_fts)`); ties break deterministically on
`(source_key, relative_path_key, chunk_ordinal, chunk_id)`. Excerpts come
from FTS5's own bounded `snippet()`, truncated again to a fixed maximum
length as a second safeguard. Results never include an absolute path,
the database path, SQL, or a full document - only `source_key`,
`relative_path`, `chunk_ordinal`, a bounded `excerpt`, `rank`, and
`chunk_id`.

## Operational interface

`scripts/knowledge.py` (see `scripts/README.md`) - `status`, `ingest
<source_key>`, and `search "<query>"` - is the only way to reach this
package today. It is a human-invoked, offline CLI outside the runtime
kernel, exactly like the existing wine-domain scripts; it is never called
by the orchestrator, a capability, or a model.

## Logging and privacy

Only fixed, symbolic metadata is ever logged: action, source key,
outcome, document/chunk counts, elapsed time, and a fixed error category.
Document content, query text, excerpts, absolute paths, the database
path, SQL, exception text, and tracebacks are never logged, in any mode -
there is no debug flag that relaxes this. Every user-facing error is one
of a small set of fixed, privacy-safe messages (see `types.py`'s error
classes and `scripts/knowledge.py`); callers must select a message by
exception type, never by relaying `str(exc)`.

## Explicitly out of scope (Milestone 36)

Embeddings, semantic/vector search, a vector database, model-generated
summaries or metadata, orchestrator context injection, automatic RAG,
WhatsApp/web/voice integration, file watchers, scheduled ingestion, cloud
sync, remote URLs, web scraping, PDF/Office/OCR/archive ingestion,
deletion commands, manual chunk editing, access-control roles, and
encryption-at-rest are all deliberately not implemented. The schema and
API here (symbolic source keys, relative paths, deterministic chunk IDs)
are designed so a later orchestrator/RAG integration can read this index
without requiring a schema change - but no such integration exists yet.
