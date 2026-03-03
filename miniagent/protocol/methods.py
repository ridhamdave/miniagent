from typing import Optional

from pydantic import BaseModel


class AgentParams(BaseModel):
    """
    Params for the "agent" RPC.
    OpenClaw: AgentParamsSchema in src/gateway/protocol/schema/agent.ts
    """

    message: str
    session_key: Optional[str] = "default"
    idempotency_key: str  # Client-generated UUID; prevents double-execution on retry
    thinking: Optional[str] = None  # "low" | "high" | None


class ChatHistoryParams(BaseModel):
    session_key: str
    limit: Optional[int] = 50


class ChatAbortParams(BaseModel):
    session_key: str
    run_id: Optional[str] = None  # None = abort the most recent active run


class BrowserNavigateParams(BaseModel):
    url: str


class BrowserClickParams(BaseModel):
    ref: str  # Aria-based element reference (Playwright locator)
    double_click: bool = False


class BrowserTypeParams(BaseModel):
    ref: str
    text: str


class BrowserGetTextParams(BaseModel):
    ref: Optional[str] = None  # None = entire page
