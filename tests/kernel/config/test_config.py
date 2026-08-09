"""Tests for kernel/config/config.py: Config/load_config(). Two kinds of
test here, deliberately: tests against the REAL, committed
kernel/config/config.yaml (proving the actual authoritative planner
selection is what it should be - the whole point of Milestone 41 P3's
configuration work), and tests against a synthetic tmp_path config.yaml
(proving load_config()'s parsing behavior generically, independent of
today's specific values)."""

import pytest
import yaml

import kernel.config.config as config_module
from kernel.config.config import Config, load_config


# --- the real, committed config.yaml - authoritative planner selection ----


def test_real_config_general_conversational_provider_is_unchanged():
    config = load_config()
    assert config.provider == "ollama"
    assert config.provider_settings["model"] == "llama3.1:8b"


def test_real_config_planner_provider_is_ollama():
    config = load_config()
    assert config.planner_provider == "ollama"


def test_real_config_planner_model_is_gemma3_12b():
    config = load_config()
    assert config.planner_provider_settings["model"] == "gemma3:12b"


def test_real_config_planner_settings_are_a_complete_provider_settings_shape():
    # Shaped exactly like provider_settings, so a future caller can
    # construct a ModelProvider from it the same way get_provider() does
    # for the general provider - not exercised here (no ModelProvider is
    # constructed in this milestone), only proven complete.
    config = load_config()
    assert set(config.planner_provider_settings.keys()) == {
        "model", "base_url", "max_tokens", "temperature"
    }


def test_real_config_planner_and_general_provider_settings_are_independent_objects():
    config = load_config()
    assert config.planner_provider_settings is not config.provider_settings
    assert config.planner_provider_settings["model"] != config.provider_settings["model"]


# --- synthetic config.yaml - generic parsing/compatibility behavior --------


def _write_config_yaml(tmp_path, data: dict):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _base_config_dict(**overrides) -> dict:
    data = {
        "provider": "ollama",
        "providers": {
            "ollama": {
                "model": "llama3.1:8b",
                "base_url": "http://localhost:11434",
                "max_tokens": 1024,
                "temperature": 1.0,
            },
        },
        "planner": {
            "provider": "ollama",
            "providers": {
                "ollama": {
                    "model": "gemma3:12b",
                    "base_url": "http://localhost:11434",
                    "max_tokens": 1024,
                    "temperature": 0.0,
                },
            },
        },
        "log_dir": "storage/logs",
        "log_file": "interactions.jsonl",
        "memory": {"storage_dir": "storage/memory"},
        "knowledge": {"storage_dir": "storage/knowledge"},
    }
    data.update(overrides)
    return data


def test_load_config_parses_a_complete_synthetic_config(tmp_path, monkeypatch):
    config_path = _write_config_yaml(tmp_path, _base_config_dict())
    monkeypatch.setattr(config_module, "_CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(config_module, "_ENV_PATH", tmp_path / "does-not-exist.env")

    config = load_config()

    assert config.provider == "ollama"
    assert config.provider_settings["model"] == "llama3.1:8b"
    assert config.planner_provider == "ollama"
    assert config.planner_provider_settings["model"] == "gemma3:12b"
    assert config.memory_settings == {"storage_dir": "storage/memory"}


def test_load_config_still_works_without_the_planner_section_absent_is_an_error(
    tmp_path, monkeypatch
):
    # Documents current, deliberately minimal behavior: load_config() does
    # not silently default a missing planner section - it fails the same
    # way a missing "provider"/"providers" section already does (a plain
    # KeyError), matching this module's existing, unchanged validation
    # style rather than inventing a stricter policy for only the new field.
    data = _base_config_dict()
    del data["planner"]
    config_path = _write_config_yaml(tmp_path, data)
    monkeypatch.setattr(config_module, "_CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(config_module, "_ENV_PATH", tmp_path / "does-not-exist.env")

    with pytest.raises(KeyError):
        load_config()


def test_load_config_planner_provider_can_differ_from_general_provider(tmp_path, monkeypatch):
    # The planner selection is independent of the general provider - a
    # config naming a different planner provider than the general one is
    # valid (this milestone does not require them to match, only that
    # each is parsed and exposed correctly).
    data = _base_config_dict()
    data["planner"] = {
        "provider": "anthropic",
        "providers": {"anthropic": {"model": "claude-planner-test"}},
    }
    config_path = _write_config_yaml(tmp_path, data)
    monkeypatch.setattr(config_module, "_CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(config_module, "_ENV_PATH", tmp_path / "does-not-exist.env")

    config = load_config()

    assert config.provider == "ollama"
    assert config.planner_provider == "anthropic"
    assert config.planner_provider_settings == {"model": "claude-planner-test"}


def test_config_constructed_without_planner_args_defaults_to_none():
    # Every pre-Milestone-41 caller that constructs a bare Config (several
    # existing tests do) keeps working unchanged.
    config = Config(
        provider="fake",
        provider_settings={},
        log_path=None,
        memory_settings={},
        knowledge_storage_dir=None,
    )
    assert config.planner_provider is None
    assert config.planner_provider_settings is None
