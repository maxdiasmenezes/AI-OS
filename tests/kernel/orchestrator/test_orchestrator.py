"""Tests for Orchestrator.handle()."""

import json
from pathlib import Path
from unittest.mock import Mock

from kernel.capabilities.base import Capability
from kernel.config.config import Config
from kernel.memory import MemoryManager
from kernel.models.base import ModelProvider, ModelResponse
from kernel.orchestrator import Orchestrator

# tests/kernel/orchestrator/test_orchestrator.py -> tests/kernel -> tests -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SYSTEM_PROMPT = (_PROJECT_ROOT / "prompts" / "system.md").read_text(encoding="utf-8").strip()


class FakeModelProvider(ModelProvider):
    """Records every prompt it receives and returns a fixed response."""

    def __init__(self, response: ModelResponse):
        self._response = response
        self.received_prompts: list[str] = []

    def send_prompt(self, prompt: str) -> ModelResponse:
        self.received_prompts.append(prompt)
        return self._response


class FakeCapability(Capability):
    """Records the prompt it receives and returns a fixed response."""

    def __init__(self, capability_id: str, response_text: str):
        self._id = capability_id
        self._response_text = response_text
        self.received_prompts: list[str] = []

    @property
    def id(self) -> str:
        return self._id

    def handle(self, prompt: str) -> str:
        self.received_prompts.append(prompt)
        return self._response_text


def _make_config(tmp_path: Path) -> Config:
    return Config(
        provider="fake",
        provider_settings={},
        log_path=tmp_path / "logs" / "interactions.jsonl",
        memory_settings={"storage_dir": str(tmp_path / "memory")},
    )


def _make_orchestrator(monkeypatch, config, fake_provider, capability_loader):
    # Patch the symbol the orchestrator module imported, not kernel.models.get_provider.
    monkeypatch.setattr(
        "kernel.orchestrator.orchestrator.get_provider",
        lambda cfg: fake_provider,
    )
    return Orchestrator(config, capability_loader=capability_loader)


def _fake_response(text: str = "fallback response") -> ModelResponse:
    return ModelResponse(
        text=text,
        model="fake-model",
        input_tokens=11,
        output_tokens=22,
        latency_seconds=0.5,
    )


def _read_last_log_record(log_path: Path) -> dict:
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


# --- Routed branch -----------------------------------------------------


def test_wine_prompt_routes_to_capability_and_skips_model(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_capability = FakeCapability("wine", "a bold Malbec would work well")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What wine goes with steak?")

    loader.assert_called_once_with("wine", fake_provider)
    assert fake_capability.received_prompts == ["What wine goes with steak?"]
    assert response.text == "a bold Malbec would work well"
    assert response.model == "capability:wine"
    assert fake_provider.received_prompts == []


# --- Fallback branch -----------------------------------------------------


def test_unrelated_prompt_uses_model_provider(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fixed_response = _fake_response("the weather tomorrow is sunny")
    fake_provider = FakeModelProvider(fixed_response)
    loader = Mock()

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What's the weather tomorrow?")

    loader.assert_not_called()
    assert len(fake_provider.received_prompts) == 1
    assert response is fixed_response


def test_fallback_prompt_contains_system_memory_and_user_prompt_in_order(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    loader = Mock()

    # Seed conversation memory using the same storage settings the
    # orchestrator's own MemoryManager will read from.
    seed_memory = MemoryManager(config.memory_settings)
    seed_memory.remember("conversation", "What's a good everyday red?", metadata={"role": "user"})
    seed_memory.remember("conversation", "Try a Cotes du Rhone.", metadata={"role": "assistant"})

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    orchestrator.handle("What's the weather tomorrow?")

    loader.assert_not_called()
    assert len(fake_provider.received_prompts) == 1
    sent_prompt = fake_provider.received_prompts[0]

    system_index = sent_prompt.index(_SYSTEM_PROMPT)
    memory_index = sent_prompt.index("Try a Cotes du Rhone.")
    user_index = sent_prompt.index("What's the weather tomorrow?")

    assert system_index < memory_index < user_index


# --- Shared: memory persistence -----------------------------------------


def test_routed_branch_persists_user_prompt_and_response_to_memory(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_capability = FakeCapability("wine", "a crisp Sauvignon Blanc")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What wine goes with fish?")

    memory = MemoryManager(config.memory_settings)
    entries = memory.recall("conversation")

    assert len(entries) == 2
    assert entries[0].content == "What wine goes with fish?"
    assert entries[0].metadata["role"] == "user"
    assert entries[1].content == response.text
    assert entries[1].metadata["role"] == "assistant"


def test_fallback_branch_persists_user_prompt_and_response_to_memory(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fixed_response = _fake_response("it's sunny tomorrow")
    fake_provider = FakeModelProvider(fixed_response)
    loader = Mock()

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    orchestrator.handle("What's the weather tomorrow?")

    memory = MemoryManager(config.memory_settings)
    entries = memory.recall("conversation")

    assert len(entries) == 2
    assert entries[0].content == "What's the weather tomorrow?"
    assert entries[0].metadata["role"] == "user"
    assert entries[1].content == "it's sunny tomorrow"
    assert entries[1].metadata["role"] == "assistant"


# --- Shared: interaction logging -----------------------------------------


def test_routed_branch_logs_interaction(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fake_provider = FakeModelProvider(_fake_response())
    fake_capability = FakeCapability("wine", "a light Pinot Noir")
    loader = Mock(return_value=fake_capability)

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    response = orchestrator.handle("What wine goes with salmon?")

    record = _read_last_log_record(config.log_path)
    assert record["prompt"] == "What wine goes with salmon?"
    assert record["response"] == response.text
    assert record["model"] == "capability:wine"
    assert record["input_tokens"] == 0
    assert record["output_tokens"] == 0
    assert record["latency_seconds"] == 0.0


def test_fallback_branch_logs_interaction(monkeypatch, tmp_path):
    config = _make_config(tmp_path)
    fixed_response = _fake_response("it's sunny tomorrow")
    fake_provider = FakeModelProvider(fixed_response)
    loader = Mock()

    orchestrator = _make_orchestrator(monkeypatch, config, fake_provider, loader)
    orchestrator.handle("What's the weather tomorrow?")

    record = _read_last_log_record(config.log_path)
    assert record["prompt"] == "What's the weather tomorrow?"
    assert record["response"] == "it's sunny tomorrow"
    assert record["model"] == "fake-model"
    assert record["input_tokens"] == 11
    assert record["output_tokens"] == 22
    assert record["latency_seconds"] == 0.5
