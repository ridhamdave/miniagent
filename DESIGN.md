# miniagent — System Design

> A simplified personal AI gateway that mirrors OpenClaw's core architecture.
> Single provider (Anthropic Claude), single channel (Web UI), browser control via Playwright.
> Language: Python (FastAPI, asyncio, Pydantic).

---

## Mental Model

Everything flows through a **local WebSocket gateway**.

The gateway is a WebSocket + HTTP server running on your machine (`localhost:18789`). Every client — the browser UI today, anything else tomorrow — connects here via the same typed message protocol. The AI agent runs as a background `asyncio.Task` spawned by the gateway, streams tokens back through an in-process event bus, which the gateway fans out to all connected WebSocket clients in real time.

```
Browser UI  ──WS──►  Gateway :18789  ──asyncio.Task──►  Claude API
                          │                                   │
                          │◄──── streaming tokens ────────────┘
                          │
                          └──HTTP──►  Browser Server :18790  ──►  Playwright Chromium
```

The browser is a **separate HTTP server** on its own port. The agent calls it as a tool via HTTP. This means the browser can crash and restart without taking down the gateway — the same isolation OpenClaw enforces.

---

## Project Structure

```
miniagent/
├── pyproject.toml               # Project metadata + dependencies
├── .env.example                 # Template: ANTHROPIC_API_KEY, MINIAGENT_PORT, etc.
├── config.yaml                  # User-editable config (safe defaults committed)
│
├── miniagent/
│   ├── __init__.py
│   ├── __main__.py              # Entry point: `python -m miniagent` or `miniagent start`
│   │
│   ├── config/
│   │   ├── __init__.py
│   │   ├── types.py             # Pydantic models: GatewayConfig, BrowserConfig, AgentConfig, MiniAgentConfig
│   │   ├── settings.py          # Pydantic BaseSettings: reads env vars (highest priority layer)
│   │   └── loader.py            # load_config(), get_config() singleton, clear_config_cache()
│   │
│   ├── protocol/
│   │   ├── __init__.py
│   │   ├── frames.py            # RequestFrame, ResponseFrame, EventFrame, HelloOk, ErrorShape
│   │   ├── methods.py           # Per-method param models: AgentParams, ChatHistoryParams, etc.
│   │   └── error_codes.py       # ErrorCode enum + error_shape() factory
│   │
│   ├── gateway/
│   │   ├── __init__.py
│   │   ├── server.py            # FastAPI app factory, /ws endpoint, GET / → index.html, lifespan
│   │   ├── connection.py        # WsConnection: per-client lifecycle (pending→connected→closed)
│   │   ├── broadcaster.py       # Broadcaster: fan-out events to all connected clients
│   │   ├── handler_registry.py  # HandlerRegistry: method name → async handler fn map
│   │   ├── session_state.py     # SessionState: active runs, dedupe map, run seq counters
│   │   └── handlers/
│   │       ├── __init__.py
│   │       ├── agent.py         # "agent" RPC: double-response + fire-and-forget asyncio.Task
│   │       ├── chat.py          # "chat.history", "chat.abort" handlers
│   │       └── browser.py       # "browser.*" RPC handlers (proxy to browser HTTP server)
│   │
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── pipeline.py          # AgentPipeline: Anthropic streaming + recursive tool call loop
│   │   ├── tools.py             # Tool definitions for Claude + BrowserClient (aiohttp HTTP)
│   │   └── events.py            # AgentEventEmitter: in-process pub/sub for streaming events
│   │
│   ├── browser/
│   │   ├── __init__.py
│   │   ├── server.py            # Standalone FastAPI app on BROWSER_PORT (default 18790)
│   │   ├── context.py           # BrowserContext: single Playwright Chromium instance lifecycle
│   │   └── routes.py            # HTTP routes: /navigate /screenshot /click /type /text /scroll
│   │
│   ├── sessions/
│   │   ├── __init__.py
│   │   └── store.py             # SessionStore: JSONL append/load per session key
│   │
│   └── ui/
│       └── index.html           # Single-file Web UI (zero build step, served at GET /)
│
└── tests/
    ├── test_protocol.py         # Round-trip Pydantic frame parse/serialize
    ├── test_gateway.py          # WS TestClient: connect + agent RPC shapes
    ├── test_agent_pipeline.py   # Mock Anthropic SDK, assert events emitted in order
    └── test_browser.py          # Mock BrowserContext, assert HTTP route shapes
```

**OpenClaw parallel:** OpenClaw's `src/` mirrors this structure almost exactly — `gateway/`, `channels/` (our `ui/`), `agents/` (our `agent/`), `browser/`, `config/`, with protocol types in `src/gateway/protocol/schema/`.

---

## 1. Config System

### Types (`miniagent/config/types.py`)

```python
from pydantic import BaseModel, Field
from typing import Optional


class GatewayConfig(BaseModel):
    """Mirrors OpenClaw's gateway.* config block (src/config/types.gateway.ts)."""
    port: int = 18789
    host: str = "127.0.0.1"          # loopback only by default
    auth_token: Optional[str] = None  # None = no auth (dev mode)


class BrowserConfig(BaseModel):
    """Mirrors OpenClaw's browser.* config block."""
    enabled: bool = True
    control_port: int = 18790         # Separate HTTP server, NEVER the gateway port
    headless: bool = False
    timeout_ms: int = 8000


class AgentConfig(BaseModel):
    """Mirrors OpenClaw's agents.defaults.* config block."""
    model: str = "claude-opus-4-6"
    max_tokens: int = 8096
    system_prompt: str = "You are a helpful AI assistant with browser control capabilities."
    thinking: Optional[str] = None    # "low" | "high" | None


class MiniAgentConfig(BaseModel):
    """Root config. Layer priority: env vars > config.yaml > these Pydantic defaults."""
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sessions_dir: str = "~/.miniagent/sessions"
    log_level: str = "info"
```

### Environment Layer (`miniagent/config/settings.py`)

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class EnvSettings(BaseSettings):
    """
    Highest-priority layer: environment variables.
    Mirrors OpenClaw reading ANTHROPIC_API_KEY etc. from process.env.
    """
    model_config = SettingsConfigDict(env_prefix="MINIAGENT_", env_file=".env")

    anthropic_api_key: str            # Required; no default — startup fails without it
    port: Optional[int] = None
    host: Optional[str] = None
    browser_port: Optional[int] = None
    log_level: Optional[str] = None
```

### Config Loader (`miniagent/config/loader.py`)

```python
import yaml
from pathlib import Path
from .types import MiniAgentConfig
from .settings import EnvSettings

_config: MiniAgentConfig | None = None


def load_config(config_path: str = "config.yaml") -> MiniAgentConfig:
    """
    Layer order (same as OpenClaw's loadConfig() in src/config/io.ts):
      1. Pydantic defaults (MiniAgentConfig field defaults)
      2. config.yaml values  (if file exists)
      3. Environment variables (highest priority, always wins)

    Returns a frozen MiniAgentConfig instance.
    """
    ...


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
```

### config.yaml (committed with safe defaults)

```yaml
gateway:
  port: 18789
  host: "127.0.0.1"

browser:
  enabled: true
  control_port: 18790
  headless: false
  timeout_ms: 8000

agent:
  model: "claude-opus-4-6"
  max_tokens: 8096
  system_prompt: "You are a helpful assistant with browser control."

sessions_dir: "~/.miniagent/sessions"
log_level: "info"
```

### .env.example (never committed with real values)

```ini
ANTHROPIC_API_KEY=sk-ant-...
# MINIAGENT_PORT=18789
# MINIAGENT_HOST=127.0.0.1
# MINIAGENT_BROWSER_PORT=18790
```

---

## 2. Gateway Protocol

Typed message frames — every byte over the WebSocket is one of these.

### Frame Types (`miniagent/protocol/frames.py`)

```python
from pydantic import BaseModel
from typing import Literal, Any, Optional


class RequestFrame(BaseModel):
    """
    Client → Server.
    Direct mirror of OpenClaw's RequestFrameSchema (src/gateway/protocol/schema/frames.ts).

    The `id` field is client-generated and must be echoed back in the ResponseFrame.
    This is the correlation ID for request/response pairing — the same id can receive
    multiple ResponseFrames (see the double-response pattern for "agent").
    """
    type: Literal["req"]
    id: str        # Client-generated UUID; echoed in ResponseFrame
    method: str    # RPC method name, e.g. "agent", "chat.history"
    params: Optional[Any] = None


class ResponseFrame(BaseModel):
    """
    Server → Client (one specific client, not broadcast).
    Matches the id from the RequestFrame it answers.

    The "agent" method sends TWO ResponseFrames with the same id:
      1st: {ok:true, payload:{run_id, status:"accepted"}}  — immediate ack
      2nd: {ok:true, payload:{run_id, status:"ok"}}        — when pipeline finishes
    This double-response lets the UI show "running..." immediately while the
    full result arrives later without blocking the handler coroutine.
    (OpenClaw source: src/gateway/server-methods/agent.ts lines 599 + 654)
    """
    type: Literal["res"]
    id: str
    ok: bool
    payload: Optional[Any] = None
    error: Optional["ErrorShape"] = None


class EventFrame(BaseModel):
    """
    Server → ALL connected clients (broadcast).
    NOT correlated to any specific RequestFrame id.

    The `seq` field is a global monotonic counter incremented for every broadcast.
    A client that reconnects and sees seq=50 when it last saw seq=47 knows it
    missed 3 events. (OpenClaw: server-broadcast.ts line 66)
    """
    type: Literal["event"]
    event: str              # e.g. "agent-event", "tick", "connect.challenge"
    payload: Optional[Any] = None
    seq: Optional[int] = None


class HelloFeatures(BaseModel):
    methods: list[str]      # All registered RPC method names
    events: list[str]       # All event names the server can emit


class HelloOk(BaseModel):
    """
    Server → Client, sent as the ResponseFrame payload after a valid "connect" request.
    Tells the client what the server supports — so clients can adapt gracefully
    when the server is an older version.
    (OpenClaw: HelloOkSchema in src/gateway/protocol/schema/frames.ts)
    """
    type: Literal["hello-ok"]
    server_version: str
    conn_id: str
    features: HelloFeatures


class ErrorShape(BaseModel):
    """
    All errors over the wire use this shape — never raw Python tracebacks.
    (OpenClaw: errorShape() in src/gateway/protocol/schema/error-codes.ts)
    """
    code: str               # e.g. "invalid_request", "internal_error", "aborted"
    message: str
    details: Optional[Any] = None
    retryable: Optional[bool] = None
```

### Method Param Models (`miniagent/protocol/methods.py`)

```python
from pydantic import BaseModel
from typing import Optional


class AgentParams(BaseModel):
    """
    Params for the "agent" RPC.
    OpenClaw: AgentParamsSchema in src/gateway/protocol/schema/agent.ts
    """
    message: str
    session_key: Optional[str] = "default"
    idempotency_key: str        # Client-generated UUID; prevents double-execution on retry
    thinking: Optional[str] = None   # "low" | "high" | None


class ChatHistoryParams(BaseModel):
    session_key: str
    limit: Optional[int] = 50


class ChatAbortParams(BaseModel):
    session_key: str
    run_id: Optional[str] = None   # None = abort the most recent active run


class BrowserNavigateParams(BaseModel):
    url: str


class BrowserClickParams(BaseModel):
    ref: str              # Aria-based element reference (Playwright locator)
    double_click: bool = False


class BrowserTypeParams(BaseModel):
    ref: str
    text: str


class BrowserGetTextParams(BaseModel):
    ref: Optional[str] = None   # None = entire page
```

### Error Codes (`miniagent/protocol/error_codes.py`)

```python
from enum import StrEnum
from .frames import ErrorShape


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE     = "unavailable"
    INTERNAL        = "internal_error"
    ABORTED         = "aborted"


def error_shape(code: ErrorCode, message: str, details=None, retryable=False) -> ErrorShape:
    """Factory ensuring all errors have consistent shape."""
    return ErrorShape(code=code, message=message, details=details, retryable=retryable)
```

### RPC Methods Reference

| Method | Params | Response payload | Notes |
|---|---|---|---|
| `connect` | `{client:{id,version}}` | `HelloOk` | Must be first message; 5s timeout |
| `agent` | `AgentParams` | `{run_id, status:"accepted"}` then `{run_id, status:"ok"}` | Double-response |
| `chat.history` | `ChatHistoryParams` | `{messages:[...]}` | Reads JSONL from disk |
| `chat.abort` | `ChatAbortParams` | `{ok:true}` | Cancels asyncio.Task |
| `browser.navigate` | `BrowserNavigateParams` | `{ok, url, title}` | Proxies to :18790 |
| `browser.screenshot` | `{}` | `{image_b64, mime_type}` | PNG as base64 |
| `browser.click` | `BrowserClickParams` | `{ok}` | |
| `browser.type` | `BrowserTypeParams` | `{ok}` | |
| `browser.get_text` | `BrowserGetTextParams` | `{text}` | |

### Events Reference

| Event | Trigger | Payload |
|---|---|---|
| `connect.challenge` | Immediately on WS open | `{nonce, ts}` |
| `agent-event` | Each streaming chunk from pipeline | `{run_id, seq, stream, ts, data}` |
| `tick` | Every 30s | `{ts}` |

**`agent-event` stream values:**

| `stream` | When | `data` shape |
|---|---|---|
| `"lifecycle"` | pipeline start/end/error | `{status:"started"\|"complete"\|"error", ...}` |
| `"assistant"` | text token from Claude | `{delta:"..."}` |
| `"tool"` | tool invocation | `{phase:"start"\|"result", tool_name, input\|result}` |

### Connection Handshake

```
Client opens ws://localhost:18789/ws
  ← Server sends EventFrame: {type:"event", event:"connect.challenge", payload:{nonce:"...", ts:1234}}
  → Client sends RequestFrame: {type:"req", id:"1", method:"connect", params:{client:{id:"web-ui", version:"1.0"}}}
  ← Server sends ResponseFrame: {type:"res", id:"1", ok:true, payload:<HelloOk>}
  [Connection is now live]

  If no "connect" frame arrives within 5 seconds:
  ← Server closes with code 1008 "handshake timeout"
```

OpenClaw source: `src/gateway/server/ws-connection.ts:175` (challenge send) and `:268` (handshake timer).

---

## 3. Gateway Server

### FastAPI App (`miniagent/gateway/server.py`)

```python
import asyncio, uuid, time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

from ..config.loader import get_config
from ..agent.events import AgentEventEmitter
from ..sessions.store import SessionStore
from .broadcaster import Broadcaster
from .handler_registry import HandlerRegistry
from .session_state import SessionState
from .connection import WsConnection
from .handlers.agent import make_agent_handler
from .handlers.chat import make_chat_handlers
from .handlers.browser import make_browser_handlers


def build_app() -> FastAPI:
    """
    Wires all components together and returns the configured FastAPI app.

    OpenClaw equivalent: server.ts / server-startup.ts which calls
    attachGatewayWsConnectionHandler() with a buildRequestContext() factory.

    Component wiring order:
      1. Create shared state objects (broadcaster, session_state, emitter, store)
      2. Wire emitter → broadcaster (all agent events become WebSocket broadcasts)
      3. Register all RPC handlers in the registry
      4. Mount WebSocket endpoint (one WsConnection per client)
      5. Mount GET / to serve the single-file UI
    """
    cfg = get_config()
    broadcaster    = Broadcaster()
    session_state  = SessionState()
    emitter        = AgentEventEmitter()
    session_store  = SessionStore(cfg.sessions_dir)

    # Wire: every agent event becomes a WebSocket broadcast to all clients
    async def _on_agent_event(evt):
        await broadcaster.broadcast("agent-event", {
            "run_id":      evt.run_id,
            "seq":         evt.seq,
            "stream":      evt.stream,
            "ts":          evt.ts,
            "data":        evt.data,
            "session_key": evt.session_key,
        })
    emitter.on(_on_agent_event)

    # Register all RPC handlers
    registry = HandlerRegistry()
    registry.register("connect", _make_connect_handler(broadcaster, cfg))
    registry.register("agent",   make_agent_handler(emitter, session_store, session_state))
    registry.register_many(make_chat_handlers(session_store, session_state))
    registry.register_many(make_browser_handlers(cfg))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tick_task = asyncio.create_task(_tick_loop(broadcaster))
        yield
        tick_task.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def serve_ui():
        """Serve the single-file Web UI. OpenClaw serves control UI from the same gateway HTTP."""
        ui_path = Path(__file__).parent.parent / "ui" / "index.html"
        return HTMLResponse(ui_path.read_text())

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """One coroutine per connected client — creates WsConnection which owns the lifecycle."""
        await websocket.accept()
        conn_id = str(uuid.uuid4())
        conn = WsConnection(websocket, conn_id, registry, broadcaster, session_state)
        try:
            await conn.run()
        finally:
            broadcaster.unregister(conn)

    return app


async def _tick_loop(broadcaster: Broadcaster) -> None:
    """30-second keep-alive. OpenClaw: TICK_INTERVAL_MS = 30_000 in server-constants.ts."""
    while True:
        await asyncio.sleep(30)
        await broadcaster.broadcast("tick", {"ts": int(time.time() * 1000)})
```

### WsConnection (`miniagent/gateway/connection.py`)

```python
import asyncio, json, uuid, time
from fastapi import WebSocket
from ..protocol.frames import RequestFrame, ResponseFrame, EventFrame, ErrorShape
from ..protocol.error_codes import ErrorCode, error_shape
from .handler_registry import HandlerRegistry, HandlerContext
from .broadcaster import Broadcaster
from .session_state import SessionState

HANDSHAKE_TIMEOUT_S = 5


class WsConnection:
    """
    Owns the full lifecycle of one WebSocket client.

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

    def __init__(
        self,
        websocket: WebSocket,
        conn_id: str,
        registry: HandlerRegistry,
        broadcaster: Broadcaster,
        session_state: SessionState,
    ):
        self.websocket    = websocket
        self.conn_id      = conn_id
        self.registry     = registry
        self.broadcaster  = broadcaster
        self.session_state = session_state
        self._connected   = False

    async def run(self) -> None:
        """Main loop: send challenge, enforce handshake timeout, then process messages."""
        await self._send_event("connect.challenge", {
            "nonce": str(uuid.uuid4()),
            "ts":    int(time.time() * 1000),
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
            await self._send_response(frame.id, False, error=error_shape(
                ErrorCode.INVALID_REQUEST, "first message must be 'connect'"
            ))
            return

        async def respond(ok: bool, payload=None, error: ErrorShape | None = None):
            await self._send_response(frame.id, ok, payload, error)

        ctx = HandlerContext(
            req_id        = frame.id,
            method        = frame.method,
            params        = frame.params or {},
            respond       = respond,
            conn_id       = self.conn_id,
            session_state = self.session_state,
            broadcaster   = self.broadcaster,
        )

        if frame.method == "connect" and not self._connected:
            # Special: register with broadcaster on successful connect
            _original_respond = respond
            async def respond_and_register(ok, payload=None, error=None):
                await _original_respond(ok, payload, error)
                if ok:
                    self._connected = True
                    self.broadcaster.register(self)
            ctx = HandlerContext(**{**ctx.__dict__, "respond": respond_and_register})

        await self.registry.dispatch(ctx)

    async def _send_response(self, req_id, ok, payload=None, error=None):
        frame = ResponseFrame(type="res", id=req_id, ok=ok, payload=payload, error=error)
        await self.websocket.send_text(frame.model_dump_json())

    async def send_event(self, event: str, payload=None, seq: int | None = None):
        frame = EventFrame(type="event", event=event, payload=payload, seq=seq)
        await self.websocket.send_text(frame.model_dump_json())

    async def _send_event(self, event: str, payload=None):
        await self.send_event(event, payload)
```

### HandlerRegistry (`miniagent/gateway/handler_registry.py`)

```python
from typing import Callable, Awaitable, Any
from dataclasses import dataclass
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
    req_id:        str
    method:        str
    params:        dict[str, Any]
    respond:       Callable[[bool, Any, Any], Awaitable[None]]  # (ok, payload, error)
    conn_id:       str
    session_state: "SessionState"
    broadcaster:   "Broadcaster"


HandlerFn = Callable[[HandlerContext], Awaitable[None]]


class HandlerRegistry:
    """
    Stateless method name → async handler function map.

    OpenClaw: coreGatewayHandlers in server-methods.ts, built by dict-spreading
    sub-module handler dicts:
      { ...agentHandlers, ...chatHandlers, ...browserHandlers }

    miniagent uses register_many() for the same effect.
    Handlers are pure functions (stateless); all mutable state lives in SessionState.
    """

    def __init__(self):
        self._handlers: dict[str, HandlerFn] = {}

    def register(self, method: str, handler: HandlerFn) -> None:
        self._handlers[method] = handler

    def register_many(self, handlers: dict[str, HandlerFn]) -> None:
        """Batch register — mirrors the {...spread} pattern in OpenClaw."""
        self._handlers.update(handlers)

    async def dispatch(self, ctx: HandlerContext) -> None:
        """Dispatch to handler; unknown method → error response."""
        handler = self._handlers.get(ctx.method)
        if handler is None:
            await ctx.respond(False, None, error_shape(
                ErrorCode.INVALID_REQUEST, f"unknown method: {ctx.method}"
            ))
            return
        try:
            await handler(ctx)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    def list_methods(self) -> list[str]:
        """Sent in HelloOk.features.methods so clients know what's available."""
        return list(self._handlers.keys())
```

### Broadcaster (`miniagent/gateway/broadcaster.py`)

```python
import asyncio
from .connection import WsConnection


class Broadcaster:
    """
    Fan-out events to all connected and authenticated clients.

    OpenClaw: createGatewayBroadcaster() in src/gateway/server-broadcast.ts

    The global `_seq` counter is the key detail:
    - Incremented for every broadcast
    - Embedded in every EventFrame
    - A reconnecting client can compare its last-seen seq to the server's current seq
      to detect whether it missed events (and re-fetch if needed)

    Slow consumer protection: if a client's send queue is backed up, asyncio.gather
    surfaces the error and we unregister the slow client (prevents one slow client
    from blocking others). OpenClaw checks WebSocket bufferedAmount > MAX_BUFFERED_BYTES.
    """

    def __init__(self):
        self._clients: set[WsConnection] = set()
        self._seq: int = 0

    def register(self, conn: WsConnection) -> None:
        """Called after successful handshake. Now eligible for broadcasts."""
        self._clients.add(conn)

    def unregister(self, conn: WsConnection) -> None:
        """Called on disconnect or error."""
        self._clients.discard(conn)

    async def broadcast(self, event: str, payload: dict | None = None) -> None:
        """Send EventFrame to ALL registered clients. Increments global seq."""
        self._seq += 1
        seq = self._seq
        dead: set[WsConnection] = set()
        results = await asyncio.gather(
            *[c.send_event(event, payload, seq) for c in self._clients],
            return_exceptions=True,
        )
        for conn, result in zip(list(self._clients), results):
            if isinstance(result, Exception):
                dead.add(conn)
        for conn in dead:
            self._clients.discard(conn)

    async def broadcast_to(self, conn_ids: set[str], event: str, payload: dict | None = None) -> None:
        """Targeted broadcast to specific connection IDs (for per-client tool events)."""
        targets = [c for c in self._clients if c.conn_id in conn_ids]
        await asyncio.gather(*[c.send_event(event, payload) for c in targets], return_exceptions=True)
```

### SessionState (`miniagent/gateway/session_state.py`)

```python
import asyncio, time
from dataclasses import dataclass, field


@dataclass
class ActiveRun:
    run_id:     str
    session_key: str
    task:       asyncio.Task
    started_at: float
    conn_id:    str      # Which client triggered this run (for targeted events)


class SessionState:
    """
    In-process mutable state for the gateway lifetime.

    OpenClaw spreads equivalent state across GatewayRequestContext in
    src/gateway/server-methods/types.ts:
      chatAbortControllers  → active_runs (asyncio.Task cancel)
      dedupe map            → dedupe
      agentRunSeq           → run_seq

    Centralizing in one class makes the Python version easier to reason about.
    """

    def __init__(self):
        self.active_runs: dict[str, ActiveRun] = {}
        self.dedupe:      dict[str, dict]       = {}   # idempotency_key → cached response
        self.run_seq:     dict[str, int]         = {}   # session_key → current seq

    def next_run_seq(self, session_key: str) -> int:
        """Monotonic sequence per session. OpenClaw: agentRunSeq map."""
        n = self.run_seq.get(session_key, 0) + 1
        self.run_seq[session_key] = n
        return n

    def register_run(self, run: ActiveRun) -> None:
        self.active_runs[run.run_id] = run

    def get_run(self, run_id: str) -> ActiveRun | None:
        return self.active_runs.get(run_id)

    def cancel_run(self, run_id: str) -> bool:
        run = self.active_runs.get(run_id)
        if run:
            run.task.cancel()
            return True
        return False

    def complete_run(self, run_id: str) -> None:
        self.active_runs.pop(run_id, None)
```

---

## 4. Agent Handler

### The Double-Response Pattern (`miniagent/gateway/handlers/agent.py`)

```python
import asyncio, uuid
from ..handler_registry import HandlerFn, HandlerContext
from ...protocol.methods import AgentParams
from ...protocol.error_codes import ErrorCode, error_shape
from ...agent.pipeline import AgentPipeline
from ...agent.events import AgentEventEmitter
from ...sessions.store import SessionStore
from ..session_state import SessionState, ActiveRun
import time


def make_agent_handler(
    emitter:       AgentEventEmitter,
    session_store: SessionStore,
    session_state: SessionState,
) -> HandlerFn:
    """
    Factory returns the "agent" RPC handler.

    This is the architectural heart of miniagent. OpenClaw equivalent:
    agentHandlers["agent"] in src/gateway/server-methods/agent.ts

    The double-response pattern:
      1. Validate params
      2. Check idempotency dedupe map → return cached response on hit
      3. Call respond(ok=True, {run_id, status:"accepted"})  ← FIRST response, immediate
      4. asyncio.create_task(run_pipeline(...))              ← fire-and-forget
         ↑ handler returns here; WebSocket is free for other messages
      5. [background] pipeline runs, emits events, calls respond again when done
      6. respond(ok=True, {run_id, status:"ok"})            ← SECOND response

    Why the callback instead of return value:
    - Step 3 and step 6 both send a ResponseFrame with the SAME req_id
    - A return-value approach can only send once
    - The callback approach lets background tasks call respond() at any time
    (OpenClaw uses the same respond() callback pattern)
    """

    async def handler(ctx: HandlerContext) -> None:
        try:
            params = AgentParams.model_validate(ctx.params)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INVALID_REQUEST, str(e)))
            return

        # Idempotency: if we've seen this key before, return the cached result
        idem_key = f"agent:{params.idempotency_key}"
        if cached := session_state.dedupe.get(idem_key):
            await ctx.respond(cached["ok"], cached.get("payload"), cached.get("error"))
            return

        run_id      = str(uuid.uuid4())
        session_key = params.session_key or "default"

        # FIRST response: immediate ack
        accepted = {"run_id": run_id, "status": "accepted"}
        session_state.dedupe[idem_key] = {"ok": True, "payload": accepted}
        await ctx.respond(True, accepted)

        # Background task: run the actual agent pipeline
        async def _run():
            pipeline = AgentPipeline(run_id, session_key, emitter, session_store, params)
            try:
                result_text = await pipeline.run(params.message)
                final = {"run_id": run_id, "status": "ok", "result": result_text}
                session_state.dedupe[idem_key] = {"ok": True, "payload": final}
                await ctx.respond(True, final)
            except asyncio.CancelledError:
                err = error_shape(ErrorCode.ABORTED, "run was aborted")
                session_state.dedupe[idem_key] = {"ok": False, "error": err.model_dump()}
                await ctx.respond(False, None, err)
            except Exception as e:
                err = error_shape(ErrorCode.INTERNAL, str(e))
                session_state.dedupe[idem_key] = {"ok": False, "error": err.model_dump()}
                await ctx.respond(False, None, err)
            finally:
                session_state.complete_run(run_id)

        task = asyncio.create_task(_run())
        session_state.register_run(ActiveRun(
            run_id=run_id, session_key=session_key,
            task=task, started_at=time.time(), conn_id=ctx.conn_id,
        ))

    return handler
```

---

## 5. Agent Execution Pipeline

### Pipeline (`miniagent/agent/pipeline.py`)

```python
import anthropic, asyncio, time
from dataclasses import dataclass
from ..config.loader import get_config
from ..protocol.methods import AgentParams
from .tools import get_tool_definitions, execute_tool
from .events import AgentEventEmitter
from ..sessions.store import SessionStore


class AgentPipeline:
    """
    Core agent execution loop.

    OpenClaw's equivalent is runEmbeddedPiAgent() in src/agents/pi-embedded.ts,
    which uses the @mariozechner/pi-coding-agent library. miniagent uses the
    Anthropic SDK directly to make every step of the loop explicit and learnable.

    The loop:
      run(message)
        ↓ load session history
        ↓ append user message to JSONL
        ↓ emit lifecycle:started
        ↓ _run_turn(messages)         ← recursive tool call loop
             ↓ stream from Claude
             ↓ text deltas → emit assistant events
             ↓ tool_use blocks → execute_tool() → emit tool events → recurse
             ↓ end_turn → return
        ↓ append assistant message to JSONL
        ↓ emit lifecycle:complete
        ↓ return final text
    """

    def __init__(
        self,
        run_id:        str,
        session_key:   str,
        emitter:       AgentEventEmitter,
        session_store: SessionStore,
        params:        AgentParams,
    ):
        self.run_id        = run_id
        self.session_key   = session_key
        self.emitter       = emitter
        self.session_store = session_store
        self.params        = params
        self.cfg           = get_config()
        self.client        = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
        self._seq          = 0

    async def run(self, message: str) -> str:
        """Entry point. Returns final assistant text."""
        messages = await self.session_store.load_messages(self.session_key)
        await self.session_store.append_message(self.session_key, "user", message, self.run_id)
        messages.append({"role": "user", "content": message})

        await self._emit("lifecycle", {"status": "started", "run_id": self.run_id})

        final_text, new_messages = await self._run_turn(messages)

        await self.session_store.append_message(self.session_key, "assistant", final_text, self.run_id)
        await self._emit("lifecycle", {
            "status": "complete",
            "run_id": self.run_id,
        })
        return final_text

    async def _run_turn(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """
        Single turn of the agentic loop.
        Returns (accumulated_text, new_messages_to_add).

        Recursion happens when stop_reason == "tool_use":
          append assistant message (with tool_use blocks) +
          user message (with tool_result blocks) → call _run_turn again
        This continues until stop_reason == "end_turn".

        OpenClaw uses the pi-coding-agent library's agentic loop.
        Making it explicit here so the loop is visible and debuggable.
        """
        accumulated_text = ""
        tool_uses:   list = []
        new_messages: list[dict] = []

        # --- Stream from Claude ---
        async with self.client.messages.stream(
            model       = self.cfg.agent.model,
            max_tokens  = self.cfg.agent.max_tokens,
            system      = self.cfg.agent.system_prompt,
            messages    = messages,
            tools       = get_tool_definitions(),
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        delta = event.delta.text
                        accumulated_text += delta
                        await self._emit("assistant", {"delta": delta})

            final_msg = await stream.get_final_message()
            stop_reason = final_msg.stop_reason

            # Collect tool_use blocks from final message content
            for block in final_msg.content:
                if block.type == "tool_use":
                    tool_uses.append(block)

        # --- end_turn: done ---
        if stop_reason == "end_turn":
            new_messages.append({"role": "assistant", "content": accumulated_text})
            return accumulated_text, new_messages

        # --- tool_use: execute tools, recurse ---
        if stop_reason == "tool_use":
            tool_results = await self._handle_tool_calls(tool_uses)

            new_messages.append({"role": "assistant", "content": final_msg.content})
            new_messages.append({"role": "user",      "content": tool_results})

            next_text, more = await self._run_turn(messages + new_messages)
            return accumulated_text + next_text, new_messages + more

        return accumulated_text, new_messages

    async def _handle_tool_calls(self, tool_uses: list) -> list[dict]:
        """
        Execute each tool_use, emit events, return list of tool_result content blocks.
        Runs tool calls sequentially (could be parallelized with gather if needed).
        """
        tool_results = []
        for block in tool_uses:
            tool_name  = block.name
            tool_input = block.input

            await self._emit("tool", {"phase": "start", "tool_name": tool_name, "input": tool_input})

            try:
                result = await execute_tool(tool_name, tool_input)
                await self._emit("tool", {"phase": "result", "tool_name": tool_name, "result": result})
            except Exception as e:
                result = {"error": str(e)}
                await self._emit("tool", {"phase": "result", "tool_name": tool_name, "result": result, "error": True})

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     str(result),
            })

        return tool_results

    async def _emit(self, stream: str, data: dict) -> None:
        """Emit one agent event. Auto-increments per-run seq."""
        await self.emitter.emit(self.run_id, stream, data, self.session_key)
```

### Event Emitter (`miniagent/agent/events.py`)

```python
import asyncio, time
from dataclasses import dataclass
from typing import Callable, Awaitable, Any


@dataclass
class AgentEventPayload:
    """
    In-process event object. Direct mirror of AgentEventPayload in
    src/infra/agent-events.ts

    Why in-process pub/sub instead of calling broadcaster directly:
    - AgentPipeline has no knowledge of WebSocket or the gateway
    - The gateway registers a listener that calls broadcaster.broadcast()
    - In tests, we register a listener that collects events into a list
    - Multiple listeners can react to the same event (e.g., store + broadcast)
    """
    run_id:      str
    seq:         int
    stream:      str          # "lifecycle" | "assistant" | "tool" | "error"
    ts:          int          # Unix ms
    data:        dict[str, Any]
    session_key: str | None = None


Listener = Callable[[AgentEventPayload], Awaitable[None]]


class AgentEventEmitter:
    """
    In-process pub/sub for agent streaming events.
    OpenClaw: onAgentEvent() / emitAgentEvent() in src/infra/agent-events.ts
    """

    def __init__(self):
        self._listeners: list[Listener] = []
        self._seq:       dict[str, int] = {}   # run_id → current seq

    def on(self, listener: Listener) -> None:
        """Subscribe. All subscribers receive every event from every run."""
        self._listeners.append(listener)

    async def emit(self, run_id: str, stream: str, data: dict, session_key: str | None = None) -> None:
        """Emit event to all listeners concurrently."""
        self._seq[run_id] = self._seq.get(run_id, 0) + 1
        evt = AgentEventPayload(
            run_id      = run_id,
            seq         = self._seq[run_id],
            stream      = stream,
            ts          = int(time.time() * 1000),
            data        = data,
            session_key = session_key,
        )
        await asyncio.gather(*[l(evt) for l in self._listeners], return_exceptions=True)
```

---

## 6. Session Storage

### JSONL Store (`miniagent/sessions/store.py`)

```python
import aiofiles, json, uuid
from datetime import datetime, timezone
from pathlib import Path


class SessionStore:
    """
    Append-only JSONL conversation history.

    File format — one JSON object per line:
      {"id":"msg_abc","role":"user","content":"Hello","created_at":"2026-03-02T12:00:00Z"}
      {"id":"msg_def","role":"assistant","content":"Hi!","created_at":"...","run_id":"run-1"}

    OpenClaw uses the Pi transcript format with parentId chain for context compaction.
    miniagent uses a simpler flat list (no compaction needed at this scale).

    JSONL chosen because:
    - Append-only → no file rewrite → crash-safe
    - Each line independently parseable → easy to tail/grep/stream
    - Same format OpenClaw uses for Pi transcripts (session-utils.fs.ts)

    Storage path: {sessions_dir}/{sanitized_session_key}.jsonl
    OpenClaw: resolveSessionFilePath() in src/config/sessions.ts
    """

    def __init__(self, sessions_dir: str):
        self.sessions_dir = Path(sessions_dir).expanduser()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_key: str) -> Path:
        safe = "".join(c for c in session_key if c.isalnum() or c in "-_")
        return self.sessions_dir / f"{safe}.jsonl"

    async def load_messages(self, session_key: str, limit: int = 50) -> list[dict]:
        """
        Returns last `limit` messages in Anthropic API format:
        [{"role":"user","content":"..."}, {"role":"assistant","content":"..."}]
        Direct pass-through to anthropic.messages.create(messages=...).
        """
        path = self._session_path(session_key)
        if not path.exists():
            return []
        records = []
        async with aiofiles.open(path) as f:
            async for line in f:
                if line.strip():
                    records.append(json.loads(line))
        return [
            {"role": r["role"], "content": r["content"]}
            for r in records[-limit:]
            if r["role"] in ("user", "assistant")
        ]

    async def append_message(
        self,
        session_key: str,
        role:        str,
        content:     str,
        run_id:      str | None = None,
    ) -> str:
        """Append one message. Returns generated message id."""
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        record = {
            "id":         msg_id,
            "role":       role,
            "content":    content,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if run_id:
            record["run_id"] = run_id
        path = self._session_path(session_key)
        async with aiofiles.open(path, "a") as f:
            await f.write(json.dumps(record) + "\n")
        return msg_id
```

---

## 7. Browser Tool System

### Architecture Decision

The browser runs as a **completely separate FastAPI app on its own port** (18790). This mirrors OpenClaw's `src/browser/server.ts`, which starts a standalone Express server on `controlPort`. The agent calls it via HTTP using `aiohttp`. Separation means:
- Browser can crash and restart without restarting the gateway
- Browser server can be replaced/upgraded independently
- Auth and lifecycle are independently controlled

### BrowserContext (`miniagent/browser/context.py`)

```python
from playwright.async_api import async_playwright, Browser, BrowserContext as PwContext, Page
from ..config.loader import get_config


class BrowserContext:
    """
    Manages one Playwright Chromium instance.

    OpenClaw equivalent: src/browser/pw-session.ts + server-context.ts
    OpenClaw supports multiple "profiles" (multiple browser instances, different
    CDP ports, separate user data dirs). miniagent simplifies to one instance.

    State machine: stopped → starting → running → stopped
    """

    def __init__(self):
        self._playwright = None
        self._browser:  Browser | None     = None
        self._context:  PwContext | None   = None
        self._page:     Page | None        = None

    async def start(self) -> None:
        """Launch Chromium. Called once at server startup."""
        cfg = get_config()
        self._playwright = await async_playwright().start()
        self._browser    = await self._playwright.chromium.launch(headless=cfg.browser.headless)
        self._context    = await self._browser.new_context()
        self._page       = await self._context.new_page()

    async def stop(self) -> None:
        """Close browser gracefully. Called at server shutdown."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def get_page(self) -> Page:
        """Returns current page; creates a new one if it was closed."""
        if not self._page or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    async def navigate(self, url: str) -> dict:
        page = await self.get_page()
        await page.goto(url, timeout=get_config().browser.timeout_ms)
        return {"ok": True, "url": page.url, "title": await page.title()}

    async def screenshot(self) -> bytes:
        """Full-page PNG screenshot."""
        page = await self.get_page()
        return await page.screenshot(full_page=True)

    async def click(self, ref: str, double_click: bool = False) -> None:
        """Click element by Playwright locator string."""
        page = await self.get_page()
        locator = page.locator(ref)
        if double_click:
            await locator.dbl_click(timeout=get_config().browser.timeout_ms)
        else:
            await locator.click(timeout=get_config().browser.timeout_ms)

    async def type_text(self, ref: str, text: str) -> None:
        page = await self.get_page()
        await page.locator(ref).fill(text, timeout=get_config().browser.timeout_ms)

    async def get_text(self, ref: str | None = None) -> str:
        page = await self.get_page()
        if ref:
            return await page.locator(ref).inner_text(timeout=get_config().browser.timeout_ms)
        return await page.inner_text("body")

    async def scroll(self, direction: str, amount: int = 500) -> None:
        page = await self.get_page()
        dx = {"left": -amount, "right": amount}.get(direction, 0)
        dy = {"up": -amount, "down": amount}.get(direction, 0)
        await page.mouse.wheel(dx, dy)
```

### Browser HTTP Routes (`miniagent/browser/routes.py`)

```python
import base64
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .context import BrowserContext


def register_browser_routes(app: FastAPI, get_ctx) -> None:
    """
    REST API for browser control.
    OpenClaw: src/browser/routes/agent.ts + routes/basic.ts

    These are plain HTTP routes — not WebSocket. The gateway's browser.* RPC handlers
    proxy WebSocket calls to these endpoints via BrowserClient (aiohttp).
    """

    @app.get("/status")
    async def status():
        ctx = get_ctx()
        page = await ctx.get_page()
        return {"running": True, "url": page.url, "title": await page.title()}

    @app.post("/navigate")
    async def navigate(body: NavigateBody):
        return await get_ctx().navigate(body.url)

    @app.get("/screenshot")
    async def screenshot():
        png = await get_ctx().screenshot()
        return {"image_b64": base64.b64encode(png).decode(), "mime_type": "image/png"}

    @app.post("/click")
    async def click(body: ClickBody):
        await get_ctx().click(body.ref, body.double_click)
        return {"ok": True}

    @app.post("/type")
    async def type_text(body: TypeBody):
        await get_ctx().type_text(body.ref, body.text)
        return {"ok": True}

    @app.get("/text")
    async def get_text(ref: str | None = None):
        return {"text": await get_ctx().get_text(ref)}

    @app.post("/scroll")
    async def scroll(body: ScrollBody):
        await get_ctx().scroll(body.direction, body.amount)
        return {"ok": True}


class NavigateBody(BaseModel):
    url: str

class ClickBody(BaseModel):
    ref: str
    double_click: bool = False

class TypeBody(BaseModel):
    ref: str
    text: str

class ScrollBody(BaseModel):
    direction: str   # "up" | "down" | "left" | "right"
    amount: int = 500
```

### Tool Definitions + Client (`miniagent/agent/tools.py`)

```python
import aiohttp, base64
from ..config.loader import get_config


def get_tool_definitions() -> list[dict]:
    """
    Tool definitions in Anthropic SDK format, passed as tools= to messages.create().
    Claude decides when to use these; the pipeline executes them via execute_tool().

    OpenClaw builds tool definitions in src/browser/pw-ai.ts and src/agents/agent-scope.ts.
    miniagent hard-codes 6 browser tools for clarity.
    """
    return [
        {
            "name": "navigate",
            "description": "Navigate the browser to a URL",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL including https://"}
                },
                "required": ["url"],
            },
        },
        {
            "name": "screenshot",
            "description": "Take a screenshot of the current browser tab. Use this to see what the page looks like.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "click",
            "description": "Click an element on the page",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "CSS or aria selector, e.g. 'button:has-text(\"Submit\")'"},
                    "double_click": {"type": "boolean", "default": False},
                },
                "required": ["ref"],
            },
        },
        {
            "name": "type_text",
            "description": "Type text into an input element",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref":  {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["ref", "text"],
            },
        },
        {
            "name": "get_text",
            "description": "Get visible text from the page or a specific element",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Optional CSS selector; omit for full page text"},
                },
            },
        },
        {
            "name": "scroll",
            "description": "Scroll the page",
            "input_schema": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "amount":    {"type": "integer", "default": 500, "description": "Pixels to scroll"},
                },
                "required": ["direction"],
            },
        },
    ]


async def execute_tool(tool_name: str, tool_input: dict) -> dict:
    """
    Execute a tool by proxying to the browser HTTP server.

    OpenClaw: gateway server-methods/browser.ts proxies browser.request RPC
    calls to the local browser HTTP server. miniagent's agent does the same
    directly from the pipeline via aiohttp.
    """
    cfg = get_config()
    client = BrowserClient(f"http://127.0.0.1:{cfg.browser.control_port}")
    return await client.call(tool_name, tool_input)


class BrowserClient:
    """aiohttp HTTP client for the browser server."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def call(self, tool_name: str, tool_input: dict) -> dict:
        route_map = {
            "navigate":    ("POST", "/navigate"),
            "screenshot":  ("GET",  "/screenshot"),
            "click":       ("POST", "/click"),
            "type_text":   ("POST", "/type"),
            "get_text":    ("GET",  "/text"),
            "scroll":      ("POST", "/scroll"),
        }
        if tool_name not in route_map:
            raise ValueError(f"Unknown tool: {tool_name}")

        method, path = route_map[tool_name]
        url = self.base_url + path

        async with aiohttp.ClientSession() as session:
            if method == "GET":
                params = tool_input if tool_input else None
                async with session.get(url, params=params) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            else:
                async with session.post(url, json=tool_input) as resp:
                    resp.raise_for_status()
                    return await resp.json()
```

---

## 8. Web UI

Single-file `miniagent/ui/index.html` — no build step, served at `GET /`.

### Layout

```
┌─────────────────────────────────────────────┐
│  miniagent                     ● connected   │
├─────────────────────────────────────────────┤
│  #messages (scrollable)                     │
│                                             │
│  ┌─ user ─────────────────────────────────┐ │
│  │ go to google.com and tell me what      │ │
│  │ you see                                │ │
│  └────────────────────────────────────────┘ │
│                                             │
│  ┌─ assistant ─────────────────────────────┐│
│  │ ⚙ navigate → https://google.com        ││
│  │ ✓ navigated                            ││
│  │ ⚙ screenshot                           ││
│  │ [screenshot PNG inline]                ││
│  │ ✓ got screenshot                       ││
│  │                                        ││
│  │ I can see Google's homepage with...    ││  ← token stream
│  └────────────────────────────────────────┘│
│                                             │
├─────────────────────────────────────────────┤
│  [ Type a message...          ] [Send][Stop] │
└─────────────────────────────────────────────┘
```

### JS Architecture Sketch

```javascript
// ── Connection ──────────────────────────────────────────────────
const ws = new WebSocket("ws://localhost:18789/ws");
const pending = new Map();   // req_id → {resolve, reject}
let seq = 0;

function rpc(method, params) {
    // Returns Promise resolving to first ResponseFrame with matching id.
    // For "agent", this resolves on the FIRST response (accepted ack).
    // The second response (final result) also resolves — same id, same Promise chain.
    // In practice we only need the run_id from the first response.
    const id = String(++seq);
    return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ type: "req", id, method, params }));
    });
}

// ── Message handling ─────────────────────────────────────────────
ws.onmessage = ({ data }) => {
    const frame = JSON.parse(data);

    if (frame.type === "res") {
        const p = pending.get(frame.id);
        if (p) {
            if (frame.ok) p.resolve(frame.payload);
            else p.reject(frame.error);
            // Note: keep the entry for the second "agent" response
            // (remove after status === "ok" to avoid memory leak)
            if (frame.payload?.status === "ok" || !frame.ok) pending.delete(frame.id);
        }
        return;
    }

    if (frame.type === "event") {
        if (frame.event === "connect.challenge") {
            rpc("connect", { client: { id: "web-ui", version: "1.0" } })
                .then(() => loadHistory());
        } else if (frame.event === "agent-event") {
            handleAgentEvent(frame.payload);
        }
    }
};

// ── Sending a message ─────────────────────────────────────────────
async function sendMessage(text) {
    const ack = await rpc("agent", {
        message:          text,
        session_key:      "default",
        idempotency_key:  crypto.randomUUID(),
    });
    // ack = {run_id, status:"accepted"} — start rendering the assistant bubble
    appendAssistantBubble(ack.run_id);
}

// ── Rendering streaming events ────────────────────────────────────
function handleAgentEvent({ run_id, stream, data }) {
    if (stream === "assistant") {
        appendToken(run_id, data.delta);                // token-by-token
    } else if (stream === "tool") {
        if (data.phase === "start") {
            appendToolIndicator(run_id, data.tool_name, data.input);
        } else if (data.phase === "result") {
            if (data.tool_name === "screenshot" && data.result?.image_b64) {
                appendScreenshot(run_id, data.result.image_b64);
            }
        }
    } else if (stream === "lifecycle") {
        if (data.status === "complete") finalizeMessage(run_id);
        if (data.status === "error")    markError(run_id);
    }
}
```

---

## 9. Full Request Data Flow

```
User types "go to google.com and tell me what you see" → clicks Send
│
│  rpc("agent", {message, session_key:"default", idempotency_key:"<uuid>"})
│  → RequestFrame {type:"req", id:"42", method:"agent", params:{...}}
│  → WebSocket → WsConnection._handle_message()
│  → HandlerRegistry.dispatch(ctx) → agent_handler(ctx)
│  → dedupe miss
│  → ctx.respond(True, {run_id:"run-1", status:"accepted"})
│     → ResponseFrame {id:"42", ok:true, payload:{run_id, status:"accepted"}}
│     → WebSocket → UI: appendAssistantBubble("run-1")
│  → asyncio.create_task(_run())   ← handler returns here immediately
│
│  [background asyncio.Task]
│  → session_store.load_messages("default")   → reads ~/.miniagent/sessions/default.jsonl
│  → session_store.append_message(role="user", content="go to google.com...")
│  → emitter.emit("lifecycle", {status:"started"})
│     → listener → broadcaster.broadcast("agent-event", {stream:"lifecycle", ...})
│     → EventFrame → WebSocket → all clients
│
│  → AgentPipeline._stream_claude(messages)   → Anthropic API
│     text_delta: "I'll navigate..."
│     → emitter.emit("assistant", {delta:"I'll"})
│     → broadcaster.broadcast("agent-event", ...) → UI: appendToken("run-1", "I'll")
│     text_delta: " navigate..."
│     → ... (more tokens)
│     stop_reason: "tool_use" block {name:"navigate", input:{url:"https://google.com"}}
│
│  → emitter.emit("tool", {phase:"start", tool_name:"navigate", input:{url:...}})
│     → UI: appendToolIndicator("navigate", ...)
│  → BrowserClient.call("navigate", {url:"https://google.com"})
│     → HTTP POST http://127.0.0.1:18790/navigate
│     → BrowserContext.navigate() → Playwright page.goto("https://google.com")
│     → returns {ok:true, url:"https://google.com", title:"Google"}
│  → emitter.emit("tool", {phase:"result", tool_name:"navigate", result:{ok:true,...}})
│
│     stop_reason: "tool_use" block {name:"screenshot"}
│  → BrowserClient.call("screenshot", {}) → GET :18790/screenshot
│     → page.screenshot() → PNG bytes → base64
│  → emitter.emit("tool", {phase:"result", ..., result:{image_b64:"...", mime_type:"image/png"}})
│     → UI: appendScreenshot(image_b64)
│
│  → append tool_results → recurse _run_turn()
│  → Anthropic API (second turn) — Claude describes what it sees
│     text_delta by text_delta → "assistant" events → UI appends tokens
│     stop_reason: "end_turn"
│
│  → session_store.append_message(role="assistant", content=final_text)
│  → emitter.emit("lifecycle", {status:"complete"})
│     → UI: finalizeMessage("run-1")
│  → ctx.respond(True, {run_id:"run-1", status:"ok"})
│     → ResponseFrame {id:"42", ok:true, payload:{run_id, status:"ok"}}
│     → WebSocket → UI Promise resolves (second time)
```

---

## 10. Component Interaction Diagram

```
┌──────────────────┐   WS frames    ┌────────────────────────────────────┐
│   Browser UI     │◄──────────────►│   Gateway Server  :18789           │
│  (index.html)    │  req/res/event  │   FastAPI  GET /  +  WS /ws        │
└──────────────────┘                └────────────────────────────────────┘
                                                    │
                                       HandlerRegistry.dispatch(ctx)
                                                    │
                              ┌─────────────────────┼─────────────────────┐
                              │                     │                     │
                        agent_handler         chat_handler         browser_handler
                              │                     │                     │
                              ▼                     ▼                     ▼ (HTTP proxy)
                       AgentPipeline          SessionStore          BrowserClient
                       asyncio.Task           ~/.miniagent/         aiohttp
                              │               sessions/*.jsonl           │
                              │                                           │
                       AgentEventEmitter                                  │
                       in-process pub/sub                                 │
                              │                                           │
                        listener:                                         │
                    broadcaster.broadcast()                               │
                              │                                           │
                         Broadcaster                                      │
                    fan-out to WsConnections                              │
                                                                          ▼
                                                        ┌──────────────────────────┐
                                                        │  Browser Server  :18790  │
                                                        │  FastAPI REST            │
                                                        │  /navigate  /screenshot  │
                                                        │  /click  /type  /text    │
                                                        └──────────────────────────┘
                                                                    │
                                                             BrowserContext
                                                             Playwright Chromium
```

---

## 11. Startup Sequence

```
python -m miniagent start
  ↓ load_config()       → merge .env + config.yaml + Pydantic defaults
  ↓ validate            → ANTHROPIC_API_KEY must be present
  ↓ create shared objects:
      Broadcaster, SessionState, AgentEventEmitter, SessionStore
  ↓ wire:               emitter.on(→ broadcaster.broadcast("agent-event", ...))
  ↓ build HandlerRegistry; register: connect, agent, chat.*, browser.*
  ↓ build gateway FastAPI app   → GET /  +  WS /ws
  ↓ build browser FastAPI app   → REST routes
  ↓ BrowserContext.start()      → Playwright.launch(chromium)
  ↓ asyncio.gather(
        uvicorn.serve(gateway_app, host="127.0.0.1", port=18789),
        uvicorn.serve(browser_app, host="127.0.0.1", port=18790),
    )
  ↓ open http://localhost:18789 in browser
```

---

## 12. Dependencies

```toml
# pyproject.toml
[project]
name = "miniagent"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "websockets>=12",
    "anthropic>=0.40",
    "playwright>=1.48",
    "pydantic>=2.8",
    "pydantic-settings>=2.5",
    "pyyaml>=6.0",
    "aiofiles>=23",
    "aiohttp>=3.10",
]

[project.scripts]
miniagent = "miniagent.__main__:main"

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Post-install: `playwright install chromium`

---

## 13. Testing Strategy

```python
# test_protocol.py — frame round-trips
def test_request_frame_parse():
    raw = '{"type":"req","id":"1","method":"agent","params":{"message":"hello","idempotency_key":"x"}}'
    frame = RequestFrame.model_validate_json(raw)
    assert frame.method == "agent"

# test_gateway.py — WS TestClient
async def test_handshake():
    client = TestClient(build_app())
    with client.websocket_connect("/ws") as ws:
        challenge = ws.receive_json()
        assert challenge["event"] == "connect.challenge"
        ws.send_json({"type":"req","id":"1","method":"connect","params":{"client":{"id":"test","version":"1"}}})
        resp = ws.receive_json()
        assert resp["ok"] is True
        assert resp["payload"]["type"] == "hello-ok"

# test_agent_pipeline.py — mock Anthropic SDK
async def test_pipeline_emits_events(mock_anthropic):
    events = []
    emitter = AgentEventEmitter()
    emitter.on(lambda e: events.append(e))
    store = InMemorySessionStore()
    pipeline = AgentPipeline("run-1", "default", emitter, store, ...)
    await pipeline.run("hello")
    streams = [e.stream for e in events]
    assert "lifecycle" in streams
    assert "assistant" in streams

# test_browser.py — mock BrowserContext
async def test_screenshot_route():
    ctx = MockBrowserContext(screenshot_bytes=b"PNG")
    client = TestClient(create_browser_app(ctx))
    resp = client.get("/screenshot")
    assert resp.json()["mime_type"] == "image/png"
```

---

## 14. OpenClaw → miniagent Pattern Map

| OpenClaw Pattern | Source Location | miniagent Equivalent |
|---|---|---|
| Typed RPC frames (TypeBox + AJV) | `src/gateway/protocol/schema/frames.ts` | `protocol/frames.py` (Pydantic) |
| Handler registry dict spread | `server-methods.ts` | `HandlerRegistry.register_many()` |
| `respond()` callback | `server-methods/types.ts` | `HandlerContext.respond` |
| Double-response for `agent` | `server-methods/agent.ts:599,654` | `agent_handler` calls `respond` twice |
| Idempotency dedupe map | `server-methods/agent.ts:222` | `SessionState.dedupe` |
| Broadcaster global seq | `server-broadcast.ts:66` | `Broadcaster._seq` |
| JSONL session files | `session-utils.fs.ts` | `SessionStore` |
| `onAgentEvent()` pub/sub | `infra/agent-events.ts` | `AgentEventEmitter` |
| Browser as separate HTTP server | `src/browser/server.ts` | `browser/server.py` on port 18790 |
| Browser gateway proxy | `server-methods/browser.ts` | `browser_handler` + `BrowserClient` |
| connect.challenge handshake | `server/ws-connection.ts:175,268` | `WsConnection._send_challenge()` |
| Config layering | `src/config/io.ts loadConfig()` | `config/loader.py load_config()` |
| `get_config()` singleton | `src/config/config.ts` | `config/loader.py get_config()` |
