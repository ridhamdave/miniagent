"""
HandlerRegistry — maps method name strings to async handler functions.

OpenClaw: coreGatewayHandlers in server-methods.ts, built by dict-spreading
sub-module handler dicts:
  { ...agentHandlers, ...chatHandlers, ...browserHandlers }

miniagent uses register_many() for the same effect.
Handlers are pure functions (stateless); all mutable state lives in SessionState.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..protocol.error_codes import ErrorCode, error_shape


@dataclass
class HandlerContext:
    """
    Everything a handler needs. Single argument to every handler function.

    OpenClaw: GatewayRequestHandlerOptions in src/gateway/server-methods/types.ts

    The `respond` callback (not a return value) enables the double-response pattern:
    a handler can call respond() once for an immediate ack, launch a background task,
    and call respond() again later with the final result — all from the same handler
    registration, without any special framework support.
    """

    req_id: str
    method: str
    params: dict[str, Any]
    respond: Callable[[bool, Any, Any], Awaitable[None]]  # (ok, payload, error)
    conn_id: str
    session_state: "SessionState"  # type: ignore[name-defined]
    broadcaster: "Broadcaster"  # type: ignore[name-defined]


HandlerFn = Callable[[HandlerContext], Awaitable[None]]


class HandlerRegistry:
    """
    Stateless method name → async handler function map.

    OpenClaw: coreGatewayHandlers in server-methods.ts, built by dict-spreading
    sub-module handler dicts.

    miniagent uses register_many() for the same effect.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, HandlerFn] = {}

    def register(self, method: str, handler: HandlerFn) -> None:
        """Register a single handler for a method name."""
        self._handlers[method] = handler

    def get(self, method: str) -> HandlerFn | None:
        """Return the handler for a method, or None if not registered."""
        return self._handlers.get(method)

    def register_many(self, handlers: dict[str, HandlerFn]) -> None:
        """Batch register — mirrors the {...spread} pattern in OpenClaw."""
        self._handlers.update(handlers)

    async def dispatch(self, ctx: HandlerContext) -> None:
        """Dispatch to handler; unknown method → error response."""
        handler = self._handlers.get(ctx.method)
        if handler is None:
            await ctx.respond(
                False,
                None,
                error_shape(ErrorCode.INVALID_REQUEST, f"unknown method: {ctx.method}"),
            )
            return
        try:
            await handler(ctx)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    def list_methods(self) -> list[str]:
        """Sent in HelloOk.features.methods so clients know what's available."""
        return list(self._handlers.keys())
