from pathlib import Path
from typing import Any

import yaml

from .settings import EnvSettings
from .types import MiniAgentConfig

_config: MiniAgentConfig | None = None


def load_config(config_path: str = "config.yaml") -> MiniAgentConfig:
    """
    Layer order (same as OpenClaw's loadConfig() in src/config/io.ts):
      1. Pydantic defaults (MiniAgentConfig field defaults)
      2. config.yaml values  (if file exists)
      3. Environment variables (highest priority, always wins)

    Returns a MiniAgentConfig instance with all layers merged.
    """
    # Layer 1: start with Pydantic defaults by dumping an empty-constructed model
    merged: dict[str, Any] = MiniAgentConfig().model_dump()

    # Layer 2: merge config.yaml on top of defaults (if file exists)
    path = Path(config_path)
    if path.exists():
        with path.open("r") as f:
            yaml_data: dict[str, Any] = yaml.safe_load(f) or {}
        # Deep-merge top-level keys; sub-dicts are merged individually
        for key, value in yaml_data.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value

    # Layer 3: apply env var overrides (highest priority)
    env = EnvSettings()
    if env.port is not None:
        merged["gateway"]["port"] = env.port
    if env.host is not None:
        merged["gateway"]["host"] = env.host
    if env.browser_port is not None:
        merged["browser"]["control_port"] = env.browser_port
    if env.log_level is not None:
        merged["log_level"] = env.log_level

    return MiniAgentConfig.model_validate(merged)


def get_config() -> MiniAgentConfig:
    """Singleton accessor — called throughout the app without passing config around."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def clear_config_cache() -> None:
    """For tests — reset singleton so each test gets a fresh config."""
    global _config
    _config = None
