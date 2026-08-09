"""
Loads kernel configuration from two sources, on purpose:

- .env          -> secrets (e.g. provider API keys)
- config.yaml   -> everything else (active provider, its settings, log location)

Nothing outside this module should read os.environ or config.yaml directly.
"""

from pathlib import Path

import yaml
from dotenv import load_dotenv

# Paths are resolved relative to this file, not the current working
# directory, so the kernel behaves the same no matter where it's run from.
_CONFIG_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _CONFIG_DIR.parent.parent
_CONFIG_YAML_PATH = _CONFIG_DIR / "config.yaml"
_ENV_PATH = _PROJECT_ROOT / ".env"


class Config:
    """Everything the kernel needs to run a single prompt.

    `planner_provider`/`planner_provider_settings` (Milestone 41) are the
    dedicated structured-planner provider selection - shaped exactly like
    `provider`/`provider_settings` (the same dict a ModelProvider subclass's
    constructor expects), so a future caller can construct one the same
    way get_provider() constructs the general one, from
    `_PROVIDERS[config.planner_provider](config.planner_provider_settings)`.
    Nothing does that yet - no planner-provider factory or construction
    exists as of this milestone. Both default to None so every existing
    caller that constructs a Config without them (production `load_config()`
    always supplies both; some tests construct a bare Config for
    conversational-only scenarios that have no need of a planner provider)
    keeps working unchanged.
    """

    def __init__(self, provider: str, provider_settings: dict, log_path: Path,
                 memory_settings: dict, knowledge_storage_dir: Path,
                 planner_provider: str | None = None,
                 planner_provider_settings: dict | None = None):
        self.provider = provider
        self.provider_settings = provider_settings
        self.log_path = log_path
        self.memory_settings = memory_settings
        self.knowledge_storage_dir = knowledge_storage_dir
        self.planner_provider = planner_provider
        self.planner_provider_settings = planner_provider_settings


def load_config() -> Config:
    """Load settings from .env and config.yaml into a single Config object."""

    # Loading .env into the process environment is a no-op if the file is
    # missing, which lets a real deployment set secrets another way.
    load_dotenv(dotenv_path=_ENV_PATH)

    with open(_CONFIG_YAML_PATH, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    provider = settings["provider"]
    provider_settings = settings["providers"][provider]
    log_path = _PROJECT_ROOT / settings["log_dir"] / settings["log_file"]
    memory_settings = settings["memory"]
    knowledge_storage_dir = _PROJECT_ROOT / settings["knowledge"]["storage_dir"]

    planner_settings = settings["planner"]
    planner_provider = planner_settings["provider"]
    planner_provider_settings = planner_settings["providers"][planner_provider]

    return Config(
        provider=provider,
        provider_settings=provider_settings,
        log_path=log_path,
        memory_settings=memory_settings,
        knowledge_storage_dir=knowledge_storage_dir,
        planner_provider=planner_provider,
        planner_provider_settings=planner_provider_settings,
    )
