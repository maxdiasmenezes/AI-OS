"""
Deterministic text normalization, chunking, and stable identifier
derivation for kernel/knowledge_base/ (Milestone 36).

Everything here is purely mechanical: no summarization, rewriting,
translation, classification, or model call, and no metadata is inferred
beyond what the source bytes themselves contain. Every identifier is a
SHA-256 digest of trusted, explicit inputs - never Python's
process-randomized hash().
"""

import hashlib
import re
from dataclasses import dataclass

from kernel.knowledge_base.types import SourceLimitExceededError

MAX_DOCUMENT_CHARACTERS = 2_000_000
MAX_CHUNKS_PER_DOCUMENT = 4_000
MAX_CHUNK_CHARACTERS = 1_200
CHUNK_OVERLAP_CHARACTERS = 200

_PARAGRAPH_SEP_RE = re.compile(r"\n\s*\n+")

_ATX_HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+\S.*$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`+|~+)[ \t]*$")


class InvalidDocumentEncodingError(ValueError):
    """Raised when raw bytes cannot be strictly decoded as UTF-8 (with or
    without a BOM) or contain a NUL byte. Callers (ingest.py) must map
    this to InvalidSourceContentError - a whole-source failure."""


def normalize_text(raw_bytes: bytes) -> str:
    """Decode raw file bytes strictly as UTF-8 (accepting an optional
    UTF-8 BOM, which is stripped), reject a NUL byte anywhere in the
    decoded text, and normalize line endings to bare "\\n". Preserves
    every other character exactly - no whitespace trimming, no case
    folding, no Unicode normalization (that only applies to search
    queries, not stored document content)."""

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidDocumentEncodingError("document is not valid UTF-8") from exc

    if "\x00" in text:
        raise InvalidDocumentEncodingError("document contains a NUL byte")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Locate every non-empty paragraph as an (start, end) offset pair
    into `text`, splitting on runs of two-or-more newlines (allowing
    blank/whitespace-only lines in between). Offsets are exact slices of
    the original text - nothing is trimmed beyond discarding paragraphs
    that are empty or whitespace-only."""

    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _PARAGRAPH_SEP_RE.finditer(text):
        if match.start() > pos:
            spans.append((pos, match.start()))
        pos = match.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return [(s, e) for s, e in spans if text[s:e].strip()]


def _markdown_heading_starts(text: str) -> list[int]:
    """Locate the start offset of every line in `text` that is a valid
    ATX heading (``^ {0,3}#{1,6}[ \\t]+\\S.*$``), skipping any line inside a
    fenced code block (a line opening with 0-3 leading spaces then three
    or more backticks or tildes, closed by a line of the same fence
    character at least as long, per CommonMark)."""

    starts: list[int] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    pos = 0
    length = len(text)

    while pos <= length:
        newline_idx = text.find("\n", pos)
        line_end = newline_idx if newline_idx != -1 else length
        line = text[pos:line_end]

        if in_fence:
            close_match = _FENCE_CLOSE_RE.match(line)
            if close_match and close_match.group(1)[0] == fence_char and len(close_match.group(1)) >= fence_len:
                in_fence = False
        else:
            open_match = _FENCE_OPEN_RE.match(line)
            if open_match:
                marker = open_match.group(1)
                fence_char = marker[0]
                fence_len = len(marker)
                in_fence = True
            elif _ATX_HEADING_RE.match(line):
                starts.append(pos)

        if newline_idx == -1:
            break
        pos = newline_idx + 1

    return starts


def _markdown_sections(text: str) -> list[tuple[int, int]]:
    """Partition `text` into (start, end) section spans at every
    Markdown heading boundary found by `_markdown_heading_starts()`, plus
    an implicit boundary at offset 0. Each heading starts its own
    section, with the heading line at the very beginning of that
    section's text - so no section (and therefore no chunk packed from
    it) can ever contain text from both before and after a real
    heading."""

    boundaries = sorted(set([0, *_markdown_heading_starts(text)]))
    ends = boundaries[1:] + [len(text)]
    return [(start, end) for start, end in zip(boundaries, ends) if end > start]


def _overlap_seed_start(text: str, end: int, floor: int) -> int:
    """Return an overlap start offset within text[floor:end): the offset
    just after the *earliest* whitespace character at or after `floor`,
    so the seeded overlap is as close as possible to
    CHUNK_OVERLAP_CHARACTERS long without starting mid-word. Falls back
    to `floor` itself (a hard start) if no whitespace exists in range.
    Deliberately searches forward from `floor` rather than backward from
    `end` - in text with frequent whitespace (e.g. short words), the
    nearest boundary to `end` would collapse the overlap to almost
    nothing instead of seeding roughly CHUNK_OVERLAP_CHARACTERS of
    context."""

    if end <= floor:
        return floor
    idx_space = text.find(" ", floor, end)
    idx_nl = text.find("\n", floor, end)
    candidates = [i for i in (idx_space, idx_nl) if i != -1]
    if not candidates:
        return floor
    return min(candidates) + 1


def _forward_split_point(text: str, start: int, limit: int) -> int:
    """Return the offset just after the last whitespace character in
    text[start:limit), or `limit` itself if none exists - used to split a
    paragraph that is longer than the maximum chunk size without cutting
    a word, falling back to an exact hard cut when no whitespace exists."""

    idx = max(text.rfind(" ", start, limit), text.rfind("\n", start, limit))
    if idx == -1 or idx <= start:
        return limit
    return idx + 1


def _hard_split_span(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split one over-long paragraph span into multiple chunk spans, each
    at most MAX_CHUNK_CHARACTERS, with CHUNK_OVERLAP_CHARACTERS of
    overlap seeded from the end of the previous piece."""

    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        limit = min(cursor + MAX_CHUNK_CHARACTERS, end)
        if limit >= end:
            spans.append((cursor, end))
            break
        split_at = _forward_split_point(text, cursor, limit)
        spans.append((cursor, split_at))
        floor = max(cursor, split_at - CHUNK_OVERLAP_CHARACTERS)
        cursor = _overlap_seed_start(text, split_at, floor)
    return spans


def _pack_chunk_spans(
    text: str, paragraph_spans: list[tuple[int, int]], section_start: int = 0
) -> list[tuple[int, int]]:
    """Greedily pack consecutive paragraph spans into chunk spans of at
    most MAX_CHUNK_CHARACTERS, seeding each new chunk with
    CHUNK_OVERLAP_CHARACTERS of trailing overlap from the previous one. A
    paragraph longer than MAX_CHUNK_CHARACTERS on its own is hard-split
    independently via _hard_split_span(). `section_start` clamps every
    overlap seed to never search or start before it, so overlap can never
    reach earlier than the current Markdown section (default 0, the
    whole-document case, leaves non-Markdown behavior unchanged)."""

    chunks: list[tuple[int, int]] = []
    pending_start: int | None = None
    pending_end: int | None = None

    def flush() -> None:
        nonlocal pending_start, pending_end
        if pending_start is not None and pending_end is not None and pending_end > pending_start:
            chunks.append((pending_start, pending_end))
        pending_start = None
        pending_end = None

    for p_start, p_end in paragraph_spans:
        if p_end - p_start > MAX_CHUNK_CHARACTERS:
            flush()
            chunks.extend(_hard_split_span(text, p_start, p_end))
            continue

        if pending_start is None:
            pending_start, pending_end = p_start, p_end
            continue

        if p_end - pending_start <= MAX_CHUNK_CHARACTERS:
            pending_end = p_end
            continue

        closed_end = pending_end
        flush()
        floor = max(p_start - CHUNK_OVERLAP_CHARACTERS, section_start)
        overlap_start = _overlap_seed_start(text, min(closed_end, p_start), floor)
        pending_start, pending_end = overlap_start, p_end

    flush()
    return chunks


@dataclass(frozen=True)
class ChunkSpan:
    ordinal: int
    text: str
    char_start: int
    char_end: int


def chunk_normalized_text(text: str, *, is_markdown: bool = False) -> list[ChunkSpan]:
    """Deterministically split already-normalized document text into
    ordered, non-empty chunks of at most MAX_CHUNK_CHARACTERS each, with
    CHUNK_OVERLAP_CHARACTERS of overlap between consecutive chunks.
    Raises SourceLimitExceededError if the document or its resulting
    chunk count exceeds the fixed safety limits.

    When `is_markdown` is True (callers pass this only for files whose
    suffix is exactly ".md", case-insensitively), the text is first
    partitioned into sections at every ATX heading boundary
    (`_markdown_sections()`), and paragraphs are packed independently
    within each section - so a heading always starts its section's first
    chunk, no chunk ever straddles a real heading, and overlap seeding is
    clamped to the current section's start. `is_markdown=False` (the
    default) packs the whole document as a single section, identical to
    this function's behavior before Milestone 38.2B."""

    if len(text) > MAX_DOCUMENT_CHARACTERS:
        raise SourceLimitExceededError("document exceeds the character limit")

    sections = _markdown_sections(text) if is_markdown else [(0, len(text))]

    chunk_spans: list[tuple[int, int]] = []
    for section_start, section_end in sections:
        paragraph_spans = [
            (start + section_start, end + section_start)
            for start, end in _paragraph_spans(text[section_start:section_end])
        ]
        chunk_spans.extend(_pack_chunk_spans(text, paragraph_spans, section_start))

    if len(chunk_spans) > MAX_CHUNKS_PER_DOCUMENT:
        raise SourceLimitExceededError("document exceeds the chunk count limit")

    return [
        ChunkSpan(ordinal=i, text=text[start:end], char_start=start, char_end=end)
        for i, (start, end) in enumerate(chunk_spans)
    ]


def compute_content_hash(normalized_text: str) -> str:
    """A real content hash - depends only on the normalized document
    text, never on source_key or relative_path."""

    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def compute_document_key(source_key: str, relative_path_key: str) -> str:
    """Stable document identity - depends on source_key and the
    case-normalized relative path, never on content."""

    return hashlib.sha256(f"{source_key}\n{relative_path_key}".encode("utf-8")).hexdigest()


def compute_chunk_text_hash(chunk_text: str) -> str:
    return hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()


def compute_chunk_id(
    document_key: str, content_hash: str, chunk_ordinal: int, chunk_text_hash: str
) -> str:
    """Stable chunk identifier, derived from trusted values only (never
    Python's process-randomized hash())."""

    payload = f"{document_key}\n{content_hash}\n{chunk_ordinal}\n{chunk_text_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
