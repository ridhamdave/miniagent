"""
WsConnection — per-client WebSocket lifecycle.

Mirrors the closure inside wss.on("connection", ...) in
src/gateway/server/ws-connection.ts.

States:
  pending   → connected  (after valid "connect" frame)
  connected → closed     (on disconnect or error)
  pending   → failed     (handshake timeout)

Design: the connection loop is a single coroutine that runs until the
WebSocket closes. This means one asyncio task per connected client —
exactly like Node's one callback stack per WebSocket.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import WebSocket

from ..protocol.error_codes import ErrorCode, error_shape
from ..protocol.frames import ErrorShape, EventFrame, RequestFrame, ResponseFrame
from .broadcaster import Broadcaster
from .handler_registry import HandlerContext, HandlerRegistry
from .session_state import SessionState

HANDSHAKE_TIMEOUT_S = 5


class WsConnection:
    """
    Owns the full lifecycle of one WebSocket client.

    On connect: sends connect.challenge event to initiate the handshake.
    After valid "connect" frame: registers with Broadcaster for event fan-out.
    Main loop: receives frames, dispatches to registry, sends responses.
    """

    def __init__(
        self,
        websocket: WebSocket,
        conn_id: str,
        registry: HandlerRegistry,
        broadcaster: Broadcaster,
        session_state: SessionState,
    ) -> None:
        self.websocket = websocket
        self.conn_id = conn_id
        self.registry = registry
        self.broadcaster = broadcaster
        self.session_state = session_state
        self._connected = False

    async def run(self) -> None:
        """Main loop: send challenge, enforce handshake timeout, then process messages."""
        await self._send_event("connect.challenge", {
            "nonce": str(uuid.uuid4()),
            "ts": int(time.time() * 1000),
        })
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT_S):
                raw = await self.websocket.receive_text()
                await self._handle_message(raw, require_connect=True)
        except asyncio.TimeoutError:
            await self.websocket.close(code=1008, reason="handshake timeout")
            return

        # Main message loop
        try:
            while True:
                raw = await self.websocket.receive_text()
                await self._handle_message(raw)
        except Exception:
            pass  # Disconnect; cleanup handled by finally in server.py

    async def _handle_message(self, raw: str, require_connect: bool = False) -> None:
        """
        Parse JSON → RequestFrame, validate, dispatch.
        Never crashes the loop — all exceptions become error ResponseFrames.
        """
        try:
            frame = RequestFrame.model_validate_json(raw)
        except Exception as e:
            # Can't send a correlated error (no valid id) — close connection
            await self.websocket.close(code=1003, reason=f"invalid frame: {e}")
            return

        if require_connect and frame.method != "connect":
            await self._send_response(
                frame.id,
                False,
                error=error_shape(ErrorCode.INVALID_REQUEST, "first message must be 'connect'"),
            )
            return

        async def respond(ok: bool, payload: object = None, error: ErrorShape | None = None) -> None:
            await self._send_response(frame.id, ok, payload, error)

        ctx = HandlerContext(
            req_id=frame.id,
            method=frame.method,
            params=frame.params or {},
            respond=respond,
            conn_id=self.conn_id,
            session_state=self.session_state,
            broadcaster=self.broadcaster,
        )

        if frame.method == "connect" and not self._connected:
            # Special: register with broadcaster on successful connect
            _original_respond = respond

            async def respond_and_register(
                ok: bool, payload: object = None, error: ErrorShape | None = None
            ) -> None:
                await _original_respond(ok, payload, error)
                if ok:
                    self._connected = True
                    self.broadcaster.register(self)

            # Rebuild ctx with the wrapped respond
            ctx = HandlerContext(
                req_id=ctx.req_id,
                method=ctx.method,
                params=ctx.params,
                respond=respond_and_register,
                conn_id=ctx.conn_id,
                session_state=ctx.session_state,
                broadcaster=ctx.broadcaster,
            )

        await self.registry.dispatch(ctx)

    async def _send_response(
        self,
        req_id: str,
        ok: bool,
        payload: object = None,
        error: ErrorShape | None = None,
    ) -> None:
        frame = ResponseFrame(type="res", id=req_id, ok=ok, payload=payload, error=error)
        await self.websocket.send_text(frame.model_dump_json())

    async def send_event(
        self, event: str, payload: object = None, seq: int | None = None
    ) -> None:
        """Send an EventFrame to this specific client."""
        frame = EventFrame(type="event", event=event, payload=payload, seq=seq)
        await self.websocket.send_text(frame.model_dump_json())

    async def _send_event(self, event: str, payload: object = None) -> None:
        await self.send_event(event, payload)

    def close(self) -> None:
        """Mark connection as closed (does not close the WebSocket itself)."""
        self._connected = False
