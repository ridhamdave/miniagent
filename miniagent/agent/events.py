"""
AgentEventEmitter — in-process pub/sub for agent streaming events.

OpenClaw equivalent: onAgentEvent() / emitAgentEvent() in src/infra/agent-events.ts

Why in-process pub/sub instead of calling broadcaster directly:
- AgentPipeline has no knowledge of WebSocket or the gateway
- The gateway registers a listener that calls broadcaster.broadcast()
- In tests, we register a listener that collects events into a list
- Multiple listeners can react to the same event (e.g., store + broadcast)
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class AgentEventPayload:
    """
    In-process event object. Direct mirror of AgentEventPayload in
    src/infra/agent-events.ts
    """

    run_id: str
    seq: int
    stream: str          # "lifecycle" | "assistant" | "tool" | "error"
    ts: int              # Unix ms
    data: dict[str, Any]
    session_key: str | None = None


Listener = Callable[[AgentEventPayload], Awaitable[None]]


class AgentEventEmitter:
    """
    In-process pub/sub for agent streaming events.
    OpenClaw: onAgentEvent() / emitAgentEvent() in src/infra/agent-events.ts

    Supports:
    - subscribe(callback) -> sub_id  (add a listener, returns subscription ID)
    - unsubscribe(sub_id)            (remove a listener by ID)
    - emit(run_id, stream, data)     (broadcast to all listeners concurrently)

    Thread-safe with respect to subscribe/unsubscribe during emit: the emit
    method takes a snapshot of listeners before gathering, so concurrent
    mutations do not affect the current emit call.
    """

    def __init__(self) -> None:
        self._listeners: dict[str, Listener] = {}  # sub_id -> callback
        self._seq: dict[str, int] = {}              # run_id -> current seq

    def subscribe(self, callback: Callable[[AgentEventPayload], Awaitable[None]]) -> str:
        """Register a callback. Returns a subscription ID for later unsubscribe."""
        sub_id = str(uuid.uuid4())
        self._listeners[sub_id] = callback
        return sub_id

    def on(self, listener: Listener) -> None:
        """Subscribe without returning an ID. All subscribers receive every event."""
        self.subscribe(listener)

    def unsubscribe(self, sub_id: str) -> None:
        """Remove a callback by subscription ID. No-op if not found."""
        self._listeners.pop(sub_id, None)

    async def emit(
        self,
        run_id: str,
        stream: str,
        data: dict[str, Any],
        session_key: str | None = None,
    ) -> None:
        """
        Emit event to all listeners concurrently.
        Takes a snapshot of current listeners so subscribe/unsubscribe during emit
        does not corrupt iteration.
        """
        self._seq[run_id] = self._seq.get(run_id, 0) + 1
        evt = AgentEventPayload(
            run_id=run_id,
            seq=self._seq[run_id],
            stream=stream,
            ts=int(time.time() * 1000),
            data=data,
            session_key=session_key,
        )
        # Snapshot to be thread-safe with concurrent subscribe/unsubscribe
        listeners = list(self._listeners.values())
        await asyncio.gather(*[listener(evt) for listener in listeners], return_exceptions=True)
