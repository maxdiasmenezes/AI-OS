"""
Prompt-data construction, structured-response parsing, and citation
validation for `/knowledge ask` (Milestone 38).

Nothing here performs I/O, invokes a model, or contacts a network - every
function is pure, given already-retrieved EvidenceChunk objects (see
evidence.py) and, for parsing, the raw text a model provider returned.
capabilities/knowledge_commands/capability.py is the only caller: it owns
the model-provider call itself, trusted-context gating, audit, and final
WhatsApp-safe reply formatting (see that module).

The model-provider contract (kernel/models/base.py) accepts one flat
prompt string - there is no true system/user role separation, and this
module does not pretend otherwise. The fixed instructions
(prompts/knowledge/ask_system.md) and the untrusted question+evidence data
are both embedded in that single string, but the untrusted data is
confined entirely inside one json.dumps(..., ensure_ascii=False) blob
between fixed marker lines. json.dumps() escapes every newline and control
character inside string values, so the whole blob is always exactly one
line of text with no embedded raw newlines - retrieved evidence content
can never produce a standalone line that looks like the closing marker,
regardless of what it contains. No delimiter mutation, zero-width
characters, XML parsing, eval, or exec are used anywhere in this module.
"""

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kernel.knowledge_base.evidence import EvidenceChunk

# kernel/knowledge_base/answer.py -> kernel/knowledge_base -> kernel -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ASK_SYSTEM_PROMPT_PATH = _PROJECT_ROOT / "prompts" / "knowledge" / "ask_system.md"
_SYSTEM_INSTRUCTIONS = _ASK_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()

_BEGIN_MARKER = "<<<BEGIN_UNTRUSTED_JSON_DATA>>>"
_END_MARKER = "<<<END_UNTRUSTED_JSON_DATA>>>"

_CITATION_LABEL_RE = re.compile(r"^S[1-5]$")
_INLINE_CITATION_RE = re.compile(r"\[(S[1-5])\]")

_REQUIRED_RESPONSE_FIELDS = frozenset({"answer", "used_citations", "sufficient"})

MAX_GENERATED_ANSWER_CHARACTERS = 2_500
MAX_DISPLAYED_SOURCE_ENTRIES = 5


@dataclass(frozen=True)
class CitationMeta:
    """Deterministic, code-generated metadata for one citation label -
    never taken from model output. label is one of "S1".."S5", assigned
    in retrieval order (see assign_citation_labels)."""

    label: str
    source_key: str
    relative_path: str
    chunk_ordinal: int


def assign_citation_labels(evidence: list[EvidenceChunk]) -> list[CitationMeta]:
    """Assign deterministic S1..S5 labels to evidence chunks in the order
    they were retrieved (already rank-ordered by evidence.py) - never by
    the model."""

    return [
        CitationMeta(
            label=f"S{index}",
            source_key=chunk.source_key,
            relative_path=chunk.relative_path,
            chunk_ordinal=chunk.chunk_ordinal,
        )
        for index, chunk in enumerate(evidence, start=1)
    ]


def build_prompt(
    question: str, evidence: list[EvidenceChunk], citations: list[CitationMeta]
) -> str:
    """Assemble the one flat prompt string sent to the model provider:
    fixed instructions, then the untrusted question+evidence data as one
    JSON object between fixed marker lines. `citations` must be
    assign_citation_labels(evidence) - passed in rather than recomputed so
    the caller and this function are guaranteed to agree on label
    assignment."""

    evidence_items = [
        {
            "label": citation.label,
            "source_key": citation.source_key,
            "relative_path": citation.relative_path,
            "chunk_ordinal": citation.chunk_ordinal,
            "text": chunk.text,
        }
        for citation, chunk in zip(citations, evidence)
    ]
    data = {
        "question": question,
        "evidence": evidence_items,
        "required_response_schema": {
            "answer": "string",
            "used_citations": ["S1"],
            "sufficient": True,
        },
    }
    json_blob = json.dumps(data, ensure_ascii=False)

    return (
        f"{_SYSTEM_INSTRUCTIONS}\n\n"
        f"{_BEGIN_MARKER}\n{json_blob}\n{_END_MARKER}\n"
    )


class AnswerOutcome(Enum):
    """The three possible outcomes of parsing a model's structured
    response - deliberately distinct from each other so the capability
    never conflates "the model said the evidence is insufficient" with
    "the model's response could not be trusted at all"."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class AnswerResult:
    """Result of parse_structured_answer(). `answer` and `used_citations`
    are only meaningful (non-empty) when outcome is SUFFICIENT - for
    INSUFFICIENT and UNVERIFIABLE the model's own answer text is never
    surfaced, by construction: the caller never reads these fields for
    those outcomes, and the capability substitutes its own fixed reply
    text instead."""

    outcome: AnswerOutcome
    answer: str = ""
    used_citations: tuple[str, ...] = ()


def extract_inline_citation_labels(text: str) -> set[str]:
    """Every complete `[S#]` token appearing in text, as a set of bare
    labels ("S1", not "[S1]"). Shared by parsing (to validate consistency
    against used_citations) and by the capability's final reply-budget
    formatting (to know which citations survive answer truncation)."""

    return set(_INLINE_CITATION_RE.findall(text))


def parse_structured_answer(raw_text: str, valid_labels: frozenset[str]) -> AnswerResult:
    """Strictly parse and validate a model provider's raw response text
    against the required {"answer", "used_citations", "sufficient"}
    schema. `valid_labels` is the exact set of citation labels that were
    actually supplied to the model for this request (e.g. {"S1", "S2"}
    for two evidence chunks) - a label outside that set is treated as
    invented, never as merely unknown-but-harmless.

    Never raises: every failure mode (empty response, invalid JSON,
    surrounding commentary, wrong shape, wrong field types, unknown or
    invented citation labels, inline/used_citations inconsistency, an
    empty answer, zero valid citations on a "sufficient" answer) maps to
    AnswerResult(UNVERIFIABLE). A syntactically and semantically valid
    "sufficient: false" response maps to AnswerResult(INSUFFICIENT) - the
    model's answer text is discarded either way, only the outcome is
    read. json.loads() requires the *entire* string to be one JSON value,
    so any leading or trailing commentary around the object already fails
    parsing outright - no separate "surrounding text" check is needed.
    """

    if not isinstance(raw_text, str) or not raw_text.strip():
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)

    try:
        parsed = json.loads(raw_text.strip())
    except json.JSONDecodeError:
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)

    if not isinstance(parsed, dict):
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)
    if set(parsed.keys()) != _REQUIRED_RESPONSE_FIELDS:
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)

    answer = parsed["answer"]
    used_citations = parsed["used_citations"]
    sufficient = parsed["sufficient"]

    if not isinstance(answer, str):
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)
    if not isinstance(used_citations, list) or not all(
        isinstance(label, str) for label in used_citations
    ):
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)
    if not isinstance(sufficient, bool):
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)

    # Citation-label format and provenance are validated unconditionally -
    # a response claiming insufficiency while also citing a label that was
    # never supplied is malformed, not merely "insufficient".
    deduped_citations: list[str] = []
    seen: set[str] = set()
    for label in used_citations:
        if not _CITATION_LABEL_RE.match(label) or label not in valid_labels:
            return AnswerResult(AnswerOutcome.UNVERIFIABLE)
        if label not in seen:
            seen.add(label)
            deduped_citations.append(label)

    if not sufficient:
        return AnswerResult(AnswerOutcome.INSUFFICIENT)

    if not answer.strip():
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)

    inline_labels = extract_inline_citation_labels(answer)
    if not inline_labels.issubset(valid_labels):
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)
    if inline_labels != set(deduped_citations):
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)
    if not deduped_citations:
        return AnswerResult(AnswerOutcome.UNVERIFIABLE)

    # Order used_citations by retrieval rank (label suffix), never by
    # whatever order the model happened to list them in.
    ordered_citations = tuple(sorted(deduped_citations, key=lambda label: int(label[1:])))

    return AnswerResult(
        outcome=AnswerOutcome.SUFFICIENT, answer=answer, used_citations=ordered_citations
    )


def build_source_section(
    used_citations: tuple[str, ...],
    citations: list[CitationMeta],
    max_entries: int,
    max_path_characters: int,
) -> str:
    """Deterministically render the "Sources:" section from code-owned
    citation metadata only - never from anything the model returned.
    `used_citations` is expected already ordered (retrieval order) and
    already validated against `citations`."""

    label_to_meta = {citation.label: citation for citation in citations}
    lines = ["Sources:"]
    for label in used_citations[:max_entries]:
        meta = label_to_meta[label]
        path = meta.relative_path
        if len(path) > max_path_characters:
            keep = max(max_path_characters - 3, 0)
            path = path[:keep] + "..."
        lines.append(f"[{label}] {meta.source_key} — {path} — chunk {meta.chunk_ordinal}")
    return "\n".join(lines)
