"""
agent/ — AgentPipeline (streaming + tool loop), tools, events.

Public API:
  AgentPipeline  — core Anthropic streaming + recursive tool call loop
  AgentEventEmitter — in-process pub/sub for streaming events
"""

from .events import AgentEventEmitter, AgentEventPayload, Listener
from .pipeline import AgentPipeline
from .tools import BrowserClient, execute_tool, get_tool_definitions

__all__ = [
    "AgentPipeline",
    "AgentEventEmitter",
    "AgentEventPayload",
    "Listener",
    "BrowserClient",
    "execute_tool",
    "get_tool_definitions",
]
