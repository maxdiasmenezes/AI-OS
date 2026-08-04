"""Tests for capabilities/knowledge_commands/capability.py: the `/knowledge
ask` verb - evidence retrieval, exactly-once model invocation, structured
response validation, grounding/insufficiency behavior, citation/source
formatting, privacy, and audit.

Every model call is a fake ModelProvider; every evidence call is a fake
evidence_fn; nothing here touches a real database, a real model, or the
network.
"""

import json
import logging

import pytest

from kernel.capabilities.base import EphemeralResult
from kernel.knowledge_base.config import KnowledgeBaseConfig, SourceSpec
from kernel.knowledge_base.evidence import EvidenceChunk
from kernel.knowledge_base.types import DatabaseUnavailableError, InvalidSourceFilterError
from kernel.models.base import ModelResponse
from kernel.tools.confirmation import ConfirmationStore

from capabilities.knowledge_commands.capability import KnowledgeCommandsCapability


# --- test doubles ------------------------------------------------------


class FakeModelProvider:
    """Records every prompt received; returns a fixed response text or
    raises a fixed exception."""

    def __init__(self, response_text=None, error=None):
        self._response_text = response_text
        self._error = error
        self.received_prompts: list[str] = []
        self.call_count = 0

    def send_prompt(self, prompt: str) -> ModelResponse:
        self.received_prompts.append(prompt)
        self.call_count += 1
        if self._error is not None:
            raise self._error
        return ModelResponse(
            text=self._response_text, model="fake", input_tokens=0, output_tokens=0, latency_seconds=0.0
        )


class _RecordingEvidenceFn:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else []
        self.error = error
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


def _config(sources: dict = None) -> KnowledgeBaseConfig:
    sources = sources or {}
    return KnowledgeBaseConfig(
        approved_sources={key: SourceSpec(path=str(path), recursive=True) for key, path in sources.items()}
    )


def _audit_recorder():
    calls = []

    def record(action, resource_key, outcome):
        calls.append((action, resource_key, outcome))

    return record, calls


def _make_ask_capability(
    model_provider=None,
    evidence_fn=None,
    config=None,
    audit_record=None,
):
    if config is None:
        config = _config()
    return KnowledgeCommandsCapability(
        model_provider,
        None,
        None,
        confirmation_store=ConfirmationStore(),
        config_loader=lambda: config,
        evidence_fn=evidence_fn,
        audit_record=audit_record,
    )


def _chunk(ordinal=1, text="Repository backups are timestamped archives.", source_key="ai_os_docs", path="architecture.md"):
    return EvidenceChunk(source_key=source_key, relative_path=path, chunk_ordinal=ordinal, text=text, rank=-1.0)


def _valid_model_text(answer="Backups are timestamped archives [S1].", citations=("S1",), sufficient=True):
    return json.dumps({"answer": answer, "used_citations": list(citations), "sufficient": sufficient})


# --- parsing integration: model never invoked on parse/auth failure -------


def test_no_model_call_on_parse_failure():
    provider = FakeModelProvider(response_text=_valid_model_text())
    capability = _make_ask_capability(model_provider=provider)

    response = capability.handle("/knowledge ask no delimiter here")

    assert response == "Invalid knowledge command. Use /knowledge help."
    assert provider.call_count == 0


# --- evidence retrieval integration ----------------------------------------


def test_ask_calls_evidence_fn_with_question_and_default_limit():
    evidence_fn = _RecordingEvidenceFn(result=[])
    capability = _make_ask_capability(
        model_provider=FakeModelProvider(response_text=_valid_model_text()), evidence_fn=evidence_fn
    )

    capability.handle("/knowledge ask -- how does backup work")

    assert len(evidence_fn.calls) == 1
    args, kwargs = evidence_fn.calls[0]
    assert args[0] == "how does backup work"
    assert kwargs["limit"] == 3  # DEFAULT_EVIDENCE_LIMIT


def test_ask_passes_source_filter_and_explicit_limit():
    evidence_fn = _RecordingEvidenceFn(result=[])
    capability = _make_ask_capability(
        model_provider=FakeModelProvider(response_text=_valid_model_text()), evidence_fn=evidence_fn
    )

    capability.handle("/knowledge ask --source ai_os_docs --limit 2 -- question")

    args, kwargs = evidence_fn.calls[0]
    assert kwargs["source_keys"] == ["ai_os_docs"]
    assert kwargs["limit"] == 2


def test_no_model_call_when_no_evidence():
    provider = FakeModelProvider(response_text=_valid_model_text())
    capability = _make_ask_capability(model_provider=provider, evidence_fn=_RecordingEvidenceFn(result=[]))

    response = capability.handle("/knowledge ask -- question")

    assert response == "No relevant local knowledge was found for that question."
    assert provider.call_count == 0


def test_no_model_call_on_retrieval_failure():
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(error=DatabaseUnavailableError("knowledge database is unavailable"))
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert response == "Knowledge database is unavailable."
    assert provider.call_count == 0


def test_unknown_source_maps_to_fixed_message_no_model_call():
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(error=InvalidSourceFilterError("unknown source in filter"))
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask --source bogus -- question")

    assert response == "Unknown knowledge source in filter."
    assert provider.call_count == 0


# --- model invocation --------------------------------------------------


def test_exactly_one_provider_call_for_sufficient_answer():
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    capability.handle("/knowledge ask -- how does backup work")

    assert provider.call_count == 1


def test_injected_provider_used_not_a_new_one():
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    capability.handle("/knowledge ask -- question")

    assert capability._model_provider is provider


def test_provider_exception_maps_to_fixed_service_unavailable_reply():
    provider = FakeModelProvider(error=ConnectionError("connection refused to 10.0.0.5:11434"))
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert response == "The knowledge answer service is temporarily unavailable. Please try again later."
    assert "10.0.0.5" not in response
    assert "ConnectionError" not in response


def test_provider_timeout_exception_maps_to_same_fixed_reply():
    provider = FakeModelProvider(error=TimeoutError("timed out"))
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert response == "The knowledge answer service is temporarily unavailable. Please try again later."


def test_empty_provider_response_maps_safely():
    provider = FakeModelProvider(response_text="")
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert response == "The model did not produce a verifiable grounded answer."


def test_no_retry_on_provider_exception():
    provider = FakeModelProvider(error=RuntimeError("boom"))
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    capability.handle("/knowledge ask -- question")

    assert provider.call_count == 1  # not retried


def test_no_second_model_call_for_citation_repair():
    provider = FakeModelProvider(response_text="not valid json at all")
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    capability.handle("/knowledge ask -- question")

    assert provider.call_count == 1


# --- grounding and insufficient-evidence outcomes ---------------------------


def test_sufficient_answer_includes_citation_and_source_section():
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(result=[_chunk(ordinal=63, path="architecture.md")])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- how does backup work")

    assert "[S1]" in response
    assert "Sources:" in response
    assert "architecture.md" in response
    assert "chunk 63" in response


def test_insufficient_evidence_returns_fixed_reply_not_model_text():
    text = json.dumps({"answer": "should never appear", "used_citations": [], "sufficient": False})
    provider = FakeModelProvider(response_text=text)
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert response == "The local knowledge sources do not contain enough information to answer that question."
    assert "should never appear" not in response


def test_malformed_response_maps_to_unverifiable_not_insufficient():
    provider = FakeModelProvider(response_text="{not json")
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert response == "The model did not produce a verifiable grounded answer."


def test_invented_citation_rejected_maps_to_unverifiable():
    text = json.dumps({"answer": "claim [S9]", "used_citations": ["S9"], "sufficient": True})
    provider = FakeModelProvider(response_text=text)
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert response == "The model did not produce a verifiable grounded answer."


def test_no_results_and_insufficient_and_unverifiable_are_distinct():
    no_results_capability = _make_ask_capability(
        model_provider=FakeModelProvider(response_text=_valid_model_text()),
        evidence_fn=_RecordingEvidenceFn(result=[]),
    )
    insufficient_text = json.dumps({"answer": "x", "used_citations": [], "sufficient": False})
    insufficient_capability = _make_ask_capability(
        model_provider=FakeModelProvider(response_text=insufficient_text),
        evidence_fn=_RecordingEvidenceFn(result=[_chunk()]),
    )
    unverifiable_capability = _make_ask_capability(
        model_provider=FakeModelProvider(response_text="not json"),
        evidence_fn=_RecordingEvidenceFn(result=[_chunk()]),
    )

    no_results = no_results_capability.handle("/knowledge ask -- question")
    insufficient = insufficient_capability.handle("/knowledge ask -- question")
    unverifiable = unverifiable_capability.handle("/knowledge ask -- question")

    assert len({no_results, insufficient, unverifiable}) == 3


# --- reply formatting / bounded limits --------------------------------------


def test_generated_answer_truncated_deterministically():
    long_answer = ("word " * 1000) + "[S1]"
    text = json.dumps({"answer": long_answer, "used_citations": ["S1"], "sufficient": True})
    provider = FakeModelProvider(response_text=text)
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert len(response) <= 3_500


def test_reply_never_exceeds_final_budget_with_five_sources():
    long_answer = " ".join(f"claim [S{i}]" for i in range(1, 6)) + " " + ("padding " * 400)
    citations = [f"S{i}" for i in range(1, 6)]
    text = json.dumps({"answer": long_answer, "used_citations": citations, "sufficient": True})
    provider = FakeModelProvider(response_text=text)
    evidence = [_chunk(ordinal=i, path="p" * 70 + ".md") for i in range(1, 6)]
    evidence_fn = _RecordingEvidenceFn(result=evidence)
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask --limit 5 -- question")

    assert len(response) <= 3_500


def test_response_never_split_into_multiple_messages():
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert isinstance(response, str)  # a single string reply, never a list


# --- trust: gate is at capability construction time, verified in orchestrator tests ---


# --- audit -----------------------------------------------------------------


def test_ask_audit_action_and_outcome_on_success():
    record, calls = _audit_recorder()
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn, audit_record=record)

    capability.handle("/knowledge ask --source ai_os_docs -- question")

    assert ("knowledge_ask", "ai_os_docs", "executed") in calls


def test_ask_audit_outcome_failed_on_retrieval_error():
    record, calls = _audit_recorder()
    evidence_fn = _RecordingEvidenceFn(error=DatabaseUnavailableError("x"))
    capability = _make_ask_capability(
        model_provider=FakeModelProvider(response_text=_valid_model_text()),
        evidence_fn=evidence_fn,
        audit_record=record,
    )

    capability.handle("/knowledge ask -- question")

    assert ("knowledge_ask", None, "failed") in calls


def test_ask_audit_outcome_failed_on_model_error():
    record, calls = _audit_recorder()
    provider = FakeModelProvider(error=RuntimeError("boom"))
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn, audit_record=record)

    capability.handle("/knowledge ask -- question")

    assert ("knowledge_ask", None, "failed") in calls


def test_ask_audit_never_contains_question_or_answer():
    record, calls = _audit_recorder()
    provider = FakeModelProvider(response_text=_valid_model_text(answer="secret answer text [S1]"))
    evidence_fn = _RecordingEvidenceFn(result=[_chunk(text="secret evidence text")])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn, audit_record=record)

    capability.handle("/knowledge ask -- secret-question-token")

    for action, resource_key, outcome in calls:
        assert "secret-question-token" not in str(resource_key)
        assert "secret answer text" not in str(resource_key)
        assert "secret evidence text" not in str(resource_key)


# --- privacy: logs -----------------------------------------------------


def test_question_absent_from_logs(caplog):
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    with caplog.at_level(logging.DEBUG):
        capability.handle("/knowledge ask -- a-very-specific-secret-question-token")

    for record in caplog.records:
        assert "a-very-specific-secret-question-token" not in record.getMessage()


def test_evidence_absent_from_logs(caplog):
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(result=[_chunk(text="a-very-specific-secret-evidence-token")])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    with caplog.at_level(logging.DEBUG):
        capability.handle("/knowledge ask -- question")

    for record in caplog.records:
        assert "a-very-specific-secret-evidence-token" not in record.getMessage()


def test_provider_response_absent_from_logs(caplog):
    provider = FakeModelProvider(response_text=_valid_model_text(answer="a-secret-answer-token [S1]"))
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    with caplog.at_level(logging.DEBUG):
        capability.handle("/knowledge ask -- question")

    for record in caplog.records:
        assert "a-secret-answer-token" not in record.getMessage()


def test_exception_text_absent_from_reply():
    provider = FakeModelProvider(error=RuntimeError("a-very-specific-secret-exception-token"))
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert "a-very-specific-secret-exception-token" not in response


# --- no pending confirmation state for ask ----------------------------------


def test_ask_never_touches_confirmation_store():
    store = ConfirmationStore()
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = KnowledgeCommandsCapability(
        provider,
        None,
        None,
        confirmation_store=store,
        config_loader=lambda: _config(),
        evidence_fn=evidence_fn,
    )

    capability.handle("/knowledge ask -- question")

    pending, _ = store.consume()
    assert pending is None


# --- returns EphemeralResult -----------------------------------------------


def test_ask_result_is_ephemeral():
    provider = FakeModelProvider(response_text=_valid_model_text())
    evidence_fn = _RecordingEvidenceFn(result=[_chunk()])
    capability = _make_ask_capability(model_provider=provider, evidence_fn=evidence_fn)

    response = capability.handle("/knowledge ask -- question")

    assert isinstance(response, EphemeralResult)


def test_ask_no_results_result_is_ephemeral():
    provider = FakeModelProvider(response_text=_valid_model_text())
    capability = _make_ask_capability(model_provider=provider, evidence_fn=_RecordingEvidenceFn(result=[]))

    response = capability.handle("/knowledge ask -- question")

    assert isinstance(response, EphemeralResult)
