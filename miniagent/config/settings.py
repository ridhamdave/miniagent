from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    """
    Highest-priority layer: environment variables.
    Mirrors OpenClaw reading ANTHROPIC_API_KEY etc. from process.env.

    All env vars use the MINIAGENT_ prefix. The .env file is loaded automatically
    if it exists alongside the process working directory.
    """

    model_config = SettingsConfigDict(env_prefix="MINIAGENT_", env_file=".env")

    anthropic_api_key: Optional[str] = None  # Optional so tests don't require a real key
    port: Optional[int] = None
    host: Optional[str] = None
    browser_port: Optional[int] = None
    log_level: Optional[str] = None
