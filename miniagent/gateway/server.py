"""
Gateway server — FastAPI app factory.

create_gateway_app() wires all components together and returns the configured
FastAPI app.

OpenClaw equivalent: server.ts / server-startup.ts which calls
attachGatewayWsConnectionHandler() with a buildRequestContext() factory.

Component wiring order:
  1. Create shared state objects (broadcaster, session_state, emitter, store)
  2. Wire emitter → broadcaster (all agent events become WebSocket broadcasts)
  3. Register all RPC handlers in the registry
  4. Mount WebSocket endpoint (one WsConnection per client)
  5. Mount GET / to serve the single-file UI
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

from ..agent.events import AgentEventEmitter, AgentEventPayload
from ..config.loader import get_config
from ..config.types import MiniAgentConfig
from ..protocol.frames import HelloFeatures, HelloOk
from ..sessions.store import SessionStore
from .broadcaster import Broadcaster
from .connection import WsConnection
from .handler_registry import HandlerContext, HandlerFn, HandlerRegistry
from .handlers.agent import make_agent_handler
from .handlers.browser import make_browser_handlers
from .handlers.chat import make_chat_handlers
from .session_state import SessionState

_SERVER_VERSION = "0.1.0"


def _make_connect_handler(broadcaster: Broadcaster, cfg: MiniAgentConfig, registry: HandlerRegistry) -> HandlerFn:
    """
    "connect" handler — responds with HelloOk listing all registered methods.

    Note: the WsConnection._handle_message() wraps this handler's respond()
    so that on ok=True, the connection is registered with the broadcaster.
    This factory is called before full registry population, so we pass registry
    by reference and read list_methods() at call time (not factory time).
    """

    async def handler(ctx: HandlerContext) -> None:
        hello = HelloOk(
            type="hello-ok",
            server_version=_SERVER_VERSION,
            conn_id=ctx.conn_id,
            features=HelloFeatures(
                methods=registry.list_methods(),
                events=["agent-event", "tick", "connect.challenge"],
            ),
        )
        await ctx.respond(True, hello.model_dump())

    return handler


async def _tick_loop(broadcaster: Broadcaster) -> None:
    """30-second keep-alive. OpenClaw: TICK_INTERVAL_MS = 30_000 in server-constants.ts."""
    while True:
        await asyncio.sleep(30)
        await broadcaster.broadcast("tick", {"ts": int(time.time() * 1000)})


def create_gateway_app(browser_base_url: str = "http://127.0.0.1:18790") -> FastAPI:
    """
    Wires all components together and returns the configured FastAPI app.

    Parameters
    ----------
    browser_base_url:
        Base URL for the browser HTTP server. Injected to allow testing
        without actually starting a browser server.
    """
    cfg = get_config()
    broadcaster = Broadcaster()
    session_state = SessionState()
    emitter = AgentEventEmitter()
    session_store = SessionStore(cfg.sessions_dir)

    # Wire: every agent event becomes a WebSocket broadcast to all clients
    async def _on_agent_event(evt: AgentEventPayload) -> None:
        await broadcaster.broadcast(
            "agent-event",
            {
                "run_id": evt.run_id,
                "seq": evt.seq,
                "stream": evt.stream,
                "ts": evt.ts,
                "data": evt.data,
                "session_key": evt.session_key,
            },
        )

    emitter.on(_on_agent_event)

    # Register all RPC handlers
    registry = HandlerRegistry()

    # Register connect handler (it reads registry.list_methods() at call time)
    # We register connect first, then the rest. The connect handler will include
    # all methods that are registered by the time the first client connects.
    connect_handler = _make_connect_handler(broadcaster, cfg, registry)
    registry.register("connect", connect_handler)
    registry.register("agent", make_agent_handler(emitter, session_store, session_state))
    registry.register_many(make_chat_handlers(session_store, session_state))
    registry.register_many(make_browser_handlers(cfg, browser_base_url=browser_base_url))

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[misc]
        tick_task = asyncio.create_task(_tick_loop(broadcaster))
        yield
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            pass

    app = FastAPI(
        title="miniagent gateway",
        version=_SERVER_VERSION,
        lifespan=lifespan,
    )

    @app.get("/", response_class=HTMLResponse)
    async def serve_ui() -> HTMLResponse:
        """Serve the single-file Web UI."""
        ui_path = Path(__file__).parent.parent / "ui" / "index.html"
        return HTMLResponse(ui_path.read_text())

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """One coroutine per connected client — creates WsConnection which owns the lifecycle."""
        await websocket.accept()
        conn_id = str(uuid.uuid4())
        conn = WsConnection(websocket, conn_id, registry, broadcaster, session_state)
        try:
            await conn.run()
        finally:
            broadcaster.unregister(conn)

    return app
