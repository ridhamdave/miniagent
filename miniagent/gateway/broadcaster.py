"""
Broadcaster — fan-out events to all connected WebSocket clients.

OpenClaw: createGatewayBroadcaster() in src/gateway/server-broadcast.ts

The global _seq counter is the key detail:
- Incremented for every broadcast
- Embedded in every EventFrame
- A reconnecting client can compare its last-seen seq to the server's current seq
  to detect whether it missed events (and re-fetch if needed)

Slow consumer protection: if a client's send queue is backed up, asyncio.gather
surfaces the error and we unregister the slow client (prevents one slow client
from blocking others). OpenClaw checks WebSocket bufferedAmount > MAX_BUFFERED_BYTES.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from .connection import WsConnection


class Broadcaster:
    """
    Fan-out events to all connected and authenticated clients.

    Maintains a global sequence counter embedded in every EventFrame,
    allowing reconnecting clients to detect missed events.
    """

    def __init__(self) -> None:
        self._clients: set[WsConnection] = set()
        self._seq: int = 0
        # Callback-based subscription (used by tests / non-WsConnection callers)
        self._callbacks: dict[str, Callable[[dict], Awaitable[None]]] = {}

    def register(self, conn: WsConnection) -> None:
        """Called after successful handshake. Now eligible for broadcasts."""
        self._clients.add(conn)

    def unregister(self, conn: WsConnection) -> None:
        """Called on disconnect or error."""
        self._clients.discard(conn)

    def subscribe(self, client_id: str, callback: Callable[[dict], Awaitable[None]]) -> None:
        """Subscribe a callback (for non-WsConnection use, e.g. tests)."""
        self._callbacks[client_id] = callback

    def unsubscribe_callback(self, client_id: str) -> None:
        """Remove a callback subscription by client_id."""
        self._callbacks.pop(client_id, None)

    async def broadcast(self, event: str, payload: dict | None = None) -> None:
        """
        Send EventFrame to ALL registered clients and callbacks.
        Increments global seq counter.
        """
        self._seq += 1
        seq = self._seq

        # Fan-out to WsConnection clients
        # Use a snapshot list to avoid modification during iteration
        clients_snapshot = list(self._clients)
        if clients_snapshot:
            results = await asyncio.gather(
                *[c.send_event(event, payload, seq) for c in clients_snapshot],
                return_exceptions=True,
            )
            for conn, result in zip(clients_snapshot, results):
                if isinstance(result, Exception):
                    self._clients.discard(conn)

        # Fan-out to callback subscribers (used in tests)
        if self._callbacks:
            event_dict = {"event": event, "payload": payload, "seq": seq}
            await asyncio.gather(
                *[cb(event_dict) for cb in list(self._callbacks.values())],
                return_exceptions=True,
            )

    async def broadcast_to(
        self, conn_ids: set[str], event: str, payload: dict | None = None
    ) -> None:
        """Targeted broadcast to specific connection IDs (for per-client tool events)."""
        targets = [c for c in self._clients if c.conn_id in conn_ids]
        await asyncio.gather(
            *[c.send_event(event, payload) for c in targets],
            return_exceptions=True,
        )
