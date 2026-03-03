from typing import Optional

from pydantic import BaseModel, Field


class GatewayConfig(BaseModel):
    """Mirrors OpenClaw's gateway.* config block (src/config/types.gateway.ts)."""

    port: int = 18789
    host: str = "127.0.0.1"  # loopback only by default
    auth_token: Optional[str] = None  # None = no auth (dev mode)


class BrowserConfig(BaseModel):
    """Mirrors OpenClaw's browser.* config block."""

    enabled: bool = True
    control_port: int = 18790  # Separate HTTP server, NEVER the gateway port
    headless: bool = False
    timeout_ms: int = 8000


class AgentConfig(BaseModel):
    """Mirrors OpenClaw's agents.defaults.* config block."""

    model: str = "claude-opus-4-6"
    max_tokens: int = 8096
    system_prompt: str = "You are a helpful AI assistant with browser control capabilities."
    thinking: Optional[str] = None  # "low" | "high" | None


class MiniAgentConfig(BaseModel):
    """Root config. Layer priority: env vars > config.yaml > these Pydantic defaults."""

    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sessions_dir: str = "~/.miniagent/sessions"
    log_level: str = "info"
