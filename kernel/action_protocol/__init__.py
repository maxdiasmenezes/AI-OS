"""
Public interface of the Milestone 39 action protocol.

Callers outside this package must import from here, matching the
convention used by kernel/tools/__init__.py and kernel/knowledge/__init__.py.
This package has no production caller yet (see README.md) - it exists as a
self-contained, testable library: deterministic candidate resolution
(Stage A), a dynamic prompt/schema (Stage B), and a strict parser. Nothing
here executes an action, accesses kernel/tools/confirmation.py, or wires
into kernel/orchestrator/.
"""

from kernel.action_protocol.candidates import resolve_action_candidates
from kernel.action_protocol.parser import parse_decision
from kernel.action_protocol.prompt import build_prompt, build_schema
from kernel.action_protocol.types import (
    PROTOCOL_VERSION,
    ActionCandidate,
    CandidateResolution,
    CannotCompleteDecision,
    Decision,
    ParseErrorCode,
    ParseFailure,
    ParseResult,
    ParseSuccess,
    RequestClarificationDecision,
    RespondDecision,
    SelectCandidateDecision,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ActionCandidate",
    "CandidateResolution",
    "CannotCompleteDecision",
    "Decision",
    "ParseErrorCode",
    "ParseFailure",
    "ParseResult",
    "ParseSuccess",
    "RequestClarificationDecision",
    "RespondDecision",
    "SelectCandidateDecision",
    "resolve_action_candidates",
    "parse_decision",
    "build_prompt",
    "build_schema",
]
