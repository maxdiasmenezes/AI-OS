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
    """Everything the kernel needs to run a single prompt."""

    def __init__(self, provider: str, provider_settings: dict, log_path: Path,
                 memory_settings: dict, knowledge_storage_dir: Path):
        self.provider = provider
        self.provider_settings = provider_settings
        self.log_path = log_path
        self.memory_settings = memory_settings
        self.knowledge_storage_dir = knowledge_storage_dir


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

    return Config(
        provider=provider,
        provider_settings=provider_settings,
        log_path=log_path,
        memory_settings=memory_settings,
        knowledge_storage_dir=knowledge_storage_dir,
    )
